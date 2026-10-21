"""Find and replace text in the body of a .docx file, keeping all formatting. Standard library only.

Works on the raw XML of word/document.xml, so everything else in the file is copied unchanged. Text split over
several runs (formatting changes in the middle of a phrase) is matched; the replacement takes the formatting of the
run where the match starts. Headers, footers and footnotes are not touched.
"""
import os
import re
import zipfile
from xml.sax.saxutils import escape, unescape

PARA = re.compile(r"<w:p[ >].*?</w:p>", re.S)
TEXT = re.compile(r"<w:t(?P<attrs>(?: [^>]*)?)>(?P<text>.*?)</w:t>", re.S)


def safe(path):
    if not isinstance(path, str) or not path or os.path.isabs(path) or ".." in path.replace("\\", "/").split("/"):
        raise ValueError("path must be a relative path inside the working folder")
    return path


def replace_in_paragraph(xml, find, new, limit):
    """(xml, count): replace up to `limit` matches of find in the text runs of one paragraph."""
    ms = list(TEXT.finditer(xml))
    texts = [unescape(m.group("text")) for m in ms]
    full = "".join(texts)
    count, pos = 0, 0
    edits = []                                       # (start, end) spans in full, non-overlapping
    while len(edits) < limit:
        i = full.find(find, pos)
        if i < 0:
            break
        edits.append((i, i + len(find)))
        pos = i + len(find)
    if not edits:
        return xml, 0
    starts = [sum(len(t) for t in texts[:k]) for k in range(len(texts))]
    out = list(texts)
    for a, b in reversed(edits):
        first = max(k for k in range(len(texts)) if starts[k] <= a)
        for k in range(first, len(texts)):
            lo, hi = starts[k], starts[k] + len(texts[k])
            if lo >= b:
                break
            cut_a, cut_b = max(a, lo) - lo, min(b, hi) - lo
            out[k] = out[k][:cut_a] + (new if k == first else "") + out[k][cut_b:]
    pieces, last = [], 0
    for m, text in zip(ms, out):
        attrs = m.group("attrs")
        if text != unescape(m.group("text")) and "xml:space" not in attrs:
            attrs += ' xml:space="preserve"'
        pieces.append(xml[last:m.start()] + f"<w:t{attrs}>{escape(text)}</w:t>")
        last = m.end()
    return "".join(pieces) + xml[last:], len(edits)


def run(inputs):
    path = safe(inputs["path"])
    find, new = inputs["find"], inputs["replace"]
    if not isinstance(find, str) or not find or "\n" in find:
        raise ValueError("find must be a non-empty single-line string")
    if not isinstance(new, str):
        raise ValueError("replace must be a string")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"{path} does not exist")
    output = safe(inputs.get("output") or path)
    if output != path and os.path.exists(output):
        raise FileExistsError(f"{output} exists; choose another output path")
    limit = 1 if inputs.get("first_only") else 10 ** 9
    total = [0]

    def para(m):
        if total[0] >= limit:
            return m.group(0)
        xml, n = replace_in_paragraph(m.group(0), find, new, limit - total[0])
        total[0] += n
        return xml
    try:
        with zipfile.ZipFile(path) as z:
            items = [(i, z.read(i.filename)) for i in z.infolist()]
    except zipfile.BadZipFile:
        raise ValueError(f"{path} is not a readable .docx file")
    if not any(i.filename == "word/document.xml" for i, _ in items):
        raise ValueError(f"{path} is not a readable .docx file")
    tmp = output + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for info, data in items:
            if info.filename == "word/document.xml":
                data = PARA.sub(para, data.decode("utf-8")).encode("utf-8")
            z.writestr(info, data)
    if total[0] == 0:
        os.remove(tmp)
        return {"path": path, "replacements": 0, "changed": False}
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    os.replace(tmp, output)
    return {"path": output, "replacements": total[0], "changed": True}
