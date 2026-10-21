"""Create a .docx file from headings, paragraphs and tables. Standard library only (zip + XML).

Blocks: {"type": "heading", "text": ..., "level": 1-3}, {"type": "paragraph", "text": ...} (a string is a paragraph)
and {"type": "table", "rows": [[cell, ...], ...]} (the first row is the header). No images or custom styling.
"""
import os
import zipfile
from xml.sax.saxutils import escape

NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
TYPES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
         '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
         '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
         '<Default Extension="xml" ContentType="application/xml"/>'
         '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
         '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
         '</Types>')
RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        '</Relationships>')
DOC_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            '</Relationships>')


def styles():
    out = [f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles {NS}>',
           '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>']
    for i, size in ((1, 32), (2, 26), (3, 24)):
        out.append(f'<w:style w:type="paragraph" w:styleId="Heading{i}"><w:name w:val="heading {i}"/>'
                   f'<w:basedOn w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:before="240" w:after="80"/></w:pPr>'
                   f'<w:rPr><w:b/><w:sz w:val="{size}"/></w:rPr></w:style>')
    out.append('<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/><w:tblPr><w:tblBorders>'
               + "".join(f'<w:{s} w:val="single" w:sz="4" w:space="0" w:color="808080"/>'
                         for s in ("top", "left", "bottom", "right", "insideH", "insideV"))
               + '</w:tblBorders></w:tblPr></w:style></w:styles>')
    return "".join(out)


def para(text, style="", bold=False):
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    rpr = "<w:rPr><w:b/></w:rPr>" if bold else ""
    parts = text.split("\n")
    runs = "".join(f'<w:r>{rpr}<w:t xml:space="preserve">{escape(part)}</w:t></w:r>' + ("<w:r><w:br/></w:r>" if i < len(parts) - 1 else "")
                   for i, part in enumerate(parts))
    return f"<w:p>{ppr}{runs}</w:p>"


def table(rows):
    if not rows or not all(isinstance(r, list) for r in rows):
        raise ValueError("a table needs rows: a list of lists of cell values")
    out = ['<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="0" w:type="auto"/></w:tblPr>']
    for i, r in enumerate(rows):
        out.append("<w:tr>" + "".join(f"<w:tc>{para('' if c is None else str(c), bold=i == 0)}</w:tc>" for c in r) + "</w:tr>")
    return "".join(out) + "</w:tbl>" + para("")


def block_xml(b):
    if isinstance(b, str):
        return para(b)
    if not isinstance(b, dict):
        raise ValueError("every block must be a string or an object")
    kind = b.get("type", "paragraph")
    if kind == "heading":
        level = int(b.get("level") or 1)
        if level not in (1, 2, 3):
            raise ValueError("heading level must be 1, 2 or 3")
        return para(str(b.get("text", "")), f"Heading{level}")
    if kind == "paragraph":
        return para(str(b.get("text", "")))
    if kind == "table":
        return table(b.get("rows"))
    raise ValueError(f"unknown block type {kind!r}: use heading, paragraph or table")


def safe(path):
    if not isinstance(path, str) or not path or os.path.isabs(path) or ".." in path.replace("\\", "/").split("/"):
        raise ValueError("path must be a relative path inside the working folder")
    return path


def run(inputs):
    path = safe(inputs["path"])
    if os.path.exists(path) and not inputs.get("overwrite"):
        raise FileExistsError(f"{path} exists; pass overwrite=true to replace it")
    blocks = inputs["blocks"]
    if not isinstance(blocks, list) or not blocks:
        raise ValueError("blocks must be a non-empty list")
    body = "".join(block_xml(b) for b in blocks)
    doc = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {NS}><w:body>{body}'
           '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" '
           'w:left="1440" w:header="708" w:footer="708" w:gutter="0"/></w:sectPr></w:body></w:document>')
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr("word/document.xml", doc)
        z.writestr("word/_rels/document.xml.rels", DOC_RELS)
        z.writestr("word/styles.xml", styles())
    return {"path": path, "blocks": len(blocks)}
