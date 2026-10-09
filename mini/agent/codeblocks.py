"""Extract fenced code blocks with file names from a model reply.

Code never travels inside a JSON tool-call argument (it broke Ollama's gemma4 parser). The model writes files as
fenced blocks in plain text; the harness finds the file name in, in this order:
  1. the info string:            ```python tool.py   or   ```python title="tool.py"
  2. a first-line comment:       # file: tool.py     or   # tool.py
  3. the closest line above the block that mentions a file name (e.g. **tool.py**)
"""
import re

FENCE = re.compile(r"^(`{3,}|~{3,})([^\n`]*)\n(.*?)^\1[ \t]*$", re.M | re.S)
OPEN_FENCE = re.compile(r"^(`{3,}|~{3,})([^\n`]*)\n", re.M)
FNAME = re.compile(r"([A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:py|md|txt|json|toml|cfg|ini|yaml|yml|csv|html|sh))\b")
FIRST_LINE = re.compile(r"^\s*(?:#|//)\s*(?:file(?:name)?\s*:\s*)?([A-Za-z0-9_][A-Za-z0-9_./-]*\.[a-z]+)\s*$", re.I)
ELISION = re.compile(r"^.*(\.\.\.|…).*(omitted|unchanged|rest of|remain|same as|lines? \d+|existing code).*$"
                     r"|^\s*(#|//)?\s*(\.\.\.|…)\s*$", re.M | re.I)


TEMPLATE_TOKENS = re.compile(r"<\|?(?:channel|message|end|start|turn|im_start|im_end)\|?>")


def clean(text):
    """Remove chat-template tokens that leak into content (they can glue text to an opening fence)."""
    return TEMPLATE_TOKENS.sub("\n", text or "")


def from_args(args, names=("tool.py", "test_tool.py")):
    """Code the model put into tool-call arguments instead of fenced blocks. Returns {file name: code}."""
    out = {}
    for k, v in (args or {}).items():
        if not isinstance(v, str) or "\n" not in v or len(v) < 40:
            continue
        key = k.lower()
        if "test" in key and "test_tool.py" in names:
            out["test_tool.py"] = v if v.endswith("\n") else v + "\n"
        elif ("code" in key or "tool" in key or "content" in key or key.endswith("py")) and "def " in v:
            out[names[0]] = v if v.endswith("\n") else v + "\n"
    return out


def extract(text):
    """Returns (files: {name: content}, problems: [str])."""
    text = clean(text)
    files, problems = {}, []
    last_end = 0
    for m in FENCE.finditer(text):
        info, body = m.group(2).strip(), m.group(3)
        name = None
        mi = FNAME.search(info)
        if mi:
            name = mi.group(1)
        if not name:
            first = body.split("\n", 1)[0]
            mf = FIRST_LINE.match(first)
            if mf:
                name = mf.group(1)
        if not name:
            before = [ln for ln in text[last_end:m.start()].strip().split("\n") if ln.strip()]
            if before:
                mb = FNAME.search(before[-1])
                if mb:
                    name = mb.group(1)
        last_end = m.end()
        if not name:
            problems.append("a code block has no file name (put the file name after the opening fence, e.g. ```python tool.py)")
            continue
        name = name.lstrip("./")
        if ELISION.search(body):
            problems.append(f"{name}: the block abbreviates code with '...' or 'omitted'; write the complete file")
            continue
        files[name] = body if body.endswith("\n") else body + "\n"
    opened = len(OPEN_FENCE.findall(text))
    if opened > 2 * len(FENCE.findall(text)) and opened % 2 == 1:
        problems.append("a code block is not closed (the reply may have been cut off); write shorter files")
    return files, problems


def numbered(content, start=1):
    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    w = len(str(len(lines) + start))
    return "\n".join(f"{i:>{w}}| {ln}" for i, ln in enumerate(lines, start))


def single(text, name):
    """The content for one requested file: a block named `name`, or the only block in the reply."""
    files, problems = extract(text)
    if name in files:
        return files[name], []
    named = {k.split("/")[-1]: v for k, v in files.items()}
    if name in named:
        return named[name], []
    blocks = list(FENCE.finditer(clean(text)))
    if len(blocks) == 1:
        body = blocks[0].group(3)
        if ELISION.search(body):
            return None, ["the block abbreviates code with '...' or 'omitted'; write the complete file"]
        return (body if body.endswith("\n") else body + "\n"), []
    if not blocks:
        return None, problems + ["no complete fenced code block found (it may have been cut off)"]
    return None, problems + [f"{len(blocks)} code blocks found; send only {name}"]
