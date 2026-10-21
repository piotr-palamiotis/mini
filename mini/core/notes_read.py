"""Read the project memory: the agent's notes and the journal of earlier tasks in the folder."""
from ..harness import journal

NAME = "read_notes"
DESCRIPTION = ("Read the project memory of the user's folder: your notes (write_notes) and the journal of earlier "
               "tasks (count: how many entries, newest first, default 20).")
SCHEMA = {"type": "object", "required": [], "properties": {"count": {"type": "integer"}}}
COVERS = ("read note", "read journal", "project memory", "earlier task", "task history")


def run(call, args):
    a = call.agent
    if not a.memory:
        return "No project memory: the user's folder is not a git repository."
    n = max(1, min(args.get("count") or 20, 200))
    rows = [f"- {e.get('time', '?')} [{e.get('status', '?')}] {e.get('task', '')}"
            + (f" -> {str(e['result'])[:500]}" if e.get("result") else "")
            + (f"; files: {', '.join(map(str, e['files']))}" if e.get("files") else "")
            + (f"; checkpoint: {e['run']}" if e.get("checkpoint") else "")
            for e in reversed(journal.entries(a.workspace)[-n:])]
    return (f"Notes (.mini/{journal.NOTES}):\n{journal.notes(a.workspace).strip() or '(none)'}\n\n"
            f"Journal, newest first:\n" + ("\n".join(rows) or "(no earlier tasks)"))
