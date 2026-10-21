"""Find files by name and lines by content."""
import fnmatch
import os
import re

from ._files import target
from ..harness import workspace as ws
from ..harness.sandbox import PolicyError

NAME = "search_files"
MAX_HITS = 60
DESCRIPTION = ("Search the user's folder (or the folder path) for files whose name matches name (a glob such as "
               "*.py or report*), and/or for lines containing content (plain text; regex true for a regular "
               "expression). Lists matching files, or path:line: text for content.")
SCHEMA = {"type": "object", "required": [], "properties": {
    "name": {"type": "string", "description": "glob on the file name"},
    "content": {"type": "string", "description": "text to find in the files"},
    "regex": {"type": "boolean"}, "path": {"type": "string", "description": "folder to search, default '.'"}}}
COVERS = ("search file", "find file", "search text", "find text", "grep", "locate file", "search content",
          "find line", "file name match")


def run(call, args):
    a = call.agent
    if not args.get("name") and not args.get("content"):
        return "Give name (a glob), content (text to find), or both."
    try:
        root, rel = target(a, args.get("path") or ".")
        pat = re.compile(args["content"] if args.get("regex") else re.escape(args["content"])) if args.get("content") else None
    except PolicyError as e:
        return f"Cannot search: {e}"
    except re.error as e:
        return f"content is not a valid regular expression: {e}"
    hits = []
    for d, dirs, names in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in ws.SKIP_DIRS and not os.path.islink(os.path.join(d, x)))
        for n in sorted(names):
            if args.get("name") and not fnmatch.fnmatch(n, args["name"]):
                continue
            full = os.path.join(d, n)
            shown = os.path.relpath(full, a.folder)
            if not pat:
                hits.append(shown)
            else:
                try:
                    with open(full, errors="strict") as f:
                        for i, line in enumerate(f, 1):
                            if pat.search(line):
                                hits.append(f"{shown}:{i}: {line.rstrip()[:200]}")
                except (OSError, UnicodeError):
                    continue                    # binary or unreadable: not a text match
            if len(hits) > MAX_HITS:
                break
        if len(hits) > MAX_HITS:
            break
    if not hits:
        return f"No matches in {rel}."
    more = f"\n[... more than {MAX_HITS} matches; narrow the search]" if len(hits) > MAX_HITS else ""
    return "\n".join(hits[:MAX_HITS]) + more
