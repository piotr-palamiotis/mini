"""tools: building tools with third-party libraries and reusing them (from bench2 make_tools.py)."""
import tempfile
from .. import Suite

SUITE = Suite('tools')
import csv, io, json, os, random, shutil, sqlite3, textwrap

TIMEOUT = 3600

HEAD = '''import os, sys, re, json, glob, math, struct, zipfile
W, A = sys.argv[1], open(sys.argv[2]).read()
def fail(m): print('FAIL', m); sys.exit(1)

def tool_runs(logs=None):
    """(installed, ran ok) tool names over the given log files (default: every run in the workspace)."""
    installed, ran = set(), set()
    for lg in logs if logs is not None else glob.glob(os.path.join(W, '.mini', 'runs', '*', 'log.jsonl')):
        for line in open(lg):
            e = json.loads(line)
            if e.get('kind') == 'install': installed.add(e.get('tool'))
            if e.get('kind') == 'invoke' and e.get('ok') is True: ran.add(e.get('tool'))
    return installed, ran

def built_and_run(n):
    """n tools ran successfully: built in this trial, upgraded, or already there (bundled or earlier) when they did
    the job as they were. Whether they did it right is checked on the outputs."""
    installed, ran = tool_runs()
    if len(ran) < n:
        fail(f'{len(ran)} tools ran successfully ({sorted(ran)}; built: {sorted(installed)}), need {n}')

def load_json(rel):
    p = os.path.join(W, rel)
    if not os.path.exists(p): fail(f'no {rel}')
    try: return json.load(open(p))
    except ValueError as e: fail(f'{rel} is not valid JSON: {e}')

def image_size(path):
    """(format, width, height) of a PNG or JPEG, read from its header."""
    b = open(path, 'rb').read()
    if b.startswith(bytes.fromhex('89504e470d0a1a0a')):
        return 'png', int.from_bytes(b[16:20], 'big'), int.from_bytes(b[20:24], 'big')
    if b[:2] == b'\\xff\\xd8':
        i = 2
        while i + 9 < len(b):
            if b[i] != 0xff: i += 1; continue
            m = b[i + 1]
            if m in (0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf):
                return 'jpeg', int.from_bytes(b[i + 7:i + 9], 'big'), int.from_bytes(b[i + 5:i + 7], 'big')
            if m in (0xd8, 0x01) or 0xd0 <= m <= 0xd7: i += 2; continue
            i += 2 + int.from_bytes(b[i + 2:i + 4], 'big')
        return 'jpeg', 0, 0
    return None, 0, 0
'''




def task(name, prompt, files, check, prompt2=None, setup=None, timeout=TIMEOUT, needs=None):
    SUITE.add(name, " ".join(prompt.split()), files, HEAD + textwrap.dedent(check) + "\nprint('PASS')\n",
              " ".join(prompt2.split()) if prompt2 else None, setup, timeout, needs)



rnd = random.Random(23)
WORDS = ["alpha", "bravo", "cedar", "delta", "ember", "fjord", "gamma", "harbor", "iris", "juniper", "kelp", "lumen",
         "maple", "nova", "onyx", "pecan", "quartz", "raven", "sable", "tundra"]

# ---------------------------------------------------------------- 1. invoice_roundtrip: PDF out, PDF back in
customers = ["acme", "bolt", "crane", "dune", "elm"]
lines, totals = ["order_id,customer,item,qty,unit_price"], {}
for i in range(1, 23):
    c = customers[(i * 3) % 5]; q = rnd.randint(1, 9); p = rnd.choice([3.5, 7.25, 12.0, 19.99, 45.0])
    lines.append(f"{i},{c},{rnd.choice(WORDS)},{q},{p}")
    totals[c] = round(totals.get(c, 0) + q * p, 2)
task("invoice_roundtrip", """
Build tools for this, one that creates PDF invoices and one that reads text back out of PDF files. Use the first to
make one PDF invoice per customer from orders.csv, saved as invoices/<customer>.pdf, listing the customer's items and
the invoice total. Then use the second on the invoice PDFs (not on the CSV) and write totals.json: an object mapping
each customer to the total printed on their invoice, as a number.""",
     {"orders.csv": "\n".join(lines) + "\n"},
     f"""
exp = {totals!r}
for c in exp:
    p = os.path.join(W, 'invoices', c + '.pdf')
    if not os.path.exists(p): fail(f'no invoices/{{c}}.pdf')
    if not open(p, 'rb').read(5) == b'%PDF-': fail(f'invoices/{{c}}.pdf is not a PDF')
d = load_json('totals.json')
if not isinstance(d, dict) or sorted(d) != sorted(exp): fail(f'totals.json customers {{sorted(d) if isinstance(d, dict) else d}}')
for c, v in exp.items():
    try: x = float(d[c])
    except (TypeError, ValueError): fail(f'{{c}}: {{d[c]!r}} is not a number')
    if abs(x - v) > 0.011: fail(f'{{c}}: {{x}} != {{v}}')
built_and_run(2)
""")

# ---------------------------------------------------------------- 2. sales_workbook: SQLite -> XLSX + chart
regions = ["north", "south", "east", "west"]
sales, rev = [], {}
for i in range(1, 41):
    r = regions[i % 4]; q = rnd.randint(1, 12); pr = rnd.choice([2.5, 4.0, 9.99, 15.0])
    sales.append((i, r, rnd.choice(WORDS), q, pr, f"2026-0{rnd.randint(1, 6)}-{rnd.randint(10, 28)}"))
    rev[r] = round(rev.get(r, 0) + q * pr, 2)
counts = {r: sum(1 for s in sales if s[1] == r) for r in regions}
tmp = os.path.join(tempfile.mkdtemp(), "shop.db")
if os.path.exists(tmp):
    os.remove(tmp)
con = sqlite3.connect(tmp)
con.execute("create table sales (id integer primary key, region text, product text, qty integer, price real, day text)")
con.executemany("insert into sales values (?, ?, ?, ?, ?, ?)", sales)
con.commit(); con.close()
with open(tmp, "rb") as f_:
    db = f_.read()
os.remove(tmp)
task("sales_workbook", """
Build the tools you need for this: one that writes Excel workbooks and one that draws bar charts. From the sales table
in shop.db, make report.xlsx with one sheet per region, named after the region, listing that region's sales with
the columns day, product, qty, price and revenue (qty * price), and a sheet named Totals with one row per region: the
region and its total revenue. Write plain values, not formulas. Then draw chart.png, a bar chart of the total revenue
per region.""",
     {"shop.db": db},
     f"""
import xml.etree.ElementTree as ET
exp, counts = {rev!r}, {counts!r}
p = os.path.join(W, 'report.xlsx')
if not os.path.exists(p): fail('no report.xlsx')
try: z = zipfile.ZipFile(p)
except zipfile.BadZipFile: fail('report.xlsx is not an xlsx (zip) file')
NS = '{{http://schemas.openxmlformats.org/spreadsheetml/2006/main}}'
RNS = '{{http://schemas.openxmlformats.org/officeDocument/2006/relationships}}'
shared = []
if 'xl/sharedStrings.xml' in z.namelist():
    for si in ET.fromstring(z.read('xl/sharedStrings.xml')).iter(NS + 'si'):
        shared.append(''.join(t.text or '' for t in si.iter(NS + 't')))
rels = {{r.get('Id'): r.get('Target') for r in ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))}}
sheets = {{}}
for s in ET.fromstring(z.read('xl/workbook.xml')).iter(NS + 'sheet'):
    t = rels[s.get(RNS + 'id')].lstrip('/')
    sheets[s.get('name')] = t if t.startswith('xl/') else 'xl/' + t
def rows(name):
    out = []
    for row in ET.fromstring(z.read(sheets[name])).iter(NS + 'row'):
        vals = []
        for c in row.iter(NS + 'c'):
            v, t = c.find(NS + 'v'), c.get('t')
            if t == 's' and v is not None: vals.append(shared[int(v.text)])
            elif t == 'inlineStr': vals.append(''.join(x.text or '' for x in c.iter(NS + 't')))
            elif v is not None:
                try: vals.append(float(v.text))
                except ValueError: vals.append(v.text)
            elif c.find(NS + 'f') is not None: vals.append('=formula')
        if vals: out.append(vals)
    return out
low = {{n.lower(): n for n in sheets}}
for r in exp:
    if r not in low: fail(f'no sheet for region {{r}} (sheets: {{list(sheets)}})')
    data = [x for x in rows(low[r]) if any(isinstance(v, float) for v in x)]
    if len(data) != counts[r]: fail(f'sheet {{low[r]}}: {{len(data)}} data rows, expected {{counts[r]}}')
if 'totals' not in low: fail(f'no Totals sheet (sheets: {{list(sheets)}})')
got = {{}}
for x in rows(low['totals']):
    names = [v for v in x if isinstance(v, str) and v.lower() in exp]
    nums = [v for v in x if isinstance(v, float)]
    if '=formula' in x: fail('Totals holds formulas without values')
    if names and nums: got[names[0].lower()] = nums[-1]
for r, v in exp.items():
    if r not in got: fail(f'Totals has no row for {{r}}')
    if abs(got[r] - v) > 0.011: fail(f'Totals {{r}}: {{got[r]}} != {{v}}')
p = os.path.join(W, 'chart.png')
if not os.path.exists(p): fail('no chart.png')
f, w, h = image_size(p)
if f != 'png' or w < 200 or h < 150: fail(f'chart.png is {{f}} {{w}}x{{h}}')
built_and_run(2)
""")

# ---------------------------------------------------------------- 3. feed_digest: feeds -> HTML -> mail (mailpit)
import datetime as dt
items = []
for fi, feed in enumerate(["harbor news", "maple weekly", "quartz journal"]):
    for k in range(7):
        t = dt.datetime(2026, 10, 1, 8, 0) + dt.timedelta(hours=rnd.randint(0, 24 * 30), minutes=rnd.randint(0, 59))
        items.append({"feed": feed, "title": f"{rnd.choice(WORDS).title()} {rnd.choice(WORDS)} report {fi}{k}",
                      "link": f"https://example.org/{fi}/{k}", "time": t})
def rss(feed, its):
    body = "".join(f"<item><title>{i['title']}</title><link>{i['link']}</link>"
                   f"<pubDate>{i['time'].strftime('%a, %d %b %Y %H:%M:%S')} +0000</pubDate></item>" for i in its)
    return (f'<?xml version="1.0"?><rss version="2.0"><channel><title>{feed}</title><link>https://example.org</link>'
            f"<description>{feed}</description>{body}</channel></rss>")
def atom(feed, its):
    body = "".join(f"<entry><title>{i['title']}</title><link href=\"{i['link']}\"/><id>{i['link']}</id>"
                   f"<updated>{i['time'].strftime('%Y-%m-%dT%H:%M:%SZ')}</updated></entry>" for i in its)
    return (f'<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom"><title>{feed}</title>'
            f"<id>urn:{feed.replace(' ', '-')}</id><updated>2026-11-01T00:00:00Z</updated>{body}</feed>")
newest = [i["title"] for i in sorted(items, key=lambda i: i["time"], reverse=True)[:10]]
task("feed_digest", """
Build the tools you need for this: one that reads RSS and Atom feeds, and one that sends an HTML email through the mail
server saved in the settings under the key mail_server. Read the three feeds in feeds/, take the 10 newest items across
all of them, and write digest.html listing them newest first, each with its title as a link, its date and the feed it
came from. Then email digest.html as the HTML body to team@example.com with the subject "Weekly digest".""",
     {"feeds/harbor.xml": rss("harbor news", [i for i in items if i["feed"] == "harbor news"]),
      "feeds/maple.atom": atom("maple weekly", [i for i in items if i["feed"] == "maple weekly"]),
      "feeds/quartz.xml": rss("quartz journal", [i for i in items if i["feed"] == "quartz journal"])},
     f"""
import urllib.request, html
exp = {newest!r}
def in_order(text, what):
    text = html.unescape(text); pos = -1
    for t in exp:
        i = text.find(t)
        if i < 0: fail(f'{{what}}: missing item {{t!r}}')
        if i < pos: fail(f'{{what}}: {{t!r}} is out of order')
        pos = i
p = os.path.join(W, 'digest.html')
if not os.path.exists(p): fail('no digest.html')
in_order(open(p, encoding='utf-8', errors='replace').read(), 'digest.html')
api = 'http://127.0.0.1:8025/api/v1/'
msgs = json.load(urllib.request.urlopen(api + 'messages', timeout=10)).get('messages') or []
sent = [m for m in msgs if any(t.get('Address', '').lower() == 'team@example.com' for t in m.get('To') or [])]
if len(sent) != 1: fail(f'{{len(sent)}} messages to team@example.com in the mail server (expected 1)')
m = json.load(urllib.request.urlopen(api + 'message/' + sent[0]['ID'], timeout=10))
if m.get('Subject') != 'Weekly digest': fail(f'subject {{m.get("Subject")!r}}')
if not m.get('HTML'): fail('the email has no HTML body')
in_order(m['HTML'], 'email HTML')
built_and_run(2)
""",
     setup="""
import json, os, subprocess, sys, time, urllib.request
W, STATE = sys.argv[1], sys.argv[2]
subprocess.run(["podman", "start", "mailpit"], capture_output=True)
for _ in range(20):
    try:
        urllib.request.urlopen("http://127.0.0.1:8025/api/v1/messages", timeout=2); break
    except OSError:
        time.sleep(0.5)
urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8025/api/v1/messages", method="DELETE"), timeout=5)
d = os.path.join(STATE, "settings"); os.makedirs(d, mode=0o700, exist_ok=True)
rec = {"key": "mail_server", "value": {"host": "mailpit", "port": 1025},
       "description": "SMTP server for outgoing mail (no login, no TLS)",
       "schema": {"type": "object", "properties": {"host": {"type": "string"}, "port": {"type": "integer"}},
                  "required": ["host", "port"]}, "secret": [], "saved_by": "bench", "saved_at": "2026-11-07T00:00:00"}
p = os.path.join(d, "mail_server.json")
with open(p, "w") as f:
    json.dump(rec, f, indent=1)
os.chmod(p, 0o600)
""", needs=["mailpit"])

# ---------------------------------------------------------------- 4. qr_labels: QR encode, decode back
prods = [(f"SKU{100 + i}", rnd.choice(WORDS), f"{rnd.choice([4.5, 9.99, 12.0, 24.5])}") for i in range(8)]
texts = [f"{s}|{n}|{p}" for s, n, p in prods]
task("qr_labels", """
Build the tools you need for this: one that makes QR code images and one that decodes QR codes from images. For each
product in products.csv make qr/<sku>.png, a QR code that encodes the text SKU|NAME|PRICE (the three fields joined
with |, e.g. SKU100|alpha|4.5). Then decode the images in qr/ (not the CSV) and write decoded.json: a list of the
decoded texts, ordered by sku.""",
     {"products.csv": "sku,name,price\n" + "\n".join(",".join(p) for p in prods) + "\n"},
     f"""
exp = {texts!r}
for t in exp:
    sku = t.split('|')[0]
    p = os.path.join(W, 'qr', sku + '.png')
    if not os.path.exists(p): fail(f'no qr/{{sku}}.png')
    f, w, h = image_size(p)
    if f != 'png' or w < 21: fail(f'qr/{{sku}}.png is {{f}} {{w}}x{{h}}')
d = load_json('decoded.json')
if d != exp: fail(f'decoded.json {{str(d)[:200]}} != {{exp[:2]}}...')
built_and_run(2)
""")

# ---------------------------------------------------------------- 5. photo_batch: thumbnails, EXIF, contact sheet, zip
DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "photo_batch")
photos, meta = {}, {}             # made once with Pillow (bench2 make_tools.py): checkerboard JPEGs, EXIF date but on #4
sizes = [(1200, 800), (800, 1200), (1024, 1024), (1600, 900), (640, 960), (2000, 1000)]
for k, (w, h) in enumerate(sizes):
    date = None if k == 3 else f"2026:0{k + 1}:1{k} 12:3{k}:00"
    name = f"img_{k + 1}.jpg"
    with open(os.path.join(DATA, name), "rb") as f:
        photos["photos/" + name] = f.read()
    meta[name] = {"w": w, "h": h, "date": date}
task("photo_batch", """
Build the tools you need for this, using an image library: one that makes thumbnails, one that reads image metadata,
and one that makes a contact sheet. For every image in photos/: write a thumbnail at thumbs/<same file name> whose
longer side is 256 pixels (keep the aspect ratio), and add an entry to meta.json, a list with one object per image:
file name, width, height, format and the EXIF date taken (null when the image has none). Then make contact.jpg, a
contact sheet of all thumbnails, and bundle.zip holding the thumbs/ folder and meta.json.""",
     photos,
     f"""
exp = {meta!r}
for n, m in exp.items():
    p = os.path.join(W, 'thumbs', n)
    if not os.path.exists(p): fail(f'no thumbs/{{n}}')
    f, w, h = image_size(p)
    if max(w, h) != 256: fail(f'thumbs/{{n}} is {{w}}x{{h}}: longer side is not 256')
    if abs(w / h - m['w'] / m['h']) > 0.02: fail(f'thumbs/{{n}} {{w}}x{{h}} does not keep the aspect of {{m["w"]}}x{{m["h"]}}')
d = load_json('meta.json')
if isinstance(d, dict):
    d = [dict(v, file=k) if isinstance(v, dict) else v for k, v in d.items()]
if not isinstance(d, list): fail('meta.json is not a list')
seen = {{}}
for o in d:
    if not isinstance(o, dict): continue
    names = [v for v in o.values() if isinstance(v, str) and os.path.basename(v) in exp]
    if names: seen[os.path.basename(names[0])] = o
for n, m in exp.items():
    if n not in seen: fail(f'meta.json has no entry for {{n}}')
    o = seen[n]; nums = [v for v in o.values() if type(v) is int]
    if m['w'] not in nums or m['h'] not in nums: fail(f'meta.json {{n}}: size {{m["w"]}}x{{m["h"]}} not in {{o}}')
    digits = lambda s: re.sub(r'\\D', '', str(s))
    dates = [v for v in o.values() if isinstance(v, str) and len(digits(v)) >= 12]
    if m['date']:
        if not any(digits(v)[:14] == digits(m['date']) for v in dates): fail(f'meta.json {{n}}: date {{m["date"]}} not in {{o}}')
    elif dates: fail(f'meta.json {{n}}: has a date {{dates}} but the image has none')
p = os.path.join(W, 'contact.jpg')
if not os.path.exists(p): fail('no contact.jpg')
f, w, h = image_size(p)
if f != 'jpeg' or w < 256 or h < 256: fail(f'contact.jpg is {{f}} {{w}}x{{h}}')
p = os.path.join(W, 'bundle.zip')
if not os.path.exists(p): fail('no bundle.zip')
try: names = [os.path.normpath(x) for x in zipfile.ZipFile(p).namelist()]
except zipfile.BadZipFile: fail('bundle.zip is not a zip')
base = [os.path.basename(x) for x in names]
if 'meta.json' not in base: fail(f'bundle.zip has no meta.json: {{names}}')
for n in exp:
    if not any(x.endswith(os.path.join('thumbs', n)) for x in names): fail(f'bundle.zip has no thumbs/{{n}}: {{names}}')
built_and_run(2)
""")

# ---------------------------------------------------------------- 6. config_migrate: YAML -> validate -> TOML
schema = {"type": "object", "required": ["name", "port", "debug", "workers"],
          "properties": {"name": {"type": "string"}, "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                         "debug": {"type": "boolean"}, "workers": {"type": "integer", "minimum": 1},
                         "tags": {"type": "array", "items": {"type": "string"}},
                         "database": {"type": "object", "required": ["url"],
                                      "properties": {"url": {"type": "string"}, "pool": {"type": "integer"}}}}}
def yaml_of(d, ind=0):
    out = []
    for k, v in d.items():
        pad = "  " * ind
        if isinstance(v, dict):
            out.append(f"{pad}{k}:"); out.append(yaml_of(v, ind + 1))
        elif isinstance(v, list):
            out.append(f"{pad}{k}:"); out += [f"{pad}  - {x}" for x in v]
        elif isinstance(v, bool):
            out.append(f"{pad}{k}: {'true' if v else 'false'}")
        else:
            out.append(f"{pad}{k}: {v}")
    return "\n".join(out)
confs, valid, invalid = {}, {}, []
for k, nm in enumerate(["api", "worker", "web", "cron", "admin", "edge"]):
    d = {"name": nm, "port": 8000 + k, "debug": k % 2 == 0, "workers": k + 1, "tags": WORDS[k:k + 2],
         "database": {"url": f"postgres://db/{nm}", "pool": 5 + k}}
    if nm == "cron":
        del d["workers"]; invalid.append(nm + ".yaml")
    elif nm == "edge":
        d["port"] = "eighty"; invalid.append(nm + ".yaml")
    else:
        valid[nm] = d
    confs[f"conf/{nm}.yaml"] = yaml_of(d) + "\n"
task("config_migrate", """
Build two tools for this: one that validates a YAML file against a JSON Schema, and one that converts YAML to TOML.
Validate every YAML file in conf/ against schema.json. Convert each valid one to TOML at toml/<name>.toml (for example
conf/api.yaml becomes toml/api.toml) with the same data. Write invalid.json: an object that maps the file name of each
invalid YAML file (for example "x.yaml") to a short error message.""",
     dict(confs, **{"schema.json": json.dumps(schema, indent=2)}),
     f"""
import tomllib
valid, invalid = {valid!r}, {sorted(invalid)!r}
for nm, d in valid.items():
    p = os.path.join(W, 'toml', nm + '.toml')
    if not os.path.exists(p): fail(f'no toml/{{nm}}.toml')
    try: got = tomllib.load(open(p, 'rb'))
    except tomllib.TOMLDecodeError as e: fail(f'toml/{{nm}}.toml is not valid TOML: {{e}}')
    if got != d: fail(f'toml/{{nm}}.toml {{got}} != {{d}}')
for n in invalid:
    if os.path.exists(os.path.join(W, 'toml', n.replace('.yaml', '.toml'))): fail(f'invalid {{n}} was converted')
d = load_json('invalid.json')
if not isinstance(d, dict): fail('invalid.json is not an object')
if sorted(os.path.basename(k) for k in d) != invalid: fail(f'invalid.json names {{sorted(d)}} != {{invalid}}')
built_and_run(2)
""")

# ---------------------------------------------------------------- 7. reuse_chain: build in task 1, reuse in task 2
first = {"name": "shop", "port": 9000, "debug": False, "owners": ["iris", "kelp"], "limits": {"cpu": 2, "memory": 512}}
more = {f"more/{nm}.yaml": {"name": nm, "port": 9100 + k, "debug": k == 1, "owners": WORDS[k + 4:k + 6],
                            "limits": {"cpu": k + 1, "memory": 256 * (k + 1)}} for k, nm in enumerate(["cart", "pay", "ship"])}
task("reuse_chain", """
Make a tool that converts a YAML file to a TOML file with the same data, and use it to convert app.yaml to app.toml.""",
     dict({"app.yaml": yaml_of(first) + "\n"}, **{k: yaml_of(v) + "\n" for k, v in more.items()}),
     f"""
import tomllib
exp = {dict({"app.toml": first}, **{k.replace(".yaml", ".toml"): v for k, v in more.items()})!r}
for rel, d in exp.items():
    p = os.path.join(W, rel)
    if not os.path.exists(p): fail(f'no {{rel}}')
    try: got = tomllib.load(open(p, 'rb'))
    except tomllib.TOMLDecodeError as e: fail(f'{{rel}} is not valid TOML: {{e}}')
    if got != d: fail(f'{{rel}} {{got}} != {{d}}')
runs = sorted(glob.glob(os.path.join(W, '.mini', 'runs', '*')), key=lambda r: r.endswith('-b'))
first_run = [r for r in runs if not r.endswith('-b')]
second = [r for r in runs if r.endswith('-b')]
if not first_run or not second: fail(f'expected two runs (task and follow-up), got {{[os.path.basename(r) for r in runs]}}')
inst1, ran1 = tool_runs([os.path.join(r, 'log.jsonl') for r in first_run])
inst2, ran2 = tool_runs([os.path.join(r, 'log.jsonl') for r in second])
if not inst1 & ran1: fail(f'task 1: no built tool ran successfully (built {{sorted(inst1)}})')
if inst2: fail(f'task 2 built new tools {{sorted(inst2)}} instead of reusing {{sorted(inst1)}}')
if not inst1 & ran2: fail(f'task 2 did not run the tool from task 1 (ran {{sorted(ran2)}})')
""",
     prompt2="""
Convert every YAML file in more/ to TOML next to it (more/<name>.toml), with the same data.""")
