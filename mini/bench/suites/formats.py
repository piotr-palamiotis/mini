"""formats: undocumented binary and compressed formats (from bench2 make_formats.py)."""
import tempfile
from .. import Suite

SUITE = Suite('formats')
import gzip, io, json, os, random, shutil, sqlite3, struct, textwrap, zipfile, zlib

HEAD = ("import os, sys, re, json, glob\nW, A = sys.argv[1], open(sys.argv[2]).read()\n"
        "def fail(m): print('FAIL', m); sys.exit(1)\n")


LONG = {"bin_records": 3600, "format_tool": 3600}    # working out a format takes a while (bench2 gave them 3600 s)


def task(name, prompt, files, check):
    SUITE.add(name, prompt.strip(), files, HEAD + textwrap.dedent(check) + "\nprint('PASS')\n",
              timeout=LONG.get(name, 1500))



rnd = random.Random(11)
WORDS = ["alpha", "bravo", "cedar", "delta", "ember", "fjord", "gamma", "harbor", "iris", "juniper", "kelp", "lumen",
         "maple", "nova", "onyx", "pecan", "quartz", "raven"]


# ---------------------------------------------------------------- record format (bin_records, format_tool)
def records(n):
    out = []
    for _ in range(n):
        out.append([rnd.randint(1000, 99999), round(rnd.uniform(-50, 150), 3), rnd.choice(WORDS)])
    return out


def rec_file(recs):
    """magic RCD1, u16 version 1, u16 count; per record u32 id, f32 value, 8-byte name (NUL padded); all little-endian."""
    b = b"RCD1" + struct.pack("<HH", 1, len(recs))
    for i, v, s in recs:
        b += struct.pack("<If8s", i, v, s.encode())
    return b


REC_CHECK = """
import math
def as_records(d):
    if isinstance(d, dict):                      # {"records": [...]} or similar: the one list inside
        lists = [v for v in d.values() if isinstance(v, list)]
        if len(lists) != 1: fail(f'{PATH}: expected a list of records, got keys {sorted(d)}')
        d = lists[0]
    if not isinstance(d, list): fail(f'{PATH}: not a list')
    return d
def vals(o):
    return list(o.values()) if isinstance(o, dict) else list(o) if isinstance(o, list) else [o]
def check_records(path, exp):
    global PATH; PATH = path
    p = os.path.join(W, path)
    if not os.path.exists(p): fail(f'no {path}')
    try: d = json.load(open(p))
    except ValueError as e: fail(f'{path} is not valid JSON: {e}')
    got = as_records(d)
    if len(got) != len(exp): fail(f'{path}: {len(got)} records, expected {len(exp)}')
    for k, (o, (i, v, s)) in enumerate(zip(got, exp)):
        vs = vals(o)
        if not any(type(x) is int and x == i for x in vs): fail(f'{path} record {k}: no id {i} in {o}')
        if not any(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isclose(x, v, abs_tol=1e-3) for x in vs):
            fail(f'{path} record {k}: no value {v} in {o}')
        if not any(isinstance(x, str) and x.rstrip("\\x00").strip() == s for x in vs): fail(f'{path} record {k}: no name {s!r} in {o}')
"""

data = records(12)
exp = [[i, struct.unpack("<f", struct.pack("<f", v))[0], s] for i, v, s in data]
task("bin_records",
     "Figure out what data.bin contains and convert it to data.json: a JSON list with one object per record, "
     "with every field of the record.",
     {"data.bin": rec_file(data)},
     REC_CHECK + f"check_records('data.json', {exp!r})\n")

sample, more = records(6), records(15)
exp_more = [[i, struct.unpack("<f", struct.pack("<f", v))[0], s] for i, v, s in more]
task("format_tool",
     "sample.bin and more.bin share an undocumented binary format. Work out the format, then make a tool that decodes "
     "a file in this format into a JSON list with one object per record, with every field of the record. Use the tool "
     "to convert more.bin to more.json.",
     {"sample.bin": rec_file(sample), "more.bin": rec_file(more)},
     REC_CHECK + f"check_records('more.json', {exp_more!r})\n" + """
ok = []
for lg in glob.glob(os.path.join(W, '.mini', 'runs', '*', 'log.jsonl')):
    for line in open(lg):
        e = json.loads(line)
        if e.get('kind') == 'invoke' and e.get('ok') is True: ok.append(e.get('tool'))
if not ok: fail('the built tool was never invoked successfully (more.json made another way)')
""")


# ---------------------------------------------------------------- gz_report: gzipped CSV, monthly totals, chart
rows, tot = [], {}
for _ in range(150):
    m = rnd.randint(1, 6); day = rnd.randint(1, 28); amt = round(rnd.uniform(5, 400), 2)
    rows.append(f"2026-{m:02d}-{day:02d},{rnd.choice(WORDS)},{amt:.2f}")
    tot[f"2026-{m:02d}"] = tot.get(f"2026-{m:02d}", 0) + amt
buf = io.BytesIO()
with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0, filename="") as g:
    g.write(("date,product,amount\n" + "\n".join(rows) + "\n").encode())
exp_tot = {k: round(v, 2) for k, v in sorted(tot.items())}
task("gz_report",
     "report.bin contains sales data. Write summary.json, an object that maps each month (YYYY-MM) to the total "
     "amount sold in it (two decimals), and draw a bar chart of the monthly totals as chart.svg or chart.png.",
     {"report.bin": buf.getvalue()},
     f"""
exp = {exp_tot!r}
p = os.path.join(W, 'summary.json')
if not os.path.exists(p): fail('no summary.json')
try: d = json.load(open(p))
except ValueError as e: fail(f'summary.json is not valid JSON: {{e}}')
if not isinstance(d, dict): fail('summary.json is not an object')
if sorted(d) != sorted(exp): fail(f'months {{sorted(d)}} != {{sorted(exp)}}')
for k, v in exp.items():
    try: x = float(d[k])
    except (TypeError, ValueError): fail(f'{{k}}: {{d[k]!r}} is not a number')
    if abs(x - v) > 0.011: fail(f'{{k}}: {{x}} != {{v}}')
svg, png = os.path.join(W, 'chart.svg'), os.path.join(W, 'chart.png')
if os.path.exists(svg):
    import xml.etree.ElementTree as ET
    try: root = ET.parse(svg).getroot()
    except ET.ParseError as e: fail(f'chart.svg is not valid XML: {{e}}')
    if not root.tag.endswith('svg'): fail('chart.svg has no <svg> root')
    shapes = [el for el in root.iter() if el.tag.split('}}')[-1] in ('rect', 'path', 'polygon', 'line')]
    if len(shapes) < len(exp): fail(f'chart.svg has {{len(shapes)}} shapes, fewer than the {{len(exp)}} months')
elif os.path.exists(png):
    b = open(png, 'rb').read()
    if not b.startswith(bytes.fromhex('89504e470d0a1a0a')): fail('chart.png is not a PNG')
    w, h = int.from_bytes(b[16:20], 'big'), int.from_bytes(b[20:24], 'big')
    if w < 100 or h < 100: fail(f'chart.png is only {{w}}x{{h}}')
else:
    fail('no chart.svg or chart.png')
""")


# ---------------------------------------------------------------- folder_inventory: real types behind wrong extensions
def png_bytes(w=4, h=3):
    raw =b"".join(b"\x00" + b"".join(bytes([(x * 60) % 256, (y * 80) % 256, 128]) for x in range(w)) for y in range(h))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def zip_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zipfile.ZipInfo("notes/readme.txt", (2026, 1, 1, 0, 0, 0)), "Shipping notes for the spring order.\n")
        z.writestr(zipfile.ZipInfo("notes/list.txt", (2026, 1, 1, 0, 0, 0)), "\n".join(WORDS[:6]) + "\n")
    return buf.getvalue()


def gz_bytes():
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0, filename="") as g:
        g.write(("Server log\n" + "\n".join(f"2026-03-0{i} ok request {i}" for i in range(1, 8)) + "\n").encode())
    return buf.getvalue()


def sqlite_bytes():
    tmp = os.path.join(tempfile.mkdtemp(), "inventory.db")
    if os.path.exists(tmp):
        os.remove(tmp)
    c = sqlite3.connect(tmp)
    c.execute("create table customers (id integer primary key, name text, city text)")
    c.executemany("insert into customers values (?, ?, ?)", [(i, WORDS[i], WORDS[-i]) for i in range(1, 6)])
    c.commit(); c.close()
    with open(tmp, "rb") as f_:
        b = f_.read()
    os.remove(tmp)
    return b


inv = {
    "files/backup.dat": (zip_bytes(), ["application/zip", "application/x-zip-compressed", "application/x-zip"]),
    "files/log.dat": (gz_bytes(), ["application/gzip", "application/x-gzip"]),
    "files/picture.txt": (png_bytes(), ["image/png"]),
    "files/customers.bin": (sqlite_bytes(), ["application/vnd.sqlite3", "application/x-sqlite3", "application/sqlite3",
                                             "application/x-sqlite", "application/sqlite"]),
    "files/settings.dat": (json.dumps({"theme": "dark", "volume": 7, "recent": WORDS[:3]}, indent=2).encode(),
                           ["application/json", "text/json"]),
    "files/letter": ("﻿Dear team,\nthe meeting moves to Thursday.\n".encode("utf-16-le"), ["text/plain"]),
}
exp_inv = {os.path.basename(k): v[1] for k, v in inv.items()}
task("folder_inventory",
     "The files in files/ have wrong or missing extensions. Make inventory.json: a JSON list with one object per file "
     "in files/, giving its file name, its real type as a MIME type, and one short line about its content.",
     {k: v[0] for k, v in inv.items()},
     f"""
exp = {exp_inv!r}
p = os.path.join(W, 'inventory.json')
if not os.path.exists(p): fail('no inventory.json')
try: d = json.load(open(p))
except ValueError as e: fail(f'inventory.json is not valid JSON: {{e}}')
if isinstance(d, dict):
    lists = [v for v in d.values() if isinstance(v, list)]
    d = lists[0] if len(lists) == 1 else [dict(v, name=k) if isinstance(v, dict) else {{'name': k, 'type': v}} for k, v in d.items()]
if not isinstance(d, list): fail('inventory.json is not a list')
found = {{}}
for o in d:
    if not isinstance(o, dict): continue
    s = [v for v in o.values() if isinstance(v, str)]
    names = [os.path.basename(v.strip()) for v in s if os.path.basename(v.strip()) in exp]
    mimes = [v.split(';')[0].strip().lower() for v in s if re.fullmatch(r'[a-z]+/[\\w.+-]+(;.*)?', v.strip().lower())]
    if names: found[names[0]] = mimes
for n, ok in exp.items():
    if n not in found: fail(f'no entry for {{n}}')
    if not any(m in ok for m in found[n]): fail(f'{{n}}: type {{found[n]}} is not one of {{ok}}')
""")
