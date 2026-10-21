"""mini-agent: parse -> confirm-first -> gap detection -> create missing tools -> complete the task with them.

The harness objects (backends, sandbox, registry, gate, watchdogs, log) are handed in; the agent never
gets more authority than they allow. Its own actions are the core capabilities (mini/core): typed calls,
validated against their schema before they run; gap detection and need_tool never build what core already does.

Model tiers (config [local], [free], [paid], [routing]): a task starts on routing.start (local by default). When
the local model runs out of time (routing.local_budget_s) or attempts, the task moves to free by itself and stays
there. Paid is only used when the user picks it; it lasts for the task. The conversation carries over on a switch.
"""
import copy
import json
import os
import re
import threading
import time

from . import prompts
from .creator import Creator, ToolFailed, trim
from .turns import AgentStop, RestartRequested, Turns, fn
from ..core import Call, load as load_catalog
from ..core._tools import describe_tool
from ..harness import checkpoint as ckpt, hwmon, journal, workspace as ws
from ..harness.approvals import ApprovalGate
from ..harness.backends import Endpoint, has_model, make_backend
from ..harness.config import llama_settings, local_candidates
from ..harness.eventlog import RunLog
from ..harness.lessons import LessonStore
from ..harness.registry import Registry
from ..harness.sandbox import PolicyError, Sandbox
from ..harness.settings import SettingsStore
from ..harness.watchdogs import Watchdogs

S = {"type": "string"}
ARR = {"type": "array", "items": S}
T_UNDERSTAND = fn("understand", "State your understanding of the task.", {
    "goal": S, "inputs": ARR, "outputs": ARR, "side_effects": ARR, "ambiguities": ARR, "plan": S},
    ["goal", "inputs", "outputs", "plan"])
T_COVERAGE = fn("decide_coverage", "Decide which installed tools cover the task and which capabilities are missing.", {
    "reasoning": S, "use": ARR, "missing": ARR}, ["reasoning", "use", "missing"])
T_BUG = fn("report_tool_bug", "An installed tool has a bug. It goes back to the creation loop as a new version.",
           {"name": S, "problem": S}, ["name", "problem"])
T_NEED = fn("need_tool", "A capability is missing; build a new tool for it. from_run_code: true when the last "
            "run_code script did this and should become the tool.", {"need": S, "from_run_code": {"type": "boolean"}},
            ["need"])
T_FINISH = fn("finish", "The task is done. answer: the reply to the user, with any list or result written out in it. "
              "files: only deliverable files you wrote or changed in the folder; leave it out when the answer is text "
              "(names a tool returned are not files).", {"answer": S, "files": ARR}, ["answer"])
CONTROL_TOOLS = [T_BUG, T_NEED, T_FINISH]         # the pipeline's own calls; everything else is a core capability


class Agent:
    def __init__(self, cfg, approval_handler=None, backend=None, tier_backends=None, start_tier=None, label="", log=None,
                 history=(), workspace=None):
        self.cfg = cfg
        self.workspace = os.path.realpath(workspace) if workspace else None   # the user's folder: a git repository
        self.history = list(history)                       # finished earlier tasks of this conversation (run_end events)
        # in a git folder the run logs live with its journal in .mini/; otherwise in the global runs dir
        self.memory = bool(self.workspace) and ckpt.is_repo(self.workspace)
        self.log = log or RunLog(journal.runs_dir(self.workspace) if self.memory else cfg.abspath("runs"), label=label)
        if self.memory:
            journal.ensure_excluded(self.workspace)
        self.run_dir = self.log.dir
        # without a folder (tests, internal runs) the agent works in an empty folder of the run, with no checkpoint
        self.folder = self.workspace or os.path.join(self.run_dir, "files")
        os.makedirs(self.folder, exist_ok=True)
        self.attachments, self.changed, self.checkpoint, self.calls = [], [], None, 0
        self.core = load_catalog()                         # core capabilities (mini/core): the agent's own actions
        self.last_invoke, self.last_script, self.web_hosts = {}, None, set()
        self.tier_backends = dict(tier_backends or {})     # tests inject scripted models per tier
        if backend is not None:
            self.tier_backends["local"] = backend
        self.local_backend = self._pick_local()
        self.start_tier = start_tier or cfg["routing"]["start"]
        self.tier_tokens, self.tiers_used, self.escalated_from = {}, [], None
        self._use_tier(self.start_tier)
        self.local_t0 = time.time()
        self.registry = Registry(cfg.abspath("state"), run_id=self.log.run_id, auto_accept=cfg["tools"]["auto_accept"])
        self.settings = SettingsStore(cfg.abspath("state"))
        self.lessons = LessonStore(cfg.abspath("state"))
        self.sandbox = Sandbox(cfg, self.log)
        self.gate = ApprovalGate(cfg, self.log, approval_handler)
        self.wd = Watchdogs(cfg, self.log)
        self.wd.tier = self.tier
        self.local_budget_noted = False
        self.stop_event = threading.Event()
        self.abort_reason = None
        self.tokens = 0
        self.created, self.repaired = [], 0
        self.escalations = 0
        self.run_t0 = time.time()
        self.on_delta = lambda kind, text: self.log.emit("delta", stream=kind, text=text)

    # ------------------------------------------------------------ model tiers
    def _pick_local(self):
        """With several local backends ([local] use), the first that answers and has its model becomes [local]
        for this task. Returns its name."""
        cands = local_candidates(self.cfg)
        if "local" in self.tier_backends:
            return cands[0][0]
        if len(cands) == 1:                           # the only backend: its own url and model, not [local]'s
            name, entry = cands[0]
        else:
            for name, entry in cands:
                if has_model(entry):
                    break
            else:
                name, entry = cands[0]                # none answers: keep the first, its error shows up in the log
        self.cfg = copy.deepcopy(self.cfg)
        self.cfg["local"] = entry
        if name != cands[0][0]:
            self.log.emit("info", text=f"local backend {cands[0][0]} is not available; using {name} ({entry['model']})")
        return name

    def _backend_for(self, tier):
        if tier not in self.tier_backends:
            tc = self.cfg[tier]
            self.tier_backends[tier] = make_backend(Endpoint(
                tc["backend"], tc["url"], options={}, api_key_env=tc.get("api_key_env", ""),
                max_tokens=self.cfg["caps"]["reply_max_tokens"], timeout_s=self.cfg["caps"]["reply_max_s"]))
        return self.tier_backends[tier]

    def _use_tier(self, tier):
        tc = self.cfg[tier]
        self.tier, self.backend, self.model = tier, self._backend_for(tier), tc["model"]
        self.model_options = {"temperature": tc["temperature"]}
        if tc.get("thinking_controls") is False:
            self.model_options["thinking_controls"] = False
        if tier not in self.tiers_used:
            self.tiers_used.append(tier)

    def tier_available(self, tier):
        """Enabled in config and answering. A gateway that is down must not eat the task's time."""
        if tier == self.tier or not self.cfg[tier].get("enabled", True):
            return False
        try:
            self._backend_for(tier).list_models()
            return True
        except (OSError, ValueError) as e:
            self.log.emit("error", text=f"{tier} tier not reachable at {self.cfg[tier]['url']}: {e}")
            return False

    def switch_tier(self, tier, why, decided_by):
        prev = self.tier
        self._use_tier(tier)
        self.wd.tier = tier
        self.wd.reset_tool()                                 # the new model gets a full attempt budget
        self.escalated_from = self.escalated_from or prev
        self.escalations += 1
        self.log.emit("tier", previous=prev, tier=tier, model=self.model, why=why, decided_by=decided_by,
                      leaves_machine=tier != "local")

    def local_time_up(self):
        budget = self.cfg["routing"]["local_budget_s"]          # 0: no limit on local model time
        return bool(budget) and self.tier == "local" and time.time() - self.local_t0 > budget

    def auto_escalate(self, why):
        """local -> free without asking (routing.auto_escalate). False when free is off or unreachable."""
        if self.tier != "local" or not self.cfg["routing"]["auto_escalate"] or not self.tier_available("free"):
            return False
        self.switch_tier("free", why, "auto")
        return True

    def offer_paid(self):
        """Paid is an option in a dialog only when it is configured and not already in use."""
        return self.tier != "paid" and self.cfg["paid"].get("enabled", True)

    def tier_tokens_cap(self):
        return self.cfg["caps"]["tokens_per_run"] if self.tier == "local" else self.cfg[self.tier]["tokens_per_run"]

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
                      model=self.model, tier=self.tier, backend=self.cfg[self.tier]["backend"], local=self.local_backend,
                      workspace=self.workspace, caps=self.cfg["caps"], watchdogs=self.cfg["watchdogs"], confirm_first=self.cfg["approvals"]["confirm_first"])
        tl = time.time()
        try:
            self.backend.load(self.model, self.model_options)
        except OSError as e:
            self.log.emit("error", text=f"{self.tier} backend not reachable: {e}")
        self.log.emit("info", text=f"model {self.model} loaded in {time.time() - tl:.1f} s; swap baseline taken after loading",
                      swap_used_mib=round(hwmon.swap_used_mib()))
        self.local_t0 = time.time()
        swap = hwmon.SwapWatch(self.cfg["watchdogs"]["swap_growth_mib"], self._swap_trip)
        if self.cfg["watchdogs"]["swap_watch"]:
            swap.begin()
        else:
            self.log.emit("info", text="swap watchdog is off ([watchdogs] swap_watch); swap growth is only recorded")
        self.wd.reset_task()
        try:
            understanding = self.parse(task)
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
                   "repairs": self.repaired, "escalations": self.escalations,
                   "tiers": {"start": self.start_tier, "used": self.tiers_used, "tokens": self.tier_tokens},
                   "watchdogs": self._watchdog_counts(), "outputs": self.produced_files(),
                   "workspace": self.workspace, "checkpoint": self.checkpoint.ref if self.checkpoint else None, "swap_growth_mib": round(swap.growth(), 1), "registry_after": self.registry.snapshot()}
        json.dump(summary, open(os.path.join(self.run_dir, "summary.json"), "w"), indent=2)
        self.log.emit("run_end", **{k: v for k, v in summary.items() if k != "registry_after"})
        if self.memory:
            try:
                journal.record(self.workspace, summary)
            except OSError as e:
                self.log.emit("error", text=f"journal not written: {e}")
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
            u = call["arguments"]
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
        self.log.emit("phase", phase="gap_detection")
        snap = self.registry.snapshot()
        self.log.emit("registry_state", when="before_run", tools=snap, count=len(snap))
        query = " ".join([understanding.get("goal", ""), " ".join(understanding.get("outputs") or []),
                          " ".join(understanding.get("side_effects") or []), understanding.get("plan", "")])
        tools = self.registry.list_tools() if len(snap) <= 40 else self.registry.search(query, limit=15)
        listing = "\n".join(describe_tool(m) for m in tools) or "(the registry is empty)"
        matches = ", ".join(c.name for c in self.core.search(query)) or "(none)"     # the catalog is searched first
        msgs = [{"role": "system", "content": prompts.SYSTEM},
                {"role": "user", "content": prompts.GAP.format(
                    understanding=json.dumps(understanding, indent=1), count=len(snap), registry=listing,
                    core=self.core.listing(), matches=matches)}]
        t = Turns(self, "gap", msgs, [T_COVERAGE])
        for _ in range(4):
            reply = t.ask()
            call = reply["tool_calls"][0]
            args = call["arguments"]
            if call["name"] != "decide_coverage":
                t.result(call["name"], "Call decide_coverage.")
                continue
            use = [u for u in (args.get("use") or []) if isinstance(u, str) and u not in self.core]
            unknown = [u for u in use if u not in self.registry.names()]
            if unknown:
                t.result("decide_coverage", f"Not installed: {', '.join(unknown)}. Only use installed tool names; "
                                            "list anything else under missing.")
                continue
            missing = [m if isinstance(m, str) else json.dumps(m) for m in (args.get("missing") or [])]
            core = {m: self.core.covering(m) for m in missing}
            core = {m: c for m, c in core.items() if c}         # a need core already covers is never built
            missing = [m for m in missing if m not in core]
            self.log.emit("gap", reasoning=args.get("reasoning"), use=use, missing=missing, core=core or None,
                          covered=not missing, registry_count=len(snap))
            self.gap_decision = {"use": use, "missing": missing}
            return {"use": use, "missing": missing}
        raise AgentStop("no coverage decision")

    # ------------------------------------------------------------ 4-6 create, then execute
    def execute(self, task, understanding, covered):
        creator = Creator(self)
        ctx = self.context()
        tools = list(covered["use"])
        for need in covered["missing"]:
            m = creator.create(need, task, ctx)
            tools.append(m["name"])
        self.wd.reset_task()
        self.log.emit("phase", phase="execute", tools=tools)
        listing = lambda: "\n".join(describe_tool(self.registry.get(n)) for n in dict.fromkeys(tools)) or "(none)"
        msgs = [{"role": "system", "content": prompts.SYSTEM},
                {"role": "user", "content": prompts.EXECUTE.format(task=task, understanding=json.dumps(understanding, indent=1),
                                                                   context=ctx, tools=listing(), core=self.core.listing(),
                                                                   files=self.list_work())}]
        finish_rejections = 0
        offered = self.core.tools() + CONTROL_TOOLS
        while True:
            t = Turns(self, "execute", msgs, offered)
            try:
                reply = t.ask()
            except RestartRequested:
                raise AgentStop("stopped at the time/turn limit during execution")
            content = reply.get("content") or ""
            for call in reply["tool_calls"]:
                name, args = call["name"], call["arguments"] or {}
                guard = self.wd.on_call(name, args)
                if guard == "block":
                    t.result(name, f"BLOCKED: this exact call was made {self.wd.call_streak} times in a row. Do something different.")
                    continue
                note = "\nWarning: same call as before. A third identical call in a row will be blocked." if guard == "warn" else ""
                self.log.emit("tool_call", step="execute", name=name, args=args)
                if name == "finish":
                    problem = self._check_finish(args, understanding, finish_rejections)
                    if problem:
                        finish_rejections += 1
                        self.log.emit("watchdog", name="finish_check", problem=problem, rejections=finish_rejections)
                        t.result(name, "Not finished: " + problem)
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
                elif name == "need_tool":
                    text = self._need(creator, args, task, ctx, tools)
                elif name == "report_tool_bug":
                    text = self._repair(creator, args, task, ctx)
                else:
                    text = "Unknown tool. Use: " + ", ".join(x["function"]["name"] for x in offered)
                self.log.emit("tool_result", step="execute", name=name, text=trim(text, 3000))
                t.result(name, text + note)

    def act(self, name, args, content="", messages=None):
        """Run a core capability: the arguments are validated against its schema first. Returns (result text,
        whether the handler already answered in the conversation)."""
        call = Call(self, content, messages)
        return self.core.invoke(call, name, args), call.answered

    def _need(self, creator, args, task, ctx, tools):
        need = str(args.get("need") or "")
        hit = self.core.covering(need)
        if hit:
            self.log.emit("watchdog", name="core_covers_need", need=need, capability=hit)
            return f"Not built: the core capability {hit} already does this. Call {hit} directly."
        s = self.last_script
        if args.get("from_run_code") and s and s.get("ok") and s.get("script"):
            need += f"\n\nA script that already did this once in the sandbox (start from it):\n```python\n{s['script']}```"
        try:
            m = creator.create(need, task, ctx)
            tools.append(m["name"])
            return "New tool installed:\n" + describe_tool(m)
        finally:
            self.wd.reset_conversation()

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

    def _change(self, rels, write):
        """Checkpoint (ensure_checkpoint), then write(), which returns the relative paths written, then record them
        in the checkpoint. PolicyError, before anything is written, when there is no checkpoint."""
        self.ensure_checkpoint(rels)
        done = write()
        if self.checkpoint:
            self.checkpoint.after_change(done)
        self.changed += [r for r in done if r not in self.changed]
        self.log.emit("files_changed", paths=done)
        return done

    def context_tokens(self):
        """The local model's context size, when it is known (llama.cpp); None otherwise."""
        if self.tier == "local" and self.local_backend == "llamacpp":
            return int(llama_settings(self.cfg).get("ctx") or 0) or None
        return None

    def produced_files(self):
        """The files this task wrote or changed in the folder, relative to it."""
        return sorted(self.changed)

    def _check_finish(self, args, understanding, rejections):
        """Harness-side check of a finish call. Returns a problem or "" (after 2 rejections the agent may finish)."""
        if rejections >= 2:
            return ""
        produced = self.produced_files()
        for f in args.get("files") or []:
            try:
                full = self._path(f)
            except PolicyError as e:
                return str(e)
            if not os.path.isfile(full) or os.path.getsize(full) == 0:
                return f"{f} does not exist or is empty. Write it first. Files present: {', '.join(produced) or '(none)'}"
        promised = " ".join(str(x) for x in (understanding.get("outputs") or []) + (understanding.get("side_effects") or []))
        # a promised deliverable: "save/write/create ... file", or a file name with an extension. "A list of files in
        # this folder" is an answer, not a file.
        if re.search(r"\b(save|write|written|create|produce|export|generate)\w*\b.*\bfiles?\b|\b\w+\.[a-z]{2,4}\b",
                     promised.lower()) and not produced:
            return ("your plan produces a file, but no file was written in the folder. Use write_file "
                    "(or a tool) to create it, then finish with its path in files.")
        return ""

    def _repair(self, creator, args, task, ctx):
        name = args.get("name", "")
        m = self.registry.get(name)
        if not m:
            return f"No installed tool named {name!r}."
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
