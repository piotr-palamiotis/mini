import os
import tempfile
import unittest

import tool


def read(path):
    with open(path, "rb") as f:
        return f.read()


class PdfCreateTest(unittest.TestCase):
    def setUp(self):
        self.old = os.getcwd()
        os.chdir(tempfile.mkdtemp(prefix="mini-pdf-"))
        self.addCleanup(os.chdir, self.old)

    def test_structure_and_text(self):
        out = tool.run({"path": "sub/r.pdf", "title": "Report", "text": "Line (one)\nLine \\ two\ncafé € end"})
        self.assertEqual(out["pages"], 1)
        data = read("sub/r.pdf")
        self.assertTrue(data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF"))
        self.assertIn(b"(Report) Tj", data)
        self.assertIn(b"(Line \\(one\\)) Tj", data)
        self.assertIn(b"(Line \\\\ two) Tj", data)
        self.assertIn(b"(caf\xe9 ? end) Tj", data)
        # the xref offsets point at their objects
        start = int(data.rsplit(b"startxref\n", 1)[1].split()[0])
        self.assertTrue(data[start:].startswith(b"xref"))
        for n, line in enumerate(data[start:].split(b"\n")[3:6], start=1):
            self.assertTrue(data[int(line[:10]):].startswith(b"%d 0 obj" % n))

    def test_long_text_makes_pages(self):
        out = tool.run({"path": "long.pdf", "text": "\n".join(f"row {i}" for i in range(120))})
        self.assertEqual(out["pages"], 3)
        self.assertEqual(read("long.pdf").count(b"/Type /Page "), 3)

    def test_refuses_overwrite_and_outside_paths(self):
        tool.run({"path": "a.pdf", "text": "x"})
        with self.assertRaises(FileExistsError):
            tool.run({"path": "a.pdf", "text": "y"})
        tool.run({"path": "a.pdf", "text": "y", "overwrite": True})
        for bad in ("/tmp/a.pdf", "../a.pdf"):
            with self.assertRaisesRegex(ValueError, "relative path"):
                tool.run({"path": bad, "text": "x"})
        with self.assertRaisesRegex(ValueError, "string"):
            tool.run({"path": "b.pdf", "text": 5})


if __name__ == "__main__":
    unittest.main()
