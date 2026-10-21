"""Write a plain-text PDF (Helvetica 11 pt on A4, wrapped lines, automatic pages) with the standard library only.

Limits: text only, no images, tables or styling; characters outside Latin-1 become "?"; a title is set in bold above
the text. Meant for simple reports and exports, not for layout.
"""
import os
import textwrap

WIDTH, HEIGHT, MARGIN, SIZE, LEAD = 595, 842, 56, 11, 14
COLS = 88
PER_PAGE = (HEIGHT - 2 * MARGIN) // LEAD


def safe(path, must_exist=False):
    if not isinstance(path, str) or not path or os.path.isabs(path) or ".." in path.replace("\\", "/").split("/"):
        raise ValueError("path must be a relative path inside the working folder")
    return path


def esc(s):
    s = s.encode("latin-1", "replace").decode("latin-1")
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def lines_of(text, title):
    out = [("B", l) for l in textwrap.wrap(title, COLS - 20)] + [("B", "")] if title else []
    for para in str(text).replace("\r\n", "\n").split("\n"):
        out.extend([("F", l) for l in textwrap.wrap(para, COLS, replace_whitespace=False, drop_whitespace=True)]
                   or [("F", "")])
    return out


def build(lines):
    chunks = [lines[i:i + PER_PAGE] for i in range(0, len(lines), PER_PAGE)] or [[]]
    objs = {}                                     # object number -> bytes; 1 catalog, 2 pages, 3-4 fonts
    objs[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    objs[4] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"
    kids = []
    for i, chunk in enumerate(chunks):
        page, content = 5 + 2 * i, 6 + 2 * i
        ops = [f"BT /F1 {SIZE} Tf {LEAD} TL {MARGIN} {HEIGHT - MARGIN - SIZE} Td"]
        font = "F1"
        for kind, line in chunk:
            want = "F2" if kind == "B" else "F1"
            if want != font:
                ops.append(f"/{want} {SIZE} Tf")
                font = want
            ops.append(f"({esc(line)}) Tj T*")
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1")
        objs[content] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
        objs[page] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {WIDTH} {HEIGHT}] /Contents {content} 0 R "
                      f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> >>").encode()
        kids.append(f"{page} 0 R")
    objs[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[2] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>".encode()
    out, offsets = bytearray(b"%PDF-1.4\n"), {}
    for n in sorted(objs):
        offsets[n] = len(out)
        out += b"%d 0 obj\n" % n + objs[n] + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for n in sorted(objs):
        out += b"%010d 00000 n \n" % offsets[n]
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out), len(chunks)


def run(inputs):
    path = safe(inputs["path"])
    if os.path.exists(path) and not inputs.get("overwrite"):
        raise FileExistsError(f"{path} exists; pass overwrite=true to replace it")
    text = inputs["text"]
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    data, pages = build(lines_of(text, str(inputs.get("title") or "")))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return {"path": path, "pages": pages, "bytes": len(data)}
