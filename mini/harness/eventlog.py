"""The event stream: one structured stream per run is the source of truth for the UI, headless mode, mini doctor and
the .mini/ journal. Every consumer is a sink (any callable taking the event dict; Sink dispatches by kind).
RunLog also writes the stream to runs/<run_id>/log.jsonl and log.txt; EventBus is the same stream without files.

Every event: {"t": seconds since the stream began, "kind": ..., **data}.

Blocks (what the UI shows as one block): start / chunk / end, linked by id.
  start  {id, block, parent, ...}   parent: the id of the tool block it runs inside, or None
  chunk  {id, block, text, elapsed} elapsed: seconds since its start
  end    {id, block, status, secs, ...}
  block      start fields                         chunk extras        end fields (status)
  thinking   step, model                          -                   chars (ok | stopped | error)
  text       step, model (the answer, streamed)   -                   chars (ok | stopped | error)
  tool       name, args (a core capability)       -                   text (ok | error)
  command    argv, purpose, image, network        stream: stdout |    exit_code, lines, timed_out
             (sandbox runs and image builds)      stderr; one line    (ok | failed | timeout)
  question   req, title, details, options,        -                   choice, answer, picks, decided_by
             choices, multi                                           (answered | refused | cancelled)
Chunks go to subscribers only, not to the files.

Other kinds: run_start, run_end, phase, model_reply, tool_call, tool_result, registry_state, gap, spec, code,
  test_run, attempt, watchdog, approval, sandbox, install, invoke, cap, error, info
"""
import json
import os
import sys
import threading
import time


class Sink:
    """A consumer of the stream. Override what you need: start, chunk and end get the block events, event the rest."""

    def __call__(self, ev):
        getattr(self, ev["kind"] if ev["kind"] in ("start", "chunk", "end") else "event")(ev)

    def start(self, ev):
        pass

    def chunk(self, ev):
        pass

    def end(self, ev):
        pass

    def event(self, ev):
        pass


class EventBus:
    def __init__(self):
        self._subs = []
        self._lock = threading.Lock()
        self._open, self._tools, self._n = {}, [], 0          # open blocks: id -> (block, start time); tool stack
        self.t0 = time.time()

    def subscribe(self, sink):
        self._subs.append(sink)
        return sink

    def _record(self, ev):
        pass

    def emit(self, kind, **data):
        ev = {"t": round(time.time() - self.t0, 2), "kind": kind, **data}
        if kind != "chunk":
            with self._lock:
                self._record(ev)
        for fn in list(self._subs):
            try:
                fn(ev)
            except Exception:
                pass
        return ev

    # ------------------------------------------------------------ blocks
    def start(self, block, **data):
        """Open a block; returns its id for chunk() and end()."""
        with self._lock:
            self._n += 1
            bid = f"b{self._n}"
            parent = self._tools[-1] if self._tools else None
            self._open[bid] = (block, time.time())
            if block == "tool":
                self._tools.append(bid)
        self.emit("start", id=bid, block=block, parent=parent, **data)
        return bid

    def chunk(self, bid, text, **data):
        block, t0 = self._open.get(bid, ("?", time.time()))
        return self.emit("chunk", id=bid, block=block, text=text, elapsed=round(time.time() - t0, 2), **data)

    def end(self, bid, status="ok", **data):
        with self._lock:
            block, t0 = self._open.pop(bid, ("?", time.time()))
            if bid in self._tools:
                self._tools.remove(bid)
        return self.emit("end", id=bid, block=block, status=status, secs=round(time.time() - t0, 2), **data)


class RunLog(EventBus):
    def __init__(self, runs_dir, run_id=None, label=""):
        super().__init__()
        self.run_id = run_id or time.strftime("%Y%m%d-%H%M%S") + (f"-{label}" if label else "")
        self.dir = os.path.join(runs_dir, self.run_id)
        os.makedirs(self.dir, exist_ok=True)
        self._jsonl = open(os.path.join(self.dir, "log.jsonl"), "a", buffering=1)
        self._txt = open(os.path.join(self.dir, "log.txt"), "a", buffering=1)
        self.counts = {}

    def _record(self, ev):
        self.counts[ev["kind"]] = self.counts.get(ev["kind"], 0) + 1
        self._jsonl.write(json.dumps(ev, default=str) + "\n")
        self._txt.write(self.format(ev) + "\n")

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


class ReplyStream:
    """on_delta(kind, text) for a model reply: thinking and answer text become thinking and text blocks."""

    def __init__(self, log, **info):
        self.log, self.info, self.open = log, info, None       # open: [block, id, characters]

    def __call__(self, kind, text):
        block = "thinking" if kind == "thinking" else "text"
        if not self.open or self.open[0] != block:
            self.close()
            self.open = [block, self.log.start(block, **self.info), 0]
        self.open[2] += len(text)
        self.log.chunk(self.open[1], text)

    def close(self, status="ok"):
        if self.open:
            self.log.end(self.open[1], status, chars=self.open[2])
            self.open = None


class HeadlessSink(Sink):
    """mini run: one formatted line per event; chunks (streamed text and output lines) are not printed."""

    def __init__(self, out=None, limit=1500):
        self.out, self.limit = out, limit

    def event(self, ev):
        print(RunLog.format(ev)[:self.limit], file=self.out or sys.stdout, flush=True)

    start = end = event


class LineSink(Sink):
    """Command output as it arrives, one say(line) per line (mini setup and doctor building the image)."""

    def __init__(self, say):
        self.say = say

    def chunk(self, ev):
        if ev["block"] == "command":
            self.say(f"  [{ev['elapsed']:6.1f}s] {ev['text']}")


class JournalSink(Sink):
    """The folder's .mini/ journal: one entry per finished task, from its run_end event."""

    def __init__(self, folder, log):
        self.folder, self.log = folder, log

    def event(self, ev):
        if ev["kind"] != "run_end":
            return
        from . import journal
        try:
            journal.record(self.folder, ev)
        except OSError as e:
            self.log.emit("error", text=f"journal not written: {e}")
