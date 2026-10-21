"""Compare two text files."""
import difflib

from ._files import show, target
from ..harness.sandbox import PolicyError

NAME = "diff_files"
DESCRIPTION = ("Show the differences between two text files in the user's folder as a unified diff. For changes "
               "since the last commit or the task's checkpoint, use git_diff.")
SCHEMA = {"type": "object", "required": ["a", "b"], "properties": {
    "a": {"type": "string", "description": "first file"}, "b": {"type": "string", "description": "second file"}}}
COVERS = ("diff file", "compare file", "difference between file", "compare two file")


def run(call, args):
    try:
        (fa, ra), (fb, rb) = target(call.agent, args["a"]), target(call.agent, args["b"])
        la, lb = open(fa).read().splitlines(True), open(fb).read().splitlines(True)
    except (OSError, UnicodeError, PolicyError) as e:
        return f"Cannot compare: {e}"
    out = "".join(difflib.unified_diff(la, lb, ra, rb))
    return show(out, 6000) if out else f"{ra} and {rb} are the same."
