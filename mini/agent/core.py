"""mini-agent: parse -> confirm-first -> gap detection -> create missing tools -> complete the task with them.

The harness objects (backend, sandbox, registry, gate, fallback, watchdogs, log) are handed in; the agent never
gets more authority than they allow.
"""
import json
import os
import shutil
import threading
import time

from . import codeblocks, prompts
from .creator import Creator, ToolFailed, trim
from .turns import AgentStop, EscalateRequested, RestartRequested, Turns, fn
from ..harness import hwmon
from ..harness.approvals import ApprovalGate
from ..harness.backends import Endpoint, make_backend
from ..harness.eventlog import RunLog
from ..harness.fallback import Fallback
from ..harness.registry import Registry
from ..harness.sandbox import PolicyError, Sandbox
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
T_READ = fn("read_file", "Read a text file in the working directory.", {"path": S}, ["path"])
T_WRITE = fn("write_file", "Write a text file in the working directory. The content is the fenced block in this "
             "reply. mode: write (default), prepend or append.", {"path": S, "mode": S}, ["path"])
T_BUG = fn("report_tool_bug", "An installed tool has a bug. It goes back to the creation loop as a new version.",
           {"name": S, "problem": S}, ["name", "problem"])
T_NEED = fn("need_tool", "A capability is missing; build a new tool for it.", {"need": S}, ["need"])
T_FINISH = fn("finish", "The task is done. answer: the reply to the user.", {"answer": S, "files": ARR}, ["answer"])
EXEC_TOOLS = [T_INVOKE, T_READ, T_WRITE, T_BUG, T_NEED, T_FINISH]
MAX_READ = 20000


def describe_tool(m, full=True):
    def fields(schema):
        props = (schema or {}).get("properties") or {}
        req = set((schema or {}).get("required") or [])
        return ", ".join(f"{k}{'' if k in req else '?'}: {v.get('type', '?') if isinstance(v, dict) else '?'}"
                         + (f" ({v.get('description')})" if full and isinstance(v, dict) and v.get("description") else "")
                         for k, v in props.items()) or "-"
    src = m.get("created_by", {}).get("provider", "?")
    return (f"- {m['name']} (v{m['version']}, built by {src}): {m['description']}\n"
            f"    inputs: {fields(m.get('input_schema'))}\n    outputs: {fields(m.get('output_schema'))}")


class Agent:
    def __init__(self, cfg, approval_handler=None, backend=None, fallback_provider=None, label="", log=None):
        self.cfg = cfg
        self.log = log or RunLog(cfg.abspath("runs"), label=label)
        self.run_dir = self.log.dir
        self.work = os.path.join(self.run_dir, "work")
        os.makedirs(os.path.join(self.work, "in"), exist_ok=True)
        os.makedirs(os.path.join(self.work, "out"), exist_ok=True)
        lc = cfg["local"]
        self.model = lc["model"]
        self.backend = backend or make_backend(Endpoint(lc["backend"], lc["url"], options={},
                                                        max_tokens=cfg["caps"]["reply_max_tokens"],
                                                        timeout_s=cfg["caps"]["reply_max_s"]))
        self.model_options = {"num_ctx": lc["num_ctx"], "temperature": lc["temperature"]} if lc["backend"] == "ollama" \
            else {"temperature": lc["temperature"]}
        self.registry = Registry(cfg.abspath("state"))
        self.sandbox = Sandbox(cfg, self.log)
        self.gate = ApprovalGate(cfg, self.log, approval_handler)
        self.fallback = Fallback(cfg, self.log, fallback_provider)
        self.wd = Watchdogs(cfg, self.log)
        self.stop_event = threading.Event()
        self.abort_reason = None
        self.tokens = 0
        self.created, self.repaired = [], 0
        self.escalations = 0
        self.run_t0 = time.time()
        self.on_delta = lambda kind, text: self.log.emit("delta", stream=kind, text=text)

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
        return "Facts about the user's environment:\n" + "\n".join(f"- {f}" for f in facts)

    def list_work(self):
        out = []
        for d, dirs, files in os.walk(self.work):
            dirs[:] = [x for x in dirs if not x.startswith(".")]
            for f in sorted(files):
                p = os.path.join(d, f)
                out.append(f"{os.path.relpath(p, self.work)} ({os.path.getsize(p)} bytes)")
        return "\n".join(out) or "(empty)"

    # ------------------------------------------------------------ run
    def run_task(self, task, attachments=()):
        t0 = self.run_t0 = time.time()
        status, answer, error = "failed", "", ""
        for a in attachments:
            shutil.copy(a, os.path.join(self.work, "in", os.path.basename(a)))
        self.log.emit("run_start", task=task, attachments=[os.path.basename(a) for a in attachments],
                      model=self.model, backend=self.cfg["local"]["backend"], fallback_enabled=self.fallback.enabled,
                      caps=self.cfg["caps"], watchdogs=self.cfg["watchdogs"], confirm_first=self.cfg["approvals"]["confirm_first"])
        tl = time.time()
        try:
            self.backend.load(self.model, self.model_options)
        except OSError as e:
            self.log.emit("error", text=f"local backend not reachable: {e}")
        self.log.emit("info", text=f"model {self.model} loaded in {time.time() - tl:.1f} s; swap baseline taken after loading",
                      swap_used_mib=round(hwmon.swap_used_mib()))
        swap = hwmon.SwapWatch(self.cfg["watchdogs"]["swap_growth_mib"], self._swap_trip).begin()
        self.wd.reset_task()
        try:
            self.task = task
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
                   "secs": round(time.time() - t0, 1), "local_tokens": self.tokens, "created": self.created,
                   "repairs": self.repaired, "escalations": self.escalations,
                   "fallback": {"calls": self.fallback.calls, "tokens": self.fallback.tokens,
                                "spend_usd": round(self.fallback.spend, 4)},
                   "watchdogs": self._watchdog_counts(), "outputs": sorted(os.listdir(os.path.join(self.work, "out"))),
                   "swap_growth_mib": round(swap.growth(), 1), "registry_after": self.registry.snapshot()}
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
                {"role": "user", "content": prompts.PARSE.format(task=task, context=ctx + "\n\nFiles given with the task "
                                                                 "(working directory):\n" + files)}]
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
                {"role": "user", "content": prompts.GAP.format(understanding=json.dumps(understanding, indent=1),
                                                               count=len(snap), registry=listing)}]
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
        while True:
            t = Turns(self, "execute", msgs, EXEC_TOOLS)
            try:
                reply = t.ask()
            except (RestartRequested, EscalateRequested):
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
                    answer = args.get("answer", "")
                    self.log.emit("phase", phase="done", answer=answer, outputs=os.listdir(os.path.join(self.work, "out")))
                    return answer
                if name == "invoke_tool":
                    text = self._invoke(args, last_invoke)
                elif name == "read_file":
                    text = self._read(args)
                elif name == "write_file":
                    text = self._write(args, content)
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
        self.log.emit("invoke", tool=name, version=m["version"], inputs=inputs)
        out, res = self.sandbox.invoke_tool(m, m["_dir"], self.work, inputs)
        last_invoke[name] = {"inputs": inputs, "output": out, "sandbox_tail": res["output"][-1500:]}
        self.log.emit("invoke", tool=name, version=m["version"], ok="error" not in out, output=out, exit_code=res["exit_code"])
        text = json.dumps(out, indent=1, default=str)
        if len(text) > 6000:
            p = os.path.join(".mini", f"result-{name}-{int(time.time())}.json")
            open(os.path.join(self.work, p), "w").write(text)
            text = text[:5000] + f"\n[... output is {len(text)} characters; full JSON saved to {p}]"
        if "error" in out:
            text += ("\nThe tool failed. If this is a bug in the tool (not a problem with your inputs), "
                     "call report_tool_bug.")
        return text

    def _path(self, p):
        full = os.path.realpath(os.path.join(self.work, str(p or "")))
        if not (full == os.path.realpath(self.work) or full.startswith(os.path.realpath(self.work) + os.sep)):
            raise PolicyError(f"path outside the working directory: {p}")
        return full

    def _read(self, args):
        try:
            full = self._path(args.get("path"))
            data = open(full, errors="replace").read()
        except (OSError, PolicyError) as e:
            return f"Cannot read: {e}\nFiles:\n{self.list_work()}"
        if len(data) > MAX_READ:
            return data[:MAX_READ] + f"\n[... file is {len(data)} characters; first {MAX_READ} shown]"
        return data

    def _write(self, args, content):
        try:
            full = self._path(args.get("path"))
        except PolicyError as e:
            return str(e)
        files, problems = codeblocks.extract(content)
        body = files.get(args.get("path", "").lstrip("./")) or (list(files.values())[0] if len(files) == 1 else None)
        if body is None and isinstance(args.get("content"), str):
            body = args["content"]
        if body is None:
            m = codeblocks.FENCE.search(content)
            body = m.group(3) if m else None
        if body is None:
            return "No content: put the file content in a fenced block in this reply. " + " ".join(problems)
        mode = args.get("mode", "write")
        os.makedirs(os.path.dirname(full), exist_ok=True)
        old = open(full).read() if os.path.exists(full) else ""
        new = body + old if mode == "prepend" else old + body if mode == "append" else body
        open(full, "w").write(new)
        return f"Wrote {os.path.relpath(full, self.work)} ({len(new)} characters, mode {mode})."

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
