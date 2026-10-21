"""Ask the user a question, with options to pick from (one, or several with multi)."""
NAME = "ask_user"
DESCRIPTION = ("Ask the user a question or for a confirmation, only when the task cannot go on without the answer. "
               "options: the answers to pick from (the user may also type their own); multi true lets the user pick "
               "several.")
SCHEMA = {"type": "object", "required": ["question"], "properties": {
    "question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}},
    "multi": {"type": "boolean"}}}
COVERS = ("ask user", "ask question", "confirm with user", "user confirmation", "user choice")


def run(call, args):
    a, q, opts, multi = call.agent, args["question"], [o for o in args.get("options") or [] if o.strip()], args.get("multi")
    d = a.gate.request("question", q, {"question": q}, options=("answer", "cancel"), choices=opts, multi=multi)
    if not d.approved or d.choice == "cancel":
        if d.decided_by.startswith("auto"):
            return "No one can answer in this run. Decide yourself and say in your answer what you assumed."
        return "The user declined to answer." + (f" Note: {d.note}" if d.note else "")
    note = (d.note or "").strip()
    if opts and multi:
        return "The user picked: " + ("; ".join(d.picks) if d.picks else "nothing")
    if opts:
        return f"The user picked: {d.choice}" + (f". Note: {note}" if note else "")
    return f"The user answered: {note}" if note else "The user gave no answer."
