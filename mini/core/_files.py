"""Helpers shared by the file capabilities."""
import os

from ..harness import workspace as ws
from ..harness.sandbox import PolicyError

MAX_TREE_FILES = 2000                   # files one copy, move or delete of a folder may touch


def target(agent, p):
    """(real path, path relative to the folder); PolicyError outside the folder or inside .git."""
    return ws.confine(agent.folder, p)


def files_under(full, rel):
    """Relative paths of the files at full (a file, or every file below a folder, links and .git left out)."""
    if not os.path.isdir(full) or os.path.islink(full):
        return [rel]
    out = []
    for d, dirs, files in os.walk(full):
        dirs[:] = sorted(x for x in dirs if x not in (".git", ws.META) and not os.path.islink(os.path.join(d, x)))
        out += [os.path.normpath(os.path.join(rel, os.path.relpath(os.path.join(d, f), full))) for f in sorted(files)]
        if len(out) > MAX_TREE_FILES:
            raise PolicyError(f"{rel} holds more than {MAX_TREE_FILES} files; name a smaller folder")
    return out


def show(text, n=4000):
    """Text for a result, the middle cut when it is long."""
    return text if len(text) <= n else text[:n // 2] + f"\n[... {len(text) - n} characters ...]\n" + text[-n // 2:]
