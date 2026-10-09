"""Run log: every step of a run goes to runs/<run_id>/log.jsonl and log.txt, and to live subscribers (the UI).

Event kinds (data is a dict):
  run_start, run_end, phase, model_reply, tool_call, tool_result, registry_state, gap, spec, code,
  test_run, attempt, watchdog, approval, sandbox, install, invoke, fallback, cap, error, info, delta
"delta" events (streamed thinking/content) go to subscribers only, not to the files.
"""
import json
import os
import threading
import time


class RunLog:
    def __init__(self, runs_dir, run_id=None, label=""):
        self.run_id = run_id or time.strftime("%Y%m%d-%H%M%S") + (f"-{label}" if label else "")
        self.dir = os.path.join(runs_dir, self.run_id)
        os.makedirs(self.dir, exist_ok=True)
        self._jsonl = open(os.path.join(self.dir, "log.jsonl"), "a", buffering=1)
        self._txt = open(os.path.join(self.dir, "log.txt"), "a", buffering=1)
        self._subs = []
        self._lock = threading.Lock()
        self.t0 = time.time()
        self.counts = {}

    def subscribe(self, fn):
        self._subs.append(fn)

    def emit(self, kind, **data):
        ev = {"t": round(time.time() - self.t0, 2), "kind": kind, **data}
        with self._lock:
            if kind != "delta":
                self.counts[kind] = self.counts.get(kind, 0) + 1
                self._jsonl.write(json.dumps(ev, default=str) + "\n")
                self._txt.write(self.format(ev) + "\n")
        for fn in list(self._subs):
            try:
                fn(ev)
            except Exception:
                pass
        return ev

    @staticmethod
    def format(ev):
        k, t = ev["kind"], ev["t"]
        d = {x: y for x, y in ev.items() if x not in ("t", "kind")}
        head = f"[{t:8.2f}] {k.upper():<14}"
        if k == "model_reply":
            parts = []
            if d.get("thinking"):
                parts.append("  thinking: " + d["thinking"].strip().replace("\n", "\n            "))
            if d.get("content"):
                parts.append("  content:  " + d["content"].strip().replace("\n", "\n            "))
            for c in d.get("tool_calls") or []:
                parts.append(f"  call:     {c.get('name')}({json.dumps(c.get('arguments'), default=str)[:2000]})")
            meta = f"step={d.get('step')} tokens={d.get('prompt_tokens')}+{d.get('output_tokens')} secs={d.get('secs')} done={d.get('done_reason')}"
            return head + meta + ("\n" + "\n".join(parts) if parts else "")
        text = d.pop("text", None)
        line = head + " ".join(f"{a}={json.dumps(b, default=str) if not isinstance(b, str) else b}" for a, b in d.items())
        if text:
            line += "\n    " + str(text).replace("\n", "\n    ")
        return line

    def close(self):
        self._jsonl.close()
        self._txt.close()
