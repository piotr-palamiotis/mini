"""Checkpoints of the user's folder in hidden git refs, and undo. Stdlib only (plus the git command).

mini works in place, so the folder must be a git repository. Before a task changes a file, the folder's tree is
snapshotted with git plumbing (a temporary index, write-tree, commit-tree, update-ref) into refs/mini/<run>. This
works with a dirty tree (uncommitted and untracked files are in the snapshot), never touches the user's index, and
adds nothing to the branch history.

refs/mini/<run> points at the "before" commit until the first change is recorded. From then on it points at an
"after" commit whose parent is "before" and whose tree holds the files the task wrote, so `mini undo` knows the
task's files from git alone: the paths that differ between the two.
"""
import os
import re
import subprocess
import sys
import time

REF = "refs/mini/"
AFTER = "mini after "                       # subject of an "after" commit
ENV = {"GIT_AUTHOR_NAME": "mini", "GIT_AUTHOR_EMAIL": "mini@localhost",
       "GIT_COMMITTER_NAME": "mini", "GIT_COMMITTER_EMAIL": "mini@localhost"}


class GitError(Exception):
    pass


def git(folder, *args, index=None, check=True):
    env = dict(os.environ, **ENV, **({"GIT_INDEX_FILE": index} if index else {}))
    try:
        r = subprocess.run(["git", "-C", folder, *args], capture_output=True, text=True, env=env, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GitError(f"git {args[0]}: {e}")
    if check and r.returncode:
        raise GitError(f"git {' '.join(args[:2])}: {(r.stderr or r.stdout).strip()}")
    return r.stdout.strip() if check else r


def is_repo(folder):
    """True when the folder is inside a git work tree (and not inside .git)."""
    try:
        return git(folder, "rev-parse", "--is-inside-work-tree", check=False).stdout.strip() == "true"
    except GitError:
        return False


def init(folder):
    git(folder, "init", "-q")


def guard_note(folder):
    return (f"{folder} is not a git repository. mini changes files in place and keeps a checkpoint of every task "
            f"in git so it can be undone, so it works only in a repository.")


def offer_init(folder, ask):
    """The git-required guard. ask(question) -> bool is asked whether to run `git init` when the folder is not a
    repository; nothing else happens until it says yes. True when the folder is (now) a repository."""
    if is_repo(folder):
        return True
    if not ask(guard_note(folder) + f"\nRun `git init` in {folder}?"):
        return False
    init(folder)
    return True


def terminal_ask(question):
    """ask() for offer_init on the terminal: only an interactive yes counts."""
    print(question, file=sys.stderr)
    if not sys.stdin.isatty():
        print("(not interactive: nothing done)", file=sys.stderr)
        return False
    try:
        return input("[y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _head(folder):
    r = git(folder, "rev-parse", "-q", "--verify", "HEAD^{commit}", check=False)
    return r.stdout.strip() if r.returncode == 0 else None


def _prefix(folder):
    return git(folder, "rev-parse", "--show-prefix")       # the folder inside the repository, "" or "sub/"


class Checkpoint:
    """The checkpoint of one task. before_change(paths) before writing, after_change(paths) after."""

    def __init__(self, folder, run_id, task=""):
        self.folder, self.task = os.path.realpath(folder), task
        self.run_id = re.sub(r"[^A-Za-z0-9._-]+", "-", run_id).strip(".-") or "run"     # a valid ref name
        self.ref = REF + run_id
        self.before, self.touched = None, []
        self.index = os.path.join(git(self.folder, "rev-parse", "--absolute-git-dir"), f"mini-index-{run_id}")

    def _tree(self, base, add_all=False, paths=()):
        """A tree: base (a commit, or empty), plus the whole folder (add_all) or the given paths, as they are now."""
        try:
            git(self.folder, "read-tree", *([base] if base else ["--empty"]), index=self.index)
            if add_all:
                git(self.folder, "add", "-A", "--", ".", index=self.index)
            here = [p for p in paths if os.path.lexists(os.path.join(self.folder, p))]
            gone = [p for p in paths if p not in here]
            if here:                                        # -f: a written file counts even when it is ignored
                git(self.folder, "add", "-A", "-f", "--", *here, index=self.index)
            if gone:
                git(self.folder, "rm", "-q", "--cached", "--ignore-unmatch", "--", *gone, index=self.index)
            return git(self.folder, "write-tree", index=self.index)
        finally:
            if os.path.exists(self.index):
                os.remove(self.index)

    def _commit(self, tree, parent, subject):
        return git(self.folder, "commit-tree", tree, *(["-p", parent] if parent else []), "-m", subject)

    def before_change(self, paths):
        """Snapshot the folder the first time; later, add files that exist but are missing from the snapshot (an
        ignored file the task is about to overwrite)."""
        paths = [p for p in paths if p not in self.touched]
        if self.before is None:
            head = _head(self.folder)
            tree = self._tree(head, add_all=True, paths=[p for p in paths if os.path.lexists(os.path.join(self.folder, p))])
            self.before = self._commit(tree, head, f"mini checkpoint {self.run_id}: {self.task[:200]}")
            self._publish()
            return
        missing = [p for p in paths if os.path.lexists(os.path.join(self.folder, p))
                   and not git(self.folder, "ls-tree", "--name-only", self.before, "--", p, check=False).stdout.strip()]
        if missing:
            parent = git(self.folder, "rev-parse", "-q", "--verify", self.before + "^1", check=False).stdout.strip() or None
            msg = git(self.folder, "log", "-1", "--format=%s", self.before)
            self.before = self._commit(self._tree(self.before, paths=missing), parent, msg)
            self._publish()

    def after_change(self, paths):
        self.touched += [p for p in paths if p not in self.touched]
        self._publish()

    def _publish(self):
        commit = self.before
        if self.touched:
            commit = self._commit(self._tree(self.before, paths=self.touched), self.before, AFTER + self.run_id)
        git(self.folder, "update-ref", self.ref, commit)


# ---------------------------------------------------------------- reading checkpoints, undo
def checkpoints(folder):
    """[(run id, time, subject of the before commit)], newest first."""
    out = git(folder, "for-each-ref", "--sort=-creatordate", "--format=%(refname)\t%(creatordate:short)", REF)
    rows = []
    for line in out.splitlines():
        ref, when = line.split("\t")
        before, _ = _resolve(folder, ref)
        rows.append((ref[len(REF):], when, git(folder, "log", "-1", "--format=%s", before)))
    return rows


def _resolve(folder, ref):
    """(before commit, after commit or None) of a checkpoint ref."""
    tip = git(folder, "rev-parse", "-q", "--verify", ref + "^{commit}", check=False).stdout.strip()
    if not tip:
        raise GitError(f"no checkpoint {ref[len(REF):]}")
    if git(folder, "log", "-1", "--format=%s", tip).startswith(AFTER):
        return git(folder, "rev-parse", tip + "^1"), tip
    return tip, None


def task_files(folder, run_id):
    """The files a task changed, relative to the folder."""
    before, after = _resolve(folder, REF + run_id)
    if not after:
        return []
    prefix = _prefix(folder)
    names = git(folder, "diff-tree", "-r", "--name-only", "--no-renames", before, after).splitlines()
    return [n[len(prefix):] for n in names if n.startswith(prefix)]


def undo(folder, run_id=None, files=()):
    """Put files back as they were before a task: the given ones, or all the task changed. Files the task created
    are deleted. The state before the undo is a checkpoint of its own (undo-<run>-<time>), so an undo can be undone.
    Returns (run id, [(relative path, "restored" | "deleted" | "refused")])."""
    folder = os.path.realpath(folder)
    if run_id is None:
        rows = [r for r in checkpoints(folder) if not r[0].startswith("undo-")]
        if not rows:
            raise GitError("no checkpoints in this folder")
        run_id = rows[0][0]
    before, _ = _resolve(folder, REF + run_id)
    rels = [_rel(folder, f) for f in files] or task_files(folder, run_id)
    if not rels:
        return run_id, []
    cp = Checkpoint(folder, f"undo-{run_id}-{time.strftime('%Y%m%d-%H%M%S')}", f"before undoing {run_id}")
    cp.before_change(rels)
    done = []
    for rel in rels:
        try:
            full = os.path.join(folder, _rel(folder, rel))         # a folder link added since must not lead out
        except GitError:
            done.append((rel, "refused"))
            continue
        entry = git(folder, "ls-tree", before, "--", rel)          # "<mode> blob <sha>\t<path>", paths below the folder
        if not entry:
            if os.path.lexists(full) and not os.path.isdir(full):
                os.remove(full)
                done.append((rel, "deleted"))
            continue
        mode, kind, sha = entry.split("\t")[0].split()
        if kind != "blob":
            continue
        data = subprocess.run(["git", "-C", folder, "cat-file", "blob", sha], capture_output=True, check=True).stdout
        if os.path.islink(full):
            os.remove(full)
        os.makedirs(os.path.dirname(full) or folder, exist_ok=True)
        if mode == "120000":
            if os.path.lexists(full):
                os.remove(full)
            os.symlink(data.decode(), full)
        else:
            with open(full, "wb") as f:
                f.write(data)
            os.chmod(full, 0o755 if mode == "100755" else 0o644)
        done.append((rel, "restored"))
    cp.after_change([r for r, how in done if how != "refused"])
    return run_id, done


def _rel(folder, p):
    joined = os.path.join(folder, os.path.expanduser(str(p)))
    full = os.path.join(os.path.realpath(os.path.dirname(joined)), os.path.basename(joined))   # a link itself, not its target
    if full == folder or not full.startswith(folder + os.sep):
        raise GitError(f"{p} is not a file in {folder}")
    rel = os.path.relpath(full, folder)
    if rel.split(os.sep)[0] == ".git":
        raise GitError(f"{p} is inside .git")
    return rel


def undo_args(folder, args):
    """(run id or None, files) from `mini undo [RUN] [FILE...]`: the first argument is a run when a checkpoint has
    that name."""
    args = list(args)
    if args and git(folder, "rev-parse", "-q", "--verify", REF + args[0], check=False).returncode == 0:
        return args[0], args[1:]
    return None, args
