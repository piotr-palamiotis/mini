import os
import tempfile
import unittest
import zipfile

import tool

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
ROWS = ('<row r="1" spans="1:2"><c r="A1" s="3" t="s"><v>0</v></c><c r="B1"><v>5</v></c></row>'
        '<row r="3"><c r="A3" t="inlineStr"><is><t>keep</t></is></c></row>')


def make(name, data=f'<sheetData>{ROWS}</sheetData>'):
    with zipfile.ZipFile(name, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook xmlns="{MAIN}" xmlns:r="{REL}"><sheets><sheet name="One" sheetId="1" r:id="rId1"/>'
                   '<sheet name="Two" sheetId="2" r:id="rId2"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="x" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="x" Target="worksheets/sheet2.xml"/></Relationships>')
        z.writestr("xl/sharedStrings.xml", f'<sst xmlns="{MAIN}"><si><t>title</t></si></sst>')
        z.writestr("xl/styles.xml", "<styleSheet/>")
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{MAIN}"><dimension ref="A1:B3"/>{data}</worksheet>')
        z.writestr("xl/worksheets/sheet2.xml", f'<worksheet xmlns="{MAIN}"><sheetData/></worksheet>')


def part(name, file="xl/worksheets/sheet1.xml"):
    with zipfile.ZipFile(name) as z:
        return z.read(file).decode()


class XlsxEditTest(unittest.TestCase):
    def setUp(self):
        self.old = os.getcwd()
        os.chdir(tempfile.mkdtemp(prefix="mini-xlsx-"))
        self.addCleanup(os.chdir, self.old)

    def test_edit_keeps_style_order_and_other_parts(self):
        make("a.xlsx")
        out = tool.run({"path": "a.xlsx", "cells": {"A1": "new <t>", "C1": 7, "B2": "=A1&B1", "A3": None, "D5": True}})
        self.assertEqual((out["sheet"], out["cells_set"]), ("One", 5))
        xml = part("a.xlsx")
        self.assertIn('<c r="A1" s="3" t="inlineStr"><is><t xml:space="preserve">new &lt;t&gt;</t></is></c>', xml)
        self.assertIn('<c r="B1"><v>5</v></c><c r="C1"><v>7</v></c>', xml)         # B1 untouched, C1 after it
        self.assertIn('<row r="2"><c r="B2"><f>A1&amp;B1</f></c></row>', xml)       # new row between 1 and 3
        self.assertIn('<row r="3"/>', xml)                                         # cleared cell, row stays
        self.assertNotIn("keep", xml)
        self.assertLess(xml.index('<row r="3"'), xml.index('<row r="5"'))
        self.assertIn('<c r="D5" t="b"><v>1</v></c>', xml)
        self.assertNotIn("spans", xml)
        self.assertNotIn("<dimension", xml)
        self.assertEqual(part("a.xlsx", "xl/styles.xml"), "<styleSheet/>")
        self.assertEqual(part("a.xlsx", "xl/sharedStrings.xml").count("title"), 1)

    def test_other_sheet_empty_sheet_and_output(self):
        make("a.xlsx")
        before = open("a.xlsx", "rb").read()
        out = tool.run({"path": "a.xlsx", "sheet": "Two", "cells": {"AA2": "x"}, "output": "b.xlsx"})
        self.assertEqual((out["path"], out["sheet"]), ("b.xlsx", "Two"))
        self.assertIn('<row r="2"><c r="AA2" t="inlineStr">', part("b.xlsx", "xl/worksheets/sheet2.xml"))
        self.assertEqual(part("b.xlsx"), part("a.xlsx"))
        self.assertEqual(open("a.xlsx", "rb").read(), before)

    def test_refusals_leave_the_file_alone(self):
        make("a.xlsx")
        before = open("a.xlsx", "rb").read()
        for cells, msg in [({}, "cells must be"), ({"a1": 1}, "not cell references"), ({"B0": 1}, "not cell references"),
                           ({"A1": [1]}, "not list"), ({"XFE1": 1}, "outside the sheet")]:
            with self.assertRaisesRegex(ValueError, msg):
                tool.run({"path": "a.xlsx", "cells": cells})
        with self.assertRaisesRegex(ValueError, "no sheet named 'X'"):
            tool.run({"path": "a.xlsx", "cells": {"A1": 1}, "sheet": "X"})
        with self.assertRaises(FileExistsError):
            make("o.xlsx")
            tool.run({"path": "a.xlsx", "cells": {"A1": 1}, "output": "o.xlsx"})
        with open("bad.xlsx", "w") as f:
            f.write("x")
        with self.assertRaisesRegex(ValueError, "not a readable"):
            tool.run({"path": "bad.xlsx", "cells": {"A1": 1}})
        with self.assertRaisesRegex(ValueError, "relative path"):
            tool.run({"path": "/tmp/a.xlsx", "cells": {"A1": 1}})
        self.assertEqual(open("a.xlsx", "rb").read(), before)
        self.assertEqual([f for f in os.listdir(".") if f.endswith(".tmp")], [])


if __name__ == "__main__":
    unittest.main()
