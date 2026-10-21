"""Recent commits of the workspace."""
from ._git import git
from ..harness import checkpoint as ckpt

NAME = "git_log"
DESCRIPTION = "Show recent commits of the user's folder (count: how many, default 10), optionally only those touching path."
SCHEMA = {"type": "object", "required": [], "properties": {"count": {"type": "integer"}, "path": {"type": "string"}}}
COVERS = ("git log", "commit history", "recent commit", "show commit")


def run(call, args):
    n = max(1, min(args.get("count") or 10, 100))
    try:
        out = git(call.agent, "log", f"-n{n}", "--format=%h %ad %an: %s", "--date=short", "--", args.get("path") or ".")
    except ckpt.GitError as e:
        return f"No log: {e}"
    return out or "No commits yet."
