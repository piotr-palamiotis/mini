"""Read a text file, in parts when it is long."""
import os

from ..harness.sandbox import PolicyError

NAME = "read_file"
MAX_READ = 8000                                   # characters of a file per read (about 2,000 tokens)
DESCRIPTION = ("Read a text file in the user's folder (path relative to it) or an attached file. Long files come in "
               f"parts of {MAX_READ} characters: offset is the character to start at.")
SCHEMA = {"type": "object", "required": ["path"], "properties": {
    "path": {"type": "string"}, "offset": {"type": "integer", "description": "character to start at"}}}
COVERS = ("read file", "open file", "show file content", "view file", "print file", "read text")


def run(call, args):
    a, p = call.agent, args["path"]
    try:
        full = a._readable(p)
        if os.path.isdir(full):
            return f"{p} is a folder; use list_files.\nFiles:\n{a.list_work()}"
        data = open(full, errors="replace").read()
    except (OSError, PolicyError) as e:
        return f"Cannot read: {e}\nFiles:\n{a.list_work()}"
    start = max(0, args.get("offset") or 0)
    if len(data) > MAX_READ or start:
        end = start + MAX_READ
        more = f"\n[... characters {start}-{min(end, len(data))} of {len(data)}; read_file with offset={end} for the rest]" \
            if end < len(data) else f"\n[... characters {start}-{len(data)} of {len(data)}: the end]"
        return data[start:end] + more
    return data
