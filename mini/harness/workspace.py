"""The user's folder: the folder mini is launched in is the workspace. Stdlib only.

The agent's own file actions work on the folder directly, with paths relative to it. A tool never sees the folder:
for each call the harness copies the files the call names into a fresh sandbox folder (same relative paths), and
afterwards exports whatever the tool wrote back to the same relative path in the folder. Every path is confined to
the folder, symlinks included, and nothing under .git is ever copied in or written. Changes are checkpointed in git
first (checkpoint.py), so a task can be undone.
"""
import hashlib
import os
import shutil

from .sandbox import PolicyError

MAX_LINES = 60                                  # lines of one listing
MAX_CHARS = 4000                                # and characters (about 1,000 tokens)
PER_DIR = 12                                    # entries shown per subfolder in a deeper listing
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mini"}
META = ".mini"                                  # the runner's files in the sandbox folder; never exported
STAGED = META + "/in"                           # files from outside the folder (attachments) as the tool sees them
MAX_COPY_FILES = 5000                           # one call copies at most this many files
MAX_COPY_BYTES = 500 * 1024 * 1024              # and this many bytes into the sandbox


def resolve(cfg, cwd=None):
    """(folder, note): the folder mini works in, or (None, why not). The launch folder is the consent, so folders
    that are too broad ([workspace] refuse: the home folder, /) are not."""
    d = os.path.realpath(cwd or os.getcwd())
    if d in {os.path.realpath(os.path.expanduser(p)) for p in cfg["workspace"]["refuse"]}:
        return None, f"{d} is too broad to work in; start mini inside a project folder"
    return d, ""


def confine(folder, p):
    """(real path, path relative to the folder) for a path in the folder (relative, or absolute inside it).
    PolicyError for anything outside it, symlinks that lead out included, and for anything inside a .git folder."""
    root = os.path.realpath(folder)
    p = os.path.expanduser(str(p or "."))
    full = os.path.realpath(p if os.path.isabs(p) else os.path.join(root, p))
    if full != root and not full.startswith(root + os.sep):
        raise PolicyError(f"path outside the folder: {p}")
    rel = os.path.relpath(full, root)
    if ".git" in rel.split(os.sep):
        raise PolicyError(f"{p} is inside .git; mini never touches it")
    return full, rel


def list_dir(folder, p, depth=1):
    """Compact listing of a folder in the workspace (see tree)."""
    full, _ = confine(folder, p)
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


# ---------------------------------------------------------------- copy in, export out
def copy_in(folder, box, value, outside=()):
    """Copy the files a call names into the sandbox folder box. value is the call's inputs (any JSON); every string
    that names a file or folder in the workspace is copied to the same relative path and becomes that relative path.
    Files outside the workspace are copied only when listed in outside (real paths; True: any file), to .mini/in/.
    Returns the inputs as the tool sees them."""
    budget = [MAX_COPY_FILES, MAX_COPY_BYTES]

    def one(v):
        if isinstance(v, dict):
            return {k: one(x) for k, x in v.items()}
        if isinstance(v, list):
            return [one(x) for x in v]
        if not isinstance(v, str) or not v.strip() or "\n" in v or len(v) > 1024:
            return v
        p = os.path.expanduser(v)
        try:
            full, rel = confine(folder, p)
        except PolicyError:
            if os.path.isabs(p) and os.path.isfile(p) and (outside is True or os.path.realpath(p) in outside):
                return stage(box, p)
            if os.path.lexists(p if os.path.isabs(p) else os.path.join(folder, p)):
                raise
            return v                                # not a path: a value like any other
        if not os.path.exists(full):                # an output path: an absolute one becomes relative
            parent = os.path.dirname(rel)
            if parent and os.path.isdir(os.path.join(folder, parent)):     # its folder exists for the tool too
                os.makedirs(os.path.normpath(os.path.join(box, parent)), exist_ok=True)
            return rel if os.path.isabs(p) else v
        _copy(full, os.path.normpath(os.path.join(box, rel)), budget)       # "." copies the whole folder
        return rel
    return one(value)


def _copy(src, dest, budget):
    if os.path.isdir(src):
        for d, dirs, files in os.walk(src):
            dirs[:] = [x for x in dirs if x not in (".git", META) and not os.path.islink(os.path.join(d, x))]
            for f in files:
                s = os.path.join(d, f)
                if not os.path.islink(s):           # links inside a copied folder are left out
                    _copy(s, os.path.join(dest, os.path.relpath(s, src)), budget)
        return
    budget[0] -= 1
    budget[1] -= os.path.getsize(src)
    if budget[0] < 0 or budget[1] < 0:
        raise PolicyError(f"the call names too much to copy into the sandbox (more than {MAX_COPY_FILES} files or "
                          f"{MAX_COPY_BYTES // 2**20} MB); name the files it needs")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copyfile(src, dest)


def stage(box, src):
    """Copy a file from outside the workspace into box/.mini/in/ and return the path the tool sees."""
    os.makedirs(os.path.join(box, STAGED), exist_ok=True)
    shutil.copyfile(src, os.path.join(box, STAGED, os.path.basename(src)))
    return STAGED + "/" + os.path.basename(src)


def changes(folder, box):
    """([(relative path, sandbox file)] the tool wrote that differ from the workspace, [(path, why)] refused).
    .mini/ is the runner's and never exported; a symlink the tool made is never followed."""
    out, refused = [], []
    for d, dirs, files in os.walk(box):
        if os.path.relpath(d, box) == ".":
            dirs[:] = [x for x in dirs if x != META]
        for f in sorted(files):
            src = os.path.join(d, f)
            rel = os.path.relpath(src, box)
            if os.path.islink(src):
                refused.append((rel, "a symlink; not exported"))
                continue
            try:
                full, rel = confine(folder, rel)
            except PolicyError as e:
                refused.append((rel, str(e)))
                continue
            if os.path.isdir(full):
                refused.append((rel, "a folder of that name exists"))
            elif not os.path.isfile(full) or _digest(full) != _digest(src):
                out.append((rel, src))
    return sorted(out), refused


def export(folder, items):
    """Copy (relative path, sandbox file) items into the workspace. Returns the relative paths written."""
    done = []
    for rel, src in items:
        full, rel = confine(folder, rel)            # again: the folder may have changed while the tool ran
        os.makedirs(os.path.dirname(full), exist_ok=True)
        shutil.copyfile(src, full)
        done.append(rel)
    return done


def _digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.digest()
