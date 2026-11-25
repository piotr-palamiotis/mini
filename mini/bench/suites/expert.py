"""expert: harder than the hard suite (gemma-4 31B passed that one 9/9 on 2026-11-25).

expert_bugfix    a small Python package with four planted bugs, a user's bug report, hidden tests
expert_schedule  meetings booked one after another from iCalendar files in three time zones (recurring events,
                 exceptions, all-day absences, buffers, working hours), each at its earliest valid slot
expert_fifo      FIFO stock accounting from an event log: transfers keep cost layers, returns take back the layers
                 a sale consumed, oversold sales are rejected
"""
import csv
import datetime as dt
import io
import json
import random
import textwrap
from decimal import Decimal, ROUND_HALF_UP

from .. import Suite

SUITE = Suite("expert")
TIMEOUT = 3600

HEAD = '''import os, sys, re, json, glob, csv, subprocess, shutil, tempfile
W, A = sys.argv[1], open(sys.argv[2]).read()
def fail(m): print('FAIL', m); sys.exit(1)

def load_json(rel):
    p = os.path.join(W, rel)
    if not os.path.exists(p): fail(f'no {rel}')
    try: return json.load(open(p))
    except ValueError as e: fail(f'{rel} is not valid JSON: {e}')
'''


def task(name, prompt, files, check, prompt2=None, timeout=TIMEOUT):
    SUITE.add(name, " ".join(prompt.split()), files, HEAD + textwrap.dedent(check) + "\nprint('PASS')\n",
              " ".join(prompt2.split()) if prompt2 else None, timeout=timeout)


rnd = random.Random(53)
H = Decimal("0.01")


def half_up(x):
    return x.quantize(H, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------- 1. expert_bugfix: four bugs, one bug report
PKG = {
    "inventory/__init__.py": '"""Monthly sales reports from the shop\'s sales export."""\n',
    "inventory/parse.py": '''\
"""Reading the sales export: a CSV file with the columns date, sku, name, qty, unit_price."""


def read_rows(path):
    """The rows of the export as dicts (all values are strings)."""
    rows = []
    with open(path, encoding="utf-8") as f:
        header = f.readline().strip().split(",")
        for line in f:
            if not line.strip():
                continue
            values = line.rstrip("\\n").split(",")
            rows.append(dict(zip(header, values)))
    return rows
''',
    "inventory/money.py": '''\
"""Money is Decimal, rounded to cents half up (the way the accounts do it)."""
from decimal import Decimal


def cents(x):
    """x rounded to cents."""
    return round(Decimal(x), 2)


def line_total(qty, unit_price):
    """What one row of the export earned: qty * unit_price, in cents."""
    return cents(Decimal(qty) * Decimal(unit_price))
''',
    "inventory/stock.py": '''\
"""Totals per product."""
from decimal import Decimal

from .money import line_total


def by_sku(rows, totals={}):
    """{sku: {"name", "qty", "revenue"}} over rows."""
    for r in rows:
        t = totals.setdefault(r["sku"], {"name": r["name"], "qty": 0, "revenue": Decimal("0")})
        t["qty"] += int(r["qty"])
        t["revenue"] += line_total(r["qty"], r["unit_price"])
    return totals
''',
    "inventory/report.py": '''\
"""The monthly report: python -m inventory.report SALES.csv MONTH [MONTH ...] writes report_<MONTH>.txt for each
month (YYYY-MM): one line per product, highest revenue first (ties: by SKU), then the total."""
import datetime
import sys

from .parse import read_rows
from .stock import by_sku


def month_rows(rows, month):
    year, mon = (int(x) for x in month.split("-"))
    return [r for r in rows if datetime.date.fromisoformat(r["date"]).year == year
            and datetime.date.fromisoformat(r["date"]).month == mon]


def render(month, totals):
    lines = [f"Sales report {month}", f"{'SKU':<8}{'NAME':<24}{'QTY':>5}{'REVENUE':>11}"]
    for sku, t in sorted(totals.items(), key=lambda kv: (str(kv[1]["revenue"]), kv[0]), reverse=True):
        lines.append(f"{sku:<8}{t['name']:<24}{t['qty']:>5}{t['revenue']:>11}")
    qty = sum(t["qty"] for t in totals.values())
    revenue = sum(t["revenue"] for t in totals.values())
    lines.append(f"{'TOTAL':<32}{qty:>5}{revenue:>11}")
    return "\\n".join(lines) + "\\n"


def main(argv):
    path, months = argv[0], argv[1:]
    rows = read_rows(path)
    for month in months:
        with open(f"report_{month}.txt", "w", encoding="utf-8") as f:
            f.write(render(month, by_sku(month_rows(rows, month))))


if __name__ == "__main__":
    main(sys.argv[1:])
''',
    "tests/__init__.py": "",
    "tests/test_inventory.py": '''\
import os
import tempfile
import unittest
from decimal import Decimal

from inventory.money import line_total
from inventory.parse import read_rows
from inventory.report import render


class InventoryTest(unittest.TestCase):
    def test_line_total(self):
        self.assertEqual(line_total("3", "2.50"), Decimal("7.50"))

    def test_read_rows(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.csv")
            with open(p, "w") as f:
                f.write("date,sku,name,qty,unit_price\\n2026-10-01,A1,Washer,2,0.10\\n")
            self.assertEqual(read_rows(p)[0]["name"], "Washer")

    def test_render_total(self):
        text = render("2026-10", {"A1": {"name": "Washer", "qty": 2, "revenue": Decimal("0.20")}})
        self.assertIn("TOTAL", text.splitlines()[-1])
        self.assertTrue(text.splitlines()[-1].endswith("0.20"))


if __name__ == "__main__":
    unittest.main()
''',
}

products = [("A100", "Hex bolt M8"), ("A101", "Hex nut M8"), ("B200", "Wood screw, 4x40"), ("B201", "Wall plug"),
            ("C300", "Cable tie, 200 mm"), ("C301", "Hose clamp"), ("D400", "Washer"), ("D401", "Spring washer"),
            ("E500", "Rivet, 4 mm"), ("E501", "Threaded rod M6")]
prices = {s: Decimal(f"{rnd.randint(1, 900)}.{rnd.choice(['005', '015', '125', '335', '445', '25', '5', '99'])}")
          / (10 if i % 3 else 1) for i, (s, _) in enumerate(products)}
prices = {s: p.quantize(Decimal("0.001")) for s, p in prices.items()}
sales = []
for month, days in (("2026-09", 30), ("2026-10", 31), ("2026-11", 5)):
    for _ in range(26):
        s, n = rnd.choice(products)
        sales.append((f"{month}-{rnd.randint(1, days):02d}", s, n, rnd.randint(1, 40), prices[s]))
sales.sort()


def report_ok(month):
    tot = {}
    for d, s, n, q, p in sales:
        if d.startswith(month):
            t = tot.setdefault(s, {"name": n, "qty": 0, "revenue": Decimal(0)})
            t["qty"] += q
            t["revenue"] += half_up(Decimal(q) * p)
    lines = [f"Sales report {month}", f"{'SKU':<8}{'NAME':<24}{'QTY':>5}{'REVENUE':>11}"]
    for sku, t in sorted(tot.items(), key=lambda kv: (-kv[1]["revenue"], kv[0])):
        lines.append(f"{sku:<8}{t['name']:<24}{t['qty']:>5}{t['revenue']:>11}")
    lines.append(f"{'TOTAL':<32}{sum(t['qty'] for t in tot.values()):>5}{sum(t['revenue'] for t in tot.values()):>11}")
    return "\n".join(lines) + "\n"


buf = io.StringIO()
cw = csv.writer(buf, lineterminator="\n")
cw.writerow(["date", "sku", "name", "qty", "unit_price"])
for d, s, n, q, p in sales:
    cw.writerow([d, s, n, q, p])
HIDDEN = '''
import os, tempfile, unittest
from decimal import Decimal
from inventory.money import line_total, cents
from inventory.parse import read_rows
from inventory.stock import by_sku
from inventory.report import render

class Hidden(unittest.TestCase):
    def test_half_up(self):
        self.assertEqual(cents(Decimal("3.325")), Decimal("3.33"))
        self.assertEqual(cents(Decimal("2.665")), Decimal("2.67"))
        self.assertEqual(line_total("1", "0.125"), Decimal("0.13"))
    def test_quoted_name(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.csv")
            with open(p, "w") as f:
                f.write('date,sku,name,qty,unit_price\\n2026-10-02,B1,"Screw, 4x40",3,0.50\\n')
            r = read_rows(p)[0]
            self.assertEqual((r["name"], r["qty"], r["unit_price"]), ("Screw, 4x40", "3", "0.50"))
    def test_two_calls_do_not_mix(self):
        a = by_sku([{"sku": "X", "name": "x", "qty": "1", "unit_price": "1.00"}])
        b = by_sku([{"sku": "Y", "name": "y", "qty": "2", "unit_price": "1.00"}])
        self.assertEqual(sorted(a), ["X"])
        self.assertEqual(sorted(b), ["Y"])
    def test_order_by_revenue(self):
        t = {"S1": {"name": "a", "qty": 1, "revenue": Decimal("9.50")},
             "S2": {"name": "b", "qty": 1, "revenue": Decimal("120.00")},
             "S3": {"name": "c", "qty": 1, "revenue": Decimal("120.00")}}
        body = [ln.split()[0] for ln in render("2026-10", t).splitlines()[2:-1]]
        self.assertEqual(body, ["S2", "S3", "S1"])

if __name__ == "__main__":
    unittest.main()
'''
task("expert_bugfix", """
Users report that the monthly sales reports made by the inventory package are wrong: some totals are a cent off,
products with a comma in their name come out garbled, the biggest sellers are not at the top, and when several months
are made in one go, the later reports include products and amounts from the earlier months. Find and fix the bugs in
the inventory package (the tests in tests/ must keep passing; add tests if you like, but do not change what the
existing ones check). Then make the reports for September and October 2026 with
`python -m inventory.report data/sales.csv 2026-09 2026-10`.""",
     dict(PKG, **{"data/sales.csv": buf.getvalue()}),
     f"""
exp = {{'2026-09': {report_ok('2026-09')!r}, '2026-10': {report_ok('2026-10')!r}}}
for m, text in exp.items():
    p = os.path.join(W, f'report_{{m}}.txt')
    if not os.path.exists(p): fail(f'no report_{{m}}.txt')
    got = open(p).read()
    if got.rstrip() != text.rstrip():
        g, e = got.splitlines(), text.splitlines()
        k = next((i for i, (x, y) in enumerate(zip(g, e)) if x.rstrip() != y.rstrip()), min(len(g), len(e)))
        fail(f'report_{{m}}.txt line {{k + 1}}: {{(g[k] if k < len(g) else None)!r}} != {{(e[k] if k < len(e) else None)!r}}')
orig = {PKG['tests/test_inventory.py']!r}
tmp = tempfile.mkdtemp()
shutil.copytree(os.path.join(W, 'inventory'), os.path.join(tmp, 'inventory'))
os.makedirs(os.path.join(tmp, 'tests'))
open(os.path.join(tmp, 'tests', '__init__.py'), 'w').close()
open(os.path.join(tmp, 'tests', 'test_inventory.py'), 'w').write(orig)
open(os.path.join(tmp, 'tests', 'test_hidden.py'), 'w').write({HIDDEN!r})
r = subprocess.run([sys.executable, '-m', 'unittest', '-q'], cwd=tmp, capture_output=True, text=True, timeout=120,
                   env={{'PATH': os.environ.get('PATH', ''), 'PYTHONDONTWRITEBYTECODE': '1'}})
if r.returncode: fail('the tests fail on the fixed package: ' + (r.stderr or r.stdout)[-400:])
""")

# ---------------------------------------------------------------- 2. expert_schedule: iCalendar, three time zones
from zoneinfo import ZoneInfo   # noqa: E402

rnd = random.Random(0)          # each task has its own stream: changing one leaves the others as they are

UTC = dt.timezone.utc
PEOPLE = {"ana": "Europe/Warsaw", "ben": "America/New_York", "chie": "Asia/Tokyo", "dora": "Europe/Warsaw",
          "eli": "America/New_York"}
WEEK = [dt.date(2026, 11, 30) + dt.timedelta(days=k) for k in range(5)]        # Mon 30 Nov .. Fri 4 Dec
WORK = {"ana": (8, 16), "ben": (7, 15), "chie": (14, 23), "dora": (9, 17), "eli": (6, 14)}  # local working hours
STEP, BUFFER = dt.timedelta(minutes=15), dt.timedelta(minutes=15)


def ics_time(t, tz):
    return t.astimezone(ZoneInfo(tz)).strftime("%Y%m%dT%H%M%S")


cal_events = {p: [] for p in PEOPLE}     # (VEVENT text, busy intervals in UTC)
busy = {p: [] for p in PEOPLE}


def add_event(p, summary, start_local, minutes, rrule=None, exdates=(), count_days=None):
    tz = PEOPLE[p]
    z = ZoneInfo(tz)
    s = start_local.replace(tzinfo=z)
    lines = ["BEGIN:VEVENT", f"UID:{p}-{len(cal_events[p])}@bench", f"SUMMARY:{summary}",
             f"DTSTART;TZID={tz}:{s.strftime('%Y%m%dT%H%M%S')}",
             f"DTEND;TZID={tz}:{(s + dt.timedelta(minutes=minutes)).strftime('%Y%m%dT%H%M%S')}"]
    occ = [s]
    if rrule:
        lines.append(f"RRULE:{rrule}")
        occ = count_days
    for ex in exdates:
        lines.append(f"EXDATE;TZID={tz}:{ex.strftime('%Y%m%dT%H%M%S')}")
    lines.append("END:VEVENT")
    cal_events[p].append("\r\n".join(lines))
    exs = {ex.replace(tzinfo=z) for ex in exdates}
    for o in occ:
        if o not in exs:
            busy[p].append((o.astimezone(UTC), (o + dt.timedelta(minutes=minutes)).astimezone(UTC)))


def local(p, day, h, m=0):
    return dt.datetime.combine(day, dt.time(h, m))


# recurring: a weekly stand-up Mon/Wed/Fri for ana since October (summer time then), with one exception this week
z = ZoneInfo(PEOPLE["ana"])
first = dt.datetime(2026, 10, 5, 13, 30)
occ = [dt.datetime.combine(d, dt.time(13, 30)).replace(tzinfo=z) for d in WEEK if d.weekday() in (0, 2, 4)]
add_event("ana", "Stand-up", first, 30, "FREQ=WEEKLY;BYDAY=MO,WE,FR;UNTIL=20261231T235959Z",
          exdates=[dt.datetime(2026, 12, 2, 13, 30)], count_days=occ)
# ben: daily 1:1 at 08:00 New York, COUNT ends on Monday
zb = ZoneInfo(PEOPLE["ben"])
add_event("ben", "1:1", dt.datetime(2026, 11, 26, 8, 0), 45, "FREQ=DAILY;COUNT=5",
          count_days=[dt.datetime(2026, 11, 26, 8, 0, tzinfo=zb) + dt.timedelta(days=k) for k in range(5)
                      if (dt.datetime(2026, 11, 26) + dt.timedelta(days=k)).date() >= WEEK[0]])
# chie: Tuesday/Thursday evening review in Tokyo
zc = ZoneInfo(PEOPLE["chie"])
add_event("chie", "Review", dt.datetime(2026, 11, 3, 20, 0), 90, "FREQ=WEEKLY;BYDAY=TU,TH",
          count_days=[dt.datetime.combine(d, dt.time(20, 0)).replace(tzinfo=zc) for d in WEEK if d.weekday() in (1, 3)])
# one-off meetings
for p in PEOPLE:
    for _ in range(6):
        d = rnd.choice(WEEK)
        h0, h1 = WORK[p]
        add_event(p, rnd.choice(["Call", "Planning", "Interview", "Lunch", "Focus time"]),
                  local(p, d, rnd.randint(h0, h1 - 2), rnd.choice([0, 15, 30, 45])), rnd.choice([30, 45, 60, 90]))
# all-day absences (DATE values, local days)
allday = {"dora": [dt.date(2026, 12, 1)], "eli": [dt.date(2026, 12, 3), dt.date(2026, 12, 4)]}
for p, days in allday.items():
    for d in days:
        cal_events[p].append("\r\n".join(["BEGIN:VEVENT", f"UID:{p}-off-{d}@bench", "SUMMARY:Out of office",
                                          f"DTSTART;VALUE=DATE:{d:%Y%m%d}",
                                          f"DTEND;VALUE=DATE:{d + dt.timedelta(days=1):%Y%m%d}", "END:VEVENT"]))
        z = ZoneInfo(PEOPLE[p])
        busy[p].append((dt.datetime.combine(d, dt.time()).replace(tzinfo=z).astimezone(UTC),
                        dt.datetime.combine(d + dt.timedelta(days=1), dt.time()).replace(tzinfo=z).astimezone(UTC)))
# a cancelled meeting that must not count
cal_events["ben"].append("\r\n".join(["BEGIN:VEVENT", "UID:ben-cancelled@bench", "SUMMARY:Board sync", "STATUS:CANCELLED",
                                      f"DTSTART;TZID={PEOPLE['ben']}:20261201T080000",
                                      f"DTEND;TZID={PEOPLE['ben']}:20261201T120000", "END:VEVENT"]))


def vcal(p):
    return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//bench//EN\r\n" + "\r\n".join(cal_events[p]) + "\r\nEND:VCALENDAR\r\n"


requests = [{"id": "kickoff", "minutes": 60, "attendees": ["ana", "ben", "chie"]},
            {"id": "design", "minutes": 90, "attendees": ["ana", "dora", "eli"]},
            {"id": "retro", "minutes": 45, "attendees": ["ben", "chie", "dora", "eli"]},
            {"id": "budget", "minutes": 120, "attendees": ["ana", "ben", "chie", "dora", "eli"]},
            {"id": "sync", "minutes": 30, "attendees": ["chie", "eli"]}]


def free(p, s, e):
    """p can attend [s, e): inside their working hours that local weekday, and nothing within BUFFER of it."""
    z = ZoneInfo(PEOPLE[p])
    ls, le = s.astimezone(z), e.astimezone(z)
    h0, h1 = WORK[p]
    day0 = dt.datetime.combine(ls.date(), dt.time()).replace(tzinfo=z)
    if ls.weekday() > 4 or ls < day0 + dt.timedelta(hours=h0) or le > day0 + dt.timedelta(hours=h1):
        return False
    return all(not (s < b1 + BUFFER and b0 - BUFFER < e) for b0, b1 in busy[p])


sched = {}
start0 = dt.datetime(2026, 11, 29, 0, 0, tzinfo=UTC)
for rq in requests:
    t, got = start0, None
    length = dt.timedelta(minutes=rq["minutes"])
    while t < dt.datetime(2026, 12, 5, 12, 0, tzinfo=UTC):
        if all(free(p, t, t + length) for p in rq["attendees"]):
            got = t
            break
        t += STEP
    if got:
        sched[rq["id"]] = {"start": got.strftime("%Y-%m-%dT%H:%MZ"), "end": (got + length).strftime("%Y-%m-%dT%H:%MZ")}
        for p in rq["attendees"]:
            busy[p].append((got, got + length))
    else:
        sched[rq["id"]] = None
people_txt = "\n".join(f"{p}: {PEOPLE[p]}, works {WORK[p][0]:02d}:00-{WORK[p][1]:02d}:00 local time, Monday to Friday"
                       for p in PEOPLE)
task("expert_schedule", """
Book the meetings in requests.json for the week of 30 November 2026, in the order they are listed. calendars/ has
each person's calendar as an iCalendar file, and people.txt says where they are and their working hours. A meeting
must lie inside every attendee's working hours (in their own time zone), must not overlap anything in their calendars
(recurring events count on the days they recur, except their EXDATEs; all-day events block the whole local day;
cancelled events do not count), and needs 15 minutes free before and after it for every attendee. Meetings start on a
quarter hour. Book each one at the earliest time that works; a booked meeting then blocks its attendees (with the
same 15 minutes around it) for the later requests. Write schedule.json: an object mapping each request id to
{"start": ..., "end": ...} in UTC, like 2026-11-30T14:15Z, or to null when it cannot be booked that week.""",
     dict({f"calendars/{p}.ics": vcal(p) for p in PEOPLE},
          **{"requests.json": json.dumps(requests, indent=1) + "\n", "people.txt": people_txt + "\n"}),
     f"""
exp = {sched!r}
d = load_json('schedule.json')
if not isinstance(d, dict): fail('schedule.json is not an object')
def norm(t):
    t = str(t).strip().replace(' ', 'T').replace('+00:00', 'Z')
    m = re.match(r'(\\d{{4}}-\\d\\d-\\d\\dT\\d\\d:\\d\\d)(:00)?(\\.0+)?Z?$', t)
    return m.group(1) + 'Z' if m else t
for k, e in exp.items():
    if k not in d: fail(f'schedule.json has no {{k}}')
    g = d[k]
    if e is None:
        if g not in (None, {{}}): fail(f'{{k}}: booked at {{g}}, but it cannot be booked that week')
        continue
    if not isinstance(g, dict): fail(f'{{k}}: {{g!r}}, expected {{e}}')
    if norm(g.get('start')) != e['start'] or norm(g.get('end')) != e['end']:
        fail(f'{{k}}: {{g.get("start")}} - {{g.get("end")}} != {{e["start"]}} - {{e["end"]}}')
""")

# ---------------------------------------------------------------- 3. expert_fifo: cost layers through transfers and returns
rnd = random.Random(71)
SKUS = ["P-1", "P-2", "P-3"]
WH = ["north", "south"]
events = []
layers = {(w, s): [] for w in WH for s in SKUS}      # [qty, unit_cost, received]
sale_layers, sale_cogs, rejected = {}, {}, []
eid = 0


def ev(kind, **kw):
    global eid
    eid += 1
    events.append(dict(id=eid, kind=kind, **kw))
    return eid


day = dt.date(2026, 10, 1)
for k in range(70):
    day += dt.timedelta(days=rnd.choice([0, 0, 1]))
    roll = rnd.random()
    w, s = rnd.choice(WH), rnd.choice(SKUS)
    if roll < .3 or k < 6:
        q, c = rnd.randint(5, 40), Decimal(f"{rnd.randint(2, 60)}.{rnd.randint(0, 9999):04d}")
        i = ev("purchase", date=str(day), warehouse=w, sku=s, qty=q, unit_cost=str(c))
        layers[(w, s)].append([q, c, i])
    elif roll < .65:
        have = sum(l[0] for l in layers[(w, s)])
        q = rnd.randint(1, 25)
        if k % 11 == 5:
            q = have + rnd.randint(1, 5)                 # more than in stock: rejected
        i = ev("sale", date=str(day), warehouse=w, sku=s, qty=q, unit_price=str(Decimal(rnd.randint(30, 120))))
        if q > have:
            rejected.append(i)
            continue
        took, left = [], q
        while left:
            l = layers[(w, s)][0]
            n = min(left, l[0])
            took.append([n, l[1], l[2]])
            l[0] -= n
            left -= n
            if not l[0]:
                layers[(w, s)].pop(0)
        sale_layers[i] = (w, s, took)
        sale_cogs[i] = sum(Decimal(n) * c for n, c, _ in took)
    elif roll < .8:
        o = [x for x in WH if x != w][0]
        have = sum(l[0] for l in layers[(w, s)])
        if not have:
            continue
        q = rnd.randint(1, have)
        ev("transfer", date=str(day), sku=s, qty=q, **{"from": w, "to": o})
        left, moved = q, []
        while left:
            l = layers[(w, s)][0]
            n = min(left, l[0])
            moved.append([n, l[1], l[2]])
            l[0] -= n
            left -= n
            if not l[0]:
                layers[(w, s)].pop(0)
        layers[(o, s)] = sorted(layers[(o, s)] + moved, key=lambda l: l[2])   # keep FIFO by original receipt
    else:
        cands = [i for i, (_, _, took) in sale_layers.items() if sum(n for n, _, _ in took) > 0]
        if not cands:
            continue
        sid = rnd.choice(cands)
        w0, s0, took = sale_layers[sid]
        q = rnd.randint(1, sum(n for n, _, _ in took))
        ev("return", date=str(day), sale=sid, qty=q)
        left, back = q, []
        for l in reversed(took):                         # the units consumed last come back first
            if not left:
                break
            n = min(left, l[0])
            back.append([n, l[1], l[2]])
            l[0] -= n
            left -= n
        sale_cogs[sid] -= sum(Decimal(n) * c for n, c, _ in back)
        layers[(w0, s0)] = sorted(layers[(w0, s0)] + back, key=lambda l: l[2])
closing = {w: {s: {"qty": sum(l[0] for l in layers[(w, s)]),
                   "value": float(half_up(sum((Decimal(l[0]) * l[1] for l in layers[(w, s)]), Decimal(0))))}
               for s in SKUS} for w in WH}
cogs = {str(i): float(half_up(v)) for i, v in sale_cogs.items()}
ebuf = io.StringIO()
cw = csv.writer(ebuf, lineterminator="\n")
cw.writerow(["id", "date", "kind", "warehouse", "sku", "qty", "unit_cost", "unit_price", "from", "to", "sale"])
for e in events:
    cw.writerow([e["id"], e["date"], e["kind"], e.get("warehouse", ""), e.get("sku", ""), e["qty"],
                 e.get("unit_cost", ""), e.get("unit_price", ""), e.get("from", ""), e.get("to", ""), e.get("sale", "")])
task("expert_fifo", """
events.csv is the stock ledger of our two warehouses for October, in order. Work out the cost of goods sold with FIFO
per warehouse and product: a sale takes the oldest units first. A transfer moves units out of one warehouse the same
way (oldest first) and they keep their unit cost and their place in the FIFO order by the purchase they came from. A
return (its sale column names the sale) puts units back into the warehouse of that sale, taking back the cost layers
that sale consumed, the ones it consumed last first, and lowers that sale's cost of goods. A sale of more units than
the warehouse has at that moment is rejected and changes nothing. Use exact decimals and round to cents (half up) only
when writing. Write cogs.json (sale id -> cost of goods sold of that sale after returns, for every sale that was not
rejected), closing.json (warehouse -> product -> {"qty": units left, "value": their cost}) and rejected.json (the
list of rejected sale ids).""",
     {"events.csv": ebuf.getvalue()},
     f"""
exp_cogs, exp_close, exp_rej = {cogs!r}, {closing!r}, {sorted(rejected)!r}
c = load_json('cogs.json')
if not isinstance(c, dict): fail('cogs.json is not an object')
c = {{str(int(float(k))): v for k, v in c.items()}}
if sorted(c, key=int) != sorted(exp_cogs, key=int): fail(f'cogs.json sales {{sorted(c, key=int)}} != {{sorted(exp_cogs, key=int)}}')
for k, v in exp_cogs.items():
    try: x = float(c[k])
    except (TypeError, ValueError): fail(f'cogs.json {{k}}: {{c[k]!r}} is not a number')
    if abs(x - v) > 0.005: fail(f'cogs.json sale {{k}}: {{x}} != {{v}}')
cl = load_json('closing.json')
if not isinstance(cl, dict): fail('closing.json is not an object')
for w, per in exp_close.items():
    if w not in cl: fail(f'closing.json has no warehouse {{w}}')
    for s, e in per.items():
        g = (cl[w] or {{}}).get(s)
        if g is None and e['qty'] == 0: continue
        if not isinstance(g, dict): fail(f'closing.json {{w}}/{{s}}: {{g!r}}')
        try: q, v = float(g.get('qty')), float(g.get('value'))
        except (TypeError, ValueError): fail(f'closing.json {{w}}/{{s}}: {{g}} has no numeric qty and value')
        if q != e['qty'] or abs(v - e['value']) > 0.005: fail(f'closing.json {{w}}/{{s}}: {{g}} != {{e}}')
r = load_json('rejected.json')
if isinstance(r, dict):
    lists = [v for v in r.values() if isinstance(v, list)]
    r = lists[0] if len(lists) == 1 else r
if not isinstance(r, list): fail(f'rejected.json is not a list: {{r!r}}')
if sorted(int(x) for x in r) != exp_rej: fail(f'rejected {{sorted(r)}} != {{exp_rej}}')
""")
