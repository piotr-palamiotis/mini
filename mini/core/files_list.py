"""List a folder of the workspace."""
import os

from ..harness import workspace as ws
from ..harness.sandbox import PolicyError

NAME = "list_files"
DESCRIPTION = ("List a folder in the user's folder (path '.' for the folder itself). The listing is compact: folders "
               "show their item count. depth 1 (default) is the folder itself, 2 or 3 add subfolders. To see inside "
               "one folder, list that folder.")
SCHEMA = {"type": "object", "required": ["path"], "properties": {
    "path": {"type": "string", "description": "folder, relative to the user's folder"},
    "depth": {"type": "integer", "description": "1 to 3"}}}
COVERS = ("list file", "list folder", "list directory", "show file", "folder content", "directory listing",
          "what is in folder")


def run(call, args):
    a, p = call.agent, args.get("path") or "."
    try:
        if not os.path.isdir(a._path(p)):
            return f"{p} is not a folder.\nFiles:\n{a.list_work()}"
        return ws.list_dir(a.folder, p, args.get("depth") or 1)
    except (OSError, PolicyError) as e:
        return f"Cannot list: {e}"
