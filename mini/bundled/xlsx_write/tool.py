"""Create an .xlsx workbook from rows of values. Standard library only (zip + XML).

Numbers and booleans are stored as such, text as text, None leaves the cell empty, and a string that starts with
"=" is stored as a formula (it has no calculated value until Excel opens the file). No styles, charts or dates.
"""
import os
import re
import zipfile
from xml.sax.saxutils import escape

TYPES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
         '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
         '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
         '<Default Extension="xml" ContentType="application/xml"/>'
         '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
         '{sheets}</Types>')
SHEET_TYPE = '<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>')
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
BAD_SHEET = re.compile(r"[\[\]:*?/\\]")


def safe(path):
    if not isinstance(path, str) or not path or os.path.isabs(path) or ".." in path.replace("\\", "/").split("/"):
        raise ValueError("path must be a relative path inside the working folder")
    return path


def col_name(i):
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def cell_xml(ref, v, style=""):
    """The <c> element for a value, or "" for None."""
    s = f' s="{style}"' if style else ""
    if v is None:
        return f'<c r="{ref}"{s}/>' if style else ""
    if isinstance(v, bool):
        return f'<c r="{ref}"{s} t="b"><v>{int(v)}</v></c>'
    if isinstance(v, (int, float)):
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError(f"{ref}: {v} cannot be stored in a spreadsheet")
        return f'<c r="{ref}"{s}><v>{v!r}</v></c>'
    if isinstance(v, str):
        if v.startswith("=") and len(v) > 1:
            return f'<c r="{ref}"{s}><f>{escape(v[1:])}</f></c>'
        return f'<c r="{ref}"{s} t="inlineStr"><is><t xml:space="preserve">{escape(v)}</t></is></c>'
    raise ValueError(f"{ref}: a cell holds a string, number, boolean or null, not {type(v).__name__}")


def sheet_xml(rows, name="?"):
    if not isinstance(rows, list) or not all(isinstance(r, list) for r in rows):
        got = type(rows).__name__ if not isinstance(rows, list) else \
            f"row {next(i for i, r in enumerate(rows, 1) if not isinstance(r, list))} is not a list"
        raise ValueError(f"sheet {name!r}: every sheet must be a list of rows, each row a list of values (got {got}). "
                         f"sheets holds only {{sheet name: rows}}; options such as overwrite go next to it in the inputs")
    out = [f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="{MAIN}"><sheetData>']
    for n, row in enumerate(rows, 1):
        cells = "".join(cell_xml(f"{col_name(i)}{n}", v) for i, v in enumerate(row))
        out.append(f'<row r="{n}">{cells}</row>' if cells else f'<row r="{n}"/>')
    return "".join(out) + "</sheetData></worksheet>"


def run(inputs):
    sheets = inputs["sheets"]
    if isinstance(sheets, dict) and isinstance(sheets.get("overwrite"), bool):   # an option put inside sheets
        sheets = {k: v for k, v in sheets.items() if k != "overwrite"}
        inputs = dict(inputs, overwrite=inputs.get("overwrite") or inputs["sheets"]["overwrite"])
    path = safe(inputs["path"])
    if os.path.exists(path) and not inputs.get("overwrite"):
        raise FileExistsError(f"{path} exists; pass overwrite=true to replace it (xlsx_edit changes cells in place)")
    if not isinstance(sheets, dict) or not sheets:
        raise ValueError("sheets must be an object {sheet name: rows} with at least one sheet")
    for name in sheets:
        if not name or len(name) > 31 or BAD_SHEET.search(name):
            raise ValueError(f"bad sheet name {name!r}: 1-31 characters, none of []:*?/\\")
    parts = {name: sheet_xml(rows, name) for name, rows in sheets.items()}
    names = list(parts)
    wb = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="{MAIN}" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
          + "".join(f'<sheet name="{escape(n, {chr(34): "&quot;"})}" sheetId="{i}" r:id="rId{i}"/>' for i, n in enumerate(names, 1))
          + "</sheets></workbook>")
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                      f'Target="worksheets/sheet{i}.xml"/>' for i in range(1, len(names) + 1)) + "</Relationships>")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", TYPES.format(sheets="".join(SHEET_TYPE.format(i=i) for i in range(1, len(names) + 1))))
        z.writestr("_rels/.rels", RELS)
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", rels)
        for i, n in enumerate(names, 1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", parts[n])
    return {"path": path, "sheets": names, "rows": {n: len(sheets[n]) for n in names}}
