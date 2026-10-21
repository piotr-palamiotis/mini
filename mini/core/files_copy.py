"""Copy a file or folder inside the workspace."""
import os
import shutil

from ._files import files_under, target
from ..harness.sandbox import PolicyError

NAME = "copy_file"
DESCRIPTION = "Copy a file or a folder in the user's folder to a new path (folders are copied whole)."
SCHEMA = {"type": "object", "required": ["source", "destination"], "properties": {
    "source": {"type": "string"}, "destination": {"type": "string"}}}
COVERS = ("copy file", "copy folder", "duplicate file", "backup file", "copy directory")


def copy(a, args, verb):
    """Copy source to destination with a checkpoint first. Returns (source rel, [written rels]) or raises."""
    src, srel = target(a, args["source"])
    dest, drel = target(a, args["destination"])
    if not os.path.exists(src):
        raise PolicyError(f"{args['source']} does not exist")
    if os.path.isdir(dest) and not os.path.isdir(src):
        dest, drel = target(a, os.path.join(drel, os.path.basename(src)))      # into an existing folder
    if dest == src or dest.startswith(src + os.sep):
        raise PolicyError(f"cannot {verb} {srel} into itself")
    if os.path.isdir(src) and os.path.exists(dest):
        raise PolicyError(f"{drel} exists; {verb} the folder to a new path")
    pairs = [(os.path.join(a.folder, r), os.path.normpath(os.path.join(drel, os.path.relpath(r, srel))))
             for r in files_under(src, srel)]
    return srel, pairs


def run(call, args):
    a = call.agent
    try:
        srel, pairs = copy(a, args, "copy")

        def write():
            for s, d in pairs:
                full = os.path.join(a.folder, d)
                os.makedirs(os.path.dirname(full), exist_ok=True)
                shutil.copyfile(s, full)
            return [d for _, d in pairs]
        done = a._change([d for _, d in pairs], write)
    except (OSError, PolicyError) as e:
        return f"Not copied: {e}"
    return f"Copied {srel} to {', '.join(done[:20])}" + (f" (+{len(done) - 20} more)" if len(done) > 20 else "")
