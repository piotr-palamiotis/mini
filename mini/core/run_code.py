"""Run a script or a command in the sandbox against the files named: the workbench where tools come from.

The sandbox starts from the base image (Python and its standard library). A run that needs packages names them
(pip or apt); the user approves new ones once per project and they are kept in .mini/packages.json, so every later
run in the folder gets the same image. Each successful script run is recorded (script, inputs it saw, files it
wrote, output) so it can be kept as a tool whose test is that run (keep_tool)."""
import json
import os
import re
import shutil

from ._files import show
from ..agent import codeblocks
from ..harness import workspace as ws
from ..harness.sandbox import PolicyError, repair_argv, validate_packages

NAME = "run_code"
SCRIPT = ws.META + "/script.py"                   # where the script sits in the call's sandbox folder
PACKAGES_FILE = ws.META + "/packages.json"        # the project's approved packages (in the user's folder)
DESCRIPTION = ("Run a Python script (the fenced python block in this reply, or script) or a command (an argv list, "
               "no shell) once in the sandbox, with no network. The sandbox has Python and its standard library; "
               "packages: the pip or apt packages the run needs (e.g. {\"pip\": [\"pyyaml\", \"pillow\"]}, "
               "{\"apt\": [\"g++\"]}): the user approves new ones once for this folder and later runs have them "
               "too. files: the files and folders it works on (default: the whole project when it is small); they are "
               "copied in at their relative paths (the working directory), and every file it writes there lands at "
               "the same path in the user's folder. Do the task's processing here; a script worth keeping for later "
               "tasks becomes a tool with keep_tool.")
ARR = {"type": "array", "items": {"type": "string"}}
SCHEMA = {"type": "object", "required": [], "properties": {
    "files": {"type": "array", "items": {"type": "string"}, "description": "paths relative to the user's folder"},
    "script": {"type": "string", "description": "only for a short script; prefer the fenced block"},
    "command": {"type": "array", "items": {"type": "string"}, "description": "argv, e.g. [\"wc\", \"-l\", \"a.txt\"]"},
    "packages": {"type": "object", "properties": {"pip": ARR, "apt": ARR},
                 "description": "packages this run needs beyond the standard library"}}}
COVERS = ("run script", "run code", "run command", "execute script", "execute code", "run python")
RECORD_MAX = 50 * 2**20                           # a run's inputs are kept for keep_tool up to this size


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
    """A package the run lacks: name it for the packages input, and an installed tool that already has it."""
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
    text = (f"\n{module} is not installed. Run again with packages {{\"pip\": [\"{pkg}\"]}} (the user approves it once "
            f"for this folder; pip install inside the script cannot work: there is no network).")
    if have:
        text += f" The installed tool {have[0]} already has it: `invoke_tool` with name {have[0]!r} may do the step."
    return text


def project_packages(folder):
    try:
        return validate_packages(json.load(open(os.path.join(folder, PACKAGES_FILE))))
    except (OSError, ValueError, PolicyError):
        return {"apt": [], "pip": []}


def ensure_packages(a, wanted):
    """The image for the project's packages plus wanted. New ones need the user's approval; approved ones are saved
    for the folder. Returns (image, problem)."""
    have = project_packages(a.folder)
    if wanted and not isinstance(wanted, dict):
        return None, 'packages is an object: {"pip": [...], "apt": [...]}'
    wanted = {str(k).strip().strip("'\"").strip().lower(): v for k, v in (wanted or {}).items()}
    odd = [k for k in wanted if k not in ("pip", "apt")]
    if odd:                         # an unknown key was silently ignored before: nothing got installed
        return None, f'packages takes the keys "pip" and "apt", not {", ".join(map(repr, odd))}'
    try:
        want = validate_packages(wanted or {})
    except PolicyError as e:
        return None, str(e)
    new = {k: [p for p in want[k] if p not in have[k]] for k in ("apt", "pip")}
    if new["apt"] or new["pip"]:
        d = a.gate.request("permissions", "run_code needs packages for this folder", {
            "pip packages (new)": new["pip"] or "-", "apt packages (new)": new["apt"] or "-",
            "for": "scripts the agent runs in the sandbox for this folder; kept for later runs here"})
        if not d.approved:
            return None, "the user refused the packages " + json.dumps(new)
        have = {k: sorted(set(have[k]) | set(new[k])) for k in ("apt", "pip")}
        try:
            image = a.sandbox.ensure_image(have)
        except PolicyError as e:
            return None, f"installing the packages failed: {str(e)[-1500:]}"
        os.makedirs(os.path.join(a.folder, ws.META), exist_ok=True)
        json.dump(have, open(os.path.join(a.folder, PACKAGES_FILE), "w"), indent=1)
        a.log.emit("packages", scope="project", packages=have, added=new)
        return image, ""
    try:
        return a.sandbox.ensure_image(have), ""
    except PolicyError as e:
        return None, f"installing the packages failed: {str(e)[-1500:]}"


def _script_of(argv, box):
    """The source of a .py file that argv runs with python (in the sandbox folder), with the arguments noted."""
    if len(argv) >= 2 and os.path.basename(argv[0]).startswith("python") and argv[1].endswith(".py"):
        p = os.path.join(box, argv[1].replace("/work/", "", 1).lstrip("/"))
        if os.path.isfile(p):
            return f"# run as: {' '.join(argv)}\n" + open(p).read()
    return None


def _record(a, box, n):
    """Keep what the run saw (its working directory before it ran) so a kept tool can be tested on it."""
    dest = os.path.join(a.run_dir, "scripts", f"{n:03d}")
    size = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(box) for f in fs)
    if size > RECORD_MAX:
        return None
    shutil.copytree(box, os.path.join(dest, "in"), ignore=shutil.ignore_patterns(ws.META))
    return dest


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
    image, problem = ensure_packages(a, args.get("packages"))
    if problem:
        return f"Not run: {problem}"
    a.calls += 1
    box = os.path.join(a.run_dir, "calls", f"{a.calls:03d}-{NAME}")
    os.makedirs(box)
    record = None
    try:
        names = list(args.get("files") or [])
        if not names and small_project(a.folder):
            names = ["."]                         # a small model often forgets files; the sandbox must not start empty
        ws.copy_in(a.folder, box, names, outside=a.attachments)
        if script:
            os.makedirs(os.path.join(box, ws.META), exist_ok=True)
            open(os.path.join(box, SCRIPT), "w").write(script)
        if not script:                            # python path/to/x.py args: the file is the script
            ran = _script_of(argv, box)
            if ran:
                script, record = ran, _record(a, box, a.calls)
        elif script:
            record = _record(a, box, a.calls)
        res = a.sandbox.run(argv, box, purpose=NAME, image=image)
        items, refused = ws.changes(a.folder, box)
        if record:
            for rel, src in items:
                os.makedirs(os.path.dirname(os.path.join(record, "out", rel)), exist_ok=True)
                shutil.copyfile(src, os.path.join(record, "out", rel))
        made = ws.new_dirs(a.folder, box)
        written = a._change([rel for rel, _ in items], lambda: ws.export(a.folder, items)) if items else []
        made = ws.make_dirs(a.folder, made)
    except (OSError, PolicyError) as e:
        return f"{NAME} failed: {e}"
    finally:
        shutil.rmtree(box, ignore_errors=True)
    ok = res["exit_code"] == 0
    a.last_script = {"script": script, "command": args.get("command"), "files": args.get("files") or [], "ok": ok,
                     "packages": project_packages(a.folder), "record": record if ok else None,
                     "written": [rel for rel, _ in items], "output": res["output"][-4000:]}
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
    if ok and script and record:
        text += ("\nIf this script is worth keeping for later tasks, call `keep_tool`: it becomes a tool, tested on this "
                 "run.")
    return text
