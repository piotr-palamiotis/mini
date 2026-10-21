"""Set cells in an existing .xlsx workbook, keeping everything else (other cells, styles, sheets) as it is.

Works on the raw XML of the one sheet, so formatting, charts and other parts of the file are copied unchanged. A
changed cell keeps its style. Values: string, number, boolean, null (clears the cell); a string starting with "="
is a formula. Calculated values of formulas are not refreshed until Excel opens the file.
"""
import os
import re
import zipfile
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG = "{http://schemas.openxmlformats.org/package/2006/relationships}"
REF = re.compile(r"^([A-Z]{1,3})([1-9][0-9]*)$")
DATA = re.compile(r"<sheetData\s*/>|<sheetData(?:\s[^>]*)?>(.*?)</sheetData>", re.S)
ROW = re.compile(r"<row\b([^>]*?)(?:/>|>(.*?)</row>)", re.S)
CELL = re.compile(r"<c\b[^>]*/>|<c\b[^>]*>.*?</c>", re.S)


def safe(path):
    if not isinstance(path, str) or not path or os.path.isabs(path) or ".." in path.replace("\\", "/").split("/"):
        raise ValueError("path must be a relative path inside the working folder")
    return path


def col_index(letters):
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def cell_xml(ref, v, style=""):
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


def sheet_file(z, want):
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels")).iter(PKG + "Relationship")}
    sheets = [(s.get("name"), rels[s.get(R + "id")]) for s in wb.iter(M + "sheet")]
    if not sheets:
        raise ValueError("the workbook has no sheets")
    if want is None:
        name, target = sheets[0]
    else:
        match = [x for x in sheets if x[0] == want]
        if not match:
            raise ValueError(f"no sheet named {want!r}; the sheets are: {', '.join(n for n, _ in sheets)}")
        name, target = match[0]
    return name, target.lstrip("/") if target.startswith("/") else "xl/" + target


def edit_sheet(xml, cells):
    m = DATA.search(xml)
    if not m:
        raise ValueError("the sheet has no sheetData")
    rows = {}                                              # row number -> [attrs, {col index: cell xml}]
    for r in ROW.finditer(m.group(1) or ""):
        attrs = re.sub(r'\s+spans="[^"]*"', "", r.group(1)).rstrip("/ ")
        num = int(re.search(r'\br="(\d+)"', attrs).group(1))
        cs = {}
        for c in CELL.findall(r.group(2) or ""):
            cs[col_index(REF.match(re.search(r'\br="([A-Z]+\d+)"', c).group(1)).group(1))] = c
        rows[num] = [attrs, cs]
    for ref, value in cells.items():
        letters, num = REF.match(ref).groups()
        col = col_index(letters)
        attrs, cs = rows.setdefault(int(num), [f' r="{num}"', {}])
        old = re.search(r'\bs="(\d+)"', re.match(r"<c\b[^>]*", cs[col]).group(0)) if col in cs else None
        new = cell_xml(ref, value, old.group(1) if old else "")
        if new:
            cs[col] = new
        else:
            cs.pop(col, None)
    body = "".join((f"<row{a}>" + "".join(cs[k] for k in sorted(cs)) + "</row>") if cs else f"<row{a}/>"
                   for _, (a, cs) in sorted(rows.items()))
    xml = xml[:m.start()] + f"<sheetData>{body}</sheetData>" + xml[m.end():]
    return re.sub(r"<dimension\b[^>]*/>", "", xml)


def run(inputs):
    path = safe(inputs["path"])
    cells = inputs["cells"]
    if not isinstance(cells, dict) or not cells:
        raise ValueError("cells must be an object {cell reference: value}, e.g. {\"B2\": 5}")
    bad = [k for k in cells if not REF.match(str(k))]
    if bad:
        raise ValueError(f"not cell references (use A1 style, capital letters): {', '.join(map(str, bad))}")
    for k, v in cells.items():
        cell_xml(k, v)                                     # reject unsupported values before touching the file
        if col_index(REF.match(k).group(1)) > 16383:
            raise ValueError(f"{k} is outside the sheet (last column XFD)")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"{path} does not exist")
    output = safe(inputs.get("output") or path)
    if output != path and os.path.exists(output):
        raise FileExistsError(f"{output} exists; choose another output path")
    try:
        with zipfile.ZipFile(path) as z:
            name, file = sheet_file(z, inputs.get("sheet"))
            items = [(i, z.read(i.filename)) for i in z.infolist()]
    except (zipfile.BadZipFile, KeyError, ET.ParseError):
        raise ValueError(f"{path} is not a readable .xlsx file")
    tmp = output + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for info, data in items:
            if info.filename == file:
                data = edit_sheet(data.decode("utf-8"), cells).encode("utf-8")
            z.writestr(info, data)
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    os.replace(tmp, output)
    return {"path": output, "sheet": name, "cells_set": len(cells)}
