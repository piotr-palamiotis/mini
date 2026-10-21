"""Read the sheets of an .xlsx workbook as rows of values. Standard library only.

Cell values come back as stored: numbers as numbers, text as text, booleans as booleans, and formulas as the value
Excel last calculated (None when the file was never calculated). Dates are stored as serial numbers and stay
numbers. Rows start at column A, so a row's index is the column. Charts, images and styles are ignored.
"""
import os
import re
import zipfile
import xml.etree.ElementTree as ET

M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG = "{http://schemas.openxmlformats.org/package/2006/relationships}"
MAX_COLS = 200


def safe(path):
    if not isinstance(path, str) or not path or os.path.isabs(path) or ".." in path.replace("\\", "/").split("/"):
        raise ValueError("path must be a relative path inside the working folder")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"{path} does not exist")
    return path


def col_index(ref):
    n = 0
    for ch in re.match(r"[A-Za-z]+", ref).group(0).upper():
        n = n * 26 + ord(ch) - 64
    return n - 1


def text_of(el):
    return "".join(t.text or "" for t in el.iter(M + "t"))


def number(v):
    f = float(v)
    return int(f) if f.is_integer() and abs(f) < 2 ** 53 else f


def cell_value(c, shared):
    t, v = c.get("t"), c.find(M + "v")
    if t == "inlineStr":
        return text_of(c)
    if v is None or v.text is None:
        return None
    if t == "s":
        return shared[int(v.text)]
    if t == "b":
        return v.text == "1"
    if t in ("str", "e"):
        return v.text
    return number(v.text)


def sheet_files(z):
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels")).iter(PKG + "Relationship")}
    out = []
    for s in wb.iter(M + "sheet"):
        target = rels[s.get(R + "id")]
        out.append((s.get("name"), target.lstrip("/") if target.startswith("/") else "xl/" + target))
    return out


def read_sheet(z, file, shared, max_rows):
    """(rows, count): rows by row number, [] for rows without cells, count up to the last row in the file."""
    rows, last = [], 0
    for _, row in ET.iterparse(z.open(file), events=("end",)):
        if row.tag != M + "row":
            continue
        last = int(row.get("r") or last + 1)
        if last <= max_rows:
            cells = {}
            for c in row.findall(M + "c"):
                if c.get("r"):
                    i = col_index(c.get("r"))
                    if i < MAX_COLS:
                        cells[i] = cell_value(c, shared)
            rows.extend([] for _ in range(last - 1 - len(rows)))
            rows.append([cells.get(i) for i in range(max(cells) + 1)] if cells else [])
        row.clear()
    return rows, last


def run(inputs):
    path = safe(inputs["path"])
    max_rows = int(inputs.get("max_rows") or 200)
    try:
        with zipfile.ZipFile(path) as z:
            sheets = sheet_files(z)
            shared = []
            if "xl/sharedStrings.xml" in z.namelist():
                shared = [text_of(si) for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(M + "si")]
            want = inputs.get("sheet")
            if want and want not in [n for n, _ in sheets]:
                raise ValueError(f"no sheet named {want!r}; the sheets are: {', '.join(n for n, _ in sheets)}")
            out = []
            for name, file in sheets:
                if want and name != want:
                    continue
                rows, total = read_sheet(z, file, shared, max_rows)
                out.append({"name": name, "rows": rows, "row_count": total, "truncated": total > len(rows)})
    except (zipfile.BadZipFile, KeyError, ET.ParseError):
        raise ValueError(f"{path} is not a readable .xlsx file")
    return {"sheets": out, "sheet_names": [n for n, _ in sheets]}
