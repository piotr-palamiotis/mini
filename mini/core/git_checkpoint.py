"""Checkpoint the workspace now, and list the checkpoints."""
from ._git import repo
from ..harness import checkpoint as ckpt
from ..harness.sandbox import PolicyError

NAME = "checkpoint"
DESCRIPTION = ("Snapshot the user's folder in git now (mini also does this by itself before the task's first change) "
               "and list the checkpoints of earlier tasks, which undo can restore.")
SCHEMA = {"type": "object", "required": [], "properties": {}}
COVERS = ("checkpoint", "snapshot folder", "list checkpoint", "save state")


def run(call, args):
    a = call.agent
    try:
        folder = repo(a)
        a.ensure_checkpoint()
        rows = ckpt.checkpoints(folder)[:10]
    except (ckpt.GitError, PolicyError) as e:
        return f"No checkpoint: {e}"
    return (f"This task's checkpoint: {a.log.run_id}\nCheckpoints, newest first (run, date, task):\n"
            + "\n".join(f"- {r} {when} {subject}" for r, when, subject in rows))
