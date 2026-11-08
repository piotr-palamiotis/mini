"""Delete a file or folder in the workspace (checkpointed, so undo brings it back)."""
import os
import shutil

from ._files import files_under, target
from ..harness.sandbox import PolicyError

NAME = "delete_file"
DESCRIPTION = ("Delete a file or a folder in the user's folder. The task's checkpoint keeps it, so undo can bring "
               "it back.")
SCHEMA = {"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}}
COVERS = ("delete file", "remove file", "delete folder", "remove folder", "remove directory", "delete directory")


def run(call, args):
    a = call.agent
    try:
        full, rel = target(a, args["path"])
        if full == os.path.realpath(a.folder):
            raise PolicyError("the user's folder itself cannot be deleted")
        if not os.path.lexists(full):
            raise PolicyError(f"{rel} does not exist")
        rels = files_under(full, rel)

        def write():
            shutil.rmtree(full) if os.path.isdir(full) and not os.path.islink(full) else os.remove(full)
            return rels
        a.guard_inputs(rels)
        a._change(rels, write)
    except (OSError, PolicyError) as e:
        return f"Not deleted: {e}"
    return f"Deleted {rel}" + (f" ({len(rels)} files)" if len(rels) > 1 else "") + ". undo can restore it."
