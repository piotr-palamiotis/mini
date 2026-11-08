"""Write a text file in place, its content taken from a fenced block of the reply."""
import os

from ._files import show, target
from ..agent import codeblocks, prompts
from ..agent.turns import Turns
from ..harness.sandbox import PolicyError

NAME = "write_file"
DESCRIPTION = ("Write a text file in the user's folder (path relative to it), in place. The content is the fenced "
               "block in this reply. mode: write (default), prepend or append.")
SCHEMA = {"type": "object", "required": ["path"], "properties": {
    "path": {"type": "string"}, "mode": {"type": "string", "enum": ["write", "prepend", "append"]},
    "content": {"type": "string", "description": "only for short text; prefer the fenced block"}}}
COVERS = ("write file", "save file", "create file", "write text", "save text", "append file", "prepend file")


def run(call, args):
    a = call.agent
    try:
        full, rel = target(a, args["path"])
    except PolicyError as e:
        return str(e)
    if os.path.isdir(full):
        return f"{args['path']} is a directory, not a file. Files:\n{a.list_work()}"
    path = str(args["path"]).lstrip("./")
    body = codeblocks.single(call.content, path)[0] if "```" in call.content else None
    if body is None and isinstance(args.get("content"), str):
        body = args["content"]
    if body is None and call.messages is not None:
        body = _text_turn(call, path)
    if body is None:
        return "No content received; the file was not written."
    mode = args.get("mode") or "write"
    try:
        old = open(full).read() if os.path.exists(full) else ""
        new = body + old if mode == "prepend" else old + body if mode == "append" else body

        def write():
            os.makedirs(os.path.dirname(full), exist_ok=True)
            open(full, "w").write(new)
            return [rel]
        a.guard_inputs([rel])
        a._change([rel], write)
    except (OSError, UnicodeError, PolicyError) as e:
        return f"Cannot write {args['path']}: {e}"
    return (f"Wrote {rel} ({len(new)} characters, mode {mode}). The file now contains:\n{show(new)}\n"
            "Do not write it again unless something in it is wrong.")


def _text_turn(call, path):
    """No content in the reply: ask for it as one fenced block, with no tools offered."""
    a, msgs = call.agent, call.messages
    msgs.append({"role": "tool", "tool_name": NAME, "content": "ok"})
    ext = os.path.splitext(path)[1].lstrip(".") or "text"
    msgs.append({"role": "user", "content": prompts.WRITE_CONTENT.format(path=path, lang=ext)})
    call.answered = True
    t = Turns(a, "write", msgs, [])
    for _ in range(3):
        reply = t.ask_text()
        text = reply.get("content") or ""
        body, problems = codeblocks.single(text, path)
        if body is None and text.strip() and "```" not in text:
            body = text.strip() + "\n"            # plain text reply: the content itself
        if body is None:
            for c in reply.get("tool_calls") or []:   # the model called a tool anyway: take its content
                v = (c.get("arguments") or {}).get("content")
                if isinstance(v, str) and v.strip():
                    body = v
                    a.log.emit("info", text=f"content for {path} arrived in a tool-call argument; accepted")
                    break
        if body is not None:
            return body
        t.user(f"Not saved: {' '.join(problems)}. Reply with the content of {path} as one fenced block.")
    return None
