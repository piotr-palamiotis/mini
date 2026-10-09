"""Tool-creation loop: spec -> permissions gate -> code -> tests in the sandbox -> fix -> install gate -> registry.

Enforced in code:
  - an attempt is one generate/fix of the code followed by a test run; the budget is caps.attempts_per_tool
  - as soon as the tests pass, editing stops and the install gate opens (no extra tests or refactors)
  - restarts after a fail streak happen inside the budget and do not reset it
  - after the budget: offer the cloud fallback (approval dialog) or stop and report
  - a permission or package increase always goes through the approval gate
"""
import hashlib
import json
import os
import re
import shutil
import time

from . import codeblocks, prompts
from .turns import AgentStop, EscalateRequested, RestartRequested, Turns, fn
from ..harness.registry import NAME_RE
from ..harness.sandbox import PolicyError, validate_packages, validate_targets
from ..harness.fallback import FallbackCapError
from ..harness.lessons import format_lessons

S = {"type": "string"}
OBJ = {"type": "object"}
T_SPEC = fn("write_spec", "Write the spec of the new tool.", {
    "name": S, "description": S, "input_schema": OBJ, "output_schema": OBJ,
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
T_UPDATE_SPEC = fn("update_spec", "Change the spec: description, schemas, permissions (network, packages) or edge "
                   "cases. Permission or package increases need the user's approval.",
                   {"description": S, "input_schema": OBJ, "output_schema": OBJ,
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
        self.last_output, self.last_hash = "", None
        self.local_attempts = 0
        a.log.emit("phase", phase="create", need=need)
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
        keys = ("name", "description", "input_schema", "output_schema", "permissions", "edge_cases")
        self.spec = {k: manifest.get(k) for k in keys}
        self.approved_perms = self._norm_perms(manifest.get("permissions"))
        self.image = a.sandbox.ensure_image(self.approved_perms["packages"])
        self.parent = manifest["version"]
        self.restarts, self.last_output, self.last_hash, self.local_attempts = 0, "", None, 0
        self.consults = 0
        self.errors = {}
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
                             rules=prompts.CREATE_RULES, need=self.need, task=self.task, context=self.context, names=names)}]
        t = Turns(a, "spec", self.messages, [T_SPEC])
        for _ in range(6):
            reply = t.ask()
            call = reply["tool_calls"][0]
            for extra in reply["tool_calls"][1:]:
                t.result(extra["name"], "ignored: call write_spec only")
            if call["name"] != "write_spec":
                t.result(call["name"], "Call write_spec now.")
                continue
            err = self._validate_spec(call["arguments"], new=True)
            if err:
                t.result("write_spec", "Spec rejected: " + err + " Fix it and call write_spec again.")
                continue
            self.spec = self._clean_spec(call["arguments"])
            a.log.emit("spec", spec=self.spec)
            ok, msg = self._approve_permissions(self.spec["permissions"])
            if not ok:
                t.result("write_spec", msg + " Write a spec that works without it, or with less.")
                continue
            t.result("write_spec", "Spec accepted." + (" " + msg if msg else ""))
            return
        raise AgentStop("no valid spec after 6 tries")

    def _norm_perms(self, p):
        p = p or {}
        return {"network": validate_targets(p.get("network") or []), "packages": validate_packages(p.get("packages") or {})}

    def _clean_spec(self, s):
        return {"name": s["name"], "description": s["description"].strip(), "input_schema": s["input_schema"],
                "output_schema": s["output_schema"], "permissions": self._norm_perms(s.get("permissions")),
                "edge_cases": [str(e) for e in s.get("edge_cases") or []]}

    def _validate_spec(self, s, new):
        if not isinstance(s, dict):
            return "arguments must be an object."
        missing = [k for k in ("name", "description", "input_schema", "output_schema", "edge_cases") if not s.get(k)]
        if missing:
            return "missing fields: " + ", ".join(missing) + "."
        if not isinstance(s["name"], str) or not NAME_RE.match(s["name"]):
            return "name must be snake_case, 3-48 characters, starting with a letter."
        if new and s["name"] in self.a.registry.names():
            return f"the name {s['name']} is already used in the registry."
        for k in ("input_schema", "output_schema"):
            if not isinstance(s[k], dict):
                return f"{k} must be a JSON Schema object."
        if not isinstance(s["edge_cases"], list):
            return "edge_cases must be a list of strings."
        try:
            self._norm_perms(s.get("permissions"))
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
                "tool": self.spec["name"], "network (new)": new_net or "-", "apt packages (new)": new_apt or "-",
                "pip packages (new)": new_pip or "-", "why": self.spec["description"]})
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
                           ("test_tool.py", prompts.WHAT_TESTS.format(edge_cases="; ".join(self.spec["edge_cases"])))):
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
        tests = open(os.path.join(self.files_dir, "test_tool.py")).read().count("def test_")
        need = len(self.spec["edge_cases"])
        if tests < need:
            return (f"Saved, but test_tool.py has {tests} tests for {need} edge cases. Add tests so that every edge "
                    f"case in the spec is covered, then call `run_tests`.")
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
        res = a.sandbox.run(["python", "-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider", "/tool/test_tool.py"],
                            work, tool_dir=self.files_dir, network=self.approved_perms["network"], image=self.image,
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
        self.local_attempts = self.a.wd.attempts
        if verdict == "review":
            return self._install(provider="local")
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
            except EscalateRequested:
                return self._escalate(reason="the user chose the fallback at the time/turn limit")
            content = reply.get("content") or ""
            tested = False
            for call in reply["tool_calls"]:
                name, args = call["name"], call["arguments"] or {}
                guard = a.wd.on_call(name, args)
                if guard == "block":
                    t.result(name, f"BLOCKED: you made this exact call {a.wd.call_streak} times in a row. "
                                   "It will not run again. Do something different.")
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
                    if verdict == "restart":
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
            spec = dict(self.spec)
            for k in ("description", "input_schema", "output_schema", "edge_cases"):
                if args.get(k):
                    spec[k] = args[k]
            if "permissions" in args:
                try:
                    spec["permissions"] = self._norm_perms(args["permissions"])
                except PolicyError as e:
                    return f"Spec change rejected: {e}", None
            ok, msg = self._approve_permissions(spec["permissions"])
            if not ok:
                return msg, None
            self.spec = spec
            a.log.emit("spec", spec=spec, updated=True)
            return "Spec updated. " + msg, None
        if name == "run_in_sandbox":
            argv = args.get("argv")
            try:
                work = os.path.join(self.build, "testwork")
                os.makedirs(work, exist_ok=True)
                res = a.sandbox.run(argv, work, tool_dir=self.files_dir, network=self.approved_perms["network"],
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

    # ================================================================== budget, fallback, install
    def _consult_user(self):
        """Budget used up: the agent asks the user for advice before the fallback or giving up.
        Returns True when the user gave advice (the budget was extended), False otherwise."""
        a = self.a
        cc = a.cfg["consult"]
        if not cc["enabled"] or self.consults >= cc["max_rounds"]:
            return False
        recurring = sorted(self.errors.items(), key=lambda x: -x[1])[:5]
        q = None
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
        d = a.gate.request("consult", "mini is stuck and would like your advice", {
            "tool": self.spec["name"], "building": q["building"], "keeps failing": q["keeps_failing"],
            "already tried": q["tried"],
            "recurring errors": "; ".join(f"{s} (x{n})" for s, n in recurring) or "-",
            "attempts used": f"{a.wd.attempts}/{a.wd.budget}", "question": q["question"],
            "if you answer": f"{cc['extra_attempts']} more attempts with your advice"}, options=("send advice", "skip"))
        advice = (d.note or "").strip()
        if not d.approved or d.choice == "skip" or not advice:
            a.log.emit("consult", answered=False, decided_by=d.decided_by)
            return False
        a.log.emit("consult", answered=True, advice=advice, extra_attempts=cc["extra_attempts"])
        a.wd.extend_budget(cc["extra_attempts"])
        self.advice = advice
        a.wd.reset_conversation()
        self.messages.append({"role": "user", "content":
            f"Advice from the user: {advice}\n\nUse it. You have {cc['extra_attempts']} more attempts. Fix the code "
            "(replace, replace_lines or rewrite_file), then call `run_tests`."})
        return True

    def _budget_exhausted(self):
        a = self.a
        self._record_lesson("failed")
        if self._consult_user():
            return None
        if not a.fallback.enabled:
            a.log.emit("cap", cap="attempt_budget", tool=self.spec["name"], attempts=a.wd.attempts, fallback="disabled")
            raise ToolFailed(f"{self.spec['name']}: tests still fail after {a.wd.attempts} attempts; fallback disabled")
        return self._escalate(reason=f"{a.wd.attempts} local attempts failed")

    def _escalate(self, reason):
        a = self.a
        prov = a.fallback.provider
        d = a.gate.request("escalate", "Escalate to the cloud fallback?", {
            "provider": f"{prov.name} ({a.cfg['fallback']['model']})", "why": reason,
            "local attempts failed": a.wd.attempts, "tool": self.spec["name"],
            "data leaving this machine": "the task text, the tool spec, the last code and the failing test output",
            "caps": f"{a.cfg['fallback']['attempts_per_tool']} attempts, {a.cfg['fallback']['tokens_per_run']} tokens, "
                    f"${a.cfg['fallback']['spend_usd_per_run']} per run"}, options=("escalate", "stop"))
        if not d.approved or d.choice == "stop":
            a.log.emit("cap", cap="attempt_budget", tool=self.spec["name"], attempts=a.wd.attempts, fallback="declined")
            raise ToolFailed(f"{self.spec['name']}: tests still fail after {a.wd.attempts} attempts; fallback declined")
        a.escalations += 1
        last_good_local = a.wd.attempts
        for i in range(1, a.cfg["fallback"]["attempts_per_tool"] + 1):
            tool_src = open(os.path.join(self.files_dir, "tool.py")).read() if os.path.exists(os.path.join(self.files_dir, "tool.py")) else ""
            test_src = open(os.path.join(self.files_dir, "test_tool.py")).read() if os.path.exists(os.path.join(self.files_dir, "test_tool.py")) else ""
            prompt = prompts.FALLBACK.format(rules=prompts.CREATE_RULES, attempts=last_good_local, task=self.task,
                                             spec=json.dumps(self.spec, indent=1), tool=tool_src, test=test_src,
                                             output=trim(self.last_output, 3000))
            a.log.emit("attempt", n=i, budget=a.cfg["fallback"]["attempts_per_tool"], provider="fallback")
            try:
                r = a.fallback.complete(prompts.FALLBACK_SYSTEM, prompt)
            except FallbackCapError as e:
                raise ToolFailed(str(e))
            if r["error"] and not r["text"]:
                self.last_output = f"(fallback error: {r['error']})"
                continue
            files, problems = codeblocks.extract(r["text"])
            files = {k.split("/")[-1]: v for k, v in files.items()}
            if not all(f in files for f in FILES):
                self.last_output = "fallback reply did not contain both files. " + " ".join(problems)
                a.log.emit("error", text=self.last_output)
                continue
            for name in FILES:
                self._write(name, files[name])
            a.log.emit("code", tool=self.spec["name"], provider="fallback", files={k: files[k] for k in FILES})
            res = self._run_tests()
            a.log.emit("attempt", n=i, budget=a.cfg["fallback"]["attempts_per_tool"], provider="fallback", passed=res["passed"])
            if res["passed"]:
                self.fallback_attempts = i
                return self._install(provider="fallback")
        a.log.emit("cap", cap="fallback_attempts", tool=self.spec["name"], attempts=a.cfg["fallback"]["attempts_per_tool"])
        raise ToolFailed(f"{self.spec['name']}: fallback could not make the tests pass either")

    def _install(self, provider):
        a = self.a
        if provider == "local" and a.wd.attempts > 1:
            self._record_lesson("fixed")
        if provider == "local":
            created_by = {"provider": "local", "model": a.model, "backend": a.cfg["local"]["backend"]}
        else:
            created_by = {"provider": a.fallback.provider.name, "model": a.cfg["fallback"]["model"], "fallback": True}
        manifest = dict(self.spec)
        manifest.update({
            "kind": "code", "files": sorted(os.listdir(self.files_dir)), "test_file": "test_tool.py",
            "test_results": self.last_test, "created_by": created_by,
            "attempts_used": {"local": self.local_attempts, "fallback": getattr(self, "fallback_attempts", 0)},
            "restarts": self.restarts, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "image": self.image, "parent_version": self.parent, "run_id": a.log.run_id,
        })
        d = a.gate.request("install", f"Install tool {manifest['name']}?", {
            "name": manifest["name"], "description": manifest["description"],
            "permissions": json.dumps(manifest["permissions"]), "tests": self.last_test["summary"],
            "created by": f"{created_by['provider']} / {created_by['model']}",
            "attempts": json.dumps(manifest["attempts_used"]), "restarts": self.restarts})
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
        return a.registry.get(manifest["name"], v)
