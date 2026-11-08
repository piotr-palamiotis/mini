"""Edit a text file in place: replace an exact piece of text, or a range of lines."""
from ._files import show, target
from ..agent import codeblocks
from ..harness.sandbox import PolicyError

NAME = "edit_file"
DESCRIPTION = ("Change part of a text file in the user's folder. Either old and new: replace the exact text old "
               "(it must appear exactly once) with new. Or start, end and new: replace lines start..end (counted from 1, "
               "inclusive) with new; empty new deletes them. The result shows the file with line numbers.")
SCHEMA = {"type": "object", "required": ["path", "new"], "properties": {
    "path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"},
    "start": {"type": "integer"}, "end": {"type": "integer"}}}
COVERS = ("edit file", "replace text", "replace line", "change line", "modify file", "delete line", "insert line")


def run(call, args):
    a = call.agent
    try:
        full, rel = target(a, args["path"])
        text = open(full).read()
    except (OSError, UnicodeError, PolicyError) as e:
        return f"Cannot edit: {e}"
    new = args["new"]
    if args.get("old"):
        n = text.count(args["old"])
        if n != 1:
            return (f"old appears {n} times in {rel}; it must appear exactly once. Copy it exactly, with a few "
                    f"surrounding lines. Current file:\n{show(codeblocks.numbered(text))}")
        out = text.replace(args["old"], new)
    elif args.get("start") is not None and args.get("end") is not None:
        lines = text.split("\n")
        if text.endswith("\n"):
            lines = lines[:-1]
        s, e = args["start"], args["end"]
        if not (1 <= s <= e <= len(lines)):
            return f"line range {s}..{e} is outside 1..{len(lines)}."
        lines[s - 1:e] = new.split("\n") if new != "" else []
        out = "\n".join(lines) + ("\n" if lines else "")
    else:
        return "Give old (exact text to replace) or start and end (a line range)."
    if codeblocks.ELISION.search(new) and not codeblocks.ELISION.search(args.get("old") or ""):
        return "new abbreviates text with '...'; write it out in full."

    def write():
        open(full, "w").write(out)
        return [rel]
    try:
        a.guard_inputs([rel])
        a._change([rel], write)
    except (OSError, PolicyError) as e:
        return f"Cannot edit {rel}: {e}"
    return f"{rel} updated. Current content (line numbers are not part of the file):\n{show(codeblocks.numbered(out))}"
