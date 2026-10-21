import os
import tempfile
import unittest
import zipfile

import tool

NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def make(name, body):
    with zipfile.ZipFile(name, "w") as z:
        z.writestr("word/document.xml", f'<?xml version="1.0"?><w:document {NS}><w:body>{body}</w:body></w:document>')


def p(text, style=""):
    st = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f"<w:p>{st}<w:r><w:t>{text}</w:t></w:r></w:p>"


class DocxReadTest(unittest.TestCase):
    def setUp(self):
        self.old = os.getcwd()
        os.chdir(tempfile.mkdtemp(prefix="mini-docx-"))
        self.addCleanup(os.chdir, self.old)

    def test_paragraphs_tables_and_order(self):
        cell = lambda t: f"<w:tc>{p(t)}</w:tc>"
        make("a.docx", p("Title", "Heading1") + p("Body &amp; more")
             + "<w:tbl><w:tr>" + cell("a") + cell("b") + "</w:tr><w:tr>" + cell("1") + cell("2") + "</w:tr></w:tbl>"
             + '<w:p><w:r><w:t>x</w:t><w:tab/><w:t>y</w:t><w:br/><w:t>z</w:t></w:r><w:r><w:t>!</w:t></w:r></w:p>')
        out = tool.run({"path": "a.docx"})
        self.assertEqual([b["type"] for b in out["blocks"]], ["paragraph", "paragraph", "table", "paragraph"])
        self.assertEqual(out["blocks"][0], {"type": "paragraph", "style": "Heading1", "text": "Title"})
        self.assertEqual(out["blocks"][1]["text"], "Body & more")
        self.assertEqual(out["blocks"][2]["rows"], [["a", "b"], ["1", "2"]])
        self.assertEqual(out["blocks"][3]["text"], "x\ty\nz!")
        self.assertEqual(out["text"], "Title\nBody & more\na | b\n1 | 2\nx\ty\nz!")
        self.assertEqual((out["paragraphs"], out["tables"]), (3, 1))

    def test_empty_document_and_bad_files(self):
        make("e.docx", "")
        self.assertEqual(tool.run({"path": "e.docx"})["text"], "")
        with open("bad.docx", "w") as f:
            f.write("not a zip")
        with self.assertRaisesRegex(ValueError, "not a readable"):
            tool.run({"path": "bad.docx"})
        with zipfile.ZipFile("nodoc.docx", "w") as z:
            z.writestr("x.txt", "x")
        with self.assertRaisesRegex(ValueError, "not a readable"):
            tool.run({"path": "nodoc.docx"})
        for bad in ("/etc/hosts", "../a.docx"):
            with self.assertRaisesRegex(ValueError, "relative path"):
                tool.run({"path": bad})
        with self.assertRaises(FileNotFoundError):
            tool.run({"path": "none.docx"})


if __name__ == "__main__":
    unittest.main()
