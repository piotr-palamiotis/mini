"""Who answers a tool's questions. Each asker takes the question message from mini_io
({"op": "ask", key, prompt, kind, choices, default, multi}) and returns {"answer": value} or {"error": why}.
"say" messages are shown and get no reply. File answers are host paths: a file in the user's folder is copied into
the call's sandbox folder at its relative path, any other file under .mini/in/, and the tool gets the relative path.
That holds for any answer naming an existing host file, whatever kind the tool asked for, the same as for values
given as command-line arguments.
"""
import getpass
import os
import sys

from . import workspace as ws
from .approvals import pick
from .sandbox import PolicyError


def stage_file(work_dir, path, folder=None):
    """Copy a host file into the call's sandbox folder and return the path the tool sees, or None if it is not a
    file. Relative paths are relative to the user's folder."""
    p = os.path.expanduser(str(path))
    if folder:
        try:
            full, rel = ws.confine(folder, p)
            if os.path.isfile(full):
                return ws.copy_in(folder, work_dir, rel)
        except PolicyError:
            pass
    src = os.path.abspath(p)
    if not os.path.isfile(src) or (folder and not os.path.isabs(p)):
        return None
    return ws.stage(work_dir, src)


class TerminalAsker:
    """Questions on the terminal (mini tool NAME, and the tool commands on PATH)."""

    def __init__(self, work_dir, out=sys.stderr, folder=None):
        self.work, self.out, self.folder = work_dir, out, folder

    def __call__(self, msg):
        if msg.get("op") == "say":
            print(msg.get("text", ""), file=self.out)
            return None
        if msg.get("op") != "ask":
            return None
        if not sys.stdin.isatty():
            if msg.get("default") is not None:
                return {"answer": msg["default"]}
            return {"error": f"no answer for {msg.get('key')}: not interactive, pass --{str(msg.get('key')).replace('_', '-')}"}
        try:
            return {"answer": self._ask(msg)}
        except (EOFError, KeyboardInterrupt):
            print(file=self.out)
            return {"error": "cancelled by the user"}

    def _ask(self, msg):
        kind, prompt, default, choices = msg.get("kind", "text"), msg.get("prompt") or msg["key"], msg.get("default"), msg.get("choices")
        hint = f" [{default}]" if default not in (None, "") and kind != "secret" else ""
        while True:
            if kind == "choice" and choices and msg.get("multi"):
                print(prompt, file=self.out)
                for i, c in enumerate(choices, 1):
                    print(f"  {i}) {c}", file=self.out)
                picks = pick(choices, input(f"pick any of 1-{len(choices)}, comma-separated{hint}: "))
                if not picks and default:
                    return default
                if picks and all(p in choices for p in picks):
                    return picks
            elif kind == "choice" and choices:
                print(prompt, file=self.out)
                for i, c in enumerate(choices, 1):
                    print(f"  {i}) {c}", file=self.out)
                a = input(f"choose 1-{len(choices)}{hint}: ").strip()
                if not a and default in choices:
                    return default
                if a.isdigit() and 1 <= int(a) <= len(choices):
                    return choices[int(a) - 1]
                if a in choices:
                    return a
            elif kind == "confirm":
                a = input(f"{prompt} (y/n){hint}: ").strip().lower()
                if not a and default is not None:
                    return bool(default)
                if a in ("y", "yes", "n", "no"):
                    return a.startswith("y")
            elif kind == "secret":
                a = getpass.getpass(f"{prompt}: ")
                if a:
                    return a
            else:
                a = input(f"{prompt}{hint}: ").strip()
                if not a and default not in (None, ""):
                    a = str(default)
                if kind == "number" and a:
                    try:
                        return float(a) if "." in a else int(a)
                    except ValueError:
                        print("  a number, please", file=self.out)
                        continue
                if kind == "file" and a:
                    staged = stage_file(self.work, a, self.folder)
                    if staged:
                        return staged
                    print(f"  no such file: {a}", file=self.out)
                    continue
                if a:
                    return stage_file(self.work, a, self.folder) or a      # a host file named in any answer is copied in
            print("  (answer needed; Ctrl-D cancels)", file=self.out)


class GateAsker:
    """Questions during a task: through the approval gate (a dialog in the UI, the terminal headless, and in
    auto mode a clear error that tells the agent to pass the value as an input)."""

    def __init__(self, gate, log, work_dir, tool, folder=None):
        self.gate, self.log, self.work, self.tool, self.folder = gate, log, work_dir, tool, folder

    def __call__(self, msg):
        if msg.get("op") == "say":
            self.log.emit("info", text=f"{self.tool}: {msg.get('text', '')}")
            return None
        if msg.get("op") != "ask":
            return None
        kind, choices = msg.get("kind", "text"), msg.get("choices") or []
        multi = bool(msg.get("multi")) and kind == "choice"
        options = ("yes", "no", "cancel") if kind == "confirm" else ("answer", "cancel")
        details = {"tool": self.tool, "question": msg.get("prompt") or msg["key"], "answer type": kind}
        if msg.get("default") not in (None, ""):
            details["default"] = str(msg["default"])
        if kind == "secret":
            details["_secret"] = True
        d = self.gate.request("question", msg.get("prompt") or msg["key"], details, options=options,
                              choices=choices if kind == "choice" else None, multi=multi)
        if not d.approved or d.choice == "cancel":
            if d.decided_by.startswith("auto") and msg.get("default") is not None:
                return {"answer": msg["default"]}       # nobody to ask: the tool's default, as its own test assumed
            return {"error": f"no answer for {msg['key']}: pass it in the tool inputs" if d.decided_by.startswith("auto")
                    else "the user cancelled"}
        if multi:
            bad = [p for p in d.picks or [] if p not in choices]
            return {"error": f"{msg['key']}: {', '.join(bad)} not among the choices"} if bad else {"answer": d.picks or []}
        if kind == "choice":
            return {"answer": d.choice}
        if kind == "confirm":
            return {"answer": d.choice == "yes"}
        a = (d.note or "").strip() or (str(msg["default"]) if msg.get("default") not in (None, "") else "")
        if not a:
            return {"error": f"no answer for {msg['key']}"}
        if kind == "number":
            try:
                return {"answer": float(a) if "." in a else int(a)}
            except ValueError:
                return {"error": f"{msg['key']}: {a!r} is not a number"}
        if kind == "file":
            staged = stage_file(self.work, a, self.folder)
            return {"answer": staged} if staged else {"error": f"no such file: {a}"}
        staged = stage_file(self.work, a, self.folder)                    # a host file named in any answer is copied in
        return {"answer": staged if staged and (self.folder or os.path.isabs(os.path.expanduser(a))) else a}
