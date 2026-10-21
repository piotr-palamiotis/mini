"""git status of the workspace."""
from ._git import git
from ..harness import checkpoint as ckpt

NAME = "git_status"
DESCRIPTION = "Show the git status of the user's folder: branch, and changed, staged and untracked files."
SCHEMA = {"type": "object", "required": [], "properties": {}}
COVERS = ("git status", "changed file", "uncommitted change", "modified file", "untracked file")


def run(call, args):
    try:
        out = git(call.agent, "status", "--short", "--branch", "--", ".")
    except ckpt.GitError as e:
        return f"No git status: {e}"
    return out or "Nothing to report."
