"""Tool-creation loop: spec -> permissions gate -> code -> tests in the sandbox -> fix -> install gate -> registry.

Enforced in code:
  - an attempt is one generate/fix of the code followed by a test run; the budget is caps.attempts_per_tool
  - as soon as the tests pass, editing stops and the install gate opens (no extra tests or refactors)
  - restarts after a fail streak happen inside the budget and do not reset it
  - after the budget: the user can advise (a few more attempts) or stop
  - a permission or package increase always goes through the approval gate
  - a spec that does what a core capability (mini/core) does is rejected
"""
import difflib
import hashlib
import json
import os
import re
import shutil
import time

from . import codeblocks, prompts, samples
from .turns import AgentStop, ModelGaveUp, RestartRequested, Turns, fn
from ..harness.registry import NAME_RE, is_bundled
from ..harness.sandbox import PolicyError, repair_argv, validate_packages, validate_targets
from ..harness import toolui
from ..harness.settings import KEY_RE, validate_declarations
from ..harness.lessons import format_lessons

S = {"type": "string"}
OBJ = {"type": "object"}
ARR = {"type": "array", "items": S}
T_DESIGN = fn("write_design", "Write what the new tool does.", {"name": S, "description": S, "how_it_works": ARR},
              ["name", "description", "how_it_works"])
T_IO = fn("write_io", "Write the tool's inputs, outputs and saved settings.", {
    "input_schema": OBJ, "output_schema": OBJ,
    "settings": {"type": "array", "items": {"type": "object", "properties": {
        "key": S, "description": S, "schema": OBJ, "secret": ARR}}}}, ["input_schema", "output_schema"])
T_RUN = fn("write_run", "Write how the tool runs: interaction, ui hints and permissions.", {
    "interaction": ARR, "ui": OBJ,
    "permissions": {"type": "object", "properties": {"network": ARR, "packages": {"type": "object"}}}}, ["interaction"])
T_EDGES = fn("write_edge_cases", "Write the edge cases the user's task states (often none).", {
    "cases": {"type": "array", "items": {"type": "object", "properties": {
        "situation": S, "expected": S, "quote": S}}}}, ["cases"])
GAP_HOLDS = 2                   # the pre-test checks hold a test run back at most this often for the same reason
T_PLAN = fn("write_tool_plan", "Write how the script becomes a tool: name, schemas and the example inputs.", {
    "name": S, "description": S, "input_schema": OBJ, "output_schema": OBJ, "example_inputs": OBJ,
    "outputs": {"type": "array", "items": S}}, ["name", "description", "input_schema", "example_inputs"])
KEEP_MAX = 10 * 2**20           # the files a kept script saw travel with the tool as its test data, up to this size
GOLDEN_TEST = '''"""Written by the harness: the tool must do what the script it was kept from did on that run."""
import json
import os
import shutil

from mini_io import MissingInput, ScriptedIO
from tool import run

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden")
EXAMPLE = json.load(open(os.path.join(HERE, "example.json")))


class Answers(ScriptedIO):
    """The example inputs answer the tool's questions; a confirmation is yes, as the user said yes by running it."""
    def ask(self, key, prompt, kind="text", choices=None, default=None, multi=False):
        try:
            return super().ask(key, prompt, kind, choices, default, multi)
        except MissingInput:
            if kind == "confirm":
                return True
            raise


def unordered(v):
    """JSON with every list's items in one fixed order: a script that listed a folder wrote them in the order the
    folder gave, which another run need not repeat."""
    if isinstance(v, dict):
        return {k: unordered(x) for k, x in v.items()}
    if isinstance(v, list):
        return sorted((unordered(x) for x in v), key=lambda x: json.dumps(x, sort_keys=True))
    return v


def same(path, want):
    got = open(path, "rb").read()
    if got == want:
        return True
    try:
        g, w = got.decode("utf-8"), want.decode("utf-8")
    except UnicodeDecodeError:          # binary output (an image, a document): the same kind of file of a similar size
        return got[:4] == want[:4] and 0.5 * len(want) <= len(got) <= 2 * len(want) + 64
    if path.endswith(".json"):
        try:
            return unordered(json.loads(g)) == unordered(json.loads(w))
        except ValueError:
            return False
    norm = lambda t: [ln.rstrip() for ln in t.replace("\\r\\n", "\\n").strip().split("\\n")]
    return norm(g) == norm(w)


def test_does_what_the_script_did():
    src = os.path.join(HERE, "in")
    for d, _, files in os.walk(src):
        for f in files:
            rel = os.path.relpath(os.path.join(d, f), src)
            os.makedirs(os.path.dirname(rel) or ".", exist_ok=True)
            shutil.copyfile(os.path.join(d, f), rel)
    out = run(dict(EXAMPLE), Answers(answers=EXAMPLE, inputs=EXAMPLE))
    assert isinstance(out, dict), f"run returned {type(out).__name__}, not a dict"
    want_dir = os.path.join(HERE, "out")
    wrong = []
    for d, _, files in os.walk(want_dir):
        for f in files:
            rel = os.path.relpath(os.path.join(d, f), want_dir)
            want = open(os.path.join(d, f), "rb").read()
            if not os.path.exists(rel):
                wrong.append(f"{rel}: not written (the script wrote it)")
            elif not same(rel, want):
                got = open(rel, "rb").read()
                wrong.append(f"{rel}: differs from the script's file; script: {want[:300]!r} tool: {got[:300]!r}")
    assert not wrong, "\\n".join(wrong)
'''
SPEC_THINK_TOKENS = 1500        # each spec phase thinks on its own budget, in its own fresh conversation
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
        self.attempt_log, self.snapshot, self.repair_note, self.advice = [], {}, "", None
        self.kept = False
        a.log.emit("phase", phase="create", need=need)
        self._copy_samples()
        self._spec_phase()
        return self._code_and_test(first=True)

    def keep(self, script_run, what, task, context):
        """A script run_code ran successfully becomes a tool: the model turns it into run(inputs, io) with the
        script's hardcoded values as inputs; the harness writes the test from the run itself (the files the script
        saw, the example inputs, the files it wrote) and the usual fix loop makes the tool pass it."""
        a = self.a
        a.wd.reset_task()
        rec = script_run.get("record")
        self.task, self.context, self.need = task, context, what or "keep the script as a tool"
        self.build = os.path.join(a.run_dir, "build", f"kept{len(a.created) + 1}")
        shutil.rmtree(self.build, ignore_errors=True)
        self.files_dir = os.path.join(self.build, "files")
        golden = os.path.join(self.files_dir, "golden")
        os.makedirs(golden)
        size = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(rec) for f in fs)
        if size > KEEP_MAX:
            raise ToolFailed(f"the script's files ({size // 2**20} MB) are too large to keep as the tool's test")
        for part in ("in", "out"):
            if os.path.isdir(os.path.join(rec, part)):
                shutil.copytree(os.path.join(rec, part), os.path.join(golden, part))
            else:
                os.makedirs(os.path.join(golden, part))
        self.spec, self.approved_perms, self.image = None, {"network": [], "packages": {"apt": [], "pip": []}}, None
        self.parent, self.restarts, self.consults, self.errors = None, 0, 0, {}
        self.samples_nudged, self.last_output, self.last_hash = True, "", None
        self.attempt_log, self.snapshot, self.repair_note, self.advice = [], {}, "", None
        self.kept = True
        a.log.emit("phase", phase="keep", what=self.need)
        seen = sorted(os.path.relpath(os.path.join(d, f), os.path.join(golden, "in"))
                      for d, _, fs in os.walk(os.path.join(golden, "in")) for f in fs)
        listing = lambda xs: ", ".join(xs[:40]) + (f" (+{len(xs) - 40} more)" if len(xs) > 40 else "") or "(none)"
        plan = self._phase("keep_plan", T_PLAN, self._lessons_block() + prompts.KEEP_PLAN.format(
            rules=prompts.CREATE_RULES, task=trim(self.task, 1500), what=f"What to keep: {what}\n" if what else "",
            script=script_run["script"], seen=listing(seen), wrote=listing(script_run.get("written") or []),
            printed=trim(script_run.get("output") or "", 800), names=", ".join(a.registry.names()) or "(none)",
            note=""), lambda d: self._check_plan(d, [os.path.normpath(w) for w in script_run.get("written") or []],
                                              script_run.get("script") or ""))
        json.dump(plan["example_inputs"], open(os.path.join(golden, "example.json"), "w"), indent=1)
        made_inputs = _self_made_inputs(plan["example_inputs"], golden, plan.get("outputs"))
        if made_inputs:                 # the script made its own sample input: for the test it is input, not result
            a.log.emit("info", text="kept tool's test gets the inputs the script made: " + ", ".join(made_inputs))
        keep_out = [o for o in plan.get("outputs") or [] if o not in made_inputs]
        if plan.get("outputs") is not None:    # scratch files and samples the script wrote are not what the tool makes
            out_dir = os.path.join(golden, "out")
            for d, _, fs in os.walk(out_dir):
                for f in fs:
                    if os.path.relpath(os.path.join(d, f), out_dir) not in keep_out:
                        os.remove(os.path.join(d, f))
            a.log.emit("info", text="kept tool's test checks: " + (", ".join(keep_out) or "its result only, no file"))
        spec = {"name": plan["name"], "description": plan["description"], "input_schema": plan["input_schema"],
                "output_schema": plan.get("output_schema") or {"type": "object", "properties": {}},
                "settings": [], "interaction": [], "ui": {}, "edge_cases": [],
                "permissions": {"network": [], "packages": script_run.get("packages") or {}}}
        self.spec = self._clean_spec(spec)
        a.log.emit("spec", spec=self.spec, kept_from_script=True)
        ok, msg = self._approve_permissions(self.spec["permissions"])
        if not ok:
            raise ToolFailed(msg)
        self.example = plan["example_inputs"]
        self.messages = [{"role": "system", "content": prompts.SYSTEM + "\n\n" + prompts.CREATE_RULES}]
        intro = (f"Plan of the tool:\n{json.dumps({**self.spec, 'example_inputs': self.example}, indent=1)}\n\n"
                 f"The script it is kept from (it worked on the user's files):\n```python\n{script_run['script']}```\n\n")
        body = self._ask_file("tool.py", prompts.CODE_FILE.format(intro=intro, path="tool.py", what=prompts.WHAT_KEPT), "code")
        if body is None:
            raise ModelGaveUp("no usable tool.py after 3 replies")
        self._write("tool.py", body)
        self._write("test_tool.py", GOLDEN_TEST)
        a.log.emit("code", tool=self.spec["name"], files={k: open(os.path.join(self.files_dir, k)).read() for k in FILES})
        res = self._run_tests()
        out = self._test_message(res)
        verdict = a.wd.on_test(res["passed"], True, self._hash())
        if not res["passed"]:
            self._record_attempt(res)
        done = self._after_test(verdict)
        if done:
            return done
        if verdict == "continue":
            self._fresh(out, head=prompts.KEPT_NOTE + "\n\n")
        return self._fix_loop()

    def _check_plan(self, d, wrote=(), script=""):
        for k in ("input_schema", "output_schema"):
            d[k] = object_schema(d.get(k) or {"type": "object", "properties": {}})
        props = (d["input_schema"] or {}).get("properties") or {}
        ex = d.get("example_inputs")
        if not isinstance(ex, dict):
            return "example_inputs must be an object: the inputs that redo this run."
        extra = [k for k in ex if k not in props]
        if extra:
            return (f"example_inputs has keys that are not input_schema properties: {', '.join(extra)}. Each input "
                    f"is a field of input_schema.properties: {{\"type\": \"object\", \"properties\": "
                    f"{{\"{extra[0]}\": {{...}}}}, \"required\": [...]}}.")
        missing = [k for k in (d["input_schema"].get("required") or []) if k not in ex]
        if missing:
            return f"example_inputs lacks the required inputs {', '.join(missing)}."
        if script:                      # the example redoes the script's run: the files it names are the script's
            other = [v for v in _strings(ex) if FILE_LIKE.match(v) and os.path.basename(v) not in script]
            if other:
                used = sorted(set(m.group(1) for m in re.finditer(r"""['"]([\w./-]+\.\w{1,8})['"]""", script)))
                return (f"example_inputs must redo the script's run, but the script does not use {', '.join(other)}"
                        + (f"; the files it works on: {', '.join(used)}" if used else "") + ".")
        outs = d.get("outputs")
        if outs is not None:
            if isinstance(outs, list):          # ["output_path"]: the input that names the file, for the file itself
                outs = [ex[o] if isinstance(o, str) and isinstance(ex.get(o), str) else o for o in outs]
            outs = [os.path.normpath(str(o)) for o in outs] if isinstance(outs, list) else None
            named = {os.path.normpath(v) for v in _strings(ex) if v}
            results = [w for w in wrote if os.path.normpath(w) not in named   # a file the example gives is a sample,
                       and not _reread(w, script)]                          # and so is one the script wrote, then used
            if outs is None or (results and not outs) or any(o not in wrote for o in outs):
                samples = [w for w in wrote if w not in results]
                return (f"outputs must list the files the tool makes: {', '.join(results) or '(none)'}"
                        + (f". The script also wrote {', '.join(samples)}: samples it made for the tool to read, "
                           f"inputs, not outputs: leave them out" if samples else "")
                        + ". Give file paths, not input names.")
            d["outputs"] = outs
        return self._validate_spec({**d, "edge_cases": []}, new=True)

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
        self.attempt_log, self.snapshot, self.advice = [], self._read_files(), None
        self.kept = False
        self.repair_note = (f"The installed v{manifest['version']} failed on the real task: {problem}\n"
                            f"Evidence:\n{trim(evidence, 1200)}\n")
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
        """The spec in four short phases, each in a fresh conversation with its own thinking budget and only what it
        needs: design, inputs/outputs, how it runs, edge cases. Each phase's output is checked when it arrives; the
        harness puts the spec together (no model call) and checks it whole."""
        a = self.a
        revision = ""
        for _ in range(4):
            note = f"\nThe user revised the plan: {revision}\n" if revision else ""
            design = self._phase("design", T_DESIGN, self._lessons_block() + prompts.SPEC_DESIGN.format(
                rules=prompts.CREATE_RULES, need=self.need, task=self.task, context=self.context,
                names=", ".join(a.registry.names()) or "(none)", revision=note) + self._preview_block(),
                self._check_design)
            how = "\n".join(f"{i}. {x}" for i, x in enumerate(design["how_it_works"], 1))
            io = self._phase("io", T_IO, prompts.SPEC_IO.format(
                name=design["name"], description=design["description"], how=how, task=trim(self.task, 1500),
                settings=self._settings_listing(), revision=note) + self._preview_block(), self._check_io)
            why = ""
            for _ in range(3):
                run = self._phase("run", T_RUN, prompts.SPEC_RUN.format(
                    name=design["name"], description=design["description"], how=how,
                    inputs=json.dumps(io["input_schema"]), revision=note + why,
                    setting_keys=", ".join(d.get("key", "?") for d in io.get("settings") or []) or "none"),
                    lambda r: self._check_run(r, io))
                edges = self._edge_phase(design, io)
                spec = {**design, **io, **run, "edge_cases": edges}
                err = self._validate_spec(spec, new=True)
                if err:
                    a.log.emit("watchdog", name="spec_rejected", problem=err)
                    why = f"\nThe spec put together from the steps was rejected: {err} Fix it here.\n"
                    continue
                self.spec = self._clean_spec(spec)
                a.log.emit("spec", spec=self.spec)
                ok, msg = self._approve_permissions(self.spec["permissions"])
                if not ok:
                    why = f"\n{msg} Plan it so that it works without that, or with less.\n"
                    continue
                break
            else:
                raise ModelGaveUp("no valid spec after 3 tries at how the tool runs")
            revision = self._review_plan()
            if not revision:
                return
        raise AgentStop("the plan was revised too many times")

    def _phase(self, name, tool, content, check, tries=4):
        """One spec phase: a fresh conversation, one tool, checked arguments. Returns them."""
        a = self.a
        a.log.emit("phase", phase="spec_" + name)
        a.wd.reset_conversation()
        msgs = [{"role": "system", "content": prompts.SYSTEM}, {"role": "user", "content": content}]
        t = Turns(a, "spec", msgs, [tool], think_tokens=a.cfg["caps"].get("spec_think_budget_tokens", SPEC_THINK_TOKENS))
        want = tool["function"]["name"]
        for _ in range(tries):
            reply = t.ask()
            call = next((c for c in reply["tool_calls"] if c["name"] == want), None)
            for extra in reply["tool_calls"]:
                if extra is not call:
                    t.result(extra["name"], f"ignored: call {want} only")
            if call is None:
                continue
            args = call["arguments"] if isinstance(call["arguments"], dict) else {}
            err = check(args)
            if err:
                a.log.emit("watchdog", name="spec_rejected", phase=name, problem=err)
                t.result(want, f"Rejected: {err} Fix it and call {want} again.")
                continue
            t.result(want, "Accepted.")
            return args
        raise ModelGaveUp(f"no valid {want} after {tries} tries")

    def _check_design(self, d):
        if not d.get("name"):
            guess = re.split(r"[:\s]", self.need.strip(), maxsplit=1)[0]      # the need often starts with the name
            if NAME_RE.match(guess):
                d["name"] = guess
        d["how_it_works"] = [str(x) for x in d.get("how_it_works") or []] if isinstance(d.get("how_it_works"), list) \
            else [str(d.get("how_it_works") or "")]
        if not d.get("description") or not d["how_it_works"][0]:
            return "description and how_it_works are both needed."
        return self._validate_spec({**d, "input_schema": {"type": "object"}, "output_schema": {"type": "object"},
                                    "edge_cases": []}, new=True)

    def _check_io(self, d):
        for k in ("input_schema", "output_schema"):
            d[k] = object_schema(d.get(k))
            if not isinstance(d[k], dict) or not d[k].get("properties"):
                return f"{k} needs properties."
        d["settings"] = d.get("settings") or []
        return validate_declarations(d["settings"])

    def _check_run(self, d, io):
        if not isinstance(d.get("interaction") or [], list):
            return "interaction must be a list of short steps."
        d["interaction"] = [str(x) for x in d.get("interaction") or []]
        d["ui"] = toolui.tidy(d.get("ui"), io["input_schema"])
        problem = toolui.validate(d["ui"], io["input_schema"])
        if problem:
            return problem
        d["permissions"] = d.get("permissions") or {"network": [], "packages": {}}
        try:
            self._norm_perms(d["permissions"], io.get("settings") or [])
        except PolicyError as e:
            return str(e) + "."
        return None

    def _edge_phase(self, design, io):
        """Edge cases the user's task states. Each must quote the task's own words; one that quotes nothing in the
        task is dropped (a guessed edge case costs attempts and tests what nobody asked for). Expected errors are
        written as "fails with an error": the tests assert any error, never a type or a message."""
        a = self.a
        args = self._phase("edges", T_EDGES, prompts.SPEC_EDGES.format(
            name=design["name"], description=design["description"], task=self.task,
            inputs=json.dumps(io["input_schema"]), preview=self._preview_block()), lambda d: None
            if isinstance(d.get("cases"), list) else "cases must be a list (empty when the task states none).")
        kept, dropped = [], []
        for c in args.get("cases") or []:
            c = c if isinstance(c, dict) else {"situation": str(c)}
            sit, exp, quote = (str(c.get(k) or "").strip() for k in ("situation", "expected", "quote"))
            if not sit or not quoted(quote, self.task):
                dropped.append(sit or str(c))
                continue
            if re.search(r"\b(error|raise|exception|fail|abort|stop)", exp, re.I):
                exp = "fails with an error"
            kept.append(f"{sit} -> {exp or 'as the task states: ' + quote}")
        if dropped:
            a.log.emit("watchdog", name="edge_case_unquoted", dropped=dropped)
        a.log.emit("edge_cases", kept=kept, dropped=dropped)
        return kept

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
        s["edge_cases"] = s.get("edge_cases") or []           # the task often states none
        missing = [k for k in ("name", "description", "input_schema", "output_schema") if not s.get(k)]
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
        if first:                   # the code is written in its own conversation: the spec phases had theirs
            self.messages = [{"role": "system", "content": prompts.SYSTEM + "\n\n" + prompts.CREATE_RULES}]
        intro = (f"Task the tool is built for: {trim(self.task, 1500)}{self._preview_block()}\n\n"
                 f"Spec approved:\n{spec_text}\n\n")
        for path, what in (("tool.py", prompts.WHAT_TOOL),
                           ("test_tool.py", prompts.WHAT_TESTS.format(edge_cases="; ".join(self.spec["edge_cases"]) or prompts.NO_EDGE_TESTS)
                                            + samples.tests_note(self.files_dir))):
            body = self._ask_file(path, prompts.CODE_FILE.format(intro=intro, path=path, what=what), "code")
            if body is None:
                raise ModelGaveUp(f"no usable {path} after 3 replies")
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
        if not res["passed"]:
            self._record_attempt(res)
        done = self._after_test(verdict)
        if done:
            return done
        if verdict == "continue":
            self._fresh(out)
        return self._fix_loop()

    def _edge_case_gap(self):
        if getattr(self, "kept", False):
            return ""
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
        if not gaps:
            return ""
        text = "Saved, but " + " ".join(gaps) + " Then call `run_tests`."
        held = getattr(self, "gap_held", {})
        self.gap_held = held
        key = tuple(g[:40] for g in gaps)
        held[key] = held.get(key, 0) + 1
        if held[key] > GAP_HOLDS:               # a check that keeps holding the tests back would loop without attempts
            self.a.log.emit("watchdog", name="gap_let_through", gaps=[g[:120] for g in gaps], holds=held[key] - 1)
            self.gap_note = "Note: " + " ".join(gaps)
            return ""
        return text

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
        res = a.sandbox.run(["python", "-m", "pytest", "-q", "--tb=short", "--disable-warnings", "-p", "no:cacheprovider",
                             "-p", "mini_pytest",
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
        if getattr(self, "gap_note", "") and not res["passed"]:
            out += "\n" + self.gap_note
        self.gap_note = ""
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
        self._fresh(trim(self.last_output), head=prompts.RESTART.format(
            streak=a.cfg["watchdogs"]["fail_streak_restart"]) + "\n\n")

    # ================================================================== attempts: state kept on disk, fresh context
    def _read_files(self):
        return {n: open(os.path.join(self.files_dir, n)).read() for n in FILES
                if os.path.exists(os.path.join(self.files_dir, n))}

    def _record_attempt(self, res):
        """One line per failed attempt, written by the harness: what changed since the last test run, and how the
        tests failed. It replaces the conversation of that attempt (see _fresh)."""
        now = self._read_files()
        changes = [_diff_summary(n, self.snapshot.get(n, ""), now.get(n, "")) for n in FILES]
        changes = [c for c in changes if c] if self.snapshot else ["first version of tool.py and test_tool.py"]
        self.snapshot = now
        entry = (f"Attempt {self.a.wd.attempts}: {self.last_test.get('summary', '')}\n"
                 f"  changed: {'; '.join(changes) or 'nothing'}\n"
                 + "".join(f"  failed: {f}\n" for f in _failures(self.last_output)))
        self.attempt_log.append(entry)
        open(os.path.join(self.build, "attempts.md"), "w").write("\n".join(self.attempt_log))
        self.a.log.emit("attempt_note", text=entry)

    def _attempts_text(self, keep=6):
        log = self.attempt_log
        if not log:
            return "(none yet)"
        older = f"({len(log) - keep} earlier attempts not shown)\n" if len(log) > keep else ""
        return older + "\n".join(log[-keep:])

    def _fresh(self, output, head=""):
        """A new conversation for the next attempt: everything it needs is rebuilt from the files and the attempt
        log, so the context stays the same size however many attempts there were."""
        a = self.a
        a.wd.reset_conversation()
        got = samples.listing(self.files_dir)
        if getattr(self, "kept", False) and prompts.KEPT_NOTE not in head:
            head = prompts.KEPT_NOTE + "\n\n" + head
        content = prompts.STATE.format(
            head=head + (self.repair_note or ""), name=self.spec["name"], task=trim(self.task, 800),
            spec=json.dumps(self.spec, indent=1), files=self._files_view(),
            samples=(f"Real samples next to tool.py: {', '.join('samples/' + p for p, _ in got)}\n" if got else ""),
            advice=(f"Advice from the user: {self.advice}\n" if self.advice else ""),
            attempts=self._attempts_text(), output=output)
        self.messages = [{"role": "system", "content": prompts.SYSTEM + "\n\n" + prompts.CREATE_RULES},
                         {"role": "user", "content": self._lessons_block() + content}]
        a.log.emit("phase", phase="fresh_context", tool=self.spec["name"], attempts=a.wd.attempts,
                   chars=len(content))

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
                        raise ModelGaveUp(f"stuck repeating {name} with the same arguments while building the tool")
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
                    if changed and not res["passed"]:
                        self._record_attempt(res)
                    done = self._after_test(verdict)
                    if done:
                        return done
                    if verdict in ("restart", "budget"):      # fresh conversation or a new model: drop the rest of this reply
                        break
                    if changed and not res["passed"]:         # a failed attempt: the next one starts fresh
                        self._fresh(out)
                        self.messages.append({"role": "user", "content": prompts.FIX})
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
            editable = ("tool.py",) if getattr(self, "kept", False) else FILES
            if path not in editable:
                return (f"You can only edit {', '.join(editable)}." + (" test_tool.py is the harness's check that the "
                        "tool does what the script did." if getattr(self, "kept", False) else "")), None
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
            dropped = ""
            if "edge_cases" in args:                  # the fix loop may drop an edge case, not add a guessed one
                given = [str(e) for e in args.get("edge_cases") or []]
                new = [e for e in given if e not in self.spec["edge_cases"]]
                args["edge_cases"] = [e for e in given if e in self.spec["edge_cases"]]
                spec["edge_cases"] = args["edge_cases"]
                if new:
                    dropped = (f" New edge cases are not added here ({'; '.join(new)}): the edge cases come from what "
                               f"the user's task states.")
            for k in ("description", "input_schema", "output_schema", "interaction", "ui"):
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
            return "Spec updated." + dropped + (" " + msg if msg else ""), None
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

    # ================================================================== budget, install
    def _consult_user(self):
        """Budget used up: ask the user. Returns "advice" (the budget was extended) or None (stop)."""
        a = self.a
        cc = a.cfg["consult"]
        can_advise = cc["enabled"] and self.consults < cc["max_rounds"]
        if not can_advise:
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
        options = ("send advice", "stop")
        details = {"tool": self.spec["name"], "model": a.model, "building": q["building"],
                   "keeps failing": q["keeps_failing"], "already tried": q["tried"],
                   "recurring errors": "; ".join(f"{s} (x{n})" for s, n in recurring) or "-",
                   "attempts used": str(a.wd.attempts), "question": q["question"],
                   "send advice": f"{cc['extra_attempts']} more attempts with your advice (write it in the note)"}
        d = a.gate.request("consult", "mini is stuck and would like your advice", details, options=options)
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
        a.log.emit("cap", cap="attempt_budget", tool=self.spec["name"], attempts=a.wd.attempts)
        raise ToolFailed(f"{self.spec['name']}: tests still fail after {a.wd.attempts} attempts")

    def _install(self):
        a = self.a
        if a.wd.attempts > 1:
            self._record_lesson("fixed")
        created_by = {"provider": "local", "model": a.model, "preset": a.cfg["llamacpp"].get("preset") or None}
        manifest = dict(self.spec)
        manifest.update({
            "kind": "code", "files": sorted(os.listdir(self.files_dir)), "test_file": "test_tool.py",
            "test_results": self.last_test, "created_by": created_by,
            "attempts_used": a.wd.attempts,
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


def quoted(quote, text, min_chars=10):
    """quote is the task's own words: at least min_chars of it, found in text (case, spacing and punctuation aside)."""
    norm = lambda x: " ".join(re.sub(r"[^\w]+", " ", str(x).lower()).split())
    q = norm(quote)
    return len(q) >= min_chars and q in norm(text)


def _diff_summary(name, old, new, max_chars=300):
    """'tool.py lines 12-15, 30 (+4 -2): <first new line>' -- what an attempt changed in one file."""
    if old == new:
        return ""
    a, b = old.splitlines(), new.splitlines()
    spans, added, removed, first = [], 0, 0, ""
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        added, removed = added + (j2 - j1), removed + (i2 - i1)
        spans.append(f"{j1 + 1}" if j2 - j1 <= 1 else f"{j1 + 1}-{j2}")
        if not first:
            first = next((ln.strip() for ln in b[j1:j2] if ln.strip()), "") or \
                next(("removed " + ln.strip() for ln in a[i1:i2] if ln.strip()), "")
    text = f"{name} lines {', '.join(spans[:6])}{' ...' if len(spans) > 6 else ''} (+{added} -{removed})"
    return (text + (f": {first}" if first else ""))[:max_chars]


def _failures(output, limit=5):
    """'test_name: first error line' for each failing test of a pytest run (collection errors included)."""
    out, seen = [], set()
    for m in re.finditer(r"^_{3,} (?:ERROR collecting )?(\S+?) _{3,}\n(.*?)(?=^_{3,} |^={3,}|\Z)", output or "", re.M | re.S):
        err = next((ln[1:].strip() for ln in m.group(2).splitlines() if ln.startswith("E ")), "")
        if err:                     # the traceback section: the summary line pytest cuts to the terminal width
            seen.add(m.group(1).split("::")[-1])
            out.append(f"{m.group(1).split('::')[-1]}: {err[:160]}")
    for m in re.finditer(r"^(FAILED|ERROR) (\S+?)(?:::(\w+))?(?: - (.*))?$", output or "", re.M):
        if (m.group(3) or os.path.basename(m.group(2))) in seen:
            continue
        out.append(f"{m.group(3) or 'collecting ' + os.path.basename(m.group(2))}: {(m.group(4) or m.group(1)).strip()[:160]}")
    if not out:
        for line in (output or "").splitlines():
            if line.startswith("E ") or "Error" in line[:40]:
                out.append(line.strip()[:160])
                break
    return out[:limit]


def _lower_types(x):
    if isinstance(x, dict):
        return {k: (v.lower() if k == "type" and isinstance(v, str) else _lower_types(v)) for k, v in x.items()}
    if isinstance(x, list):
        return [_lower_types(v) for v in x]
    return x


FILE_LIKE = re.compile(r"^(?:[\w.-]+/)*[\w-][\w.-]*\.\w{1,8}$")


def _strings(v):
    if isinstance(v, str):
        return [v.strip()]
    if isinstance(v, list):
        return [s for x in v for s in _strings(x)]
    if isinstance(v, dict):
        return [s for x in v.values() for s in _strings(x)]
    return []


def _reread(path, script):
    """The script names this file it wrote more than once: it wrote a sample, then worked on it (a test script that
    makes test_invalid.yaml and validates it). Such a file is not a result the tool must make."""
    return bool(script) and script.count(os.path.basename(path)) >= 2


def _self_made_inputs(example, golden, outputs):
    """Files the example inputs name that the script wrote and did not find, and that are not among the tool's
    outputs (a sample it made, then worked on): moved from the run's results to its inputs, so the kept tool's test
    starts with them. With outputs unknown (None) nothing tells a sample from a result, and nothing moves; with none
    (an empty list) the tool writes no file, so every file the example names is a sample. Returns their paths."""
    moved = []
    if outputs is None:
        return moved
    outs = {os.path.normpath(str(o)) for o in outputs}
    for rel in (os.path.normpath(x) for x in _strings(example) if x):
        if rel.startswith("..") or os.path.isabs(rel) or rel in outs:
            continue
        src, dest = os.path.join(golden, "out", rel), os.path.join(golden, "in", rel)
        if os.path.isfile(src) and not os.path.exists(dest):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.move(src, dest)
            moved.append(rel)
    return moved


def object_schema(x):
    """A schema the local model gave as a bare field map ({"csv": {"type": "string"}}) becomes the object schema it
    meant, and type names are lower case ("STRING" is "string"); anything else is returned as it is."""
    x = _lower_types(x)
    if isinstance(x, dict) and x and not {"type", "properties"} & set(x) \
            and all(isinstance(v, dict) and ("type" in v or "description" in v) for v in x.values()):
        return {"type": "object", "properties": x}
    if isinstance(x, dict) and x.get("type") == "object":      # fields put next to "type" instead of in properties
        stray = {k: v for k, v in x.items() if k not in SCHEMA_WORDS and isinstance(v, dict)
                 and ("type" in v or "description" in v)}
        if stray:
            props = x.get("properties") if isinstance(x.get("properties"), dict) else {}
            return {**{k: v for k, v in x.items() if k not in stray}, "properties": {**stray, **props}}
    return x


SCHEMA_WORDS = {"type", "properties", "required", "additionalProperties", "description", "title", "items", "enum",
                "default", "$schema", "examples", "patternProperties", "minProperties", "maxProperties"}


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
