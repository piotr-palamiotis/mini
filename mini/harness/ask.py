"""Who answers a tool's questions. Each asker takes the question message from mini_io
({"op": "ask", key, prompt, kind, choices, default}) and returns {"answer": value} or {"error": why}.
"say" messages are shown and get no reply. File answers are host paths: they are copied into the run's work
directory under in/ and the tool gets the relative path.
"""
import getpass
import os
import shutil
import sys


def stage_file(work_dir, path):
    """Copy a host file into <work>/in/ and return the path the tool sees, or None if it is not a file."""
    src = os.path.abspath(os.path.expanduser(str(path)))
    if not os.path.isfile(src):
        return None
    os.makedirs(os.path.join(work_dir, "in"), exist_ok=True)
    shutil.copy(src, os.path.join(work_dir, "in", os.path.basename(src)))
    return "in/" + os.path.basename(src)


class TerminalAsker:
    """Questions on the terminal (mini tool NAME, and the tool commands on PATH)."""

    def __init__(self, work_dir, out=sys.stderr):
        self.work, self.out = work_dir, out

    def __call__(self, msg):
        if msg.get("op") == "say":
            print(msg.get("text", ""), file=self.out)
            return None
        if msg.get("op") != "ask":
            return None
        if not sys.stdin.isatty():
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
            if kind == "choice" and choices:
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
                    staged = stage_file(self.work, a)
                    if staged:
                        return staged
                    print(f"  no such file: {a}", file=self.out)
                    continue
                if a:
                    return a
            print("  (answer needed; Ctrl-D cancels)", file=self.out)


class GateAsker:
    """Questions during a task: through the approval gate (a dialog in the UI, the terminal headless, and in
    auto mode a clear error that tells the agent to pass the value as an input)."""

    def __init__(self, gate, log, work_dir, tool):
        self.gate, self.log, self.work, self.tool = gate, log, work_dir, tool

    def __call__(self, msg):
        if msg.get("op") == "say":
            self.log.emit("info", text=f"{self.tool}: {msg.get('text', '')}")
            return None
        if msg.get("op") != "ask":
            return None
        kind, choices = msg.get("kind", "text"), msg.get("choices") or []
        options = tuple(choices) + ("cancel",) if kind == "choice" and choices else \
            ("yes", "no", "cancel") if kind == "confirm" else ("answer", "cancel")
        details = {"tool": self.tool, "question": msg.get("prompt") or msg["key"], "answer type": kind}
        if msg.get("default") not in (None, ""):
            details["default"] = str(msg["default"])
        if kind == "secret":
            details["_secret"] = True
        d = self.gate.request("question", msg.get("prompt") or msg["key"], details, options=options)
        if not d.approved or d.choice == "cancel":
            return {"error": f"no answer for {msg['key']}: pass it in the tool inputs" if d.decided_by.startswith("auto")
                    else "the user cancelled"}
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
            staged = stage_file(self.work, a)
            return {"answer": staged} if staged else {"error": f"no such file: {a}"}
        return {"answer": a}
