"""Put files back as they were before a task (this one by default), from its checkpoint."""
from ._git import repo
from ..harness import checkpoint as ckpt
from ..harness.sandbox import PolicyError

NAME = "undo"
DESCRIPTION = ("Put files back as they were before a task, from its checkpoint: run is the task (default: this "
               "task, or the last one when this task changed nothing; checkpoint lists them), files limits it to "
               "some files. Files the task created are deleted. An undo can itself be undone.")
SCHEMA = {"type": "object", "required": [], "properties": {"run": {"type": "string"},
                                                          "files": {"type": "array", "items": {"type": "string"}}}}
COVERS = ("undo", "revert change", "restore file", "undo change", "roll back change", "revert file")


def run(call, args):
    a = call.agent
    try:
        folder = repo(a)
        run_id = args.get("run") or (a.log.run_id if a.checkpoint else None)
        if run_id is None:
            rows = [r for r in ckpt.checkpoints(folder) if not r[0].startswith("undo-")]
            if not rows:
                return "Nothing to undo: this folder has no checkpoints."
            run_id = rows[0][0]
        rels = list(args.get("files") or []) or ckpt.task_files(folder, run_id)
        if not rels:
            return f"Task {run_id} changed no files."
        done = []

        def write():
            done.extend(ckpt.undo(folder, run_id, rels)[1])
            return [r for r, how in done if how != "refused"]
        a._change(rels, write)
    except (ckpt.GitError, PolicyError, OSError) as e:
        return f"Not undone: {e}"
    if run_id == a.log.run_id:                   # this task's own changes undone: they are no longer its output
        left = set(ckpt.task_files(folder, run_id))
        a.changed = [c for c in a.changed if c in left]
    return f"Undid task {run_id}: " + ", ".join(f"{r} {how}" for r, how in done)
