import os
import tempfile
import unittest
import zlib

import tool


def pdf(streams, compress=False, extra=b""):
    """A minimal PDF with one page per content stream, built by hand."""
    objs = {1: b"<< /Type /Catalog /Pages 2 0 R >>", 3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"}
    kids = []
    for i, s in enumerate(streams):
        page, cont = 4 + 2 * i, 5 + 2 * i
        body = zlib.compress(s) if compress else s
        flt = b" /Filter /FlateDecode" if compress else b""
        objs[cont] = b"<< /Length %d%s >>\nstream\n" % (len(body), flt) + body + b"\nendstream"
        objs[page] = b"<< /Type /Page /Parent 2 0 R /Contents %d 0 R /Resources << /Font << /F1 3 0 R >> >> >>" % cont
        kids.append(b"%d 0 R" % page)
    objs[2] = b"<< /Type /Pages /Kids [" + b" ".join(kids) + b"] /Count %d >>" % len(kids)
    out = b"%PDF-1.4\n"
    for n in sorted(objs):
        out += b"%d 0 obj\n" % n + objs[n] + b"\nendobj\n"
    return out + extra + b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"


class PdfReadTest(unittest.TestCase):
    def setUp(self):
        self.old = os.getcwd()
        os.chdir(tempfile.mkdtemp(prefix="mini-pdf-"))
        self.addCleanup(os.chdir, self.old)

    def write(self, name, data):
        with open(name, "wb") as f:
            f.write(data)

    def test_two_pages_plain_and_compressed(self):
        s1 = b"BT /F1 12 Tf 72 700 Td (Hello \\(world\\)) Tj 0 -14 Td (Second line) Tj ET"
        s2 = b"BT /F1 12 Tf 72 700 Td [(Wor) -300 (ds) 20 (joined)] TJ ET"
        for compress in (False, True):
            self.write("a.pdf", pdf([s1, s2], compress))
            out = tool.run({"path": "a.pdf"})
            self.assertEqual(out["page_count"], 2)
            self.assertEqual(out["pages"][0]["text"], "Hello (world)\nSecond line")
            self.assertEqual(out["pages"][1]["text"], "Wor dsjoined")
            self.assertEqual(out["empty_pages"], [])

    def test_hex_strings_octal_escapes_and_page_range(self):
        self.write("a.pdf", pdf([b"BT <48656C6C6F> Tj ET", b"BT (caf\\351) Tj ET", b"BT () Tj ET"]))
        out = tool.run({"path": "a.pdf", "first_page": 2})
        self.assertEqual([p["text"] for p in out["pages"]], ["café", ""])
        self.assertEqual(out["empty_pages"], [3])
        self.assertEqual(out["page_count"], 3)
        self.assertEqual(tool.run({"path": "a.pdf", "last_page": 1})["text"], "Hello")

    def test_refusals(self):
        self.write("note.pdf", b"just text")
        with self.assertRaisesRegex(ValueError, "not a PDF"):
            tool.run({"path": "note.pdf"})
        self.write("enc.pdf", pdf([b"BT (x) Tj ET"], extra=b"<< /Encrypt 9 0 R >>\n"))
        with self.assertRaisesRegex(ValueError, "encrypted"):
            tool.run({"path": "enc.pdf"})
        self.write("empty.pdf", b"%PDF-1.5\n")
        with self.assertRaisesRegex(ValueError, "no pages"):
            tool.run({"path": "empty.pdf"})
        for bad in ("/etc/passwd", "../x.pdf"):
            with self.assertRaisesRegex(ValueError, "relative path"):
                tool.run({"path": bad})
        with self.assertRaises(FileNotFoundError):
            tool.run({"path": "missing.pdf"})


if __name__ == "__main__":
    unittest.main()
