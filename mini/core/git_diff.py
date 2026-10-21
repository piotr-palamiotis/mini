"""git diff of the workspace, against HEAD or the task's checkpoint."""
from ._files import show
from ._git import git
from ..harness import checkpoint as ckpt

NAME = "git_diff"
DESCRIPTION = ("Show changes in the user's folder as a diff: since the last commit (since: head, the default), or "
               "what this task changed (since: checkpoint). path limits it to a file or folder. New untracked files "
               "show in git_status, not here, unless since is checkpoint.")
SCHEMA = {"type": "object", "required": [], "properties": {
    "path": {"type": "string"}, "since": {"type": "string", "enum": ["head", "checkpoint"]}}}
COVERS = ("git diff", "show change", "what changed", "uncommitted change", "diff since commit")


def run(call, args):
    a, path = call.agent, args.get("path") or "."
    try:
        if args.get("since") == "checkpoint":
            if not a.checkpoint:
                return "This task has changed nothing yet, so it has no checkpoint."
            out = git(a, "diff", a.checkpoint.before, a.checkpoint.ref, "--", path)
        else:
            has_head = ckpt.git(a.workspace or a.folder, "rev-parse", "-q", "--verify", "HEAD", check=False).returncode == 0
            out = git(a, "diff", *(["HEAD"] if has_head else []), "--", path)
    except ckpt.GitError as e:
        return f"No diff: {e}"
    return show(out, 6000) if out else "No changes."
