"""Write the agent's notes about the folder: they are shown at the start of every later task here."""
from ..harness import journal

NAME = "write_notes"
DESCRIPTION = ("Write your notes about the user's folder (facts worth knowing in later tasks here: layout, "
               "conventions, what the user prefers). They are shown at the start of every later task in this folder. "
               "mode: append (default) or replace.")
SCHEMA = {"type": "object", "required": ["text"], "properties": {
    "text": {"type": "string"}, "mode": {"type": "string", "enum": ["append", "replace"]}}}
COVERS = ("write note", "remember", "save note", "update note", "take note")


def run(call, args):
    a = call.agent
    if not a.memory:
        return "No project memory: the user's folder is not a git repository."
    try:
        new = journal.write_notes(a.workspace, args["text"], args.get("mode") or "append")
    except OSError as e:
        return f"Notes not written: {e}"
    a.log.emit("info", text=f"notes written ({len(new)} characters)")
    return f"Notes saved ({len(new)} characters)."
