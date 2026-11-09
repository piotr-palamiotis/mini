"""Run a script or a command in the sandbox against the files named: the cheap path before building a tool."""
import os
import re
import shutil

from ._files import show
from ..agent import codeblocks
from ..harness import workspace as ws
from ..harness.sandbox import PolicyError, repair_argv

NAME = "run_code"
SCRIPT = ws.META + "/script.py"                   # where the script sits in the call's sandbox folder
DESCRIPTION = ("Run a Python script (the fenced python block in this reply, or script) or a command (an argv list, "
               "no shell) once in the sandbox, with no network. files: the files and folders it works on (default: the whole project when it is small); they are "
               "copied in at their relative paths (the working directory), and every file it writes there lands at "
               "the same path in the user's folder. Use it for exact processing this task needs; build a tool only "
               "for what is worth keeping.")
SCHEMA = {"type": "object", "required": [], "properties": {
    "files": {"type": "array", "items": {"type": "string"}, "description": "paths relative to the user's folder"},
    "script": {"type": "string", "description": "only for a short script; prefer the fenced block"},
    "command": {"type": "array", "items": {"type": "string"}, "description": "argv, e.g. [\"wc\", \"-l\", \"a.txt\"]"}}}
COVERS = ("run script", "run code", "run command", "execute script", "execute code", "run python")


SMALL_FILES, SMALL_BYTES = 200, 20 * 2**20       # a project this small is copied in when the call names no files


def small_project(folder):
    n = size = 0
    for d, dirs, files in os.walk(folder):
        dirs[:] = [x for x in dirs if x not in ws.SKIP_DIRS and not os.path.islink(os.path.join(d, x))]
        for f in files:
            n += 1
            size += os.path.getsize(os.path.join(d, f))
            if n > SMALL_FILES or size > SMALL_BYTES:
                return False
    return True


IMPORT_NAMES = {"pil": "pillow", "yaml": "pyyaml", "cv2": "opencv-python", "sklearn": "scikit-learn", "bs4": "beautifulsoup4",
                "fitz": "pymupdf", "docx": "python-docx", "pptx": "python-pptx", "dateutil": "python-dateutil"}


def _module_hint(a, module):
    """run_code's sandbox has the standard library only. A tool that needs a package gets its own image: name the
    installed tools that have this one, built ones first."""
    top = module.split(".")[0].lower()
    pkg = IMPORT_NAMES.get(top, top)
    built = getattr(a, "built_names", None) or []
    have = []
    for t in a.registry.names():
        pips = [str(p).lower().split("=")[0].split("<")[0].split(">")[0].strip()
                for p in (((a.registry.get(t) or {}).get("permissions") or {}).get("packages") or {}).get("pip") or []]
        if pkg in pips or top in pips:
            have.append(t)
    have.sort(key=lambda t: (t not in built, t))
    if have:
        return (f"\n{module} is not in run_code's sandbox (standard library only), and it cannot be installed there. "
                f"The installed tool {have[0]} has it: run it with `invoke_tool` (name {have[0]!r}).")
    return (f"\n{module} is not in run_code's sandbox (standard library only), and it cannot be installed there. "
            f"Work that needs it is a tool: call need_tool, and the tool gets an image with the package.")


def run(call, args):
    a = call.agent
    script = args.get("script")
    if not script and "```" in call.content:
        script = codeblocks.single(call.content, "script.py")[0]
    if bool(script) == bool(args.get("command")):
        return "Give a script (a fenced python block in the reply) or a command (argv), not both."
    argv = repair_argv(list(args.get("command") or ["python", "/work/" + SCRIPT]))
    try:
        a.sandbox.check_argv(argv)
    except PolicyError as e:
        return f"Not run: {e}"
    a.calls += 1
    box = os.path.join(a.run_dir, "calls", f"{a.calls:03d}-{NAME}")
    os.makedirs(box)
    try:
        names = list(args.get("files") or [])
        if not names and small_project(a.folder):
            names = ["."]                         # a small model often forgets files; the sandbox must not start empty
        ws.copy_in(a.folder, box, names, outside=a.attachments)
        if script:
            os.makedirs(os.path.join(box, ws.META), exist_ok=True)
            open(os.path.join(box, SCRIPT), "w").write(script)
        res = a.sandbox.run(argv, box, purpose=NAME)
        items, refused = ws.changes(a.folder, box)
        made = ws.new_dirs(a.folder, box)
        written = a._change([rel for rel, _ in items], lambda: ws.export(a.folder, items)) if items else []
        made = ws.make_dirs(a.folder, made)
    except (OSError, PolicyError) as e:
        return f"{NAME} failed: {e}"
    finally:
        shutil.rmtree(box, ignore_errors=True)
    ok = res["exit_code"] == 0
    a.last_script = {"script": script, "command": args.get("command"), "files": args.get("files") or [], "ok": ok}
    text = f"exit {res['exit_code']}" + (" (timed out)" if res.get("timed_out") else "") + "\n" + show(res["output"], 4000)
    if written:
        text += "\nFiles written to the folder: " + ", ".join(written)
    if made:
        text += "\nFolders made in the folder: " + ", ".join(d + "/" for d in made)
    if ok and not res["output"].strip() and not written and not refused and not made:
        text += ("\nThe script ran but printed nothing and wrote no file. Check that it calls the function it defines, "
                 "prints what it finds, and writes the file it should produce.")
    if refused:
        text += "\nNot written: " + "; ".join(f"{rel} ({why})" for rel, why in refused)
    missing = re.search(r"No module named '([\w.]+)'", res["output"]) if not ok else None
    if missing:
        text += _module_hint(a, missing.group(1))
    if ok and script and not getattr(a, "built_names", None):
        text += ("\nIf this script is worth keeping for later tasks, call need_tool with from_run_code true: it becomes "
                 "the starting point of a tool.")
    return text
