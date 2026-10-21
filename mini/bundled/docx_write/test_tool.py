import os
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET

import tool

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class DocxWriteTest(unittest.TestCase):
    def setUp(self):
        self.old = os.getcwd()
        os.chdir(tempfile.mkdtemp(prefix="mini-docx-"))
        self.addCleanup(os.chdir, self.old)

    def test_package_is_well_formed_and_content_in_order(self):
        out = tool.run({"path": "out/a.docx", "blocks": [
            {"type": "heading", "text": "Plan <1> & co", "level": 2}, "plain",
            {"type": "paragraph", "text": "two\nlines"},
            {"type": "table", "rows": [["h1", "h2"], [1, None]]}]})
        self.assertEqual(out, {"path": "out/a.docx", "blocks": 4})
        with zipfile.ZipFile("out/a.docx") as z:
            self.assertIsNone(z.testzip())
            for name in z.namelist():
                ET.fromstring(z.read(name))                      # every part parses
            self.assertIn("[Content_Types].xml", z.namelist())
            root = ET.fromstring(z.read("word/document.xml"))
        body = root.find(W + "body")
        kinds = [el.tag.replace(W, "") for el in body]
        self.assertEqual(kinds, ["p", "p", "p", "tbl", "p", "sectPr"])
        self.assertEqual(body[0].find(f"{W}pPr/{W}pStyle").get(W + "val"), "Heading2")
        self.assertEqual("".join(t.text for t in body[0].iter(W + "t")), "Plan <1> & co")
        self.assertEqual(len(list(body[2].iter(W + "br"))), 1)
        cells = ["".join(t.text or "" for t in c.iter(W + "t")) for c in body[3].iter(W + "tc")]
        self.assertEqual(cells, ["h1", "h2", "1", ""])

    def test_refusals(self):
        tool.run({"path": "a.docx", "blocks": ["x"]})
        with self.assertRaises(FileExistsError):
            tool.run({"path": "a.docx", "blocks": ["y"]})
        tool.run({"path": "a.docx", "blocks": ["y"], "overwrite": True})
        for blocks, msg in [([], "non-empty"), ([{"type": "image"}], "unknown block type"),
                            ([{"type": "heading", "text": "x", "level": 7}], "level"),
                            ([{"type": "table", "rows": []}], "needs rows"), ([5], "string or an object")]:
            with self.assertRaisesRegex(ValueError, msg):
                tool.run({"path": "b.docx", "blocks": blocks})
        self.assertFalse(os.path.exists("b.docx"))
        with self.assertRaisesRegex(ValueError, "relative path"):
            tool.run({"path": "/tmp/x.docx", "blocks": ["x"]})


if __name__ == "__main__":
    unittest.main()
