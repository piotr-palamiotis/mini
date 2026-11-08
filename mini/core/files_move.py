"""Move or rename a file or folder inside the workspace."""
import os
import shutil

from .files_copy import copy
from ..harness.sandbox import PolicyError

NAME = "move_file"
DESCRIPTION = "Move or rename a file or a folder in the user's folder."
SCHEMA = {"type": "object", "required": ["source", "destination"], "properties": {
    "source": {"type": "string"}, "destination": {"type": "string"}}}
COVERS = ("move file", "rename file", "move folder", "rename folder", "move directory")


def run(call, args):
    a = call.agent
    try:
        srel, pairs = copy(a, args, "move")
        gone = [os.path.relpath(s, a.folder) for s, _ in pairs]

        def write():
            for s, d in pairs:
                full = os.path.join(a.folder, d)
                os.makedirs(os.path.dirname(full), exist_ok=True)
                shutil.move(s, full)
            full = os.path.join(a.folder, srel)
            if os.path.isdir(full):
                shutil.rmtree(full, ignore_errors=True)
            return [d for _, d in pairs] + gone
        a.guard_inputs([d for _, d in pairs] + gone)
        a._change([d for _, d in pairs] + gone, write)
    except (OSError, PolicyError) as e:
        return f"Not moved: {e}"
    return f"Moved {srel} to {os.path.commonpath([d for _, d in pairs]) if len(pairs) > 1 else pairs[0][1]}"
