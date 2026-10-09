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


def extract(text):
    """Returns (files: {name: content}, problems: [str])."""
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
