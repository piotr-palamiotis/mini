"""Run a script or a command in the sandbox against the files named: the cheap path before building a tool."""
import os
import shutil

from ._files import show
from ..agent import codeblocks
from ..harness import workspace as ws
from ..harness.sandbox import PolicyError

NAME = "run_code"
SCRIPT = ws.META + "/script.py"                   # where the script sits in the call's sandbox folder
DESCRIPTION = ("Run a Python script (the fenced python block in this reply, or script) or a command (an argv list, "
               "no shell) once in the sandbox, with no network. files: the files and folders it works on; they are "
               "copied in at their relative paths (the working directory), and every file it writes there lands at "
               "the same path in the user's folder. Use it for exact processing this task needs; build a tool only "
               "for what is worth keeping.")
SCHEMA = {"type": "object", "required": [], "properties": {
    "files": {"type": "array", "items": {"type": "string"}, "description": "paths relative to the user's folder"},
    "script": {"type": "string", "description": "only for a short script; prefer the fenced block"},
    "command": {"type": "array", "items": {"type": "string"}, "description": "argv, e.g. [\"wc\", \"-l\", \"a.txt\"]"}}}
COVERS = ("run script", "run code", "run command", "execute script", "execute code", "run python")


def run(call, args):
    a = call.agent
    script = args.get("script")
    if not script and "```" in call.content:
        script = codeblocks.single(call.content, "script.py")[0]
    if bool(script) == bool(args.get("command")):
        return "Give a script (a fenced python block in the reply) or a command (argv), not both."
    argv = list(args.get("command") or ["python", "/work/" + SCRIPT])
    try:
        a.sandbox.check_argv(argv)
    except PolicyError as e:
        return f"Not run: {e}"
    a.calls += 1
    box = os.path.join(a.run_dir, "calls", f"{a.calls:03d}-{NAME}")
    os.makedirs(box)
    try:
        ws.copy_in(a.folder, box, list(args.get("files") or []), outside=a.attachments)
        if script:
            os.makedirs(os.path.join(box, ws.META), exist_ok=True)
            open(os.path.join(box, SCRIPT), "w").write(script)
        res = a.sandbox.run(argv, box, purpose=NAME)
        items, refused = ws.changes(a.folder, box)
        written = a._change([rel for rel, _ in items], lambda: ws.export(a.folder, items)) if items else []
    except (OSError, PolicyError) as e:
        return f"{NAME} failed: {e}"
    finally:
        shutil.rmtree(box, ignore_errors=True)
    ok = res["exit_code"] == 0
    a.last_script = {"script": script, "command": args.get("command"), "files": args.get("files") or [], "ok": ok}
    text = f"exit {res['exit_code']}" + (" (timed out)" if res.get("timed_out") else "") + "\n" + show(res["output"], 4000)
    if written:
        text += "\nFiles written to the folder: " + ", ".join(written)
    if refused:
        text += "\nNot written: " + "; ".join(f"{rel} ({why})" for rel, why in refused)
    if ok and script:
        text += ("\nIf this script is worth keeping for later tasks, call need_tool with from_run_code true: it becomes "
                 "the starting point of a tool.")
    return text
