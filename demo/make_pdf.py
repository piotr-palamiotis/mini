"""Deterministic demo PDF with scanned pages, plus its ground truth. Demo fixture only, never a tool.

Pages 1 and 3 are born-digital (real text layer). Pages 2 and 4 are rendered to images, rotated slightly, blurred
and sprinkled with seeded noise, then embedded as images: there is no text layer, so OCR is really needed.
The same seed always gives the same bytes of image content, so test results are comparable across runs.

Run in a throwaway container (needs Pillow and fpdf2), e.g.:
  podman run --rm -v ./demo:/demo:Z docker.io/library/python:3.12-slim \
      sh -c "pip install -q pillow==11.1.0 fpdf2==2.8.2 && python /demo/make_pdf.py /demo"
Writes: report.pdf, report_malformed.pdf (truncated, for failure injection), ground_truth.json
"""
import json
import os
import random
import sys

from fpdf import FPDF
from PIL import Image, ImageDraw, ImageFilter, ImageFont

SEED = 20261009
TITLE = "Riverside Community Garden - Annual Water Report 2026"
PAGES = [
    ("text", [
        "Riverside Community Garden - Annual Water Report 2026",
        "",
        "Prepared by the garden committee for the members meeting.",
        "",
        "Summary of the season: the garden used 182,400 litres of water between",
        "April and September, which is 14 percent less than in 2025. The new drip",
        "irrigation on beds 1 to 12 saved most of the water. Rainwater from the",
        "two collection tanks covered 38 percent of the total use.",
    ]),
    ("scan", [
        "Monthly water use (litres)",
        "",
        "April      18,900",
        "May        27,300",
        "June       39,800",
        "July       44,100",
        "August     35,600",
        "September  16,700",
        "",
        "The peak in July followed eleven days without rain.",
    ]),
    ("text", [
        "Costs and repairs",
        "",
        "The water bill for the season was 2,310 euros. Two leaks were repaired:",
        "one at the main valve near the shed and one in the hose line to bed 7.",
        "The committee bought a third rain tank for 420 euros, to be installed",
        "before the next season.",
    ]),
    ("scan", [
        "Decisions for 2027",
        "",
        "1. Extend drip irrigation to beds 13 to 20.",
        "2. Install the third rain tank by March.",
        "3. Read the water meter every two weeks.",
        "4. Next members meeting: Friday 6 November, 18:00, in the shed.",
    ]),
]
KEY_FACTS = ["182,400", "14 percent", "38 percent", "44,100", "2,310", "420", "beds 13 to 20", "6 November"]


def scan_image(lines, rng):
    w, h = 1700, 2200                       # ~200 dpi A4-ish
    img = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=44)
    y = 180
    for ln in lines:
        d.text((150, y), ln, fill=20, font=font)
        y += 72
    img = img.rotate(rng.uniform(-1.5, 1.5), resample=Image.BICUBIC, fillcolor=255, expand=False)
    img = img.filter(ImageFilter.GaussianBlur(0.8))
    px = img.load()
    for _ in range(int(w * h * 0.004)):     # salt-and-pepper specks
        x, yy = rng.randrange(w), rng.randrange(h)
        px[x, yy] = 0 if rng.random() < 0.5 else 255
    for yy in range(0, h, 1):               # faint uneven background, like paper
        if rng.random() < 0.02:
            shade = rng.randrange(225, 245)
            for x in range(0, w, 3):
                if px[x, yy] > shade:
                    px[x, yy] = shade
    return img


def main(out):
    rng = random.Random(SEED)
    pdf = FPDF(format="A4")
    pdf.set_creation_date(__import__("datetime").datetime(2026, 10, 1, 12, 0, 0))
    pdf.set_title(TITLE)
    truth = {"title": TITLE, "seed": SEED, "pages": [], "key_facts": KEY_FACTS}
    for i, (kind, lines) in enumerate(PAGES, 1):
        pdf.add_page()
        if kind == "text":
            pdf.set_font("Helvetica", size=12)
            for ln in lines:
                pdf.cell(0, 8, ln, new_x="LMARGIN", new_y="NEXT")
        else:
            img = scan_image(lines, rng)
            p = os.path.join(out, f".scan{i}.png")
            img.save(p)
            pdf.image(p, x=0, y=0, w=210, h=297)
            os.remove(p)
        truth["pages"].append({"page": i, "kind": kind, "text": "\n".join(lines)})
    pdf.output(os.path.join(out, "report.pdf"))
    data = open(os.path.join(out, "report.pdf"), "rb").read()
    open(os.path.join(out, "report_malformed.pdf"), "wb").write(data[: len(data) // 3])
    json.dump(truth, open(os.path.join(out, "ground_truth.json"), "w"), indent=1)
    print("wrote report.pdf", len(data), "bytes")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__)))
