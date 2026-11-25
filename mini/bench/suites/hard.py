"""hard: rule-heavy data work (from bench2 make_hard.py)."""
import tempfile
from .. import Suite

SUITE = Suite('hard')
import csv, datetime as dt, heapq, io, json, os, random, shutil, sqlite3, textwrap
from decimal import Decimal, ROUND_HALF_UP

TIMEOUT = 3600

HEAD = '''import os, sys, re, json, glob, csv
W, A = sys.argv[1], open(sys.argv[2]).read()
def fail(m): print('FAIL', m); sys.exit(1)

def tool_runs(logs=None):
    installed, ran = set(), set()
    for lg in logs if logs is not None else glob.glob(os.path.join(W, '.mini', 'runs', '*', 'log.jsonl')):
        for line in open(lg):
            e = json.loads(line)
            if e.get('kind') == 'install': installed.add(e.get('tool'))
            if e.get('kind') == 'invoke' and e.get('ok') is True: ran.add(e.get('tool'))
    return installed, ran

def load_json(rel):
    p = os.path.join(W, rel)
    if not os.path.exists(p): fail(f'no {rel}')
    try: return json.load(open(p))
    except ValueError as e: fail(f'{rel} is not valid JSON: {e}')
'''


def task(name, prompt, files, check, prompt2=None, timeout=TIMEOUT):
    SUITE.add(name, " ".join(prompt.split()), files, HEAD + textwrap.dedent(check) + "\nprint('PASS')\n",
              " ".join(prompt2.split()) if prompt2 else None, timeout=timeout)



rnd = random.Random(31)
WORDS = ["alpha", "bravo", "cedar", "delta", "ember", "fjord", "gamma", "harbor", "iris", "juniper", "kelp", "lumen",
         "maple", "nova", "onyx", "pecan", "quartz", "raven", "sable", "tundra"]

# ---------------------------------------------------------------- 1. hard_reconcile: bank lines -> invoices
CUSTOMERS = ["Acme GmbH", "Bolt AG", "Crane KG", "Dune SE", "Elm OHG", "Fjord BV"]
inv = {}                                       # id -> (customer, cents)
for i in range(1, 25):
    inv[i] = (CUSTOMERS[i % 6], rnd.randint(150_00, 4_800_00) + rnd.choice([0, 50, 99, 1]))
credits = {3: 120_00, 8: 45_50, 15: inv[15][1] // 2, 19: 300_00}
owed = {i: c - credits.get(i, 0) for i, (_, c) in inv.items()}

day0 = dt.date(2026, 10, 1)
rates = {}
for k in range(31):
    d = day0 + dt.timedelta(days=k)
    if d.weekday() < 5:
        rates[d] = Decimal(f"0.{rnd.randint(9050, 9390)}")   # EUR per USD, weekdays only


def rate_on(d):
    while d not in rates:
        d -= dt.timedelta(days=1)
    return rates[d]


def eur_str(cents):                            # 1234567 -> "12.345,67"
    s = f"{abs(cents) // 100:,}".replace(",", ".") + "," + f"{abs(cents) % 100:02d}"
    return ("-" if cents < 0 else "") + s


def usd_for(cents_eur, d):                     # a USD amount near the EUR value, 2 decimals
    return (Decimal(cents_eur) / 100 / rate_on(d)).quantize(Decimal("0.01"))


ref = [lambda n: f"INV-{n:04d}", lambda n: f"inv {n}", lambda n: f"Invoice no. {n}", lambda n: f"RE: INV{n:04d}",
       lambda n: f"ord 44{n:02d} / INV-{n:04d}"]
bank = []                                      # (date, memo, cents or Decimal usd, currency, refs)
def pay(d, refs, amount, cur="EUR", memo=None):
    if memo is None:
        memo = (" + ".join(f"INV-{n:04d}" for n in refs) if len(refs) > 1 and rnd.random() < .5 else
                ("invoices " + ",".join(str(n) for n in refs)) if len(refs) > 1 else rnd.choice(ref)(refs[0]))
        memo = f"{CUSTOMERS[refs[0] % 6]} {memo}"
    bank.append((d, memo, amount, cur, refs))

D = lambda k: day0 + dt.timedelta(days=k)
pay(D(1), [1], owed[1])                                       # exact
pay(D(2), [2], owed[2] - 100_00)                              # partial
pay(D(3), [3], owed[3])                                       # exact after credit note
pay(D(4), [4], usd_for(owed[4], D(4)), "USD")                 # USD on a Monday... weekday rate
pay(D(5), [5, 6], owed[5] + owed[6] + 25_00)                  # two invoices, 25 EUR over -> last listed
pay(D(6), [7], usd_for(owed[7] // 2, D(6)), "USD")            # USD on a Saturday -> Friday's rate
pay(D(7), [8], inv[8][1])                                     # paid the full amount, ignoring the credit: overpaid
pay(D(8), [], 99_00, memo="Bolt AG thank you")                # no reference -> unmatched
pay(D(9), [9, 10], owed[9] + owed[10] // 3)                   # second one partial
pay(D(10), [77], 210_00)                                      # unknown invoice -> unmatched
pay(D(11), [11], owed[11])
pay(D(12), [2], 100_00)                                       # rest of invoice 2
pay(D(13), [12], usd_for(owed[12], D(13)), "USD")             # Tuesday 13th? whatever weekday it is
pay(D(14), [], -12_50, memo="Kontofuehrung / account fee")    # outgoing: not a payment
pay(D(15), [13, 14, 16], owed[13] + owed[14] + owed[16])
pay(D(17), [17], owed[17] + 1)                                # 1 cent over
pay(D(18), [18], owed[18] - 1)                                # 1 cent short
pay(D(19), [20], usd_for(owed[20], D(18)), "USD")             # Monday 19? uses its own date
pay(D(20), [], -1_234_00, memo="Supplier payment Gamma Ltd")
pay(D(21), [21], owed[21] // 4)
pay(D(22), [21], owed[21] // 4)
pay(D(24), [22], usd_for(owed[22], D(23)), "USD")             # Sunday -> Friday 23rd's rate
pay(D(26), [23, 1], owed[23] + 50_00)                         # invoice 1 already paid -> the 50 lands there
paid = {i: 0 for i in inv}
unmatched = []
lines = ["booked;memo;amount;currency"]
for row, (d, memo, amount, cur, refs) in enumerate(bank, 1):
    if cur == "USD":
        cents = int((amount * rate_on(d) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        shown = f"{amount:,.2f}".replace(",", " ").replace(".", ",").replace(" ", ".")
    else:
        cents, shown = amount, eur_str(amount)
    lines.append(f"{d:%d.%m.%Y};{memo};{shown};{cur}")
    if cents <= 0:
        continue
    known = [n for n in refs if n in inv]
    if not known:
        unmatched.append(row)
        continue
    left = cents
    for k, n in enumerate(known):
        take = left if k == len(known) - 1 else min(left, max(owed[n] - paid[n], 0))
        paid[n] += take
        left -= take
recon = {}
for i in inv:
    bal = owed[i] - paid[i]
    status = "overpaid" if bal < 0 else "paid" if bal == 0 else "open" if paid[i] == 0 else "partial"
    recon[str(i)] = {"paid": paid[i] / 100, "balance": bal / 100, "status": status}

tmp = os.path.join(tempfile.mkdtemp(), "books.db")
if os.path.exists(tmp):
    os.remove(tmp)
con = sqlite3.connect(tmp)
con.execute("create table invoices (id integer primary key, customer text, issued text, amount_cents integer)")
con.executemany("insert into invoices values (?, ?, ?, ?)",
                [(i, c, f"2026-09-{(i % 28) + 1:02d}", a) for i, (c, a) in inv.items()])
con.execute("create table credit_notes (id integer primary key, invoice_id integer, amount_cents integer, reason text)")
con.executemany("insert into credit_notes (invoice_id, amount_cents, reason) values (?, ?, ?)",
                [(i, a, rnd.choice(["damaged goods", "discount", "cancelled"])) for i, a in credits.items()])
con.commit(); con.close()
with open(tmp, "rb") as f_:
    db = f_.read()
os.remove(tmp)
rates_csv = "date,eur_per_usd\n" + "".join(f"{d.isoformat()},{r}\n" for d, r in sorted(rates.items()))

task("hard_reconcile", """
Reconcile October's bank statement against our invoices. bank.csv is the bank's export; books.db holds the
invoices (amounts in cents) and credit notes, which reduce what a customer owes on an invoice. rates.csv has the
EUR per USD exchange rate for each banking day. Rules: only incoming money (positive amounts) is a payment. A USD
payment is converted with the rate of its booking day, or of the latest earlier day that has a rate, and rounded half
up to the cent. A payment names the invoices it pays in its text; it pays them in the order named, each up to what
is still owed on it, and anything left over goes to the last invoice named. Write reconciliation.json: an object
keyed by invoice id, for every invoice, with paid (EUR), balance (EUR, what is still owed after credit notes and
payments, negative when overpaid) and status: paid, partial, open or overpaid. Also write unmatched.json: the list of
bank.csv row numbers (1 = the first row after the header) of payments that name no invoice we have.""",
     {"bank.csv": "\n".join(lines) + "\n", "books.db": db, "rates.csv": rates_csv},
     f"""
exp, exp_un = {recon!r}, {unmatched!r}
d = load_json('reconciliation.json')
if not isinstance(d, dict): fail('reconciliation.json is not an object')
d = {{str(int(re.sub(r'\\D', '', str(k)) or -1)): v for k, v in d.items()}}
if sorted(d, key=int) != sorted(exp, key=int): fail(f'invoice ids {{sorted(d, key=int)}} != {{sorted(exp, key=int)}}')
for k, e in exp.items():
    g = d[k]
    if not isinstance(g, dict): fail(f'invoice {{k}}: {{g!r}} is not an object')
    for f in ('paid', 'balance'):
        try: x = float(g.get(f))
        except (TypeError, ValueError): fail(f'invoice {{k}}: {{f}} = {{g.get(f)!r}} is not a number')
        if abs(x - e[f]) > 0.005: fail(f'invoice {{k}}: {{f}} {{x}} != {{e[f]}}')
    if str(g.get('status', '')).strip().lower() != e['status']: fail(f'invoice {{k}}: status {{g.get("status")!r}} != {{e["status"]!r}}')
u = load_json('unmatched.json')
if isinstance(u, dict):
    lists = [v for v in u.values() if isinstance(v, list)]
    u = lists[0] if len(lists) == 1 else u
if not isinstance(u, list): fail(f'unmatched.json is not a list: {{u!r}}')
rows = sorted(int(x['row']) if isinstance(x, dict) else int(x) for x in u)
if rows != exp_un: fail(f'unmatched rows {{rows}} != {{exp_un}}')
""")

# ---------------------------------------------------------------- 2. hard_logs: three services, DST change
UTC = dt.timezone.utc
def warsaw_offset(t):                          # CEST until 2026-10-25 01:00 UTC, then CET
    return dt.timedelta(hours=2) if t < dt.datetime(2026, 10, 25, 1, 0, tzinfo=UTC) else dt.timedelta(hours=1)

start = dt.datetime(2026, 10, 24, 22, 40, tzinfo=UTC)
reqs = []
t = start
for k in range(46):
    t += dt.timedelta(seconds=rnd.randint(60, 420), milliseconds=rnd.randint(0, 999))
    rid = "".join(rnd.choice("0123456789abcdef") for _ in range(8))
    failed = k % 3 == 1 or k in (40, 41, 42)                     # several failures inside the repeated local hour
    status = rnd.choice([500, 502, 503]) if failed else rnd.choice([200, 200, 201, 204, 404])
    reqs.append({"rid": rid, "t": t, "status": status, "failed": failed,
                 "api_err": rnd.choice([0, 1, 2, 3]) if failed else rnd.choice([0, 0, 0, 1]),
                 "db": (rnd.choice(["deadlock detected", "canceling statement due to statement timeout",
                                    "could not serialize access due to concurrent update",
                                    'duplicate key value violates unique constraint "orders_pkey"'])
                        if (failed and rnd.random() < .6) or (not failed and rnd.random() < .1) else None)})
gw, api, dbl = [], [], []
for r in reqs:
    off = warsaw_offset(r["t"])
    lt = (r["t"] + off).replace(tzinfo=None)
    sign = f"+{off.seconds // 3600:02d}00"
    path = rnd.choice(["/api/orders/", "/api/cart/", "/api/pay/"]) + str(rnd.randint(1, 999))
    gw.append((r["t"], f'10.0.{rnd.randint(0, 9)}.{rnd.randint(2, 250)} - - [{lt:%d/%b/%Y:%H:%M:%S} {sign}] '
                       f'"{rnd.choice(["GET", "POST"])} {path} HTTP/1.1" {r["status"]} {rnd.randint(90, 4000)} '
                       f'rid={r["rid"]} rt={rnd.randint(3, 2900)}ms'))
    ms = int(r["t"].timestamp() * 1000)
    api.append((ms - 40, {"ts": ms - 40, "level": "INFO", "req": r["rid"].upper(), "msg": f"start {path}"}))
    for e in range(r["api_err"]):
        api.append((ms - 30 + e, {"ts": ms - 30 + e, "level": "ERROR", "req": r["rid"].upper(),
                                  "msg": rnd.choice(["upstream timeout", "db call failed", "retry exhausted"])}))
    if r["api_err"] == 0 and r["failed"]:
        api.append((ms - 20, {"ts": ms - 20, "level": "WARN", "req": r["rid"].upper(), "msg": "slow response"}))
    if rnd.random() < .3:
        api.append((ms + 5, {"ts": ms + 5, "level": "ERROR", "req": None, "msg": "metrics push failed"}))
    if r["db"]:
        dt_ = r["t"] - dt.timedelta(milliseconds=rnd.randint(50, 400))
        pid = rnd.randint(1000, 9999)
        dbl.append((dt_, f"{dt_:%Y-%m-%d %H:%M:%S}.{dt_.microsecond // 1000:03d} UTC [{pid}] ERROR:  {r['db']}\n"
                         f"\tDETAIL:  Process {pid} waits for ShareLock on transaction {rnd.randint(10**6, 10**7)}.\n"
                         f"\tCONTEXT:  request req-{r['rid']}\n"
                         f"\tSTATEMENT:  UPDATE orders SET state = 'paid' WHERE id = {rnd.randint(1, 999)}"))
    if rnd.random() < .5:
        lt2 = r["t"] + dt.timedelta(milliseconds=rnd.randint(1, 30))
        dbl.append((lt2, f"{lt2:%Y-%m-%d %H:%M:%S}.{lt2.microsecond // 1000:03d} UTC [{rnd.randint(1000, 9999)}] "
                         f"LOG:  duration: {rnd.randint(1, 900)}.{rnd.randint(100, 999)} ms"))
fails = sorted((r for r in reqs if r["failed"]), key=lambda r: (r["t"].replace(microsecond=0), r["rid"]))
exp_rows = [{"request_id": r["rid"], "utc_time": f"{r['t']:%Y-%m-%dT%H:%M:%S}Z", "status": r["status"],
             "api_errors": r["api_err"], "db_error": r["db"] or ""} for r in fails]
by_hour, by_status = {}, {}
for r in fails:
    h = f"{r['t']:%Y-%m-%dT%H}"
    by_hour[h] = by_hour.get(h, 0) + 1
    by_status[str(r["status"])] = by_status.get(str(r["status"]), 0) + 1
exp_summary = {"failures": len(fails), "by_status": by_status, "by_hour": by_hour}
task("hard_logs", """
Last night's outage left logs from three services in logs/: the gateway's access log, the API's JSON lines and the
database log. The gateway log uses Warsaw local time, and the clocks went back an hour that night. Request ids are
the same across the services, though not always written the same way. Find every request the gateway answered with
a 5xx status and write failures.csv with the columns request_id (as the gateway writes it), utc_time (the gateway's
time in UTC, like 2026-10-24T23:05:09Z), status, api_errors (how many ERROR lines the API logged for that request)
and db_error (the message of the database ERROR entry for that request, empty if there is none), sorted by
utc_time. Also write summary.json with failures (the count), by_status (status -> count) and by_hour (UTC hour like
2026-10-24T23 -> count).""",
     {"logs/gateway/access.log": "\n".join(l for _, l in sorted(gw)) + "\n",
      "logs/api/app.jsonl": "".join(json.dumps(o) + "\n" for _, o in sorted(api, key=lambda x: x[0])),
      "logs/db/postgresql.log": "\n".join(l for _, l in sorted(dbl, key=lambda x: x[0])) + "\n"},
     f"""
exp, summ = {exp_rows!r}, {exp_summary!r}
p = os.path.join(W, 'failures.csv')
if not os.path.exists(p): fail('no failures.csv')
rows = list(csv.DictReader(open(p, newline='')))
if not rows: fail('failures.csv has no rows')
cols = {{c.strip().lower(): c for c in rows[0]}}
for c in ('request_id', 'utc_time', 'status', 'api_errors', 'db_error'):
    if c not in cols: fail(f'failures.csv has no column {{c}} (columns: {{list(rows[0])}})')
if len(rows) != len(exp): fail(f'failures.csv: {{len(rows)}} rows, expected {{len(exp)}}')
for k, (g, e) in enumerate(zip(rows, exp)):
    g = {{c: (g[cols[c]] or '').strip() for c in cols}}
    if g['request_id'] != e['request_id']: fail(f'row {{k + 1}}: request_id {{g["request_id"]!r}} != {{e["request_id"]!r}} (order or ids wrong)')
    t = g['utc_time'].replace(' ', 'T').replace('+00:00', 'Z')
    if not t.endswith('Z'): t += 'Z'
    if t != e['utc_time']: fail(f'row {{k + 1}} ({{e["request_id"]}}): utc_time {{g["utc_time"]!r}} != {{e["utc_time"]!r}}')
    for c in ('status', 'api_errors'):
        try: v = int(float(g[c]))
        except ValueError: fail(f'row {{k + 1}}: {{c}} {{g[c]!r}} is not a number')
        if v != e[c]: fail(f'row {{k + 1}} ({{e["request_id"]}}): {{c}} {{v}} != {{e[c]}}')
    if g['db_error'] != e['db_error']: fail(f'row {{k + 1}} ({{e["request_id"]}}): db_error {{g["db_error"]!r}} != {{e["db_error"]!r}}')
s = load_json('summary.json')
if not isinstance(s, dict): fail('summary.json is not an object')
if s.get('failures') != summ['failures']: fail(f'summary failures {{s.get("failures")!r}} != {{summ["failures"]}}')
bs = {{str(k): v for k, v in (s.get('by_status') or {{}}).items()}}
if bs != summ['by_status']: fail(f'summary by_status {{bs}} != {{summ["by_status"]}}')
bh = {{str(k).replace(' ', 'T')[:13]: v for k, v in (s.get('by_hour') or {{}}).items()}}
if bh != summ['by_hour']: fail(f'summary by_hour {{bh}} != {{summ["by_hour"]}}')
""")

# ---------------------------------------------------------------- 3. hard_buildorder: build a tool, then upgrade it
def toml_pkg(name, version, deps, dev=None, extra=""):
    s = f'[package]\nname = "{name}"\nversion = "{version}"\n{extra}\n[dependencies]\n'
    s += "".join(f'{d} = "{v}"\n' for d, v in deps.items())
    if dev:
        s += "\n[dev-dependencies]\n" + "".join(f'{d} = "{v}"\n' for d, v in dev.items())
    return s


def kahn(graph):                               # graph: name -> set of in-repo deps; ties alphabetical
    indeg = {n: len(ds) for n, ds in graph.items()}
    users = {n: set() for n in graph}
    for n, ds in graph.items():
        for d in ds:
            users[d].add(n)
    ready = [n for n, k in indeg.items() if k == 0]
    heapq.heapify(ready)
    out = []
    while ready:
        n = heapq.heappop(ready)
        out.append(n)
        for u in users[n]:
            indeg[u] -= 1
            if indeg[u] == 0:
                heapq.heappush(ready, u)
    return out


ver = lambda: f"^{rnd.randint(0, 3)}.{rnd.randint(0, 9)}"
repo1 = {  # name: (dir, deps)
    "logging": ("logging", ["requests"]), "strutil": ("string-utils", []), "config": ("config", ["strutil", "pyyaml"]),
    "core": ("core", ["logging", "config"]), "db": ("db", ["core", "sqlalchemy"]), "auth": ("auth", ["core", "db"]),
    "api": ("api", ["auth", "db", "strutil"]), "cli": ("cli", ["config", "core"]), "web": ("web", ["api", "auth"]),
    "metrics": ("metrics", ["logging"]), "admin": ("admin", ["web", "metrics"]), "bench": ("bench", ["cli"]),
}
files = {}
for n, (d, deps) in repo1.items():
    files[f"repo1/packages/{d}/package.toml"] = toml_pkg(n, f"1.{rnd.randint(0, 9)}.0", {x: ver() for x in deps})
files["repo1/README.md"] = "Packages live in packages/<dir>/package.toml.\n"
order1 = kahn({n: {x for x in deps if x in repo1} for n, (_, deps) in repo1.items()})

repo2 = {  # name: (dir, deps, dev-deps)
    "base": ("base", ["six"], ["testkit"]), "testkit": ("testkit", ["base"], []),     # dev dep would make a false cycle
    "codec": ("codec-lib", ["base"], []), "store": ("store", ["codec", "base"], ["testkit"]),
    "queue": ("queue", ["store", "events"], []), "events": ("events", ["worker"], []),
    "worker": ("worker", ["queue"], []),                                               # real cycle queue->events->worker
    "jobs": ("jobs", ["worker", "codec"], []), "ui": ("ui", ["jobs"], []),             # blocked through the cycle
    "report": ("report", ["store"], ["ui"]), "export": ("export", ["report", "codec"], []),
    "a11y": ("a11y", [], ["ui"]), "zeta": ("zeta", ["export", "a11y"], []),
    "lint": ("lint", ["lint-core"], []), "lint-core": ("lint-core", ["lint"], []),     # second cycle
}
for n, (d, deps, dev) in repo2.items():
    files[f"repo2/packages/{d}/package.toml"] = toml_pkg(n, f"2.{rnd.randint(0, 9)}.1", {x: ver() for x in deps},
                                                         {x: ver() for x in dev})
files["repo2/packages/docs/README.md"] = "Documentation only, not a package.\n"
g2 = {n: {x for x in deps if x in repo2} for n, (_, deps, _) in repo2.items()}
order2 = kahn(g2)
blocked2 = sorted(set(repo2) - set(order2))

task("hard_buildorder", """
Make a tool that works out the build order of a folder of packages: each package is described by
packages/<dir>/package.toml, and its [dependencies] name other packages by their package name. Dependencies that are
not packages in the folder are external and ignored. Every package must come after the packages it depends on; when
several are ready, the alphabetically first one goes first. Use the tool on repo1 and write repo1/build_order.json, a
JSON list of package names.""",
     files,
     f"""
E1, E2, B2 = {order1!r}, {order2!r}, {blocked2!r}
o1 = load_json('repo1/build_order.json')
if isinstance(o1, dict) and isinstance(o1.get('order'), list): o1 = o1['order']
if o1 != E1: fail(f'repo1 order {{o1}} != {{E1}}')
o2 = load_json('repo2/build_order.json')
if not isinstance(o2, dict): fail(f'repo2/build_order.json is not an object: {{str(o2)[:200]}}')
if o2.get('order') != E2: fail(f'repo2 order {{o2.get("order")}} != {{E2}}')
if sorted(o2.get('blocked') or []) != B2: fail(f'repo2 blocked {{o2.get("blocked")}} != {{B2}}')
runs = sorted(glob.glob(os.path.join(W, '.mini', 'runs', '*')))
first = [r for r in runs if not r.endswith('-b')]
second = [r for r in runs if r.endswith('-b')]
if not first or not second: fail(f'expected two runs, got {{[os.path.basename(r) for r in runs]}}')
inst1, ran1 = tool_runs([os.path.join(r, 'log.jsonl') for r in first])
inst2, ran2 = tool_runs([os.path.join(r, 'log.jsonl') for r in second])
if not inst1 & ran1: fail(f'task 1: no built tool ran successfully (built {{sorted(inst1)}})')
if inst2 - inst1: fail(f'task 2 built new tools {{sorted(inst2 - inst1)}} instead of upgrading {{sorted(inst1)}}')
if not inst1 & ran2: fail(f'task 2 did not run the tool from task 1 (ran {{sorted(ran2)}})')
""",
     prompt2="""
repo2 has two things the build-order tool does not handle yet. Packages may have [dev-dependencies], which do not
count for the build order. And there are dependency cycles: a package in a cycle cannot be built, and neither can a
package that depends on one, directly or through others. Use the tool on repo2 and write repo2/build_order.json as an
object: order (the buildable packages, by the same rules as before) and blocked (the packages that cannot be built,
sorted).""")
