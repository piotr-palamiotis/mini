"""Run a script or a command in the sandbox against the files named: the workbench where tools come from.

The sandbox starts from the base image (Python and its standard library). A run that needs packages names them
(pip or apt); the user approves new ones once per project and they are kept in .mini/packages.json, so every later
run in the folder gets the same image. Each successful script run is recorded (script, inputs it saw, files it
wrote, output) so it can be kept as a tool whose test is that run (keep_tool)."""
import ast
import hashlib
import json
import os
import re
import shutil
import sys

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


# shared libraries a pip package loads at import, by the name the error gives: the apt package of the sandbox's base
# image (Debian 13, where many library packages carry a t64 suffix)
APT_LIBS = {"zbar": "libzbar0t64", "libzbar": "libzbar0t64", "libgl": "libgl1", "libglib-2.0": "libglib2.0-0t64",
            "libgthread-2.0": "libglib2.0-0t64", "libxcb": "libxcb1", "libsm": "libsm6", "libxext": "libxext6",
            "libxrender": "libxrender1", "libgomp": "libgomp1", "libmagic": "libmagic1t64", "libcairo": "libcairo2",
            "libpango-1.0": "libpango-1.0-0", "libsndfile": "libsndfile1", "libgdal": "libgdal36",
            "libpq": "libpq5", "libzmq": "libzmq5", "libtesseract": "libtesseract5", "libdmtx": "libdmtx0t64",
            "libexpat": "libexpat1", "libfontconfig": "libfontconfig1", "libfreetype": "libfreetype6"}
X_LIBS = ("libgl", "libxcb", "libsm", "libxext", "libxrender", "libgthread-2.0", "libglib-2.0")


def _library_hint(output):
    """A pip package that needs a system library the image lacks: name the apt package (or a pip build without it)."""
    m = (re.search(r"([\w.+-]+?)\.so(?:\.[\d.]+)?: cannot open shared object file", output)
         or re.search(r"Unable to find (\w+) shared library", output)
         or re.search(r"(?:could not|cannot|unable to) (?:find|load) (?:the )?(?:library )?['\"]?(lib[\w.+-]+?)(?:\.so)?['\"]?\b",
                      output, re.I))
    if not m:
        return ""
    lib = m.group(1).lower()
    pkg = APT_LIBS.get(lib) or APT_LIBS.get(re.sub(r"[\d.]+$", "", lib))
    text = (f"\nThe system library {m.group(1)} is missing: pip installs Python packages only. Run again with packages "
            f"{{\"apt\": [\"{pkg}\"]}} (packages approved before stay installed)" if pkg else
            f"\nThe system library {m.group(1)} is missing: pip installs Python packages only. Add its apt package to "
            f"packages ({{\"apt\": [...]}}); the sandbox is Debian 13, where library packages are often named with a t64 "
            f"suffix (e.g. libzbar0t64)")
    if lib in X_LIBS or re.sub(r"[\d.]+$", "", lib) in X_LIBS:
        text += "; for OpenCV, pip opencv-python-headless instead of opencv-python needs no display libraries"
    return text + ". Or use a pure-Python package that needs no system library."


# imports the script's third-party modules (and each package's direct submodules) in the run's image: an import that
# fails there (a missing system library, a missing package) is named even when the script caught the error
PROBE = r"""import importlib, json, pkgutil, sys
out, subs = {}, {}
def load(m, into):
    try:
        importlib.import_module(m)
        return True
    except BaseException as e:
        into[m] = (type(e).__name__ + ": " + str(e))[:400]
for m in json.loads(sys.argv[1]):
    if load(m, out) and "." not in m:
        names = [s.name for s in pkgutil.iter_modules(getattr(sys.modules[m], "__path__", None) or [])
                 if not s.name.startswith("_") and "test" not in s.name]
        for s in names[:30]:
            load(m + "." + s, subs)
print("MINI_PROBE " + json.dumps({"direct": out, "sub": subs}))
"""


def _imports(script, box):
    """The modules a script imports that are neither the standard library nor files beside it."""
    try:
        tree = ast.parse(script)
    except (SyntaxError, ValueError):
        return []
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {x.name for x in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
            mods.add(n.module)
    local = {os.path.splitext(f)[0] for f in os.listdir(box)} if os.path.isdir(box) else set()
    return sorted(m for m in mods if m.split(".")[0] not in sys.stdlib_module_names and m.split(".")[0] not in local
                  and m.split(".")[0] != "mini_io")


def _probe_imports(a, script, box, image, output):
    """Imports that fail in the image although the run's output does not show it: (module, error) pairs."""
    mods = _imports(script, box)
    cache = a.__dict__.setdefault("import_probe", {})
    todo = [m for m in mods if (image, m) not in cache]
    if todo:
        d = os.path.join(box, ws.META)
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "probe.py"), "w").write(PROBE)
        try:
            res = a.sandbox.run(["python", "/work/" + ws.META + "/probe.py", json.dumps(todo)], box,
                                purpose="run_code import check", image=image, timeout=120)
            found = re.findall(r"^MINI_PROBE (.*)$", res["output"], re.M)
            got = json.loads(found[-1]) if found else None
        except (OSError, PolicyError, ValueError):
            got = None
        if got is None:
            return []
        for m in todo:
            subs = {k: v for k, v in got["sub"].items() if k.startswith(m + ".") and _library_hint(v)}
            cache[(image, m)] = ([(m, got["direct"][m])] if m in got["direct"] else []) + sorted(subs.items())
        a.log.emit("import_probe", image=image, modules=todo, failed={m: cache[(image, m)] for m in todo})
    bad = [(m, e) for x in mods for m, e in cache.get((image, x), [])]
    return [(m, e) for m, e in dict(bad).items() if e.split(": ", 1)[-1][:80] not in output]


def _probe_note(a, failed):
    by_err = {}
    for m, err in failed:
        by_err.setdefault(err, []).append(m)
    text = ""
    for err, mods in list(by_err.items())[:3]:
        text += f"\nIn the sandbox, importing {' and '.join(mods)} fails: {err} (the script's output does not show it)."
        miss = re.search(r"No module named '([\w.]+)'", err)
        text += _library_hint(err) or (_module_hint(a, miss.group(1)) if miss else "")
    return text


def _same_results(a, script, box, touched):
    """Files this run wrote with content that earlier, different scripts wrote too: (path, how many scripts, text).
    A fix that keeps producing the same file has not reached the cause."""
    seen = a.__dict__.setdefault("results_seen", {})            # (path, content hash) -> script hashes
    sh = hashlib.sha256(script.encode()).hexdigest()
    out = []
    for rel in touched:
        try:
            data = open(os.path.join(box, rel), "rb").read()
        except OSError:
            continue
        try:
            head = data[:4000].decode("utf-8")[:300]
        except UnicodeDecodeError:
            head = None                                         # binary: rewritten unchanged, never "stuck"
        scripts = seen.setdefault((rel, hashlib.sha256(data).hexdigest()), set())
        scripts.add(sh)
        out.append((rel, len(scripts) if head is not None else 0, head))
    return out


def _same_note(a, same, written):
    limit = a.cfg["watchdogs"].get("same_result_scripts", 3)
    text = ""
    unchanged = [rel for rel, n, _ in same if rel not in written]
    if unchanged:
        text += "\nWritten with the same content the folder already had (unchanged): " + ", ".join(unchanged)
    stuck = [(rel, n, head) for rel, n, head in same if n >= limit]
    if stuck:
        rel, n, head = stuck[0]
        a.log.emit("watchdog", name="same_result", path=rel, scripts=n, limit=limit)
        text += (f"\n{n} different scripts have now written exactly the same {rel}:\n{head}\nIf that is not what the "
                 f"task asks for, the changes are not reaching the cause. Before changing the script again, run a short "
                 f"script on one input that prints repr() of each intermediate value (what each library call returns, "
                 f"before you index, convert or format it), and compare it with what the task expects.")
    return text


def project_packages(folder):
    try:
        return validate_packages(json.load(open(os.path.join(folder, PACKAGES_FILE))))
    except (OSError, ValueError, PolicyError):
        return {"apt": [], "pip": []}


def _pip_name(p):
    return re.split(r"[<>=!~\[ ;]", str(p), 1)[0].strip().lower()


def _headless(have, want):
    """A pip package and its -headless build cannot share an image (both install the same module): the headless one
    wins, so a run that asks for it no longer gets the other from the folder's earlier packages. Returns (have, want,
    removed names)."""
    heads = {_pip_name(p)[:-len("-headless")] for p in have["pip"] + want["pip"] if _pip_name(p).endswith("-headless")}
    gone = sorted({_pip_name(p) for p in have["pip"] + want["pip"] if _pip_name(p) in heads})
    if not gone:
        return have, want, []
    keep = lambda ps: [p for p in ps if _pip_name(p) not in heads]
    return {**have, "pip": keep(have["pip"])}, {**want, "pip": keep(want["pip"])}, gone


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
    have, want, swapped = _headless(have, want)
    new = {k: [p for p in want[k] if p not in have[k]] for k in ("apt", "pip")}
    if swapped and not (new["apt"] or new["pip"]):
        json.dump(have, open(os.path.join(a.folder, PACKAGES_FILE), "w"), indent=1)
        a.log.emit("packages", scope="project", packages=have, removed=swapped)
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
            return None, f"installing the packages failed: {str(e)[-1500:]}" + _apt_note(str(e))
        os.makedirs(os.path.join(a.folder, ws.META), exist_ok=True)
        json.dump(have, open(os.path.join(a.folder, PACKAGES_FILE), "w"), indent=1)
        a.log.emit("packages", scope="project", packages=have, added=new)
        return image, ""
    try:
        return a.sandbox.ensure_image(have), ""
    except PolicyError as e:
        return None, f"installing the packages failed: {str(e)[-1500:]}"


def _apt_note(err):
    gone = re.findall(r"Unable to locate package ([\w.+-]+)", err) + re.findall(
        r"Package '?([\w.+-]+)'? has no installation candidate", err)
    if not gone:
        return ""
    return (f"\nNo apt package named {', '.join(sorted(set(gone)))} in the sandbox's Debian 13: many library packages "
            f"there end in t64 (libzbar0 is libzbar0t64, libglib2.0-0 is libglib2.0-0t64). Retry with the Debian 13 name.")


def _script_of(argv, box):
    """The source of a .py file that argv runs with python (in the sandbox folder), with the arguments noted."""
    if len(argv) >= 2 and os.path.basename(argv[0]).startswith("python") and argv[1].endswith(".py"):
        p = os.path.join(box, argv[1].replace("/work/", "", 1).lstrip("/"))
        if os.path.isfile(p):
            return f"# run as: {' '.join(argv)}\n" + open(p).read()
    return None


def _stamps(box):
    """(mtime, size) of every file in the sandbox folder but mini's own, by relative path."""
    out = {}
    for d, dirs, fs in os.walk(box):
        dirs[:] = [x for x in dirs if os.path.relpath(os.path.join(d, x), box) != ws.META]
        for f in fs:
            st = os.stat(os.path.join(d, f))
            out[os.path.relpath(os.path.join(d, f), box)] = (st.st_mtime_ns, st.st_size)
    return out


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
    record, touched, hidden, same = None, [], [], []
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
        before = _stamps(box)
        res = a.sandbox.run(argv, box, purpose=NAME, image=image)
        items, refused = ws.changes(a.folder, box)
        after = _stamps(box)            # what it wrote, even when that equals the folder's file (a rerun)
        touched = sorted(r for r, st in after.items() if before.get(r) != st)
        if record:
            for rel in touched:
                os.makedirs(os.path.dirname(os.path.join(record, "out", rel)), exist_ok=True)
                shutil.copyfile(os.path.join(box, rel), os.path.join(record, "out", rel))
        made = ws.new_dirs(a.folder, box)
        written = a._change([rel for rel, _ in items], lambda: ws.export(a.folder, items)) if items else []
        made = ws.make_dirs(a.folder, made)
        if script:
            same = _same_results(a, script, box, [r for r in touched if r not in {x for x, _ in refused}])
        if script and not re.search(r"No module named|cannot open shared object|shared library", res["output"]):
            hidden = _probe_imports(a, script, box, image, res["output"])
    except (OSError, PolicyError) as e:
        return f"{NAME} failed: {e}"
    finally:
        shutil.rmtree(box, ignore_errors=True)
    ok = res["exit_code"] == 0
    a.last_script = {"script": script, "command": args.get("command"), "files": args.get("files") or [], "ok": ok,
                     "packages": project_packages(a.folder), "record": record if ok else None,
                     "written": touched, "output": res["output"][-4000:]}
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
    elif not ok:
        text += _library_hint(res["output"])
    text += _probe_note(a, hidden)
    text += _same_note(a, same, written)
    if ok and script and record:
        text += ("\nIf this script is worth keeping for later tasks, call `keep_tool`: it becomes a tool, tested on this "
                 "run.")
    return text
