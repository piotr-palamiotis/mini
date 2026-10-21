"""Read a .docx file: paragraphs (with their style) and tables, in document order. Standard library only.

Limits: body text and tables of word/document.xml; headers, footers, footnotes, comments, text boxes, images and
tracked-change history are not read (inserted text is, deleted text is not).
"""
import os
import zipfile
import xml.etree.ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def safe(path):
    if not isinstance(path, str) or not path or os.path.isabs(path) or ".." in path.replace("\\", "/").split("/"):
        raise ValueError("path must be a relative path inside the working folder")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"{path} does not exist")
    return path


def paragraph_text(p):
    out = []
    for el in p.iter():
        if el.tag == W + "t":
            out.append(el.text or "")
        elif el.tag == W + "tab":
            out.append("\t")
        elif el.tag in (W + "br", W + "cr"):
            out.append("\n")
    return "".join(out)


def style_of(p):
    s = p.find(f"{W}pPr/{W}pStyle")
    return s.get(W + "val") if s is not None else ""


def table_rows(t):
    return [[" ".join(paragraph_text(p) for p in c.iter(W + "p")) for c in r.findall(W + "tc")]
            for r in t.findall(W + "tr")]


def run(inputs):
    path = safe(inputs["path"])
    try:
        with zipfile.ZipFile(path) as z:
            root = ET.fromstring(z.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ET.ParseError):
        raise ValueError(f"{path} is not a readable .docx file")
    body = root.find(W + "body")
    blocks = []
    for el in body if body is not None else []:
        if el.tag == W + "p":
            blocks.append({"type": "paragraph", "style": style_of(el), "text": paragraph_text(el)})
        elif el.tag == W + "tbl":
            blocks.append({"type": "table", "rows": table_rows(el)})
    lines = []
    for b in blocks:
        if b["type"] == "paragraph":
            lines.append(b["text"])
        else:
            lines.extend(" | ".join(r) for r in b["rows"])
    return {"blocks": blocks, "text": "\n".join(lines).strip(),
            "paragraphs": sum(b["type"] == "paragraph" for b in blocks),
            "tables": sum(b["type"] == "table" for b in blocks)}
