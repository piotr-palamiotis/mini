import os
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET

import tool

M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


class XlsxWriteTest(unittest.TestCase):
    def setUp(self):
        self.old = os.getcwd()
        os.chdir(tempfile.mkdtemp(prefix="mini-xlsx-"))
        self.addCleanup(os.chdir, self.old)

    def test_workbook_parts_and_cells(self):
        out = tool.run({"path": "o/a.xlsx", "sheets": {
            "Data": [["name", "qty", "ok"], ["a & <b>", 3, True], ["c", 2.5, None, "=B2*B3"], [], ["z"]],
            "Two": [[1]]}})
        self.assertEqual(out["sheets"], ["Data", "Two"])
        self.assertEqual(out["rows"], {"Data": 5, "Two": 1})
        with zipfile.ZipFile("o/a.xlsx") as z:
            self.assertIsNone(z.testzip())
            for n in z.namelist():
                ET.fromstring(z.read(n))
            self.assertEqual([s.get("name") for s in ET.fromstring(z.read("xl/workbook.xml")).iter(M + "sheet")], ["Data", "Two"])
            sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
        cells = {c.get("r"): c for c in sheet.iter(M + "c")}
        self.assertEqual(set(cells), {"A1", "B1", "C1", "A2", "B2", "C2", "A3", "B3", "D3", "A5"})
        self.assertEqual(cells["A2"].find(f"{M}is/{M}t").text, "a & <b>")
        self.assertEqual((cells["C2"].get("t"), cells["C2"].find(M + "v").text), ("b", "1"))
        self.assertEqual(cells["B3"].find(M + "v").text, "2.5")
        self.assertEqual(cells["D3"].find(M + "f").text, "B2*B3")
        self.assertEqual(len(sheet.findall(f"{M}sheetData/{M}row")), 5)

    def test_wide_rows_get_two_letter_columns(self):
        tool.run({"path": "w.xlsx", "sheets": {"S": [list(range(28))]}})
        with zipfile.ZipFile("w.xlsx") as z:
            refs = [c.get("r") for c in ET.fromstring(z.read("xl/worksheets/sheet1.xml")).iter(M + "c")]
        self.assertEqual(refs[25:28], ["Z1", "AA1", "AB1"])

    def test_refusals(self):
        tool.run({"path": "a.xlsx", "sheets": {"S": [[1]]}})
        with self.assertRaises(FileExistsError):
            tool.run({"path": "a.xlsx", "sheets": {"S": [[2]]}})
        tool.run({"path": "a.xlsx", "sheets": {"S": [[2]]}, "overwrite": True})
        for sheets, msg in [({}, "at least one"), ({"a/b": [[1]]}, "bad sheet name"), ({"x" * 32: [[1]]}, "bad sheet name"),
                            ({"S": [1]}, "list of rows"), ({"S": [[1]], "T": True}, "sheet 'T'.*got bool"), ({"S": [[{"a": 1}]]}, "not dict"),
                            ({"S": [[float("nan")]]}, "cannot be stored")]:
            with self.assertRaisesRegex(ValueError, msg):
                tool.run({"path": "b.xlsx", "sheets": sheets})
        got = tool.run({"path": "a.xlsx", "sheets": {"S": [[3]], "overwrite": True}})    # the option put inside sheets
        self.assertEqual(got["sheets"], ["S"])
        with self.assertRaisesRegex(ValueError, "relative path"):
            tool.run({"path": "../b.xlsx", "sheets": {"S": [[1]]}})


if __name__ == "__main__":
    unittest.main()
