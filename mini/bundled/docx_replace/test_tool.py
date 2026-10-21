import os
import tempfile
import unittest
import zipfile

import tool

NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
BODY = ('<w:p><w:r><w:t>Dear NAME, hello</w:t></w:r></w:p>'
        '<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Dear N</w:t></w:r><w:r><w:t>AME</w:t></w:r><w:r><w:t xml:space="preserve">, again NAME</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>nothing here</w:t></w:r></w:p>')


def make(name, body=BODY):
    with zipfile.ZipFile(name, "w") as z:
        z.writestr("word/document.xml", f'<?xml version="1.0"?><w:document {NS}><w:body>{body}</w:body></w:document>')
        z.writestr("word/styles.xml", "<styles/>")


def doc(name):
    with zipfile.ZipFile(name) as z:
        return z.read("word/document.xml").decode(), z.read("word/styles.xml").decode()


class DocxReplaceTest(unittest.TestCase):
    def setUp(self):
        self.old = os.getcwd()
        os.chdir(tempfile.mkdtemp(prefix="mini-docx-"))
        self.addCleanup(os.chdir, self.old)

    def test_replaces_across_runs_keeping_formatting_and_other_parts(self):
        make("a.docx")
        out = tool.run({"path": "a.docx", "find": "NAME", "replace": "Ana & Bo"})
        self.assertEqual((out["replacements"], out["changed"]), (3, True))
        xml, styles = doc("a.docx")
        self.assertIn('<w:t xml:space="preserve">Dear Ana &amp; Bo, hello</w:t>', xml)
        self.assertIn('<w:rPr><w:b/></w:rPr><w:t xml:space="preserve">Dear Ana &amp; Bo</w:t>', xml)       # the match started in the bold run
        self.assertIn('<w:t xml:space="preserve"></w:t>', xml)                           # the emptied middle run stays
        self.assertIn('<w:t xml:space="preserve">, again Ana &amp; Bo</w:t>', xml)
        self.assertIn("nothing here", xml)
        self.assertEqual(styles, "<styles/>")

    def test_first_only_output_and_no_match(self):
        make("a.docx")
        before = open("a.docx", "rb").read()
        out = tool.run({"path": "a.docx", "find": "NAME", "replace": "X", "first_only": True, "output": "b.docx"})
        self.assertEqual((out["path"], out["replacements"]), ("b.docx", 1))
        self.assertEqual(open("a.docx", "rb").read(), before, "the source stays when an output path is given")
        self.assertEqual(doc("b.docx")[0].count("X"), 1)
        none = tool.run({"path": "a.docx", "find": "zzz", "replace": "q"})
        self.assertEqual((none["replacements"], none["changed"]), (0, False))
        self.assertEqual(open("a.docx", "rb").read(), before)
        self.assertFalse(os.path.exists("a.docx.tmp"))

    def test_refusals(self):
        make("a.docx")
        with self.assertRaisesRegex(ValueError, "find"):
            tool.run({"path": "a.docx", "find": "", "replace": "x"})
        with self.assertRaises(FileExistsError):
            make("o.docx")
            tool.run({"path": "a.docx", "find": "NAME", "replace": "x", "output": "o.docx"})
        with open("bad.docx", "w") as f:
            f.write("x")
        with self.assertRaisesRegex(ValueError, "not a readable"):
            tool.run({"path": "bad.docx", "find": "a", "replace": "b"})
        with self.assertRaisesRegex(ValueError, "relative path"):
            tool.run({"path": "../a.docx", "find": "a", "replace": "b"})
        with self.assertRaises(FileNotFoundError):
            tool.run({"path": "none.docx", "find": "a", "replace": "b"})


if __name__ == "__main__":
    unittest.main()
