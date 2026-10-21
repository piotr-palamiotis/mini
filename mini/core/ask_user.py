"""Ask the user a question, with options to pick from (one, or several with multi)."""
import re

NAME = "ask_user"
DESCRIPTION = ("Ask the user a question or for a confirmation, only when the task cannot go on without the answer. "
               "options: the answers to pick from (the user may also type their own); multi true lets the user pick "
               "several.")
SCHEMA = {"type": "object", "required": ["question"], "properties": {
    "question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}},
    "multi": {"type": "boolean"}}}
COVERS = ("ask user", "ask question", "confirm with user", "user confirmation", "user choice")


def run(call, args):
    a, q, opts = call.agent, args["question"], [o for o in args.get("options") or [] if o.strip()]
    details = {"question": q}
    if opts and args.get("multi"):
        details["options"] = "\n".join(f"{i}) {o}" for i, o in enumerate(opts, 1))
        details["how"] = "type the numbers or names of your picks, separated by commas"
        choices = ("answer", "cancel")
    else:
        choices = tuple(opts) + ("cancel",) if opts else ("answer", "cancel")
    d = a.gate.request("question", q, details, options=choices)
    if not d.approved or d.choice == "cancel":
        if d.decided_by.startswith("auto"):
            return "No one can answer in this run. Decide yourself and say in your answer what you assumed."
        return "The user declined to answer." + (f" Note: {d.note}" if d.note else "")
    note = (d.note or "").strip()
    if opts and args.get("multi"):
        picked = []
        for part in filter(None, (p.strip() for p in re.split(r"[,;\n]", note))):
            pick = opts[int(part) - 1] if part.isdigit() and 1 <= int(part) <= len(opts) else part
            if pick not in picked:
                picked.append(pick)
        return "The user picked: " + ("; ".join(picked) if picked else "nothing")
    if opts:
        return f"The user picked: {d.choice}" + (f". Note: {note}" if note else "")
    return f"The user answered: {note}" if note else "The user gave no answer."
