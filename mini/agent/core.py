"""mini-agent: parse -> confirm-first -> gap detection -> create missing tools -> complete the task with them.

The harness objects (backends, sandbox, registry, gate, watchdogs, log) are handed in; the agent never
gets more authority than they allow.

Model tiers (config [local], [free], [paid], [routing]): a task starts on routing.start (local by default). When
the local model runs out of time (routing.local_budget_s) or attempts, the task moves to free by itself and stays
there. Paid is only used when the user picks it; it lasts for the task. The conversation carries over on a switch.
"""
import copy
import json
import os
import re
import shutil
import threading
import time

from . import codeblocks, prompts
from .creator import Creator, ToolFailed, trim
from .turns import AgentStop, RestartRequested, Turns, fn
from ..harness import checkpoint as ckpt, hwmon, workspace as ws
from ..harness.approvals import ApprovalGate
from ..harness.ask import GateAsker
from ..harness.backends import Endpoint, has_model, make_backend
from ..harness.config import llama_settings, local_candidates
from ..harness.eventlog import RunLog
from ..harness.lessons import LessonStore
from ..harness.registry import Registry
from ..harness.sandbox import PolicyError, Sandbox
from ..harness.settings import MissingSetting, SettingsStore
from ..harness.watchdogs import Watchdogs

S = {"type": "string"}
ARR = {"type": "array", "items": S}
T_UNDERSTAND = fn("understand", "State your understanding of the task.", {
    "goal": S, "inputs": ARR, "outputs": ARR, "side_effects": ARR, "ambiguities": ARR, "plan": S},
    ["goal", "inputs", "outputs", "plan"])
T_COVERAGE = fn("decide_coverage", "Decide which installed tools cover the task and which capabilities are missing.", {
    "reasoning": S, "use": ARR, "missing": ARR}, ["reasoning", "use", "missing"])
T_INVOKE = fn("invoke_tool", "Run an installed tool in the sandbox.", {"name": S, "inputs": {"type": "object"}},
              ["name", "inputs"])
T_READ = fn("read_file", "Read a text file in the user's folder (path relative to it) or an attached file. Long "
            "files come in parts: offset is the character to start at.", {"path": S, "offset": {"type": "integer"}}, ["path"])
T_WRITE = fn("write_file", "Write a text file in the user's folder (path relative to it), in place. The content is "
             "the fenced block in this reply. mode: write (default), prepend or append.", {"path": S, "mode": S}, ["path"])
T_BUG = fn("report_tool_bug", "An installed tool has a bug. It goes back to the creation loop as a new version.",
           {"name": S, "problem": S}, ["name", "problem"])
T_NEED = fn("need_tool", "A capability is missing; build a new tool for it.", {"need": S}, ["need"])
T_FINISH = fn("finish", "The task is done. answer: the reply to the user, with any list or result written out in it. "
              "files: only deliverable files you wrote or changed in the folder; leave it out when the answer is text "
              "(names a tool returned are not files).", {"answer": S, "files": ARR}, ["answer"])
T_LIST = fn("list_files", "List a folder in the user's folder (path '.' for the folder itself). The listing is "
            "compact: folders show their item count. depth 1 (default) is the folder itself, 2 or 3 add subfolders. "
            "To see inside one folder, list that folder.",
            {"path": S, "depth": {"type": "integer"}}, ["path"])
EXEC_TOOLS = [T_INVOKE, T_READ, T_LIST, T_WRITE, T_BUG, T_NEED, T_FINISH]
MAX_READ = 8000                                   # characters of a file per read (about 2,000 tokens)


def describe_tool(m, full=True):
    def fields(schema):
        props = (schema or {}).get("properties") or {}
        req = set((schema or {}).get("required") or [])
        return ", ".join(f"{k}{'' if k in req else '?'}: {v.get('type', '?') if isinstance(v, dict) else '?'}"
                         + (f" ({v.get('description')})" if full and isinstance(v, dict) and v.get("description") else "")
                         for k, v in props.items()) or "-"
    src = m.get("created_by", {}).get("provider", "?")
    sets = ", ".join(f"{d['key']} ({d.get('description', '')})" for d in m.get("settings") or [])
    return (f"- {m['name']} (v{m['version']}, built by {src}): {m['description']}\n"
            f"    inputs: {fields(m.get('input_schema'))}\n    outputs: {fields(m.get('output_schema'))}"
            + (f"\n    saved settings: {sets}" if sets else ""))


class Agent:
    def __init__(self, cfg, approval_handler=None, backend=None, tier_backends=None, start_tier=None, label="", log=None,
                 history=(), workspace=None):
        self.cfg = cfg
        self.workspace = os.path.realpath(workspace) if workspace else None   # the user's folder: a git repository
        self.history = list(history)                       # finished earlier tasks of this conversation (run_end events)
        self.log = log or RunLog(cfg.abspath("runs"), label=label)
        self.run_dir = self.log.dir
        # without a folder (tests, internal runs) the agent works in an empty folder of the run, with no checkpoint
        self.folder = self.workspace or os.path.join(self.run_dir, "files")
        os.makedirs(self.folder, exist_ok=True)
        self.attachments, self.changed, self.checkpoint, self.calls = [], [], None, 0
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
            out += (f"\n\nYou work in the user's folder {self.workspace}. Every path is relative to it. list_files, "
                    f"read_file and write_file work on it directly, and write_file changes files in place. A tool gets "
                    f"relative paths in its inputs: the files a call names are copied into the tool's sandbox, and "
                    f"every file the tool writes lands at the same relative path in the folder. Put new files where "
                    f"the task says. mini checkpoints the folder in git before the first change, so the user can undo "
                    f"the task. \"The current folder\", \"this directory\" or \"here\" in a task means this folder (path '.').")
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
        msgs = [{"role": "system", "content": prompts.SYSTEM},
                {"role": "user", "content": prompts.GAP.format(
                    understanding=json.dumps(understanding, indent=1), count=len(snap), registry=listing)}]
        t = Turns(self, "gap", msgs, [T_COVERAGE])
        for _ in range(4):
            reply = t.ask()
            call = reply["tool_calls"][0]
            args = call["arguments"]
            if call["name"] != "decide_coverage":
                t.result(call["name"], "Call decide_coverage.")
                continue
            use = [u for u in (args.get("use") or []) if isinstance(u, str)]
            unknown = [u for u in use if u not in self.registry.names()]
            if unknown:
                t.result("decide_coverage", f"Not installed: {', '.join(unknown)}. Only use installed tool names; "
                                            "list anything else under missing.")
                continue
            missing = [m if isinstance(m, str) else json.dumps(m) for m in (args.get("missing") or [])]
            self.log.emit("gap", reasoning=args.get("reasoning"), use=use, missing=missing,
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
                                                                   context=ctx, tools=listing(), files=self.list_work())}]
        last_invoke = {}
        finish_rejections = 0
        while True:
            t = Turns(self, "execute", msgs, EXEC_TOOLS)
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
                if name == "invoke_tool":
                    text = self._invoke(args, last_invoke)
                elif name == "read_file":
                    text = self._read(args)
                elif name == "list_files":
                    text = self._list(args)
                elif name == "write_file":
                    self._text_turn_done = False
                    text = self._write(args, content, msgs)
                    if self._text_turn_done:
                        self.log.emit("tool_result", step="execute", name=name, text=text)
                        t.user(text + note)
                        continue
                elif name == "need_tool":
                    try:
                        m = creator.create(args.get("need", ""), task, ctx)
                        tools.append(m["name"])
                        text = "New tool installed:\n" + describe_tool(m)
                    finally:
                        self.wd.reset_conversation()
                elif name == "report_tool_bug":
                    text = self._repair(creator, args, last_invoke, task, ctx)
                else:
                    text = "Unknown tool. Use: " + ", ".join(x["function"]["name"] for x in EXEC_TOOLS)
                self.log.emit("tool_result", step="execute", name=name, text=trim(text, 3000))
                t.result(name, text + note)

    def _invoke(self, args, last_invoke):
        name = args.get("name", "")
        m = self.registry.get(name)
        if not m:
            return f"No installed tool named {name!r}. Installed: {', '.join(self.registry.names()) or '(none)'}"
        inputs = args.get("inputs") or {}
        if not isinstance(inputs, dict):
            return "inputs must be an object."
        req = [k for k in ((m.get("input_schema") or {}).get("required") or []) if k not in inputs]
        if req:
            return f"Missing required inputs for {name}: {', '.join(req)}.\n" + describe_tool(m)
        self.calls += 1
        box = os.path.join(self.run_dir, "calls", f"{self.calls:03d}-{name}")      # the tool's /work for this call
        os.makedirs(box)
        try:
            inputs = ws.copy_in(self.folder, box, inputs, outside=self.attachments)
        except (OSError, PolicyError) as e:
            shutil.rmtree(box, ignore_errors=True)
            return f"{name} was not run: {e}"
        try:
            return self._invoke_in(m, name, inputs, box, last_invoke)
        finally:
            shutil.rmtree(box, ignore_errors=True)

    def _invoke_in(self, m, name, inputs, box, last_invoke):
        asker = GateAsker(self.gate, self.log, box, name, self.folder)
        try:
            settings = self.settings.resolve(m, asker)
            network = self.settings.network(m, settings)
        except MissingSetting as e:
            return (f"{name} cannot run: {e}. Tell the user in your answer what to save "
                    f"(mini settings, or run the tool once from the terminal).")
        self.log.emit("invoke", tool=name, version=m["version"], inputs=inputs,
                      settings=sorted(settings) or None, network=network or None)
        out, res = self.sandbox.invoke_tool(m, m["_dir"], box, inputs, on_message=asker, settings=settings,
                                            network=network)
        items, refused = ws.changes(self.folder, box)
        written = []
        if items:
            try:
                written = self._change([rel for rel, _ in items], lambda: ws.export(self.folder, items))
            except (OSError, PolicyError) as e:
                refused.append(("(all files)", str(e)))
        last_invoke[name] = {"inputs": inputs, "output": out, "sandbox_tail": res["output"][-1500:],
                             "network": res.get("network_note") or None}
        self.log.emit("invoke", tool=name, version=m["version"], ok="error" not in out, output=out, exit_code=res["exit_code"])
        text = json.dumps(out, indent=1, default=str)
        if len(text) > 6000:
            p = os.path.join(self.run_dir, f"result-{name}-{int(time.time())}.json")
            open(p, "w").write(text)
            text = text[:5000] + f"\n[... output is {len(text)} characters; full JSON saved to {p} (read_file takes that path)]"
        if written:
            text += "\nFiles written to the folder: " + ", ".join(written)
        if refused:
            text += "\nNot written: " + "; ".join(f"{rel} ({why})" for rel, why in refused)
            self.log.emit("watchdog", name="export_refused", tool=name, refused=refused)
        note = res.get("network_note") or ((self._net_note(out, m)) if "error" in out else "")
        if note:
            text += "\n" + note
            self.log.emit("watchdog", name="network_diagnosis", tool=name, note=note)
        if "error" in out:
            text += ("\nThe tool failed. If this is a bug in the tool (not a problem with your inputs), "
                     "call report_tool_bug.")
        else:
            text += ("\nCheck this output against the request before you go on: is any part missing, empty, marked "
                     "as not found, or different from what was asked? If so, call report_tool_bug with what is wrong.")
        return text

    @staticmethod
    def _net_note(out, manifest):
        from ..harness.sandbox import network_diagnosis
        return network_diagnosis(json.dumps(out), [], (manifest.get("permissions") or {}).get("network"))

    def _path(self, p):
        return ws.confine(self.folder, p)[0]

    def _readable(self, p):
        """A path read_file may read: in the folder, an attachment, or a file this run saved (a long tool result)."""
        full = os.path.realpath(os.path.expanduser(str(p or "")))
        if os.path.isabs(str(p)) and (full in self.attachments or full.startswith(os.path.realpath(self.run_dir) + os.sep)):
            return full
        return self._path(p)

    def _change(self, rels, write):
        """Checkpoint the folder (once per task, and any file not yet in the checkpoint), then write(), which returns
        the relative paths written, then record them in the checkpoint. PolicyError, before anything is written, when
        the folder is not a git repository or the checkpoint fails."""
        if self.workspace:
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
        done = write()
        if self.checkpoint:
            self.checkpoint.after_change(done)
        self.changed += [r for r in done if r not in self.changed]
        self.log.emit("files_changed", paths=done)
        return done

    def _list(self, args):
        p = str(args.get("path") or ".")
        try:
            depth = int(args.get("depth") or (2 if args.get("recursive") else 1))
        except (TypeError, ValueError):
            depth = 1
        try:
            if not os.path.isdir(self._path(p)):
                return f"{p} is not a folder.\nFiles:\n{self.list_work()}"
            return ws.list_dir(self.folder, p, depth)
        except (OSError, PolicyError) as e:
            return f"Cannot list: {e}"

    def context_tokens(self):
        """The local model's context size, when it is known (llama.cpp); None otherwise."""
        if self.tier == "local" and self.local_backend == "llamacpp":
            return int(llama_settings(self.cfg).get("ctx") or 0) or None
        return None

    def _read(self, args):
        try:
            p = str(args.get("path") or "")
            full = self._readable(p)
            if os.path.isdir(full):
                return f"{p} is a folder; use list_files.\nFiles:\n{self.list_work()}"
            data = open(full, errors="replace").read()
        except (OSError, PolicyError) as e:
            return f"Cannot read: {e}\nFiles:\n{self.list_work()}"
        try:
            start = max(0, int(args.get("offset") or 0))
        except (TypeError, ValueError):
            start = 0
        if len(data) > MAX_READ or start:
            end = start + MAX_READ
            more = f"\n[... characters {start}-{min(end, len(data))} of {len(data)}; read_file with offset={end} for the rest]" \
                if end < len(data) else f"\n[... characters {start}-{len(data)} of {len(data)}: the end]"
            return data[start:end] + more
        return data

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

    def _write(self, args, content, msgs=None):
        try:
            full, rel = ws.confine(self.folder, args.get("path"))
        except PolicyError as e:
            return str(e)
        if os.path.isdir(full):
            return f"{args.get('path')} is a directory, not a file. Files:\n{self.list_work()}"
        path = str(args.get("path", "")).lstrip("./")
        body = None
        if "```" in (content or ""):
            body = codeblocks.single(content, path)[0]
        if body is None and isinstance(args.get("content"), str):
            body = args["content"]
        if body is None and msgs is not None:
            # text-only turn: the model sends the content as a fenced block with no tools offered
            msgs.append({"role": "tool", "tool_name": "write_file", "content": "ok"})
            ext = os.path.splitext(path)[1].lstrip(".") or "text"
            msgs.append({"role": "user", "content": prompts.WRITE_CONTENT.format(path=path, lang=ext)})
            t = Turns(self, "write", msgs, [])
            for _ in range(3):
                reply = t.ask_text()
                text = reply.get("content") or ""
                body, problems = codeblocks.single(text, path)
                if body is None and text.strip() and "```" not in text:
                    body = text.strip() + "\n"            # plain text reply: the content itself
                if body is None:
                    for c in reply.get("tool_calls") or []:   # the model called a tool anyway: take its content
                        v = (c.get("arguments") or {}).get("content")
                        if isinstance(v, str) and v.strip():
                            body = v
                            self.log.emit("info", text=f"content for {path} arrived in a tool-call argument; accepted")
                            break
                if body is not None:
                    break
                t.user(f"Not saved: {' '.join(problems)}. Reply with the content of {path} as one fenced block.")
            self._text_turn_done = True
        if body is None:
            return "No content received; the file was not written."
        mode = args.get("mode", "write")
        try:
            old = open(full).read() if os.path.exists(full) else ""
            new = body + old if mode == "prepend" else old + body if mode == "append" else body

            def write():
                os.makedirs(os.path.dirname(full), exist_ok=True)
                open(full, "w").write(new)
                return [rel]
            self._change([rel], write)
        except (OSError, UnicodeError, PolicyError) as e:
            return f"Cannot write {args.get('path')}: {e}"
        shown = new if len(new) <= 4000 else new[:2000] + f"\n[... {len(new) - 4000} characters ...]\n" + new[-2000:]
        return (f"Wrote {rel} ({len(new)} characters, mode {mode}). The file now "
                f"contains:\n{shown}\nDo not write it again unless something in it is wrong.")

    def _repair(self, creator, args, last_invoke, task, ctx):
        name = args.get("name", "")
        m = self.registry.get(name)
        if not m:
            return f"No installed tool named {name!r}."
        if self.repaired >= self.cfg["caps"]["max_repairs_per_task"]:
            self.log.emit("cap", cap="max_repairs_per_task", limit=self.cfg["caps"]["max_repairs_per_task"])
            raise AgentStop(f"repair limit reached ({self.repaired}); {name} still fails on the real task")
        self.repaired += 1
        ev = json.dumps(last_invoke.get(name, {}), indent=1, default=str)
        try:
            new = creator.repair(m, args.get("problem", ""), ev, task, ctx)
        finally:
            self.wd.reset_conversation()
        return f"{name} repaired and installed as v{new['version']}. Invoke it again.\n" + describe_tool(new)
