"""Tool-creation loop: spec -> permissions gate -> code -> tests in the sandbox -> fix -> install gate -> registry.

Enforced in code:
  - an attempt is one generate/fix of the code followed by a test run; the budget is caps.attempts_per_tool
  - as soon as the tests pass, editing stops and the install gate opens (no extra tests or refactors)
  - restarts after a fail streak happen inside the budget and do not reset it
  - after the budget: local moves to the free tier by itself; then the user can advise, pick paid, or stop
  - a permission or package increase always goes through the approval gate
  - a spec that does what a core capability (mini/core) does is rejected
"""
import hashlib
import json
import os
import re
import shutil
import time

from . import codeblocks, prompts, samples
from .turns import AgentStop, RestartRequested, Turns, fn
from ..harness.registry import NAME_RE, is_bundled
from ..harness.sandbox import PolicyError, repair_argv, validate_packages, validate_targets
from ..harness import toolui
from ..harness.settings import KEY_RE, validate_declarations
from ..harness.lessons import format_lessons

S = {"type": "string"}
OBJ = {"type": "object"}
T_SPEC = fn("write_spec", "Write the spec of the new tool.", {
    "name": S, "description": S, "input_schema": OBJ, "output_schema": OBJ,
    "settings": {"type": "array", "items": {"type": "object", "properties": {
        "key": S, "description": S, "schema": OBJ, "secret": {"type": "array", "items": S}}}},
    "interaction": {"type": "array", "items": S}, "ui": OBJ,
    "permissions": {"type": "object", "properties": {"network": {"type": "array", "items": S},
                                                     "packages": {"type": "object"}}},
    "edge_cases": {"type": "array", "items": S}}, ["name", "description", "input_schema", "output_schema", "edge_cases"])
T_REPLACE = fn("replace", "Replace one exact piece of text in a file. old must appear exactly once; include a few "
               "surrounding lines to make it unique. Never include line-number prefixes.",
               {"path": S, "old": S, "new": S}, ["path", "old", "new"])
T_REPLACE_LINES = fn("replace_lines", "Replace lines start..end (inclusive, numbers as shown) with new text. "
                     "Empty new text deletes the lines.",
                     {"path": S, "start": {"type": "integer"}, "end": {"type": "integer"}, "new": S},
                     ["path", "start", "end", "new"])
T_REWRITE = fn("rewrite_file", "Replace a whole file. The harness then asks you for the new content.", {"path": S}, ["path"])
T_RUN_TESTS = fn("run_tests", "Run the tests in the sandbox.", {})
T_UPDATE_SPEC = fn("update_spec", "Change the spec: description, schemas, settings, interaction, ui hints, permissions "
                   "(network, packages) or edge cases. Permission or package increases need the user's approval.",
                   {"description": S, "input_schema": OBJ, "output_schema": OBJ, "settings": {"type": "array"},
                    "interaction": {"type": "array", "items": S}, "ui": OBJ,
                    "permissions": {"type": "object"}, "edge_cases": {"type": "array", "items": S}})
T_SANDBOX = fn("run_in_sandbox", "Run one inspection command in the tool's sandbox: an argv list, no shell, "
               "e.g. [\"python\", \"-c\", \"import x; print(x.__version__)\"]. Tool files are in /tool (read-only), "
               "the working directory is an empty scratch directory.",
               {"argv": {"type": "array", "items": S}}, ["argv"])
T_LESSON = fn("record_lesson", "Write a lesson for future attempts at building this kind of tool.",
              {"problem": S, "cause": S, "avoid": S, "try_instead": S}, ["problem", "cause", "avoid", "try_instead"])
T_ASK_HELP = fn("ask_for_advice", "Ask the user for advice: what you are building, what keeps failing, what you "
                "tried, and one concrete question.", {"building": S, "keeps_failing": S, "tried": S, "question": S},
                ["building", "keeps_failing", "tried", "question"])
T_EXPLAIN = fn("explain", "Explain what still fails, why, and the one change you will make next.",
               {"what_fails": S, "why": S, "next_change": S}, ["what_fails", "why", "next_change"])
FIX_TOOLS = [T_REPLACE, T_REPLACE_LINES, T_REWRITE, T_RUN_TESTS, T_UPDATE_SPEC, T_SANDBOX]
FILES = ("tool.py", "test_tool.py")
MAX_OUT = 3500


def trim(text, n=MAX_OUT):
    text = text or ""
    if len(text) <= n:
        return text
    return text[:n // 3] + f"\n[... {len(text) - n} characters cut ...]\n" + text[-(2 * n) // 3:]


class ToolFailed(Exception):
    pass


class Creator:
    def __init__(self, agent):
        self.a = agent

    # ================================================================== entry points
    def create(self, need, task, context):
        a = self.a
        a.wd.reset_task()       # time/turn limits apply per tool creation, the attempt budget per tool
        self.task, self.context, self.need = task, context, need
        self.build = os.path.join(a.run_dir, "build", f"tool{len(a.created) + 1}")
        shutil.rmtree(self.build, ignore_errors=True)
        os.makedirs(os.path.join(self.build, "files"))
        self.files_dir = os.path.join(self.build, "files")
        self.spec, self.approved_perms, self.image = None, {"network": [], "packages": {"apt": [], "pip": []}}, None
        self.parent = None
        self.restarts = 0
        self.consults = 0
        self.errors = {}
        self.samples_nudged = False
        self.last_output, self.last_hash = "", None
        a.log.emit("phase", phase="create", need=need)
        self._copy_samples()
        self._spec_phase()
        return self._code_and_test(first=True)

    def repair(self, manifest, problem, evidence, task, context):
        """The real run found a bug: the tool goes back into the loop as a new version."""
        a = self.a
        a.wd.reset_task()
        self.task, self.context, self.need = task, context, manifest["description"]
        self.build = os.path.join(a.run_dir, "build", f"{manifest['name']}-repair{manifest['version']}")
        shutil.rmtree(self.build, ignore_errors=True)
        shutil.copytree(manifest["_dir"], os.path.join(self.build, "files"))
        self.files_dir = os.path.join(self.build, "files")
        self._copy_samples()
        keys = ("name", "description", "input_schema", "output_schema", "settings", "interaction", "ui", "permissions",
                "edge_cases")
        self.spec = {k: manifest.get(k) for k in keys}
        self.approved_perms = self._norm_perms(manifest.get("permissions"))
        self.image = a.sandbox.ensure_image(self.approved_perms["packages"])
        self.parent = manifest["version"]
        self.restarts, self.last_output, self.last_hash = 0, "", None
        self.consults = 0
        self.errors = {}
        self.samples_nudged = False
        if a.cfg["lessons"]["enabled"]:
            stored = a.lessons.add({"capability": f"{manifest['name']}: {manifest['description']}",
                                    "outcome": "real_run_failure", "problem": problem[:400], "cause": "", "avoid": "",
                                    "try_instead": "", "errors": [], "run_id": a.log.run_id})
            a.log.emit("lessons", action="recorded", lesson=stored)
        a.log.emit("phase", phase="repair", tool=manifest["name"], from_version=manifest["version"], problem=problem)
        msg = (f"The installed tool {manifest['name']} v{manifest['version']} failed on the real task.\n"
               f"Problem: {problem}\nEvidence:\n{trim(evidence, 2500)}\n\n"
               f"Spec:\n{json.dumps(self.spec, indent=1)}\n\n{self._files_view()}\n\n"
               "Find the root cause first: why does the tool not produce what was asked? Check what it sends and "
               "what it reads. Add a test that reproduces the problem, fix the cause (do not hide the failure), "
               "then call `run_tests`.")
        msg = self._lessons_block() + msg
        self.messages = [{"role": "system", "content": prompts.SYSTEM + "\n\n" + prompts.CREATE_RULES},
                         {"role": "user", "content": msg}]
        return self._fix_loop()

    # ================================================================== spec
    def _spec_phase(self):
        a = self.a
        names = ", ".join(a.registry.names()) or "(none)"
        self.messages = [{"role": "system", "content": prompts.SYSTEM},
                         {"role": "user", "content": self._lessons_block() + prompts.SPEC.format(
                             rules=prompts.CREATE_RULES, need=self.need, task=self.task, context=self.context, names=names,
                             settings=self._settings_listing()) + self._preview_block()}]
        t = Turns(a, "spec", self.messages, [T_SPEC])
        for _ in range(6):
            reply = t.ask()
            call = reply["tool_calls"][0]
            for extra in reply["tool_calls"][1:]:
                t.result(extra["name"], "ignored: call write_spec only")
            if call["name"] != "write_spec":
                t.result(call["name"], "Call write_spec now.")
                continue
            args = call["arguments"]
            if isinstance(args, dict) and not args.get("name"):
                guess = re.split(r"[:\s]", self.need.strip(), maxsplit=1)[0]      # the need often starts with the name
                if NAME_RE.match(guess):
                    args["name"] = guess
            err = self._validate_spec(args, new=True)
            if err:
                a.log.emit("watchdog", name="spec_rejected", problem=err)
                t.result("write_spec", "Spec rejected: " + err + " Fix it and call write_spec again.")
                continue
            self.spec = self._clean_spec(call["arguments"])
            a.log.emit("spec", spec=self.spec)
            ok, msg = self._approve_permissions(self.spec["permissions"])
            if not ok:
                t.result("write_spec", msg + " Write a spec that works without it, or with less.")
                continue
            revised = self._review_plan()
            if revised:
                t.result("write_spec", "The user revised the plan: " + revised + "\nCall write_spec again with the "
                                       "plan changed accordingly.")
                continue
            t.result("write_spec", "Spec accepted." + (" " + msg if msg else ""))
            return
        raise AgentStop("no valid spec after 6 tries")

    def _preview_block(self):
        p = samples.preview(self.a)
        return "\n\n" + p if p else ""

    def _copy_samples(self):
        got = samples.copy(self.a, self.files_dir)
        if got:
            self.a.log.emit("info", text="samples for the tests: " + ", ".join(f"{p} ({n} B)" for p, n in got))

    def _settings_listing(self):
        recs = self.a.settings.list()
        return "; ".join(f"{r['key']}: {r['description']} (fields: {', '.join((r.get('schema') or {}).get('properties') or {})})"
                         for r in recs) or "(none)"

    def _review_plan(self):
        """confirm_first: the user sees how the tool will work (settings, questions, actions) before any code.
        Returns the user's revision, or "" to go ahead."""
        a = self.a
        if not a.cfg["approvals"]["confirm_first"]:
            return ""
        sp = self.spec
        props = (sp["input_schema"] or {}).get("properties") or {}
        d = a.gate.request("plan", f"Plan for the new tool {sp['name']}", {
            "does": sp["description"],
            "saved settings": "; ".join(f"{x['key']}: {x['description']} (fields: {', '.join((x['schema'].get('properties') or {}))}"
                                        + (f"; secret: {', '.join(x['secret'])}" if x["secret"] else "") + ")"
                                        for x in sp["settings"]) or "-",
            "how it works": "\n".join(f"{i}. {x}" for i, x in enumerate(sp["interaction"], 1)) or "-",
            "one-line use": f"{sp['name']} " + " ".join(f"--{k} ..." for k in props) if props else sp["name"],
            "network": ", ".join(sp["permissions"]["network"]) or "none",
            "edge cases": "; ".join(sp["edge_cases"])}, options=("proceed", "revise", "cancel"))
        if d.choice == "cancel" or not d.approved:
            raise AgentStop(f"the user cancelled the tool {sp['name']} at its plan")
        if d.choice == "revise" and (d.note or "").strip():
            a.log.emit("plan", tool=sp["name"], revised=d.note)
            return d.note.strip()
        return ""

    def _norm_perms(self, p, settings=None):
        """Network targets are "host:port", or "setting:<key>" for a host:port the user saves in a declared setting."""
        p = p or {}
        net = [str(t) for t in p.get("network") or []]
        keys = {d.get("key") for d in (settings if settings is not None else (self.spec or {}).get("settings") or [])}
        refs = [t for t in net if t.startswith("setting:")]
        for t in refs:
            if not KEY_RE.match(t[8:]) or t[8:] not in keys:
                raise PolicyError(f"{t}: network from a setting needs that key in the spec's settings")
        return {"network": validate_targets([t for t in net if t not in refs]) + refs,
                "packages": validate_packages(p.get("packages") or {})}

    def _clean_spec(self, s):
        settings = [{"key": d["key"], "description": str(d.get("description", "")), "schema": d["schema"],
                     "secret": list(d.get("secret") or [])} for d in s.get("settings") or []]
        return {"name": s["name"], "description": s["description"].strip(), "input_schema": s["input_schema"],
                "output_schema": s["output_schema"], "settings": settings,
                "interaction": [str(x) for x in s.get("interaction") or []], "ui": s.get("ui") or {},
                "permissions": self._norm_perms(s.get("permissions"), settings),
                "edge_cases": [str(e) for e in s.get("edge_cases") or []]}

    def _validate_spec(self, s, new):
        if not isinstance(s, dict):
            return "arguments must be an object."
        for k in ("input_schema", "output_schema"):
            s[k] = object_schema(s.get(k))
        missing = [k for k in ("name", "description", "input_schema", "output_schema", "edge_cases") if not s.get(k)]
        if missing:
            return "missing fields: " + ", ".join(missing) + "."
        if not isinstance(s["name"], str) or not NAME_RE.match(s["name"]):
            return "name must be snake_case, 3-48 characters, starting with a letter."
        if is_bundled(s["name"]):
            return f"{s['name']} is a bundled tool and cannot be replaced; invoke it, or choose a different name."
        if new and s["name"] in self.a.registry.names():
            return f"the name {s['name']} is already used in the registry."
        core = self.a.core.covering(s["name"]) or self.a.core.covering(s["description"])
        if core:                                    # never shadow a core capability with a tool
            return f"this tool would do what the core capability {core} already does; build only what core cannot."
        for k in ("input_schema", "output_schema"):
            if not isinstance(s[k], dict):
                return f"{k} must be a JSON Schema object."
        if not isinstance(s["edge_cases"], list):
            return "edge_cases must be a list of strings."
        problem = validate_declarations(s.get("settings") or [])
        if problem:
            return problem
        if not isinstance(s.get("interaction") or [], list):
            return "interaction must be a list of short steps."
        s["ui"] = toolui.tidy(s.get("ui"), s["input_schema"])         # hints name only input_schema properties
        problem = toolui.validate(s["ui"], s["input_schema"])
        if problem:
            return problem
        try:
            self._norm_perms(s.get("permissions"), s.get("settings") or [])
        except PolicyError as e:
            return str(e) + "."
        return None

    def _approve_permissions(self, perms):
        """Approve only what goes beyond what is already approved. Authority never grows without the user."""
        a = self.a
        old = self.approved_perms
        new_net = [t for t in perms["network"] if t not in old["network"]]
        new_apt = [p for p in perms["packages"]["apt"] if p not in old["packages"]["apt"]]
        new_pip = [p for p in perms["packages"]["pip"] if p not in old["packages"]["pip"]]
        if new_net or new_apt or new_pip:
            d = a.gate.request("permissions", f"Tool {self.spec['name']} requests more authority", {
                "tool": self.spec["name"], "network (new)": [t if not t.startswith("setting:") else
                                                             f"the host:port you save in setting {t[8:]}" for t in new_net] or "-",
                "apt packages (new)": new_apt or "-", "pip packages (new)": new_pip or "-", "why": self.spec["description"]})
            if not d.approved:
                return False, "The user refused these permissions: " + json.dumps({"network": new_net, "apt": new_apt, "pip": new_pip})
        self.approved_perms = perms
        try:
            self.image = a.sandbox.ensure_image(perms["packages"])
        except PolicyError as e:
            self.approved_perms = old
            return False, f"Package installation failed: {trim(str(e), 1500)}"
        return True, ""

    # ================================================================== code + tests
    def _ask_file(self, path, instruction, step):
        """Text-only turn: the model answers with the complete file as a fenced block. Returns the content."""
        a = self.a
        self.messages.append({"role": "user", "content": instruction})
        t = Turns(a, step, self.messages, [])
        for _ in range(3):
            reply = t.ask_text()
            body, problems = codeblocks.single(reply.get("content") or "", path)
            if body is None:
                for c in reply.get("tool_calls") or []:       # the model called a tool anyway: take code from it
                    got = codeblocks.from_args(c.get("arguments"), names=(path,) if path != "test_tool.py" else
                                               ("tool.py", "test_tool.py"))
                    if got.get(path) and not codeblocks.ELISION.search(got[path]):
                        a.log.emit("info", text=f"{path} arrived in a tool-call argument; accepted")
                        body = got[path]
                        break
            if body is not None:
                return body
            a.log.emit("info", text=f"no usable fenced block for {path}: {' '.join(problems)}")
            t.user(f"Not saved: {' '.join(problems)}. Reply with the complete {path} as one fenced block "
                   f"(```python {path} ... ```) and nothing else.")
        return None

    def _code_and_test(self, first):
        a = self.a
        spec_text = json.dumps(self.spec, indent=1)
        intro = f"Spec approved:\n{spec_text}\n\n"
        for path, what in (("tool.py", prompts.WHAT_TOOL),
                           ("test_tool.py", prompts.WHAT_TESTS.format(edge_cases="; ".join(self.spec["edge_cases"]))
                                            + samples.tests_note(self.files_dir))):
            body = self._ask_file(path, prompts.CODE_FILE.format(intro=intro, path=path, what=what), "code")
            if body is None:
                raise AgentStop(f"no usable {path} after 3 replies")
            self._write(path, body)
            intro = ""
        a.log.emit("code", tool=self.spec["name"], files={k: open(os.path.join(self.files_dir, k)).read() for k in FILES})
        short = self._edge_case_gap()
        if short:
            self.messages.append({"role": "user", "content": short + "\n\n" + self._files_view()})
            return self._fix_loop()
        res = self._run_tests()
        out = self._test_message(res)
        verdict = a.wd.on_test(res["passed"], True, self._hash())
        self.messages.append({"role": "user", "content": out + ("" if res["passed"] else "\n\n" + self._files_view())})
        return self._after_test(verdict) or self._fix_loop()

    def _edge_case_gap(self):
        gaps = []
        tests = open(os.path.join(self.files_dir, "test_tool.py")).read().count("def test_")
        need = len(self.spec["edge_cases"])
        if tests < need:
            gaps.append(f"test_tool.py has {tests} tests for {need} edge cases. Add tests so that every edge case in "
                        f"the spec is covered.")
        typed = samples.retyped(self.files_dir, open(os.path.join(self.files_dir, "test_tool.py")).read())
        if typed:
            gaps.append(f"test_tool.py types the content of {', '.join('samples/' + p for p in typed)} into the test. "
                        f"A typed copy differs from the real file. Read the file from "
                        f"`os.path.join(os.path.dirname(__file__), \"samples\", ...)` and copy it into tmp_path; type "
                        f"only small inputs no sample covers (a missing file, an edge case).")
        got = samples.listing(self.files_dir)
        if got and not getattr(self, "samples_nudged", False) and \
                "samples" not in open(os.path.join(self.files_dir, "test_tool.py")).read():
            self.samples_nudged = True          # once: a tool that reads none of these files is left as it is
            gaps.append(f"test_tool.py does not read the real samples ({', '.join('samples/' + p for p, _ in got)}). "
                        f"If this tool reads files like these, the normal-use test reads them from "
                        f"`os.path.join(os.path.dirname(__file__), \"samples\", ...)` instead of data typed into the "
                        f"test; if it reads none of them, leave the tests as they are.")
        unknown = ask_keys_outside_schema(open(os.path.join(self.files_dir, "tool.py")).read(), self.spec)
        if unknown:
            props = sorted((self.spec.get("input_schema") or {}).get("properties") or {})
            gaps.append(f"tool.py calls io.ask with keys that are not input_schema properties: {', '.join(unknown)}. "
                        f"A caller answers a question by passing its key in the inputs, so every io.ask key must be an "
                        f"input property (now: {', '.join(props) or 'none'}). Use a property name as the key, or add "
                        f"the property with `update_spec`.")
        if gaps:
            return "Saved, but " + " ".join(gaps) + " Then call `run_tests`."
        return ""

    def _hash(self):
        h = hashlib.sha256()
        for name in sorted(os.listdir(self.files_dir)):
            p = os.path.join(self.files_dir, name)
            if os.path.isfile(p):
                h.update(name.encode() + b"\0" + open(p, "rb").read())
        return h.hexdigest()

    def _write(self, name, content):
        open(os.path.join(self.files_dir, name), "w").write(content)

    def _files_view(self):
        out = []
        for name in FILES:
            p = os.path.join(self.files_dir, name)
            if os.path.exists(p):
                out.append(f"{name} (line numbers are not part of the file):\n" + codeblocks.numbered(open(p).read()))
        return "\n\n".join(out)

    def _run_tests(self):
        a = self.a
        work = os.path.join(self.build, "testwork")
        shutil.rmtree(work, ignore_errors=True)
        os.makedirs(work)
        net = [t for t in self.approved_perms["network"] if not t.startswith("setting:")]   # tests fake those
        res = a.sandbox.run(["python", "-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider", "-p", "mini_pytest",
                             "/tool/test_tool.py"],
                            work, tool_dir=self.files_dir, network=net, image=self.image,
                            purpose=f"tests {self.spec['name']}")
        passed = res["exit_code"] == 0
        summary = next((ln for ln in reversed(res["output"].strip().split("\n")) if ln.strip()), "")
        self.last_output = res["output"]
        if not passed:
            self._count_errors(res["output"])
        a.log.emit("test_run", tool=self.spec["name"], passed=passed, exit_code=res["exit_code"], summary=summary,
                   attempt=a.wd.attempts + (1 if self._hash() != self.last_hash else 0), text=res["output"][-6000:])
        self.last_test = {"passed": passed, "summary": summary, "output_tail": res["output"][-1500:],
                          "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        res["passed"] = passed
        return res

    def _count_errors(self, output):
        """Compact error signatures of one failing test run (exception type + message), counted across attempts."""
        sigs = set()
        for line in output.splitlines():
            m = re.match(r"^E\s+(\w+(?:Error|Exception|Exit)|AssertionError|assert)\b:?\s*(.*)", line)
            if m:
                sigs.add((m.group(1) + ": " + m.group(2))[:120])
            elif line.startswith("ERROR ") or "ModuleNotFoundError" in line:
                sigs.add(line.strip()[:120])
        for sig in sigs:
            self.errors[sig] = self.errors.get(sig, 0) + 1

    def _lessons_block(self):
        if not self.a.cfg["lessons"]["enabled"]:
            return ""
        found = self.a.lessons.search(self.need + " " + (self.spec or {}).get("description", "") + " " + self.task,
                                      limit=self.a.cfg["lessons"]["max_in_prompt"])
        if not found:
            return ""
        self.a.log.emit("lessons", action="used", count=len(found), lessons=found)
        return ("Lessons from earlier attempts at similar tools (written by you in earlier runs). Use them: avoid "
                "what failed before and take a different approach where one is suggested.\n" + format_lessons(found) + "\n\n")

    def _record_lesson(self, outcome):
        """Ask the model for a lesson and store it. outcome: restart, failed, fixed, real_run_failure."""
        a = self.a
        if not a.cfg["lessons"]["enabled"] or not self.spec:
            return
        recurring = sorted(self.errors.items(), key=lambda x: -x[1])[:5]
        base = {"capability": f"{self.spec['name']}: {self.spec['description']}", "outcome": outcome,
                "errors": recurring, "attempts": a.wd.attempts, "run_id": a.log.run_id,
                "user_advice": getattr(self, "advice", None)}
        ask = {"restart": "Earlier tries kept failing and the conversation is being restarted.",
               "failed": "The attempt budget is used up and the tool could not be built.",
               "fixed": "The tests pass now, after earlier failures."}[outcome]
        msgs = list(self.messages) + [{"role": "user", "content":
            f"{ask} Recurring errors (count = failing test runs):\n" +
            ("\n".join(f"- {s} (x{n})" for s, n in recurring) or "- (none recorded)") +
            "\n\nCall `record_lesson` for future attempts at this kind of tool: the problem, its root cause, what "
            "to avoid, and what to try instead" + (" (what finally worked)." if outcome == "fixed" else ".")}]
        lesson = None
        try:
            t = Turns(a, "fix", msgs, [T_LESSON])
            reply = t.ask()
            for c in reply["tool_calls"]:
                if c["name"] == "record_lesson" and c["arguments"].get("problem"):
                    lesson = {k: str(c["arguments"].get(k, ""))[:400] for k in ("problem", "cause", "avoid", "try_instead")}
                    break
        except AgentStop:
            raise
        except Exception as e:   # noqa: BLE001 - a lesson is best effort; the harness still keeps the signatures
            a.log.emit("error", text=f"lesson not written by the model: {e}")
        if lesson is None:
            top = recurring[0][0] if recurring else "unknown"
            lesson = {"problem": f"tests kept failing with: {top}", "cause": "not diagnosed", "avoid": "", "try_instead": ""}
        stored = a.lessons.add({**base, **lesson})
        a.log.emit("lessons", action="recorded", lesson=stored)

    def _test_message(self, res):
        n = self.a.wd.attempts + (1 if self._hash() != self.last_hash else 0)
        status = "PASSED" if res["passed"] else "FAILED"
        if res["exit_code"] == 5:
            status = "FAILED (no tests were collected)"
        out = prompts.TEST_RESULT.format(attempt=n, budget=self.a.wd.budget, status=status,
                                         output=trim(res["output"]))
        if res.get("network_note") and not res["passed"]:
            out += "\n" + res["network_note"]
        return out

    def _after_test(self, verdict):
        """Handle the watchdog verdict after a test run. Returns a manifest when the tool is installed."""
        self.last_hash = self._hash()
        if verdict == "review":
            return self._install()
        if verdict == "budget":
            return self._budget_exhausted()
        if verdict == "restart":
            self._restart()
        return None

    def _restart(self):
        a = self.a
        self._record_lesson("restart")
        self.restarts += 1
        a.log.emit("phase", phase="restart", tool=self.spec["name"], restarts=self.restarts, attempts=a.wd.attempts)
        a.wd.reset_conversation()
        self.messages = [{"role": "system", "content": prompts.SYSTEM + "\n\n" + prompts.CREATE_RULES},
                         {"role": "user", "content": self._lessons_block() + prompts.RESTART.format(
                             streak=a.cfg["watchdogs"]["fail_streak_restart"], spec=json.dumps(self.spec, indent=1),
                             files=self._files_view(), output=trim(self.last_output))}]

    # ================================================================== fix loop
    def _fix_loop(self, pending=None):
        a = self.a
        self.messages.append({"role": "user", "content": prompts.FIX})
        while True:
            t = Turns(a, "fix", self.messages, FIX_TOOLS)
            try:
                reply = t.ask()
            except RestartRequested:
                self._restart()
                continue
            content = reply.get("content") or ""
            tested = False
            for call in reply["tool_calls"]:
                name, args = call["name"], call["arguments"] or {}
                guard = a.wd.on_call(name, args)
                if guard == "block":
                    if a.wd.call_streak >= 3 * a.wd.w["identical_call_repeat"]:
                        raise AgentStop(f"stuck repeating {name} with the same arguments")
                    t.result(name, f"BLOCKED: you made this exact call {a.wd.call_streak} times in a row, and it gave: "
                                   f"{trim(a.wd.last_text, 300)}\nIt will not run again. Change the arguments or use "
                                   "a different tool.")
                    continue
                note = "\nWarning: you just made this same call. A third identical call in a row will be blocked." \
                    if guard == "warn" else ""
                a.log.emit("tool_call", step="fix", name=name, args=args)
                if name == "run_tests":
                    if tested:
                        t.result(name, "Tests already ran in this reply.")
                        continue
                    tested = True
                    changed = self._hash() != self.last_hash
                    short = self._edge_case_gap()
                    if short:
                        t.result(name, short + note)
                        continue
                    res = self._run_tests()
                    out = self._test_message(res)
                    if not changed and not res["passed"]:
                        out += "\nNothing changed since the last run, so this does not count as an attempt. Edit the code first."
                    verdict = a.wd.on_test(res["passed"], changed, self._hash())
                    t.result(name, out + note)
                    done = self._after_test(verdict)
                    if done:
                        return done
                    if verdict in ("restart", "budget"):      # fresh conversation or a new model: drop the rest of this reply
                        break
                    continue
                self._pending_result_done = False
                text, step_back = self._edit(name, args, content)
                if self._pending_result_done:      # rewrite_file already answered; the content came in a text turn
                    t.user(text + note)
                else:
                    t.result(name, text + note)
                a.log.emit("tool_result", step="fix", name=name, text=trim(text, 1500))
                if step_back:
                    self._step_back(step_back)

    def _edit(self, name, args, content):
        """Run an edit or inspection call. Returns (result text, path that needs a step back or None)."""
        a = self.a
        if name in ("replace", "replace_lines", "rewrite_file"):
            path = os.path.basename(str(args.get("path", "")))
            if path not in FILES:
                return f"You can only edit {', '.join(FILES)}.", None
            full = os.path.join(self.files_dir, path)
            old_text = open(full).read() if os.path.exists(full) else ""
            if name == "replace":
                old, new = args.get("old", ""), args.get("new", "")
                if not old:
                    return "old is empty.", None
                n = old_text.count(old)
                if n != 1:
                    hint = " old abbreviates text; copy it exactly." if codeblocks.ELISION.search(old) else ""
                    return (f"old appears {n} times in {path}; it must appear exactly once.{hint} "
                            f"Current file:\n{codeblocks.numbered(old_text)}"), None
                if codeblocks.ELISION.search(new) and not codeblocks.ELISION.search(old):
                    return "new abbreviates code with '...'; write it out in full.", None
                self._write(path, old_text.replace(old, new))
            elif name == "replace_lines":
                try:
                    s, e = int(args.get("start")), int(args.get("end"))
                except (TypeError, ValueError):
                    return "start and end must be integers.", None
                lines = old_text.split("\n")
                if old_text.endswith("\n"):
                    lines = lines[:-1]
                if not (1 <= s <= e <= len(lines)):
                    return f"line range {s}..{e} is outside 1..{len(lines)}.", None
                new = args.get("new", "")
                if codeblocks.ELISION.search(new):
                    return "new abbreviates code with '...'; write it out in full.", None
                lines[s - 1:e] = new.split("\n") if new != "" else []
                self._write(path, "\n".join(lines) + "\n")
            else:
                body = codeblocks.single(content, path)[0] if "```" in (content or "") else None
                if body is None:
                    body = codeblocks.from_args({k: v for k, v in args.items() if k != "path"}, names=(path,)).get(path)
                if body is None:
                    self.messages.append({"role": "tool", "tool_name": name, "content": "ok"})
                    body = self._ask_file(path, prompts.REWRITE.format(path=path), "fix")
                    self._pending_result_done = True
                if body is None:
                    return f"No usable content for {path}; the file is unchanged.", None
                self._write(path, body)
                if a.wd.on_rewrite(path) == "step_back":
                    return f"{path} rewritten.", path
            cur = open(full).read()
            return f"{path} updated. Current content:\n{codeblocks.numbered(cur)}", None
        if name == "update_spec":
            if another_tool(self.spec, args):
                return (f"Spec change rejected: this build is {self.spec['name']}: {self.spec['description']} "
                        f"Make it do that. A tool for something else is built on its own after this one, "
                        f"if the task needs it."), None
            spec = dict(self.spec)
            for k in ("input_schema", "output_schema"):
                if args.get(k):
                    args[k] = object_schema(args[k])
            for k in ("description", "input_schema", "output_schema", "edge_cases", "interaction", "ui"):
                if args.get(k):
                    spec[k] = args[k]
            spec["ui"] = toolui.tidy(spec.get("ui"), spec["input_schema"])
            problem = toolui.validate(spec["ui"], spec["input_schema"])
            if problem:
                return f"Spec change rejected: {problem}", None
            if "settings" in args:
                problem = validate_declarations(args["settings"] or [])
                if problem:
                    return f"Spec change rejected: {problem}", None
                spec["settings"] = self._clean_spec({**spec, "settings": args["settings"]})["settings"]
            if "permissions" in args or "settings" in args:
                try:
                    spec["permissions"] = self._norm_perms(args.get("permissions", spec["permissions"]), spec["settings"])
                except PolicyError as e:
                    return f"Spec change rejected: {e}", None
            ok, msg = self._approve_permissions(spec["permissions"])
            if not ok:
                return msg, None
            self.spec = spec
            a.log.emit("spec", spec=spec, updated=True)
            return "Spec updated. " + msg, None
        if name == "run_in_sandbox":
            argv = repair_argv(args.get("argv"))
            try:
                work = os.path.join(self.build, "testwork")
                os.makedirs(work, exist_ok=True)
                net = [t for t in self.approved_perms["network"] if not t.startswith("setting:")]
                res = a.sandbox.run(argv, work, tool_dir=self.files_dir, network=net,
                                    image=self.image, timeout=60, purpose="inspect")
            except PolicyError as e:
                a.log.emit("watchdog", name="command_policy", argv=argv, reason=str(e))
                return f"Denied: {e}", None
            return f"exit {res['exit_code']}\n{trim(res['output'], 2500)}", None
        if name == "explain":
            return "Noted. Make that change now.", None
        return f"Unknown tool {name}. Use: " + ", ".join(t["function"]["name"] for t in FIX_TOOLS), None

    def _step_back(self, path):
        a = self.a
        n = a.cfg["watchdogs"]["file_rewrites"] + 1
        self.messages.append({"role": "user", "content": prompts.STEP_BACK.format(path=path, n=n)})
        t = Turns(a, "fix", self.messages, [T_EXPLAIN])
        reply = t.ask()
        for c in reply["tool_calls"]:
            a.log.emit("tool_call", step="step_back", name=c["name"], args=c["arguments"])
            t.result(c["name"], "Noted. Make that one change now, then run the tests.")

    # ================================================================== budget, tiers, install
    def _consult_user(self):
        """Budget used up and no automatic move left: ask the user. Returns "advice" (the budget was extended),
        "paid" (the user picked the paid tier for this task) or None (stop)."""
        a = self.a
        cc = a.cfg["consult"]
        can_advise = cc["enabled"] and self.consults < cc["max_rounds"]
        if not can_advise and not a.offer_paid():
            return None
        recurring = sorted(self.errors.items(), key=lambda x: -x[1])[:5]
        q = None
        if can_advise:
            try:
                msgs = list(self.messages) + [{"role": "user", "content":
                    "You have used all attempts. Call `ask_for_advice`: explain to the user, in plain words, what you are "
                    "building, what keeps failing, what you already tried, and ask one concrete question whose answer "
                    "would help you most."}]
                reply = Turns(a, "fix", msgs, [T_ASK_HELP]).ask()
                for c in reply["tool_calls"]:
                    if c["name"] == "ask_for_advice" and c["arguments"].get("question"):
                        q = {k: str(c["arguments"].get(k, ""))[:600] for k in ("building", "keeps_failing", "tried", "question")}
                        break
            except AgentStop:
                raise
            except Exception as e:   # noqa: BLE001
                a.log.emit("error", text=f"advice request not written by the model: {e}")
        if q is None:
            q = {"building": self.spec["description"], "keeps_failing": recurring[0][0] if recurring else "the tests",
                 "tried": f"{a.wd.attempts} attempts", "question": "What should I change so that it works?"}
        self.consults += 1
        options = (("send advice",) if can_advise else ()) + (("use paid",) if a.offer_paid() else ()) + ("stop",)
        details = {"tool": self.spec["name"], "model": f"{a.tier} ({a.model})", "building": q["building"],
                   "keeps failing": q["keeps_failing"], "already tried": q["tried"],
                   "recurring errors": "; ".join(f"{s} (x{n})" for s, n in recurring) or "-",
                   "attempts used": json.dumps(a.wd.tier_attempts), "question": q["question"]}
        if can_advise:
            details["send advice"] = f"{cc['extra_attempts']} more attempts with your advice (write it in the note)"
        if a.offer_paid():
            details["use paid"] = f"the paid model ({a.cfg['paid']['model']}) takes over for the rest of this task"
        d = a.gate.request("consult", "mini is stuck and would like your advice", details, options=options)
        if d.approved and d.choice == "use paid":
            a.log.emit("consult", answered=True, choice="paid", decided_by=d.decided_by)
            return "paid"
        advice = (d.note or "").strip()
        if not d.approved or d.choice != "send advice" or not advice:
            a.log.emit("consult", answered=False, decided_by=d.decided_by)
            return None
        a.log.emit("consult", answered=True, advice=advice, extra_attempts=cc["extra_attempts"])
        a.wd.extend_budget(cc["extra_attempts"])
        self.advice = advice
        a.wd.reset_conversation()
        self.messages.append({"role": "user", "content":
            f"Advice from the user: {advice}\n\nUse it. You have {cc['extra_attempts']} more attempts. Fix the code "
            "(replace, replace_lines or rewrite_file), then call `run_tests`."})
        return "advice"

    def _budget_exhausted(self):
        a = self.a
        self._record_lesson("failed")
        choice = self._consult_user()
        if choice == "advice":
            return None
        if choice == "paid":
            a.switch_tier("paid", f"the user picked paid after {a.wd.attempts} failed attempts", "user")
            self._handover()
            return None
        a.log.emit("cap", cap="attempt_budget", tool=self.spec["name"], attempts=a.wd.tier_attempts, tier=a.tier)
        raise ToolFailed(f"{self.spec['name']}: tests still fail after {sum(a.wd.tier_attempts.values())} attempts")

    def _handover(self):
        """A stronger model takes over the tool: fresh conversation with the spec, the files and the last failure."""
        a = self.a
        a.log.emit("phase", phase="handover", tool=self.spec["name"], tier=a.tier, model=a.model)
        self.messages = [{"role": "system", "content": prompts.SYSTEM + "\n\n" + prompts.CREATE_RULES},
                         {"role": "user", "content": self._lessons_block() + prompts.HANDOVER.format(
                             attempts=json.dumps(a.wd.tier_attempts), spec=json.dumps(self.spec, indent=1),
                             files=self._files_view(), output=trim(self.last_output))}]
        self.messages.append({"role": "user", "content": prompts.FIX})

    def _install(self):
        a = self.a
        if a.wd.attempts > 1:
            self._record_lesson("fixed")
        created_by = {"provider": a.tier, "model": a.model, "backend": a.cfg[a.tier]["backend"]}
        if a.escalated_from:
            created_by["escalated_from"] = a.escalated_from
        manifest = dict(self.spec)
        manifest.update({
            "kind": "code", "files": sorted(os.listdir(self.files_dir)), "test_file": "test_tool.py",
            "test_results": self.last_test, "created_by": created_by,
            "attempts_used": dict(a.wd.tier_attempts),
            "restarts": self.restarts, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "image": self.image, "parent_version": self.parent, "run_id": a.log.run_id,
        })
        d = a.gate.request("install", f"Install tool {manifest['name']}?", {
            "name": manifest["name"], "description": manifest["description"],
            "permissions": json.dumps(manifest["permissions"]), "tests": self.last_test["summary"],
            "created by": f"{created_by['provider']} / {created_by['model']}",
            "attempts": json.dumps(manifest["attempts_used"]), "restarts": self.restarts,
            "samples kept with the tool": ", ".join(f"{p} ({n} B)" for p, n in samples.listing(self.files_dir))
                                          or "none"})
        if not d.approved:
            raise ToolFailed(f"install of {manifest['name']} refused by the user")
        manifest["approvals"] = {"install": d.decided_by, "permissions": manifest["permissions"]}
        for p in ("__pycache__", ".pytest_cache"):
            shutil.rmtree(os.path.join(self.files_dir, p), ignore_errors=True)
        v = a.registry.install(manifest, self.files_dir)
        a.log.emit("install", tool=manifest["name"], version=v, created_by=created_by, attempts=manifest["attempts_used"],
                   restarts=self.restarts, registry=a.registry.snapshot())
        a.created.append({"name": manifest["name"], "version": v, "created_by": created_by,
                          "attempts": manifest["attempts_used"], "restarts": self.restarts})
        return a.registry.get(manifest["name"])


def _lower_types(x):
    if isinstance(x, dict):
        return {k: (v.lower() if k == "type" and isinstance(v, str) else _lower_types(v)) for k, v in x.items()}
    if isinstance(x, list):
        return [_lower_types(v) for v in x]
    return x


def object_schema(x):
    """A schema the local model gave as a bare field map ({"csv": {"type": "string"}}) becomes the object schema it
    meant, and type names are lower case ("STRING" is "string"); anything else is returned as it is."""
    x = _lower_types(x)
    if isinstance(x, dict) and x and not {"type", "properties"} & set(x) \
            and all(isinstance(v, dict) and ("type" in v or "description" in v) for v in x.values()):
        return {"type": "object", "properties": x}
    return x


def _content_words(text):
    return {w for w in re.findall(r"[a-z]{4,}", str(text).lower())} - {
        "with", "from", "that", "this", "into", "using", "returns", "return", "list", "file", "files", "tool", "given",
        "each", "their", "them", "items", "data", "output", "input", "inputs", "path", "paths", "html"}


def another_tool(spec, args):
    """True when an update_spec during a build would make it a different tool: its new description shares no
    content word with the current one and its new inputs (if given) share no field with the current inputs."""
    if not args.get("description") or _content_words(args["description"]) & _content_words(spec.get("description")):
        return False
    new = set(((args.get("input_schema") or {}).get("properties") or {}))
    return not new or not new & set(((spec.get("input_schema") or {}).get("properties") or {}))


def ask_keys_outside_schema(source, spec):
    """Literal keys of io.ask(...) calls in tool source that the spec's input_schema does not declare."""
    import ast
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    props = set((spec.get("input_schema") or {}).get("properties") or {})
    keys = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "ask":
            arg = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == "key"), None)
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value not in props \
                    and arg.value not in keys:
                keys.append(arg.value)
    return keys
