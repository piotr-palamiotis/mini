"""mini-agent: parse -> confirm-first -> gap detection -> create missing tools -> complete the task with them.

The harness objects (backends, sandbox, registry, gate, watchdogs, log) are handed in; the agent never
gets more authority than they allow. Its own actions are the core capabilities (mini/core): typed calls,
validated against their schema before they run; gap detection and need_tool never build what core already does.

The model is local only: llama-server, built and run by mini setup, with the active preset's model ([local] and
[llamacpp] in the config). The user picks the preset (`mini preset use`, /model in the UI); a task never moves to
another model by itself.
"""
import contextlib
import fnmatch
import hashlib
import json
import os
import re
import threading
import time

from . import prompts, samples
from .creator import _content_words, Creator, ToolFailed, trim
from .turns import AgentStop, ModelGaveUp, RestartRequested, Turns, fn
from ..core import Call, load as load_catalog
from ..core._tools import describe_tool
from ..core.run_code import ARGV_IDLE, argv_idle, job_of
from ..harness import checkpoint as ckpt, gates, hwmon, journal, workspace as ws
from ..harness.approvals import ApprovalGate
from ..harness.backends import Endpoint, make_backend
from ..harness.config import llama_settings
from ..harness.eventlog import JournalSink, RunLog
from ..harness.lessons import LessonStore
from ..harness.registry import Registry, is_bundled
from ..harness.sandbox import PolicyError, Sandbox
from ..harness.settings import SettingsStore
from ..harness.watchdogs import Watchdogs

S = {"type": "string"}
ARR = {"type": "array", "items": S}
MIN_IMPLICIT_ANSWER = 200
SAMPLING_KEYS = ("seed", "top_k", "top_p", "min_p", "repeat_penalty", "presence_penalty")   # optional, in [local]


def as_list(v):
    """A list field a model may send as a string ("-", "none", "a; b") or null."""
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return list(v)
    v = str(v).strip()
    return [] if v.lower() in ("", "-", "none", "n/a", "no", "null") else [v]


def normalize_understanding(u):
    u = dict(u)
    for k in ("inputs", "outputs", "side_effects", "ambiguities"):
        u[k] = [x if isinstance(x, str) else json.dumps(x) for x in as_list(u.get(k))]
    for k in ("goal", "plan"):   # some models send the plan as a list of steps
        v = u.get(k)
        if isinstance(v, (list, tuple)):
            u[k] = "\n".join(x if isinstance(x, str) else json.dumps(x) for x in v)
        elif v is not None and not isinstance(v, str):
            u[k] = json.dumps(v)
    return u


T_UNDERSTAND = fn("understand", "State your understanding of the task.", {
    "goal": S, "inputs": ARR, "outputs": ARR, "side_effects": ARR, "ambiguities": ARR, "plan": S},
    ["goal", "inputs", "outputs", "plan"])
T_COVERAGE = fn("decide_coverage", "Decide which installed tools cover the task and which capabilities are missing.", {
    "reasoning": S, "use": ARR, "missing": ARR}, ["reasoning", "use", "missing"])
T_JUDGE = fn("judge_tool", "Say whether this one tool can do a step of the task.", {
    "fits": {"type": "boolean"}, "step": S, "why": S}, ["fits"])
T_SCREEN = fn("shortlist", "Name the installed tools that could do a step of the task.", {"names": ARR}, ["names"])
T_CHOOSE = fn("choose_tools", "Pick the tools to use among the ones that fit.", {"use": ARR, "why": S}, ["use"])
ASKS_FOR_TOOL = re.compile(r"\b(build|make|create|write|develop)\b[^.]{0,60}?\btools?\b", re.I)
NUMBER_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5}


def named_tools(task):
    """The tools a task asks for by description ("Build the tools you need: one that makes X and one that reads Y"),
    as their descriptions; "Build two tools" alone gives that many empty ones. [] when it names no count."""
    m = ASKS_FOR_TOOL.search(task or "")
    if not m:
        return []
    sentence = re.split(r"(?<=[.!?])\s", task[m.start():], maxsplit=1)[0]
    parts = [p.strip(" ,;:") for p in re.split(r"\bone that\b", sentence, flags=re.I)[1:]]
    parts = [re.sub(r"(,?\s*\band\b|,)\s*$", "", p).strip(" ,;:.") for p in parts]
    n = re.search(r"\b(two|three|four|five)\s+tools\b", sentence, re.I)
    count = max(len(parts), NUMBER_WORDS[n.group(1).lower()] if n else 0)
    return (parts + [""] * count)[:count] if count >= 2 else []


GAP_CANDIDATES = 5              # registry tools judged one by one for a task (the best search matches)
T_FORM = fn("judge_form", "Say whether this file has the form the task asks for it.", {
    "matches": {"type": "boolean"}, "why": S}, ["matches", "why"])
GAP_THINK_TOKENS = 700          # each judgement is one small question
T_BUG = fn("report_tool_bug", "An installed tool has a bug. It goes back to the creation loop as a new version.",
           {"name": S, "problem": S}, ["name", "problem"])
T_NEED = fn("need_tool", "Build a tool from a spec, for a capability a run_code script cannot try in the sandbox: it "
            "needs the network or a dialogue with the user. Work a script can do is run with run_code and kept with "
            "keep_tool.", {"need": S, "from_run_code": {"type": "boolean"}}, ["need"])
T_KEEP = fn("keep_tool", "Keep the last run_code script that ran without an error as a tool for later tasks: the "
            "harness tests the tool on that run. what: what the tool does, in general words.", {"what": S}, [])
T_FINISH = fn("finish", "The task is done. answer: the reply to the user, with any list or result written out in it. "
              "files: only deliverable files you wrote or changed in the folder; leave it out when the answer is text "
              "(names a tool returned are not files). checks: only when the harness asks for them, one per file.",
              {"answer": S, "files": ARR, "checks": {"type": "array", "items": {
                  "type": "object", "required": ["file", "task_asks", "file_has", "matches"], "properties": {
                      "file": S, "sample": S, "task_asks": S, "file_has": S, "matches": {"type": "boolean"}}}}},
              ["answer"])
PATH_LIKE = re.compile(r"^(?:[\w.-]+/)*[\w.-]*(?:\.\w{1,8}|/)$")   # a.csv, out/, reports/x.md
CONTROL_TOOLS = [T_BUG, T_KEEP, T_NEED, T_FINISH]         # the pipeline's own calls; everything else is a core capability



def form_gap(task, text):
    """The task gives a line form in backticks, words between punctuation (`path:line: text`), and most of the
    text's lines have that form when spacing is ignored, but some differ in spacing: what to fix, else ""."""
    lines = [re.sub(r"^\s*(?:[-*+]\s+)?`?(.*?)`?\s*$", r"\1", ln) for ln in text.splitlines() if ln.strip()]
    for form in re.findall(r"`([^`\n]{3,80})`", task):
        parts = re.split(r"(\w+)", form)
        seps = parts[2:-1:2]
        if len(parts[1::2]) < 2 or not all(re.search(r"[^\w\s]", sp) for sp in seps):
            continue
        lead, tail = parts[0], parts[-1]
        val = r"(\S(?:.*?\S)?)"                # a value does not start or end with a space
        strict = re.compile("^" + re.escape(lead) + val + "".join(re.escape(sp) + val for sp in seps) +
                            re.escape(tail) + "$")
        loose_sep = lambda sp: r"\s*" + re.escape(sp.strip()) + r"\s*"
        loose = re.compile("^" + loose_sep(lead) + "(.+?)" + "".join(loose_sep(sp) + "(.+?)" for sp in seps) +
                           loose_sep(tail) + "$")
        near = [ln for ln in lines if loose.match(ln)]
        off = [ln for ln in near if not strict.match(ln)]
        if near and len(near) * 2 >= len(lines) and off:
            return (f"the task gives each line the form `{form}` (spaces exactly as there), but {len(off)} of "
                    f"{len(near)} lines differ from it in spacing, e.g. {off[0]!r}. Write every line in exactly "
                    "that form, then finish with checks again.")
    return ""

class Agent:
    def __init__(self, cfg, approval_handler=None, backend=None, label="", log=None, history=(), workspace=None):
        self.cfg = cfg
        self.workspace = os.path.realpath(workspace) if workspace else None   # the user's folder: a git repository
        self.history = list(history)                       # finished earlier tasks of this conversation (run_end events)
        # in a git folder the run logs live with its journal in .mini/; otherwise in the global runs dir
        self.memory = bool(self.workspace) and ckpt.is_repo(self.workspace)
        self.log = log or RunLog(journal.runs_dir(self.workspace) if self.memory else cfg.abspath("runs"), label=label)
        if self.memory:
            journal.ensure_excluded(self.workspace)
            self.log.subscribe(JournalSink(self.workspace, self.log))   # the journal is a sink of the stream
        self.run_dir = self.log.dir
        # without a folder (tests, internal runs) the agent works in an empty folder of the run, with no checkpoint
        self.folder = self.workspace or os.path.join(self.run_dir, "files")
        os.makedirs(self.folder, exist_ok=True)
        self.attachments, self.changed, self.checkpoint, self.calls = [], [], None, 0
        self.core = load_catalog()                         # core capabilities (mini/core): the agent's own actions
        self.last_invoke, self.last_script, self.web_hosts = {}, None, set()
        self.invoked_ok = set()                  # tools that ran without an error in this task
        self.task_inputs = set()                 # relative paths the task reads and does not change (guard_inputs)
        self.built_names, self.need_reminded = [], False
        self.invoked_any = set()                 # tools invoke_tool actually ran in this task (ok or not)
        self.kept_jobs, self.kept_told = {}, {}  # tools kept from a script in this task: what that script did
        self.task_text = ""
        self._use_model(backend)                           # tests inject a scripted model
        self.registry = Registry(cfg.abspath("state"), run_id=self.log.run_id, auto_accept=cfg["tools"]["auto_accept"])
        self.registry.seed_bundled()                       # bundled document tools: installed on first run
        self.settings = SettingsStore(cfg.abspath("state"))
        self.lessons = LessonStore(cfg.abspath("state"))
        self.sandbox = Sandbox(cfg, self.log)
        self.wd = Watchdogs(cfg, self.log)
        self.gate = ApprovalGate(cfg, self.log, self._unclocked(approval_handler) if approval_handler else None)
        self.stop_event = threading.Event()
        self.abort_reason = None
        self.tokens = 0
        self.created, self.repaired = [], 0
        self.run_t0 = time.time()

    # ------------------------------------------------------------ the model
    def _use_model(self, backend=None):
        lc = self.cfg["local"]
        self.model = lc["model"]
        self.backend = backend or make_backend(Endpoint(
            lc["backend"], lc["url"], options={},
            max_tokens=self.cfg["caps"]["reply_max_tokens"], timeout_s=self.cfg["caps"]["reply_max_s"],
            think_max_chars=self.cfg["caps"].get("reply_think_max_chars", 0),
            think_budget_tokens=self.cfg["caps"].get("reply_think_budget_tokens", 0),
            max_tokens_tools=self.cfg["caps"].get("reply_max_tokens_tools", 0),
            comment_cut_chars=self.cfg["caps"].get("reply_comment_cut_chars", 0)))
        self.model_options = {"temperature": lc["temperature"]}
        # Set these to sample every model the same way (a GGUF may carry its own top_k/top_p defaults) and, with a
        # seed, to make a run repeatable.
        self.model_options.update({k: lc[k] for k in SAMPLING_KEYS if lc.get(k) is not None})

    # ------------------------------------------------------------ control
    def abort(self, reason):
        self.abort_reason = reason
        self.stop_event.set()

    def check_abort(self):
        if self.abort_reason:
            raise AgentStop(self.abort_reason)

    def wd_elapsed(self):
        return time.time() - self.wd.task_t0

    def context(self):
        facts = [f"Today is {time.strftime('%A %Y-%m-%d')}, local time {time.strftime('%H:%M %Z')}."]
        facts += list(self.cfg["environment"]["facts"])
        out = "Facts about the user's environment:\n" + "\n".join(f"- {f}" for f in facts)
        if self.workspace:
            out += (f"\n\nYou work in the user's folder {self.workspace}. Every path is relative to it. The core "
                    f"capabilities work on it directly and change files in place. A tool gets "
                    f"relative paths in its inputs: the files a call names are copied into the tool's sandbox, and "
                    f"every file the tool writes lands at the same relative path in the folder. Put new files where "
                    f"the task says. mini checkpoints the folder in git before the first change, so the user can undo "
                    f"the task. \"The current folder\", \"this directory\" or \"here\" in a task means this folder (path '.').")
        past = journal.summary_text(self.workspace) if self.memory else ""
        if past:
            out += "\n\n" + past
        if self.attachments:
            out += ("\n\nFiles attached to the task (read_file and tool inputs accept these paths):\n"
                    + "\n".join(f"- {a}" for a in self.attachments))
        if self.history:
            out += ("\n\nEarlier in this conversation (oldest first). The new task may refer to it: \"retry\", "
                    "\"again\", \"that file\", \"why did it fail\".\n" + "\n".join(self._past(h) for h in self.history[-6:]))
        return out

    @staticmethod
    def _past(h):
        cut = lambda v, n: (str(v) if v else "").replace("\n", " ")[:n]
        if h.get("via") == "slash":                        # the user ran a tool directly, from a slash command
            line = f"- the user ran the tool {cut(h.get('task'), 300)} themselves (not you) -> {h.get('status')}"
            if h.get("error"):
                line += f"; error: {cut(h['error'], 300)}"
            if h.get("answer"):
                line += f"; its result: {cut(h['answer'], 600)}"
            if h.get("outputs"):
                line += f"; files written: {cut(h['outputs'], 150)}"
            return line
        line = f"- the user asked: {cut(h.get('task'), 300)} -> {h.get('status')}"
        if h.get("error"):
            line += f"; error: {cut(h['error'], 300)}"
        if h.get("answer"):
            line += f"; you answered: {cut(h['answer'], 600)}"
        if h.get("created"):
            line += f"; tools built: {cut(h['created'], 150)}"
        if h.get("outputs"):
            line += f"; files produced: {cut(h['outputs'], 150)}"
        return line

    def list_work(self):
        return ws.tree(self.folder, ".", 1)

    # ------------------------------------------------------------ run
    def run_task(self, task, attachments=()):
        t0 = self.run_t0 = time.time()
        status, answer, error = "failed", "", ""
        self.task = task
        self.attachments = [os.path.realpath(a) for a in attachments]
        self.log.emit("run_start", task=task, attachments=[os.path.basename(a) for a in attachments],
                      model=self.model, preset=self.cfg["llamacpp"].get("preset") or None,
                      workspace=self.workspace, caps=self.cfg["caps"], watchdogs=self.cfg["watchdogs"], confirm_first=self.cfg["approvals"]["confirm_first"])
        tl = time.time()
        try:
            self.backend.load(self.model, self.model_options)
        except OSError as e:
            self.log.emit("error", text=f"the model server is not reachable at {self.cfg['local']['url']}: {e}")
        self.log.emit("info", text=f"model {self.model} loaded in {time.time() - tl:.1f} s; swap baseline taken after loading",
                      swap_used_mib=round(hwmon.swap_used_mib()))
        swap = hwmon.SwapWatch(self.cfg["watchdogs"]["swap_growth_mib"], self._swap_trip)
        if self.cfg["watchdogs"]["swap_watch"]:
            swap.begin()
        else:
            self.log.emit("info", text="swap watchdog is off ([watchdogs] swap_watch); swap growth is only recorded")
        self.wd.reset_task()
        self.task_text = task
        try:
            understanding = self.parse(task)
            self.task_inputs = self._input_files(understanding)
            covered = self.gap(understanding)
            answer = self.execute(task, understanding, covered)
            status = "success"
        except (AgentStop, ToolFailed, PolicyError) as e:
            error = str(e)
            self.log.emit("error", text=error, kind_=type(e).__name__)
            status = "stopped" if isinstance(e, AgentStop) else "failed"
        except Exception as e:   # noqa: BLE001 - a harness bug must still end the run with a log entry
            import traceback
            error = f"{type(e).__name__}: {e}"
            self.log.emit("error", text=traceback.format_exc())
            status = "crashed"
        finally:
            swap.stop()
        summary = {"run_id": self.log.run_id, "task": task, "status": status, "answer": answer, "error": error,
                   "secs": round(time.time() - t0, 1), "tokens": self.tokens, "created": self.created,
                   "repairs": self.repaired, "model": self.model,
                   "watchdogs": self._watchdog_counts(), "outputs": self.produced_files(),
                   "workspace": self.workspace, "checkpoint": self.checkpoint.ref if self.checkpoint else None, "swap_growth_mib": round(swap.growth(), 1), "registry_after": self.registry.snapshot()}
        json.dump(summary, open(os.path.join(self.run_dir, "summary.json"), "w"), indent=2)
        self.log.emit("run_end", **{k: v for k, v in summary.items() if k != "registry_after"})
        return summary

    def _swap_trip(self, growth):
        self.log.emit("watchdog", name="hardware_swap", growth_mib=growth, limit=self.cfg["watchdogs"]["swap_growth_mib"],
                      action="abort")
        self.abort(f"swap grew by {growth} MiB (limit {self.cfg['watchdogs']['swap_growth_mib']}); run aborted")

    def _watchdog_counts(self):
        counts = {}
        with open(os.path.join(self.run_dir, "log.jsonl")) as f:
            for line in f:
                ev = json.loads(line)
                if ev["kind"] == "watchdog":
                    counts[ev["name"]] = counts.get(ev["name"], 0) + 1
                elif ev["kind"] == "phase" and ev.get("phase") == "restart":
                    counts["restarts"] = counts.get("restarts", 0) + 1
        return counts

    # ------------------------------------------------------------ 1-2 parse and confirm
    def parse(self, task):
        self.log.emit("phase", phase="parse")
        ctx = self.context()
        files = self.list_work()
        msgs = [{"role": "system", "content": prompts.SYSTEM},
                {"role": "user", "content": prompts.PARSE.format(task=task, context=ctx + "\n\nThe folder:\n" + files)}]
        t = Turns(self, "parse", msgs, [T_UNDERSTAND])
        for _ in range(4):
            reply = t.ask()
            call = reply["tool_calls"][0]
            for extra in reply["tool_calls"][1:]:
                t.result(extra["name"], "ignored")
            if call["name"] != "understand" or not call["arguments"].get("goal"):
                t.result(call["name"], "Call understand with goal, inputs, outputs and plan.")
                continue
            u = normalize_understanding(call["arguments"])
            self.log.emit("phase", phase="understood", understanding=u)
            if not self.cfg["approvals"]["confirm_first"]:
                t.result("understand", "ok")
                return u
            d = self.gate.request("confirm", "Confirm the plan", {
                **({"folder": self.workspace} if self.workspace else {}),
                "goal": u.get("goal"), "inputs": u.get("inputs"), "outputs": u.get("outputs"),
                "side effects": u.get("side_effects") or "-", "ambiguities": u.get("ambiguities") or "-",
                "plan": u.get("plan")}, options=("proceed", "clarify", "cancel"))
            if d.choice == "cancel" or not d.approved:
                raise AgentStop("cancelled by the user at confirm-first")
            if d.choice == "proceed":
                t.result("understand", "The user confirmed." + (f" Note: {d.note}" if d.note else ""))
                if d.note:
                    u["user_note"] = d.note
                return u
            t.result("understand", f"The user clarified: {d.note}. Call understand again with the corrected understanding.")
        raise AgentStop("no confirmed understanding")

    # ------------------------------------------------------------ 3 gap detection
    def gap(self, understanding):
        """Which installed tools this task uses, decided one tool at a time: the registry's best matches for the task
        are each judged in a fresh conversation (fits a step, or not); when several fit, one more call picks among
        them. Then one call decides what is missing (only what a script cannot try: network, a dialogue)."""
        self.log.emit("phase", phase="gap_detection")
        snap = self.registry.snapshot()
        self.log.emit("registry_state", when="before_run", tools=snap, count=len(snap))
        u = understanding
        query = " ".join([self.task_text, u.get("goal", ""), " ".join(u.get("outputs") or []),
                          " ".join(u.get("side_effects") or []), u.get("plan", "")])
        g = self.cfg.get("gap") or {}
        candidates = [m for score, m in self.registry.scored(query, limit=g.get("candidates", GAP_CANDIDATES))
                      if score >= g.get("min_score", 2)] if g.get("judge", True) else []
        if len(candidates) > 1:
            candidates = self._screen(candidates)
        fits = []
        for m in candidates:
            v = self._judge(u, m)
            self.log.emit("gap_judge", tool=m["name"], fits=v["fits"], step=v.get("step"), why=v.get("why"))
            if v["fits"]:
                fits.append((m, v))
        chosen = [m["name"] for m, _ in fits] if g.get("judge", True) else \
            [m["name"] for m in self.registry.search(query, limit=15)]
        if len(fits) > 1:
            chosen = self._choose(fits) or chosen
        matches = ", ".join(c.name for c in self.core.search(query)) or "(none)"     # the catalog is searched first
        msgs = [{"role": "system", "content": prompts.SYSTEM},
                {"role": "user", "content": prompts.GAP.format(
                    understanding=json.dumps(u, indent=1), core=self.core.listing(), matches=matches,
                    chosen=", ".join(chosen) or "(none: no installed tool fits)")}]
        t = Turns(self, "gap", msgs, [T_COVERAGE])
        for _ in range(4):
            reply = t.ask()
            call = reply["tool_calls"][0]
            args = call["arguments"]
            if call["name"] != "decide_coverage":
                t.result(call["name"], "Call decide_coverage.")
                continue
            use = [x for x in (args.get("use") or []) if isinstance(x, str) and x not in self.core]
            unknown = [x for x in use if x not in self.registry.names()]
            if unknown:
                t.result("decide_coverage", f"Not installed: {', '.join(unknown)}. Only use installed tool names; "
                                            "list anything else under missing.")
                continue
            use = list(dict.fromkeys(chosen + use))       # a tool judged to fit stays offered
            missing = [x if isinstance(x, str) else json.dumps(x) for x in (args.get("missing") or [])]
            core = {x: self.core.covering(x) for x in missing}
            core = {x: c for x, c in core.items() if c}         # a need core already covers is never built
            missing = [x for x in missing if x not in core]
            self.log.emit("gap", reasoning=args.get("reasoning"), use=use, missing=missing, core=core or None,
                          covered=not missing, registry_count=len(snap), judged=[m["name"] for m in candidates],
                          fits=[m["name"] for m, _ in fits])
            self.gap_decision = {"use": use, "missing": missing}
            return {"use": use, "missing": missing}
        raise AgentStop("no coverage decision")

    def _screen(self, candidates):
        """One call over the candidates' names and descriptions: only the ones it names are judged one by one."""
        listing = "\n".join(f"- {m['name']}: {(m.get('description') or '').strip()[:200]}" for m in candidates)
        msgs = [{"role": "system", "content": prompts.SYSTEM},
                {"role": "user", "content": prompts.GAP_SCREEN.format(task=trim(self.task_text, 1500), tools=listing)}]
        t = Turns(self, "gap", msgs, [T_SCREEN], think_tokens=GAP_THINK_TOKENS)
        names = {m["name"] for m in candidates}
        for _ in range(3):
            reply = t.ask()
            c = next((c for c in reply["tool_calls"] if c["name"] == "shortlist"), None)
            got = ((c or {}).get("arguments") or {}).get("names")
            if isinstance(got, list):
                keep = [m for m in candidates if m["name"] in got]
                self.log.emit("gap_screen", among=sorted(names), shortlist=[m["name"] for m in keep])
                return keep
            t.user(f"Call shortlist with names: a list of names from {', '.join(sorted(names))} (or an empty list).")
        return candidates

    def _judge(self, u, m, need=""):
        """Does installed tool m do a step of the task (u: the understanding), or, with need, that one request?"""
        ask = (prompts.GAP_NEED.format(need=trim(need, 1500), tool=describe_tool(m)) if need else
               prompts.GAP_ONE.format(task=trim(self.task_text, 1500), goal=u.get("goal", ""), plan=u.get("plan", ""),
                                      tool=describe_tool(m)))
        msgs = [{"role": "system", "content": prompts.SYSTEM}, {"role": "user", "content": ask}]
        t = Turns(self, "gap", msgs, [T_JUDGE], think_tokens=GAP_THINK_TOKENS)
        for _ in range(3):
            reply = t.ask()
            c = next((c for c in reply["tool_calls"] if c["name"] == "judge_tool"), None)
            if c and isinstance(c.get("arguments"), dict) and isinstance(c["arguments"].get("fits"), (bool, str)):
                v = dict(c["arguments"])
                v["fits"] = v["fits"] is True or str(v["fits"]).strip().lower() in ("true", "yes")
                return v
            t.user("Call judge_tool with fits true or false.")
        return {"fits": False, "why": "no judgement"}

    def _choose(self, fits):
        listing = "\n".join(f"{describe_tool(m)}\n  step it fits: {v.get('step') or '?'}" for m, v in fits)
        msgs = [{"role": "system", "content": prompts.SYSTEM},
                {"role": "user", "content": prompts.GAP_PICK.format(task=trim(self.task_text, 1500), fits=listing)}]
        t = Turns(self, "gap", msgs, [T_CHOOSE], think_tokens=GAP_THINK_TOKENS * 2)
        names = {m["name"] for m, _ in fits}
        for _ in range(3):
            reply = t.ask()
            c = next((c for c in reply["tool_calls"] if c["name"] == "choose_tools"), None)
            use = [x for x in ((c or {}).get("arguments") or {}).get("use") or [] if x in names]
            if use:
                self.log.emit("gap_choose", use=use, among=sorted(names), why=(c["arguments"].get("why") or "")[:300])
                return use
            t.user(f"Call choose_tools with use: names from {', '.join(sorted(names))}.")
        return None

    # ------------------------------------------------------------ 4-6 create, then execute
    def execute(self, task, understanding, covered):
        creator = Creator(self)
        ctx = self.context()
        tools = list(covered["use"])
        built = []
        for need in covered["missing"]:
            m = creator.create(need, task, ctx)
            tools.append(m["name"])
            built.append(m["name"])
        self.wd.reset_task()
        self.built_names, self.need_reminded = list(built), False
        self.invoked_any, self.keep_reminded = set(), False
        self.kept_jobs, self.kept_told, self.judge_seen = {}, {}, {}
        self.log.emit("phase", phase="execute", tools=tools)
        listing = lambda: "\n".join(describe_tool(self.registry.get(n)) for n in dict.fromkeys(tools)) or "(none)"
        msgs = [{"role": "system", "content": prompts.SYSTEM},
                {"role": "user", "content": prompts.EXECUTE.format(task=task, understanding=json.dumps(understanding, indent=1),
                                                                   context=ctx, tools=listing(), core=self.core.listing(),
                                                                   files=self.list_work(),
                                                                   built=prompts.BUILT.format(names=", ".join(built)) if built else "")}]
        finish_rejections, tool_rejections, reviews = 0, 0, 0
        offered = self.core.tools() + CONTROL_TOOLS
        while True:
            t = Turns(self, "execute", msgs, offered, implicit=self._implicit_finish)
            try:
                reply = t.ask()
            except RestartRequested:
                raise AgentStop("stopped at the time/turn limit during execution")
            content = reply.get("content") or ""
            for call in reply["tool_calls"]:
                name, args = call["name"], call["arguments"] or {}
                guard = self.wd.on_call(name, args)
                if guard == "block":
                    if self.wd.call_streak >= 3 * self.wd.w["identical_call_repeat"]:
                        raise AgentStop(f"stuck repeating {name} with the same arguments")
                    t.result(name, f"BLOCKED: this exact call was made {self.wd.call_streak} times in a row, and it gave: "
                                   f"{trim(self.wd.last_text, 300)}\nChange the arguments or use a different tool.")
                    continue
                note = "\nWarning: same call as before. A third identical call in a row will be blocked." if guard == "warn" else ""
                self.log.emit("tool_call", step="execute", name=name, args=args)
                if name == "finish":
                    problem = self._check_finish(args, understanding, finish_rejections, tool_rejections)
                    if problem:
                        if self.finish_kind == "tools":
                            tool_rejections += 1
                        else:
                            finish_rejections += 1
                        self.log.emit("watchdog", name="finish_check", problem=problem, check=self.finish_kind,
                                      rejections=finish_rejections + tool_rejections)
                        t.result(name, "Not finished: " + problem)
                        continue
                    review = self._review_outputs(task, args.get("checks"), reviews)
                    if review:
                        reviews += 1
                        self.log.emit("watchdog", name="finish_review", round=reviews, checks=args.get("checks"),
                                      text=trim(review, 3000))
                        t.result(name, "Not finished: " + review)
                        continue
                    answer = args.get("answer", "")
                    self.log.emit("phase", phase="done", answer=answer, outputs=self.produced_files())
                    return answer
                if name in self.core:
                    text, answered = self.act(name, args, content, msgs)
                    if answered:                       # the handler already replied in the conversation (a text turn)
                        self.log.emit("tool_result", step="execute", name=name, text=trim(text, 3000))
                        t.user(text + note)
                        continue
                    if name == "delete_file" and reviews:          # a stray file removed at the end may have been read
                        made = [r for r in self.produced_files() if os.path.isfile(os.path.join(self.folder, r))]
                        if made:
                            text += ("\nFiles this task wrote before: " + ", ".join(made[:8]) + ". If a step that made "
                                     "one of them read the deleted file, run that step again so they no longer "
                                     "include it.")
                elif name == "keep_tool" or (name == "need_tool" and args.get("from_run_code")
                                             and (self.last_script or {}).get("record")):
                    text = self._keep(creator, args.get("what") or args.get("need") or "", task, ctx, tools)
                elif name == "need_tool":
                    text = self._need(creator, args, task, ctx, tools)
                elif name == "report_tool_bug":
                    text = self._repair(creator, args, task, ctx)
                else:
                    text = "Unknown tool. Use: " + ", ".join(x["function"]["name"] for x in offered)
                self.log.emit("tool_result", step="execute", name=name, text=trim(text, 3000))
                t.result(name, text + note)

    @staticmethod
    def _implicit_finish(reply):
        """A substantial text-only reply is the answer: the model wrote it out instead of calling finish."""
        text = (reply.get("content") or "").strip()
        if len(text) >= MIN_IMPLICIT_ANSWER and not text.endswith(":"):
            return {"name": "finish", "arguments": {"answer": text}}
        return None

    def act(self, name, args, content="", messages=None):
        """Run a core capability: the arguments are validated against its schema first. Returns (result text,
        whether the handler already answered in the conversation)."""
        call = Call(self, content, messages)
        bid = self.log.start("tool", name=name, args=args)        # the sandbox runs and questions inside nest in it
        try:
            text = self.core.invoke(call, name, args)
        except BaseException as e:
            self.log.end(bid, "error", text=f"{type(e).__name__}: {e}")
            raise
        self.log.end(bid, "ok", text=trim(text, 3000))
        return text, call.answered

    def _need(self, creator, args, task, ctx, tools):
        need = str(args.get("need") or "")
        hit = self.core.covering(need)
        if hit:
            self.log.emit("watchdog", name="core_covers_need", need=need, capability=hit)
            return f"Not built: the core capability {hit} already does this. Call {hit} directly."
        unrun = [n for n in self.built_names if n not in self.invoked_any and self._same_need(n, need)]
        if unrun:                                   # every time: build, then run; never build the same thing again
            self.log.emit("watchdog", name="built_tool_unrun", built=unrun, need=need[:200])
            return (f"Not built: {', '.join(unrun)} was built and installed for this task and has not run yet. Call "
                    "`invoke_tool` with it first. If it fails on the real data, call `report_tool_bug`: it is repaired "
                    "as a new version instead of built again.")
        if any(self._same_need(n, need) for n in self.built_names) and not self.need_reminded:
            self.need_reminded = True               # once: a tool built for this task is usually the answer
            self.log.emit("watchdog", name="built_tool_unused", built=self.built_names)
            return (f"Not built yet: {', '.join(self.built_names)} was built and installed for this task. Call "
                    "`invoke_tool` with it. If it truly cannot do what the task needs, call need_tool again.")
        told = self.__dict__.setdefault("need_installed", set())
        judged = bool(tools) and (self.cfg.get("gap") or {}).get("judge", True)   # off: offered = a plain search
        for name in [n for n in dict.fromkeys(tools) if judged and n not in self.built_names and n not in told]:
            m = self.registry.get(name)              # an installed tool offered for this task: reuse it as it is
            if not m:
                continue
            v = self._judge({}, m, need=need)        # the request alone: the whole task fits any tool somewhere
            self.log.emit("gap_judge", tool=name, fits=v["fits"], step=v.get("step"), why=v.get("why"), need=need[:200])
            if v["fits"]:
                told.add(name)
                return (f"Not built: {name} is installed and does this ({trim(str(v.get('why') or ''), 200)}). Call "
                        f"`invoke_tool` with it (inspect_tool shows its inputs). If it truly cannot do this step, call "
                        f"need_tool again and say what {name} lacks.")
        s = self.last_script
        if args.get("from_run_code") and s and s.get("ok") and s.get("script"):
            need += f"\n\nA script that already did this once in the sandbox (start from it):\n```python\n{s['script']}```"
        try:
            m = creator.create(need, task, ctx)
            tools.append(m["name"])
            self.built_names.append(m["name"])
            return "New tool installed:\n" + describe_tool(m) + f"\nRun it now with `invoke_tool` (name {m['name']!r})."
        except (ToolFailed, ModelGaveUp) as e:    # mid-task, a build that fails costs the tool, not the task
            self.log.emit("watchdog", name="need_failed", problem=str(e))
            return (f"Not built: {e}. Do this step with run_code instead (a script that worked can be kept with "
                    "keep_tool), and go on with the task.")
        finally:
            self.wd.reset_conversation()

    def _kept_elsewhere(self, what, s):
        """The files keep_tool's description names that this task made, but not in the last script's run, when nothing
        the last script wrote is named in it: the description is of an earlier script. [] when it may fit."""
        written = [w.replace("\\", "/") for w in s.get("written") or []]
        if not written:
            return []
        words = [w for w in re.findall(r"[a-z0-9]+", what.lower()) if len(w) >= 3]
        def said(token):                        # thumbs ~ thumbnails, zip ~ zips, bundle ~ bundles
            return any(w[:n] == token[:n] for w in words for n in [min(len(w), len(token), 5)])
        tokens = {t for p in written for t in re.split(r"[^a-z0-9]+", p.lower()) if len(t) >= 3}
        if any(said(t) for t in tokens):
            return []
        named = re.findall(r"[\w.-]+/|[\w-]+\.[A-Za-z0-9]{2,4}\b", what)
        made = [n for n in dict.fromkeys(named)
                if any(c == n or c.startswith(n if n.endswith("/") else n + "/") for c in self.changed)
                and not any(w == n or w.startswith(n) for w in written)]
        return made

    def _keep(self, creator, what, task, ctx, tools):
        """keep_tool: the last successful script becomes a tool, tested on its own run; then the installed tool runs
        once on the user's files with the example inputs (it writes what the script wrote, so nothing changes)."""
        s = self.last_script or {}
        if not (s.get("ok") and s.get("script") and s.get("record")):
            return ("Nothing to keep: keep_tool keeps the last run_code run, and it must be a Python script (script, "
                    "`python file.py` or `python -c code`) that ran without an error. Run the script that does this "
                    "step with run_code again, then call keep_tool.")
        if argv_idle(s.get("script"), s.get("output"), s.get("written")):
            self.log.emit("watchdog", name="keep_argv_idle", what=what[:200])
            return "Nothing to keep: " + ARGV_IDLE
        other = self._kept_elsewhere(what, s)
        if other and not s.get("mismatch_told"):
            s["mismatch_told"] = True              # once per run: the description may still be right
            self.log.emit("watchdog", name="keep_mismatch", what=what[:200], wrote=(s.get("written") or [])[:10])
            return (f"Not kept: keep_tool keeps the last run_code script, and it wrote "
                    f"{', '.join((s.get('written') or [])[:5])}, while you describe a tool that makes "
                    f"{', '.join(other)}, which an earlier script made. Run the script that makes those with run_code "
                    "again (it writes the same files), then call keep_tool right after it. If the last script is the "
                    "one to keep, call keep_tool again with what it does.")
        same = [n for n in self.built_names if self._same_need(n, what)]
        if same and not getattr(self, "keep_reminded", False):
            self.keep_reminded = True               # once: a tool made for this task already does it
            self.log.emit("watchdog", name="keep_duplicate", tool=same[0], what=what[:200])
            return (f"Not kept: {same[0]} was made for this task and already does this. Run it with `invoke_tool` "
                    f"(name {same[0]!r}); if it cannot do this step as it is, call `report_tool_bug` and it is upgraded "
                    f"instead of kept twice. If this script does something else, call keep_tool again.")
        try:
            m = creator.keep(s, what, task, ctx)
        except (ToolFailed, ModelGaveUp) as e:    # a keep that fails costs the tool, not the task: the script ran
            self.log.emit("watchdog", name="keep_failed", problem=str(e))
            return f"Not kept: {e}. The script already did the work; go on with the task."
        finally:
            self.wd.reset_conversation()
        self.last_script = None
        tools.append(m["name"])
        self.built_names.append(m["name"])
        self.kept_jobs[m["name"]] = job_of(s.get("script"), s.get("written"))
        text, _ = self.act("invoke_tool", {"name": m["name"], "inputs": dict(getattr(creator, "example", {}) or {})})
        return (f"Kept as the tool {m['name']} v{m['version']}: its test is the script's run, and it just ran once on "
                f"your files with the example inputs:\n{trim(text, 800)}\n" + describe_tool(m))

    def _same_need(self, tool, need):
        """need asks again for what tool, built in this task, does: it names the tool, or half or more of the
        tool's own content words (name and description) are in it. A different capability is built at once."""
        m = self.registry.get(tool) or {}
        if tool in need:
            return True
        words = _content_words(tool.replace("_", " ") + " " + str(m.get("description") or ""))
        return bool(words) and len(words & _content_words(need)) * 2 >= len(words)

    def _path(self, p):
        return ws.confine(self.folder, p)[0]

    def _readable(self, p):
        """A path read_file may read: in the folder, an attachment, or a file this run saved (a long tool result)."""
        full = os.path.realpath(os.path.expanduser(str(p or "")))
        if os.path.isabs(str(p)) and (full in self.attachments or full.startswith(os.path.realpath(self.run_dir) + os.sep)):
            return full
        return self._path(p)

    def ensure_checkpoint(self, rels=()):
        """Checkpoint the folder (once per task, and any file of rels not yet in the checkpoint). PolicyError when
        the folder is not a git repository or the checkpoint fails. Nothing to do without a folder."""
        if not self.workspace:
            return
        try:
            if self.checkpoint is None:
                if not ckpt.is_repo(self.workspace):
                    raise PolicyError(ckpt.guard_note(self.workspace))
                self.checkpoint = ckpt.Checkpoint(self.workspace, self.log.run_id, getattr(self, "task", ""))
                self.checkpoint.before_change(rels)
                self.log.emit("checkpoint", ref=self.checkpoint.ref, commit=self.checkpoint.before)
            else:
                self.checkpoint.before_change(rels)
        except ckpt.GitError as e:
            raise PolicyError(f"no checkpoint, so nothing was changed: {e}")

    def _excuse(self, secs):
        """Time spent waiting for the user or for another session does not count against the time caps."""
        self.wd.task_t0 += secs
        self.run_t0 += secs

    def _unclocked(self, handler):
        """The approval handler, with the time the user takes to answer taken off the clocks."""
        def wrapped(*a, **kw):
            t = time.time()
            try:
                return handler(*a, **kw)
            finally:
                self._excuse(time.time() - t)
        return wrapped

    @contextlib.contextmanager
    def _waiting_for(self, gate, **kw):
        t = time.time()
        with gate.hold(stop_event=self.stop_event, **kw):
            self._excuse(time.time() - t)                   # the queue, not the work
            yield

    def _input_files(self, u):
        """Files the understanding names as inputs (a folder stands for the files in it), less those it also names
        as outputs: the task reads them, and the model's file capabilities may not change them."""
        def files(items):
            out = set()
            for x in items:
                p = str(x).strip().strip("`'\"").rstrip("/")
                if p and not os.path.exists(os.path.join(self.folder, p)):     # "feeds/ (the three feeds)"
                    p = p.split()[0].strip("`'\",:;()").rstrip("/")
                full = os.path.normpath(os.path.join(self.folder, p))
                if not p or not full.startswith(os.path.normpath(self.folder) + os.sep):
                    continue
                if os.path.isfile(full):
                    out.add(os.path.relpath(full, self.folder))
                elif os.path.isdir(full):
                    for d, _, fs in os.walk(full):
                        out |= {os.path.relpath(os.path.join(d, f), self.folder) for f in fs}
            return out
        if not self.folder or not any(PATH_LIKE.match(str(x).strip().strip("`'\"").split(" ")[0].rstrip(",:;"))
                                      for x in u.get("outputs") or []):
            return set()                # no output file named: the task changes its files in place (rename, fix)
        return files(u.get("inputs") or []) - files(u.get("outputs") or [])

    def guard_inputs(self, rels):
        """PolicyError when a model file edit would change an input of the task: the data stays as the user gave it,
        and a tool that cannot read it is a tool bug."""
        hit = sorted(r for r in rels if os.path.normpath(r) in self.task_inputs)
        if hit:
            self.log.emit("watchdog", name="input_protected", paths=hit)
            raise PolicyError(f"{', '.join(hit)} is an input of this task and stays as the user gave it. If a tool "
                              f"cannot read it as it is, call report_tool_bug so that tool is fixed for this data")

    def _change(self, rels, write):
        """Checkpoint (ensure_checkpoint), then write(), which returns the relative paths written, then record them
        in the checkpoint. PolicyError, before anything is written, when there is no checkpoint. Changes to the
        folder happen one at a time across sessions (gates.FOLDER)."""
        try:
            with self._waiting_for(gates.FOLDER, on_wait=lambda: self.log.emit("gate", gate="folder", state="waiting")):
                self.ensure_checkpoint(rels)
                done = write()
                if self.checkpoint:
                    self.checkpoint.after_change(done)
        except gates.Stopped:
            raise AgentStop("stopped while waiting to change the folder")
        self.changed += [r for r in done if r not in self.changed]
        self.log.emit("files_changed", paths=done)
        return done

    def hold_model(self):
        """The local model serves one reply at a time across sessions (gates.LOCAL)."""
        return self._waiting_for(gates.LOCAL, on_wait=lambda: self.log.emit("gate", gate="local", state="waiting"))

    def context_tokens(self):
        """The model's context size (llama-server's ctx), or None when it is not known (scripted test models)."""
        if self.cfg["local"]["backend"] == "fake":
            return None
        return int(llama_settings(self.cfg).get("ctx") or 0) or None

    def produced_files(self):
        """The files this task wrote or changed in the folder, relative to it."""
        return sorted(self.changed)

    def _check_finish(self, args, understanding, rejections, tool_rejections=0):
        """Harness-side check of a finish call. Returns a problem or "". The check that each tool the task names has
        run keeps its own budget: after 2 rejections of each kind the agent may finish. self.finish_kind says which
        kind the problem is ("general" or "tools")."""
        self.finish_kind = "general"
        problem = "" if rejections >= 2 else self._finish_problem(args, understanding)
        if problem or tool_rejections >= 2:
            return problem
        self.finish_kind = "tools"
        wanted = named_tools(self.task_text)
        worked = sorted(self.invoked_ok)
        if len(wanted) > len(worked):               # "one that makes X and one that reads Y": each one is a tool
            asked = "; ".join(f"one that {w}" for w in wanted if w) or f"{len(wanted)} tools"
            return (f"the task asks for {len(wanted)} tools ({asked}), and {len(worked)} ran successfully"
                    + (f" ({', '.join(worked)})" if worked else "") + ". For each one missing, call `keep_tool` on a "
                    "run_code script that does only that step (one script per tool), or run an installed tool that "
                    "does it with invoke_tool.")
        return ""

    def _finish_problem(self, args, understanding):
        """The general finish checks: named files exist, built tools ran, a tool was made when asked, a promised file
        was written."""
        produced = self.produced_files()
        for f in args.get("files") or []:
            try:
                full = self._path(f)
            except PolicyError as e:
                return str(e)
            if os.path.isdir(full):                 # a folder of outputs (thumbs/) is fine once it holds a file
                if any(fs for _, _, fs in os.walk(full)):
                    continue
                return f"{f} is an empty folder. Write its files first, or leave it out of files."
            if not os.path.isfile(full) or os.path.getsize(full) == 0:
                return f"{f} does not exist or is empty. Write it first. Files present: {', '.join(produced) or '(none)'}"
        built = list(dict.fromkeys(c["name"] for c in self.created))
        ran = [n for n in built if n in self.invoked_ok]
        about = lambda n: n.replace("_", " ") + " " + str((self.registry.get(n) or {}).get("description") or "")
        # each tool built for the task runs, unless a tool that ran does the same job (a rebuilt copy replaces one)
        unused = [n for n in built if n not in ran and not any(self._same_need(n, about(r)) for r in ran)]
        if unused:
            return (f"{' / '.join(unused)} was built for this task but never ran successfully. Call invoke_tool with it "
                    "to produce the result: output made another way (run_code, write_file) does not show the tool works. "
                    "If it cannot be used, say why in your answer.")
        if ASKS_FOR_TOOL.search(self.task_text or "") and not (built or self.built_names or self.invoked_any):
            return ("the task asks for a tool, and none was made or used. Call `keep_tool` on the run_code script that "
                    "did the work (it becomes a tool tested on that run), or run an installed tool with invoke_tool.")
        promised = " ".join(str(x) for x in (understanding.get("outputs") or []) + (understanding.get("side_effects") or []))
        # a promised deliverable: "save/write/create ... file", or a file name with an extension. "A list of files in
        # this folder" is an answer, not a file.
        if re.search(r"\b(save|write|written|create|produce|export|generate)\w*\b.*\bfiles?\b|\b\w+\.[a-z]{2,4}\b",
                     promised.lower()) and not produced:
            return ("your plan produces a file, but no file was written in the folder. Use write_file "
                    "(or a tool) to create it, then finish with its path in files.")
        return ""

    @staticmethod
    def _units(files):
        """The written files as units to check: files in one subfolder with one extension are a set (qr/*.png), checked
        once through any of them; any other file is its own unit. [(label, members)]"""
        groups = {}
        for f in files:
            d, ext = os.path.dirname(f), os.path.splitext(f)[1]
            groups.setdefault((d, ext) if d else (f, None), []).append(f)
        return [(f"{d}/*{ext}" if ext is not None and len(m) > 1 else m[0], m) for (d, ext), m in groups.items()]

    def _judge_key(self, f):
        try:
            return f, hashlib.sha256(open(os.path.join(self.folder, f), "rb").read()).hexdigest()
        except OSError:
            return None

    def _judged(self, task, f):
        """The fresh judge's verdict on a text file, asked once per content of the file."""
        key = self._judge_key(f)
        if not key:
            return {"matches": True, "why": ""}
        cache = self.__dict__.setdefault("judge_seen", {})
        if key not in cache:                    # a "yes" is asked again: any "no" refuses the file
            for _ in range(max(1, int((self.cfg.get("finish") or {}).get("judge_votes", 2)))):
                cache[key] = self._judge_form(task, f)
                if not cache[key]["matches"]:
                    break
        return cache[key]

    def _review_outputs(self, task, checks, rounds):
        """Before the task ends, the model checks each file it wrote against the task's words: finish carries one check
        per file or set of files (what the task asks, what the file has, whether they match), and a fresh judge looks
        at each text file whose check passed. A file that does not match, or has no check, keeps the task going; after
        3 rounds it may finish, once each text file has been judged. "" when off, done or nothing was written."""
        fin = self.cfg.get("finish") or {}
        files = [r for r in self.produced_files() if os.path.isfile(os.path.join(self.folder, r))]
        if not fin.get("review_outputs", True) or not files:
            return ""
        judge = lambda f: fin.get("judge", True) and (samples.is_text(os.path.join(self.folder, f)) or
                                                      bool(samples.describe(os.path.join(self.folder, f))))
        n = fin.get("review_max_files", 8)
        units = self._units(files)[:n]
        if rounds >= 6:
            return ""
        if rounds >= 3:                         # out of rounds: no file ends unjudged, nor as the judge last refused it
            gaps = [g for _, m in units for g in [self._form_gap(task, m)] if g] if rounds == 3 else []
            seen = set(self.__dict__.get("judge_seen") or {})
            out = []
            for _, m in units:
                f = m[0]
                if not judge(f):
                    continue
                v = self._judged(task, f)
                if v["matches"]:
                    continue
                same = rounds > 3 and self._judge_key(f) in seen      # refused before, in this very content
                out.append(f"{f} does not have the form the task asks for: {v['why']} " + (
                    "It is unchanged since that was found: running the same step again writes the same file. If a "
                    "tool made it, call report_tool_bug with that tool's name and this problem, or redo the step with "
                    "run_code, then finish." if same else "Fix it, then finish."))
            return "\n".join(gaps + out)
        if rounds == 0 or not isinstance(checks, list) or not checks:
            heads = [samples.head(self.folder, m[0]) + (f"\n({label}: {len(m)} files like this one)" if len(m) > 1 else "")
                     for label, m in units]
            rest = [f for _, m in self._units(files)[n:] for f in m]
            more = f"\n\n(and {len(rest)} more: {', '.join(rest)})" if rest else ""
            return prompts.REVIEW_OUTPUTS.format(task=task, heads="\n\n".join(heads) + more)
        norm = lambda f: os.path.normpath(str(f or "").strip().lstrip("./") or ".")
        cs = [c for c in checks if isinstance(c, dict)]

        def check_for(label, members):
            for c in cs:
                cf = norm(c.get("file"))
                if cf in members or cf == label or cf == os.path.dirname(members[0]) or \
                        any(fnmatch.fnmatch(x, cf) for x in members):
                    return c
            return None
        bare = lambda t: re.sub(r"\W+", "", str(t or "")).lower()
        out, unchecked = [], []
        for label, members in units:
            c, f = check_for(label, members), members[0]
            if not c:
                unchecked.append(label)
                continue
            if samples.is_text(os.path.join(self.folder, f)):
                body = "".join(bare(open(os.path.join(self.folder, x), encoding="utf-8", errors="replace").read(200_000))
                               for x in members)
                if not bare(c.get("sample")) or bare(c.get("sample")) not in body:
                    out.append(f"{label}: sample is not in the file. Copy its first entry or first line exactly as the "
                               "file has it, then describe file_has from that sample.")
                    continue
            gap = self._form_gap(task, members)
            if gap:
                out.append(gap)
            elif bare(c.get("file_has")) and bare(c.get("file_has")) in bare(c.get("task_asks")):
                out.append(f"{label}: file_has repeats the task's words. Describe the form the sample "
                           f"{trim(str(c.get('sample') or ''), 200)!r} shows, then compare it with task_asks.")
            elif c.get("matches") is not True:
                out.append(f"{label}: the task asks for {c.get('task_asks')!r} but the file has "
                           f"{c.get('file_has')!r}. Change it to what the task asks, then finish with checks again.")
            elif judge(f):                      # its own check passes: a fresh look, task next to file
                v = self._judged(task, f)
                if not v["matches"]:
                    out.append(f"{label} does not have the form the task asks for: {v['why']} Fix it, then finish "
                               "with checks again.")
        if unchecked:
            out.append("no check for " + ", ".join(unchecked) + ": give one for each file the task wrote (one for a "
                       "set like qr/*.png), or delete the file if the task did not ask for it (a test or scratch file).")
        return "\n".join(out)

    def _form_gap(self, task, members):
        """A line form the task spells out in backticks (`path:line: text`) that the file's lines follow except for
        spacing: say where, since a reader skims past a missing space. "" when there is none."""
        f = os.path.join(self.folder, members[0])
        if not samples.is_text(f):
            return ""
        got = form_gap(task, open(f, encoding="utf-8", errors="replace").read(200_000))
        return f"{members[0]}: {got}" if got else ""

    def _judge_form(self, task, rel):
        """One fresh question, apart from the work that made the file: the task and the file's head, does the file
        have the form the task asks for it? An unclear answer counts as a match (the agent's own checks passed)."""
        msgs = [{"role": "system", "content": prompts.SYSTEM},
                {"role": "user", "content": prompts.JUDGE_FORM.format(
                    task=trim(task, 2000), file=rel, head=samples.head(self.folder, rel),
                    partial="" if samples.is_text(os.path.join(self.folder, rel)) else prompts.JUDGE_PARTIAL)}]
        t = Turns(self, "review", msgs, [T_FORM], think_tokens=GAP_THINK_TOKENS)
        for _ in range(2):
            c = next((c for c in t.ask()["tool_calls"] if c["name"] == "judge_form"), None)
            v = (c or {}).get("arguments")
            if isinstance(v, dict) and isinstance(v.get("matches"), (bool, str)):
                ok = v["matches"] is True or str(v["matches"]).strip().lower() in ("true", "yes")
                self.log.emit("judge_form", file=rel, matches=ok, why=trim(str(v.get("why") or ""), 300))
                return {"matches": ok, "why": str(v.get("why") or "").strip()}
            t.user("Call judge_form with matches (true or false) and why.")
        return {"matches": True, "why": ""}

    def _repair(self, creator, args, task, ctx):
        name = args.get("name", "")
        m = self.registry.get(name)
        if not m:
            return f"No installed tool named {name!r}."
        if is_bundled(name):
            return (f"{name} is a bundled tool and cannot be repaired here. Tell the user in your answer what failed "
                    "so it can be fixed in mini itself, and do the step another way (run_code) if you can.")
        if self.repaired >= self.cfg["caps"]["max_repairs_per_task"]:
            self.log.emit("cap", cap="max_repairs_per_task", limit=self.cfg["caps"]["max_repairs_per_task"])
            raise AgentStop(f"repair limit reached ({self.repaired}); {name} still fails on the real task")
        self.repaired += 1
        ev = json.dumps(self.last_invoke.get(name, {}), indent=1, default=str)
        try:
            new = creator.repair(m, args.get("problem", ""), ev, task, ctx)
        finally:
            self.wd.reset_conversation()
        return f"{name} repaired and installed as v{new['version']}. Invoke it again.\n" + describe_tool(new)
