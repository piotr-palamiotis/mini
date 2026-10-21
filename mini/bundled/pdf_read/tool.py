"""Extract the text of a PDF, page by page, with the standard library only.

Limits (no PDF library is in the sandbox image): works on PDFs whose pages are plain content streams (raw or
Flate-compressed) with simple font encodings. Not handled: scanned pages (images, no OCR), objects packed into
object streams or cross-reference streams (PDF 1.5+ writers often do this), encrypted files, fonts that use custom
encodings or ToUnicode maps (the text then comes out as the raw codes), and column or table layout (text comes in
content order). Pages that yield nothing are reported in `empty_pages`.
"""
import os
import re
import zlib

OBJ = re.compile(rb"(\d+)\s+(\d+)\s+obj\b(.*?)\bendobj", re.S)
REF = re.compile(rb"(\d+)\s+\d+\s+R")
ESC = {ord("n"): "\n", ord("r"): "\r", ord("t"): "\t", ord("b"): "\b", ord("f"): "\f"}


def safe(path):
    if not isinstance(path, str) or not path or os.path.isabs(path) or ".." in path.replace("\\", "/").split("/"):
        raise ValueError("path must be a relative path inside the working folder")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"{path} does not exist")
    return path


def objects(data):
    out = {}
    for m in OBJ.finditer(data):
        body = m.group(3)
        s = body.find(b"stream")
        head, stream = (body[:s], body[s + 6:]) if s >= 0 else (body, None)
        if stream is not None:
            stream = stream[2:] if stream.startswith(b"\r\n") else stream[1:]
            e = stream.rfind(b"endstream")
            stream = stream[:e] if e >= 0 else stream
            stream = stream.rstrip(b"\r\n") if b"FlateDecode" not in head else stream
        out[int(m.group(1))] = (head, stream)
    return out


def decode(head, stream):
    if stream is None:
        return b""
    if b"FlateDecode" in head:
        try:
            return zlib.decompressobj().decompress(stream)
        except zlib.error:
            return b""
    return stream


def pages(objs):
    """Page objects in document order: the /Pages tree when it can be followed, else the order in the file."""
    flat = [n for n in sorted(objs) if re.search(rb"/Type\s*/Page(?![s\w])", objs[n][0])]
    root = next((n for n in sorted(objs) if re.search(rb"/Type\s*/Pages(?!\w)", objs[n][0])
                 and b"/Parent" not in objs[n][0]), None)
    order = []

    def walk(n, depth=0):
        if n not in objs or depth > 20:
            return
        head = objs[n][0]
        if re.search(rb"/Type\s*/Page(?![s\w])", head):
            order.append(n)
            return
        k = re.search(rb"/Kids\s*\[(.*?)\]", head, re.S)
        for r in REF.finditer(k.group(1)) if k else []:
            walk(int(r.group(1)), depth + 1)
    if root is not None:
        walk(root)
    return order if len(order) == len(flat) else flat


def contents(objs, page):
    head = objs[page][0]
    m = re.search(rb"/Contents\s*(\[.*?\]|\d+\s+\d+\s+R)", head, re.S)
    if not m:
        return b""
    return b"\n".join(decode(*objs[int(r.group(1))]) for r in REF.finditer(m.group(1)) if int(r.group(1)) in objs)


def tokens(data):
    """(kind, value) tokens of a content stream: str (a text string), op, other."""
    i, n = 0, len(data)
    while i < n:
        c = data[i:i + 1]
        if c in b" \t\r\n\f\0":
            i += 1
        elif c == b"%":
            while i < n and data[i] not in b"\r\n":
                i += 1
        elif c == b"(":
            depth, i, buf = 1, i + 1, bytearray()
            while i < n and depth:
                ch = data[i]
                if ch == 0x5C and i + 1 < n:
                    i += 1
                    nx = data[i]
                    if 0x30 <= nx <= 0x37:
                        j = i
                        while j < n and j < i + 3 and 0x30 <= data[j] <= 0x37:
                            j += 1
                        buf.append(int(data[i:j], 8) & 255)
                        i = j - 1
                    elif nx in (10, 13):
                        if nx == 13 and data[i + 1:i + 2] == b"\n":
                            i += 1
                    else:
                        buf.extend(ESC.get(nx, chr(nx)).encode("latin-1"))
                elif ch == 0x28:
                    depth += 1
                    buf.append(ch)
                elif ch == 0x29:
                    depth -= 1
                    if depth:
                        buf.append(ch)
                else:
                    buf.append(ch)
                i += 1
            yield "str", bytes(buf).decode("cp1252", "replace")
        elif c == b"<" and data[i + 1:i + 2] != b"<":
            j = data.find(b">", i)
            j = n if j < 0 else j
            h = re.sub(rb"[^0-9A-Fa-f]", b"", data[i + 1:j])
            h += b"0" if len(h) % 2 else b""
            yield "str", bytes.fromhex(h.decode()).decode("cp1252", "replace")
            i = j + 1
        elif c in b"[]":
            yield "other", c.decode()
            i += 1
        elif c in b"<>":
            i += 2 if data[i + 1:i + 2] == c else 1
        elif c == b"/":
            m = re.compile(rb"/[^\s/<>\[\]()]*").match(data, i)
            yield "other", m.group(0).decode("latin-1")
            i = m.end()
        else:
            m = re.compile(rb"[^\s/<>\[\]()%]+").match(data, i)
            if not m:
                i += 1
                continue
            word = m.group(0).decode("latin-1")
            yield ("other" if re.fullmatch(r"[-+.\d]+", word) else "op"), word
            i = m.end()


def page_text(data):
    out, strings = [], []
    for kind, v in tokens(data):
        if kind == "str":
            strings.append(v)
        elif kind == "other":
            if v == "]":
                continue
            try:                                          # a big negative TJ adjustment is a word space
                if float(v) < -200 and strings:
                    strings.append(" ")
            except ValueError:
                pass
        else:
            if v in ("Tj", "TJ"):
                out.append("".join(strings))
            elif v in ("'", '"'):
                out.append("\n" + "".join(strings))
            elif v in ("Td", "TD", "T*", "Tm", "ET"):
                out.append("\n")
            strings = []
    text = "".join(out)
    return "\n".join(line.rstrip() for line in re.sub(r"\n{2,}", "\n", text).split("\n")).strip()


def run(inputs):
    path = safe(inputs["path"])
    with open(path, "rb") as f:
        data = f.read()
    if not data.startswith(b"%PDF"):
        raise ValueError(f"{path} is not a PDF file")
    if re.search(rb"/Encrypt\b", data):
        raise ValueError(f"{path} is encrypted; this tool cannot read it")
    objs = objects(data)
    order = pages(objs)
    if not order:
        raise ValueError("no pages found: the file may use object streams, which this tool cannot read")
    first, last = int(inputs.get("first_page") or 1), inputs.get("last_page")
    last = len(order) if not last else min(int(last), len(order))
    result, empty = [], []
    for number in range(max(first, 1), last + 1):
        text = page_text(contents(objs, order[number - 1]))
        result.append({"page": number, "text": text})
        if not text:
            empty.append(number)
    return {"pages": result, "page_count": len(order), "empty_pages": empty,
            "text": "\n\n".join(p["text"] for p in result if p["text"])}
