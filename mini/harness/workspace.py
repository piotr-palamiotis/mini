"""The user's folder as the agent sees it. Stdlib only.

mini shares the folder it was launched in, read-only, as /host (to the agent and to tools in the sandbox). Nothing
is ever written there by a run: changes are written to out/<same relative path> in the run's scratch folder, and
`mini apply` (or /apply in the UI) shows them as diffs and copies them into the folder after the user says yes.
"""
import difflib
import json
import os
import shutil

from .sandbox import PolicyError

MOUNT = "/host"
MAX_LINES = 60                                  # lines of one listing
MAX_CHARS = 4000                                # and characters (about 1,000 tokens)
PER_DIR = 12                                    # entries shown per subfolder in a deeper listing
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mini"}


def resolve(cfg, cwd=None):
    """(folder, note): the folder to share, or (None, why not). The launch folder is the consent, so folders that
    are too broad to share ([workspace] refuse: the home folder, /) are not."""
    ws = cfg["workspace"]
    if not ws["enabled"]:
        return None, ""
    d = os.path.realpath(cwd or os.getcwd())
    if d in {os.path.realpath(os.path.expanduser(p)) for p in ws["refuse"]}:
        return None, f"{d} is too broad to share with the agent; start mini inside a project folder to give it that folder"
    return d, ""


def host_path(workspace, p):
    """A /host/... path (or /host) as a real path inside the shared folder. PolicyError for anything outside it,
    symlinks that lead out included."""
    p = str(p or "")
    if workspace is None or not (p == MOUNT or p.startswith(MOUNT + "/")):
        raise PolicyError(f"not a shared path: {p!r}" if workspace else "no folder is shared in this run")
    root = os.path.realpath(workspace)
    full = os.path.realpath(os.path.join(root, p[len(MOUNT):].lstrip("/")))
    if full != root and not full.startswith(root + os.sep):
        raise PolicyError(f"path outside the shared folder: {p}")
    return full


def list_dir(workspace, p, depth=1):
    """Compact listing of a shared folder (see tree)."""
    full = host_path(workspace, p)
    if not os.path.isdir(full):
        raise PolicyError(f"{p} is not a folder")
    return tree(full, p, depth)


def tree(full, label, depth=1):
    """A short, bounded listing of a folder: names without their path, folders with their item count, files with
    their size. depth 1 is the folder itself; 2 and 3 add subfolders (at most PER_DIR entries each). Never more than
    MAX_LINES lines or MAX_CHARS characters, so one call cannot fill the model's context."""
    depth = max(1, min(int(depth or 1), 3))
    lines = []

    def entries(d):
        try:
            names = os.listdir(d)
        except OSError:
            return []
        return sorted(names, key=lambda n: (not os.path.isdir(os.path.join(d, n)) or os.path.islink(os.path.join(d, n)),
                                            n.lower()))

    def walk(d, level, indent):
        names = entries(d)
        shown = names if level == 1 else names[:PER_DIR]
        for n in shown:
            fp = os.path.join(d, n)
            if os.path.islink(fp):
                lines.append(f"{indent}{n} (link)")
            elif os.path.isdir(fp):
                kids = len(entries(fp))
                skip = n in SKIP_DIRS
                lines.append(f"{indent}{n}/ ({kids} items{', not expanded' if skip else ''})")
                if level < depth and not skip:
                    walk(fp, level + 1, indent + "  ")
            else:
                lines.append(f"{indent}{n} ({_size(os.path.getsize(fp))})")
        if len(names) > len(shown):
            lines.append(f"{indent}... +{len(names) - len(shown)} more")

    top = entries(full)
    folders = sum(1 for n in top if os.path.isdir(os.path.join(full, n)) and not os.path.islink(os.path.join(full, n)))
    walk(full, 1, "")
    head = f"{label}: {folders} folders, {len(top) - folders} files" + (f" (depth {depth})" if depth > 1 else "")
    body, used = [], len(head)
    for ln in lines:
        if len(body) >= MAX_LINES or used + len(ln) > MAX_CHARS:
            break
        body.append(ln)
        used += len(ln) + 1
    hidden = len(lines) - len(body)
    if hidden:
        body.append(f"[... {hidden} more lines hidden. List a subfolder to see it, e.g. path={label.rstrip('/')}/NAME]")
    return head + "\n" + ("\n".join(body) if body else "(empty)")


def _size(n):
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


# ---------------------------------------------------------------- applying a run's changes
def run_workspace(run_dir):
    """The folder a run shared, from its log, or None."""
    try:
        with open(os.path.join(run_dir, "log.jsonl")) as f:
            for line in f:
                ev = json.loads(line)
                if ev.get("kind") == "run_start":
                    return ev.get("workspace")
    except (OSError, ValueError):
        pass
    return None


def pending(run_dir):
    """[(relative path, status, diff)] for the files a run put in out/ that differ from the shared folder.
    status: new | changed. Files identical to the folder's are left out."""
    ws, out_dir = run_workspace(run_dir), os.path.join(run_dir, "work", "out")
    if not ws or not os.path.isdir(out_dir):
        return []
    items = []
    for d, dirs, files in os.walk(out_dir):
        for name in sorted(files):
            src = os.path.join(d, name)
            rel = os.path.relpath(src, out_dir)
            try:
                dest = target(ws, rel)
            except PolicyError as e:
                items.append((rel, "refused", str(e)))
                continue
            new = _text(src)
            if not os.path.exists(dest):
                items.append((rel, "new", f"new file, {os.path.getsize(src)} bytes"))
            elif _bytes(src) != _bytes(dest):
                old = _text(dest)
                diff = ("binary file differs" if new is None or old is None else "".join(
                    difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"{rel} (now)", f"{rel} (after)")))
                items.append((rel, "changed", diff))
    return items


def target(ws, rel):
    root = os.path.realpath(ws)
    full = os.path.realpath(os.path.join(root, rel))
    if full != root and not full.startswith(root + os.sep):
        raise PolicyError(f"{rel} leads outside {ws}")
    return full


def apply(run_dir):
    """Copy the pending files into the shared folder. Returns the relative paths written."""
    ws, done = run_workspace(run_dir), []
    for rel, status, _ in pending(run_dir):
        if status == "refused":
            continue
        dest = target(ws, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(os.path.join(run_dir, "work", "out", rel), dest)
        done.append(rel)
    return done


def _bytes(path):
    with open(path, "rb") as f:
        return f.read()


def _text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (UnicodeDecodeError, OSError):
        return None


def latest_run_with_changes(runs_dir):
    """Newest run folder that shared a folder and left something to apply, or None."""
    for name in sorted(os.listdir(runs_dir), reverse=True) if os.path.isdir(runs_dir) else []:
        rd = os.path.join(runs_dir, name)
        if pending(rd):
            return rd
    return None
