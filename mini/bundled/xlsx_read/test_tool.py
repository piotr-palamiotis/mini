import os
import tempfile
import unittest
import zipfile

import tool

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def make(name, sheets, shared=("hello", "world")):
    """A workbook written by hand the way Excel does: shared strings, relationship ids, absolute and relative targets."""
    with zipfile.ZipFile(name, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook xmlns="{MAIN}" xmlns:r="{REL}"><sheets>'
                   + "".join(f'<sheet name="{n}" sheetId="{i}" r:id="rId{i}"/>' for i, n in enumerate(sheets, 1)) + "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   + "".join(f'<Relationship Id="rId{i}" Type="x" Target="{"/xl/" if i == 2 else ""}worksheets/sheet{i}.xml"/>'
                             for i in range(1, len(sheets) + 1)) + "</Relationships>")
        z.writestr("xl/sharedStrings.xml", f'<sst xmlns="{MAIN}">' + "".join(f"<si><t>{s}</t></si>" for s in shared) + "</sst>")
        for i, rows in enumerate(sheets.values(), 1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", f'<worksheet xmlns="{MAIN}"><sheetData>{rows}</sheetData></worksheet>')


ROWS = ('<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
        '<row r="2"><c r="A2"><v>3</v></c><c r="C2"><v>2.5</v></c><c r="D2" t="b"><v>1</v></c></row>'
        '<row r="4"><c r="B4" t="inlineStr"><is><t>inline</t></is></c><c r="C4"><f>A2*2</f><v>6</v></c><c r="D4"><f>1+1</f></c></row>')


class XlsxReadTest(unittest.TestCase):
    def setUp(self):
        self.old = os.getcwd()
        os.chdir(tempfile.mkdtemp(prefix="mini-xlsx-"))
        self.addCleanup(os.chdir, self.old)

    def test_values_gaps_and_sheets(self):
        make("a.xlsx", {"One": ROWS, "Two": '<row r="1"><c r="A1"><v>9</v></c></row>'})
        out = tool.run({"path": "a.xlsx"})
        self.assertEqual(out["sheet_names"], ["One", "Two"])
        one = out["sheets"][0]
        self.assertEqual(one["rows"], [["hello", "world"], [3, None, 2.5, True], [], [None, "inline", 6, None]])
        self.assertEqual((one["row_count"], one["truncated"]), (4, False))
        self.assertEqual(out["sheets"][1]["rows"], [[9]])
        only = tool.run({"path": "a.xlsx", "sheet": "Two"})
        self.assertEqual([s["name"] for s in only["sheets"]], ["Two"])

    def test_max_rows_truncates(self):
        make("a.xlsx", {"One": ROWS})
        s = tool.run({"path": "a.xlsx", "max_rows": 1})["sheets"][0]
        self.assertEqual((len(s["rows"]), s["row_count"], s["truncated"]), (1, 4, True))

    def test_refusals(self):
        make("a.xlsx", {"One": ROWS})
        with self.assertRaisesRegex(ValueError, "no sheet named 'Nope'.*One"):
            tool.run({"path": "a.xlsx", "sheet": "Nope"})
        with open("bad.xlsx", "w") as f:
            f.write("x")
        with self.assertRaisesRegex(ValueError, "not a readable"):
            tool.run({"path": "bad.xlsx"})
        with self.assertRaisesRegex(ValueError, "relative path"):
            tool.run({"path": "/etc/hosts"})
        with self.assertRaises(FileNotFoundError):
            tool.run({"path": "none.xlsx"})


if __name__ == "__main__":
    unittest.main()
