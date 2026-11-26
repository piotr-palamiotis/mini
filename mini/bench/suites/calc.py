"""calc: arithmetic that must be done by a program (from bench2 make_calc.py)."""
import tempfile
from .. import Suite

SUITE = Suite('calc')
import csv, datetime as dt, io, json, os, random, shutil, statistics, textwrap
from decimal import Decimal, ROUND_HALF_UP

TIMEOUT = 2400

HEAD = r'''import os, sys, re, json, glob, csv, io
W, A = sys.argv[1], open(sys.argv[2]).read()
def fail(m): print('FAIL', m); sys.exit(1)

PROGRAM = ('run_code', 'invoke_tool', 'keep_tool')      # their results are program output (keep_tool runs the kept tool)
TYPED = ('write_file', 'edit_file', 'replace', 'replace_lines', 'append_file', 'run_code', 'invoke_tool', 'finish')
NUM = re.compile(r'-?\d+(?:\.\d+)?')                  # plain numbers; CSV commas separate them
GROUPED = re.compile(r'-?\d{1,3}(?:,\d{3})+(?:\.\d+)?')  # and 1,234.56 read whole as well

def logs():
    out = []
    for lg in glob.glob(os.path.join(W, '.mini', 'runs', '*', 'log.jsonl')):
        ev = [json.loads(l) for l in open(lg)]
        out.append((ev[0].get('task', '') if ev else '', ev))
    return out

def nums(text):
    got = []
    for m in list(NUM.finditer(text or '')) + list(GROUPED.finditer(text or '')):
        try: got.append(float(m.group().replace(',', '')))
        except ValueError: pass
    return got

def trace():
    """[(order, 'program' | 'typed', call name, text)] over every run: program outputs and the text the model typed
    (call arguments and, for calls that take a fenced block, the reply that carried it). Plus {path: writer}."""
    items, writer, k = [], {}, 0
    for _, ev in logs():
        reply, last, read_path = '', None, None
        for e in ev:
            k += 1
            if e.get('step') not in (None, 'execute'): continue
            kind = e.get('kind')
            if kind == 'model_reply': reply = e.get('content') or ''
            elif kind == 'tool_call':
                last = e.get('name')
                a = e.get('args') if isinstance(e.get('args'), dict) else {}
                read_path = os.path.normpath(str(a.get('path', ''))) if last == 'read_file' else None
                if last in TYPED:
                    a = e.get('args')
                    if last == 'finish' and isinstance(a, dict):   # finish's checks quote the file, and mini refuses
                        a = {x: y for x, y in a.items() if x != 'checks'}   # a sample that is not in it
                    items.append((k, 'typed', last, (a if isinstance(a, str) else json.dumps(a)) + '\n' + reply))
            elif kind == 'tool_result' and e.get('name') in PROGRAM:
                items.append((k, 'program', e.get('name'), e.get('text') or ''))
            elif kind == 'tool_result' and e.get('name') == 'read_file' and writer.get(read_path) in PROGRAM:
                items.append((k, 'program', 'read_file', e.get('text') or ''))    # a file a program wrote
            elif kind == 'watchdog' and e.get('name') == 'finish_review':   # mini showing the files the run wrote:
                items.append((k, 'program', 'finish_review', e.get('text') or ''))  # typed ones were caught when typed
            elif kind == 'files_changed':
                for p in e.get('paths') or []: writer[os.path.normpath(p)] = last
    return items, writer

def close(a, b, dec):
    return abs(a - b) <= 0.5 * 10 ** -dec + 1e-9

def from_program(values, what):
    """Each key value the model typed must have been printed by a program before it was typed. values: [(value,
    decimals)]; integers under 50 are skipped (they turn up anywhere)."""
    items, _ = trace()
    for v, dec in values:
        if dec == 0 and abs(v) < 50: continue
        for k, kind, name, text in items:
            if kind != 'typed' or not any(close(x, v, dec) for x in nums(text)): continue
            if not any(k2 < k and kind2 == 'program' and any(close(x, v, dec) for x in nums(t2))
                       for k2, kind2, _, t2 in items):
                fail(f'{what}: {v} was typed in {name} without a program printing it first (computed by the model)')

def written_by(rel):
    return trace()[1].get(os.path.normpath(rel))

def read_csv(rel):
    p = os.path.join(W, rel)
    if not os.path.exists(p): fail(f'no {rel}')
    rows = list(csv.DictReader(io.StringIO(open(p, encoding='utf-8-sig').read())))
    if not rows: fail(f'{rel} has no rows')
    return [{(k or '').strip().lower(): (v or '').strip() for k, v in r.items()} for r in rows]

def num(s, what):
    try: return float(str(s).replace(',', '').replace('$', '').replace('EUR', '').strip())
    except ValueError: fail(f'{what}: {s!r} is not a number')

def load_json(rel):
    p = os.path.join(W, rel)
    if not os.path.exists(p): fail(f'no {rel}')
    try: return json.load(open(p))
    except ValueError as e: fail(f'{rel} is not valid JSON: {e}')

def answer(run_hint=''):
    """The final answer of the run whose task contains run_hint."""
    got = ''
    for task, ev in logs():
        if run_hint in task:
            for e in ev:
                if e.get('kind') == 'run_end': got = e.get('answer') or ''
    return got
'''


def task(name, prompt, files, check, prompt2=None, timeout=TIMEOUT):
    SUITE.add(name, " ".join(prompt.split()), files, HEAD + textwrap.dedent(check) + "\nprint('PASS')\n",
              " ".join(prompt2.split()) if prompt2 else None, timeout=timeout)



def cents(x):
    return Decimal(x).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


rnd = random.Random(1108)
NAMES = ["ada", "bruno", "celia", "dmitri", "elena", "farid", "greta", "hugo"]

# ---------------------------------------------------------------- 1. calc_loan: amortization schedule
P, RATE, N = Decimal("18750"), Decimal("6.35"), 36
r = RATE / 100 / 12
pay = cents(P * r / (1 - (1 + r) ** -N))
bal, sched = P, []
for m in range(1, N + 1):
    i = cents(bal * r)
    pr = bal if m == N else pay - i
    bal -= pr
    sched.append((m, float(pr + i), float(i), float(pr), float(bal)))
tot_int = float(sum(Decimal(str(s[2])) for s in sched))
task("calc_loan", """
loan.json describes a loan repaid in equal monthly payments. Write schedule.csv with the columns month, payment,
interest, principal and balance, one row per month. The monthly rate is the annual rate / 12. The payment is the
standard annuity payment, rounded to cents. Each month the interest is the balance times the monthly rate, rounded
half up to cents, the principal is the payment minus the interest, and the balance is what is left. The last
payment is whatever clears the balance to exactly 0. Then tell me the total interest paid over the loan.""",
     {"loan.json": json.dumps({"principal": 18750, "annual_rate_percent": 6.35, "months": 36}, indent=2) + "\n"},
     f"""
exp, tot = {sched!r}, {tot_int!r}
rows = read_csv('schedule.csv')
if len(rows) != len(exp): fail(f'schedule.csv has {{len(rows)}} rows, expected {{len(exp)}}')
for r, (m, pay, i, pr, bal) in zip(rows, exp):
    for col, v in (('payment', pay), ('interest', i), ('principal', pr), ('balance', bal)):
        if col not in r: fail(f'schedule.csv has no {{col}} column ({{list(r)}})')
        if abs(num(r[col], col) - v) > 0.021: fail(f'month {{m}} {{col}}: {{r[col]}} != {{v:.2f}}')
a = answer('loan.json')
if not any(abs(x - tot) <= 0.05 for x in nums(a)): fail(f'answer does not give the total interest {{tot:.2f}}: {{a[:200]!r}}')
from_program([(tot, 2), (exp[0][1], 2), (exp[-1][1], 2)], 'loan')
""")

# ---------------------------------------------------------------- 2. calc_stats: per-sensor statistics
sensors = ["s1", "s2", "s3", "s4", "s5"]
base = {"s1": 21.4, "s2": 19.8, "s3": 23.1, "s4": 18.2, "s5": 25.6}
rows, vals = ["sensor,time,value"], {s: [] for s in sensors}
t = dt.datetime(2026, 10, 1)
for k in range(640):
    s = sensors[rnd.randrange(5)]
    t += dt.timedelta(minutes=rnd.randint(1, 9))
    if rnd.random() < 0.04:
        rows.append(f"{s},{t:%Y-%m-%dT%H:%M},{rnd.choice(['', 'NA', 'err'])}")
        continue
    v = round(base[s] + rnd.gauss(0, 1.7), 2)
    vals[s].append(v)
    rows.append(f"{s},{t:%Y-%m-%dT%H:%M},{v}")
stats = {s: {"count": len(v), "mean": round(statistics.mean(v), 3), "median": round(statistics.median(v), 3),
             "stdev": round(statistics.stdev(v), 3), "min": min(v), "max": max(v)} for s, v in vals.items()}
task("calc_stats", """
readings.csv holds temperature readings from several sensors. Some values are missing or not numbers; leave those
rows out. Write stats.json: an object with one entry per sensor, each holding count, mean, median, stdev (the sample
standard deviation), min and max of its readings, rounded to 3 decimals. Then tell me which sensor has the highest
mean and what that mean is.""",
     {"readings.csv": "\n".join(rows) + "\n"},
     f"""
exp = {stats!r}
d = load_json('stats.json')
if not isinstance(d, dict) or sorted(map(str.lower, d)) != sorted(exp): fail(f'stats.json sensors {{list(d) if isinstance(d, dict) else d}}')
d = {{k.lower(): v for k, v in d.items()}}
for s, e in exp.items():
    for f, v in e.items():
        if f not in d[s]: fail(f'{{s}} has no {{f}}')
        if abs(num(d[s][f], f) - v) > 0.0015: fail(f'{{s}} {{f}}: {{d[s][f]}} != {{v}}')
best = max(exp, key=lambda s: exp[s]['mean'])
a = answer('readings.csv')
if best not in a.lower() or not any(abs(x - exp[best]['mean']) <= 0.0015 for x in nums(a)):
    fail(f'answer should name {{best}} with mean {{exp[best]["mean"]}}: {{a[:200]!r}}')
from_program([(v[f], 3) for v in exp.values() for f in ('mean', 'median', 'stdev')], 'stats')
""")

# ---------------------------------------------------------------- 3. calc_budget: currencies, totals, shares
rates = {"EUR": 1, "USD": 0.9183, "GBP": 1.1572, "PLN": 0.2311, "CHF": 1.0468}
cats = ["rent", "travel", "food", "software", "hardware", "training"]
rows, tot = ["date,category,amount,currency"], {}
for k in range(95):
    c, cur = rnd.choice(cats), rnd.choice(list(rates))
    amt = round(rnd.uniform(8, 900), 2)
    rows.append(f"2026-{rnd.randint(1, 10):02d}-{rnd.randint(1, 28):02d},{c},{amt},{cur}")
    tot[c] = tot.get(c, 0) + amt * rates[cur]
tot = {c: round(v, 2) for c, v in tot.items()}
grand = round(sum(tot.values()), 2)
share = {c: round(v / grand * 100, 1) for c, v in tot.items()}
task("calc_budget", """
expenses.csv lists expenses in several currencies; rates.json gives the value of one unit of each currency in EUR.
Write summary.md: a markdown table with the columns category, total_eur and share, one row per category, sorted by
total_eur from highest to lowest. total_eur is the category's expenses converted to EUR and summed, rounded to
cents; share is that total as a percentage of all expenses, one decimal. End with a line `Total: X EUR` with the
grand total in EUR.""",
     {"expenses.csv": "\n".join(rows) + "\n", "rates.json": json.dumps(rates, indent=2) + "\n"},
     f"""
exp, grand, share = {tot!r}, {grand!r}, {share!r}
p = os.path.join(W, 'summary.md')
if not os.path.exists(p): fail('no summary.md')
t = open(p).read()
got = []
for line in t.splitlines():
    cells = [c.strip(' *') for c in line.strip().strip('|').split('|')]
    if len(cells) >= 3 and cells[0].lower() in exp:
        got.append((cells[0].lower(), num(cells[1], 'total_eur'), num(cells[2].rstrip('%'), 'share')))
if sorted(c for c, _, _ in got) != sorted(exp): fail(f'categories {{[c for c, _, _ in got]}}')
for c, v, s in got:
    if abs(v - exp[c]) > 0.011: fail(f'{{c}} total {{v}} != {{exp[c]}}')
    if abs(s - share[c]) > 0.051: fail(f'{{c}} share {{s}} != {{share[c]}}')
if [c for c, _, _ in got] != sorted(exp, key=exp.get, reverse=True): fail('rows not sorted by total')
m = re.search(r'Total:\\s*\\**\\s*([\\d,]+\\.?\\d*)', t)
if not m or abs(num(m.group(1), 'Total') - grand) > 0.021: fail(f'Total line should be {{grand}}')
from_program([(v, 2) for v in exp.values()] + [(grand, 2)] + [(v, 1) for v in share.values() if v >= 5], 'budget')
""")

# ---------------------------------------------------------------- 4. calc_payroll: time arithmetic, overtime
hourly = {n: round(rnd.uniform(18, 42), 2) for n in NAMES[:6]}
rows, pay = ["employee,start,end"], {n: [0, 0] for n in hourly}      # minutes regular, overtime
for k in range(70):
    n = rnd.choice(list(hourly))
    st = dt.datetime(2026, 10, 1) + dt.timedelta(days=rnd.randint(0, 29), hours=rnd.choice([6, 7, 8, 14, 18, 21]),
                                                minutes=rnd.randint(0, 59))
    en = st + dt.timedelta(minutes=rnd.randint(4 * 60, 11 * 60 + 30))
    rows.append(f"{n},{st:%Y-%m-%d %H:%M},{en:%Y-%m-%d %H:%M}")
    mins = int((en - st).total_seconds() // 60)
    pay[n][0] += min(mins, 480)
    pay[n][1] += max(mins - 480, 0)
payroll = {n: (round(r_ / 60, 2), round(o / 60, 2), float(cents(Decimal(r_) / 60 * Decimal(str(hourly[n]))
                                                               + Decimal(o) / 60 * Decimal(str(hourly[n])) * Decimal("1.5"))))
           for n, (r_, o) in pay.items()}
task("calc_payroll", """
shifts.csv lists the shifts each employee worked in October (some run past midnight) and rates.csv their hourly
rate. In each shift the first 8 hours are regular time and anything beyond 8 hours is overtime, paid at 1.5 times the
hourly rate. Write payroll.csv with the columns employee, regular_hours, overtime_hours and pay, one row per employee:
the hours summed over their shifts (two decimals) and the pay for the month rounded to cents.""",
     {"shifts.csv": "\n".join(rows) + "\n",
      "rates.csv": "employee,hourly_rate\n" + "".join(f"{n},{v}\n" for n, v in hourly.items())},
     f"""
exp = {payroll!r}
rows = read_csv('payroll.csv')
got = {{r.get('employee', '').lower(): r for r in rows}}
if sorted(got) != sorted(exp): fail(f'employees {{sorted(got)}}')
for n, (reg, ot, pay) in exp.items():
    r = got[n]
    for col, v, tol in (('regular_hours', reg, 0.011), ('overtime_hours', ot, 0.011), ('pay', pay, 0.02)):
        if col not in r: fail(f'payroll.csv has no {{col}} column ({{list(r)}})')
        if abs(num(r[col], col) - v) > tol: fail(f'{{n}} {{col}}: {{r[col]}} != {{v}}')
from_program([(p, 2) for _, _, p in exp.values()] + [(o, 2) for _, o, _ in exp.values() if o >= 1], 'payroll')
""")

# ---------------------------------------------------------------- 5. calc_question: an answer, no file
rows, hits = ["order_id,customer,qty,unit_price,discount_pct"], []
for k in range(1, 301):
    q, up, dsc = rnd.randint(1, 40), round(rnd.uniform(2, 60), 2), rnd.choice([0, 0, 5, 7.5, 10, 12, 15, 20])
    rows.append(f"{1000 + k},{rnd.choice(NAMES)},{q},{up},{dsc}")
    net = q * up * (1 - dsc / 100)
    if net > 250:
        hits.append(net)
n_hits, sum_hits = len(hits), round(sum(hits), 2)
task("calc_question", """
In orders.csv, the net value of an order is qty * unit_price * (1 - discount_pct / 100). How many orders have a net
value above 250, and what is their combined net value, rounded to cents? Just tell me; do not write any files.""",
     {"orders.csv": "\n".join(rows) + "\n"},
     f"""
n, s = {n_hits!r}, {sum_hits!r}
a = answer('orders.csv')
xs = nums(a)
if n not in [round(x) for x in xs if x == int(x)]: fail(f'answer does not give the count {{n}}: {{a[:200]!r}}')
if not any(abs(x - s) <= 0.011 for x in xs): fail(f'answer does not give the sum {{s}}: {{a[:200]!r}}')
from_program([(s, 2), (n, 0)], 'question')
""")


# ---------------------------------------------------------------- 6. calc_vat_tool: a tool, exact rounding, reused
def invoice_batch(n_inv, prefix):
    files, totals, trap = {}, {}, 0
    for k in range(1, n_inv + 1):
        lines, tn = [], [Decimal(0), Decimal(0)]
        for _ in range(rnd.randint(3, 7)):
            qty = rnd.randint(1, 12)
            up = Decimal(rnd.randint(5, 9000)) / 1000 if rnd.random() < 0.5 else Decimal(rnd.randint(50, 20000)) / 100
            if rnd.random() < 0.3:                  # a half cent: odd qty times a price ending in 5 at the 3rd decimal
                qty, up = rnd.choice([1, 3, 5, 7]), Decimal(rnd.randint(1, 4000) * 10 + 5) / 1000
            rate = rnd.choice([Decimal("0.23"), Decimal("0.08"), Decimal("0.05")])
            net = cents(qty * up)
            vat = cents(net * rate)
            if round(float(qty * up), 2) != float(net) or round(float(net * rate), 2) != float(vat):
                trap += 1
            tn[0] += net; tn[1] += vat
            lines.append({"item": rnd.choice(["bolt", "cable", "drill", "glue", "hinge", "lamp", "nut", "saw"]),
                          "qty": qty, "unit_price": str(up), "vat_rate": str(rate)})
        inv = f"{prefix}-{k:03d}"
        files[f"invoices/{prefix}/{inv}.json"] = json.dumps({"invoice": inv, "lines": lines}, indent=2) + "\n"
        totals[inv] = (float(tn[0]), float(tn[1]), float(tn[0] + tn[1]))
    return files, totals, trap


b1, t1, trap1 = invoice_batch(9, "A")
b2, t2, trap2 = invoice_batch(7, "B")
assert trap1 >= 3 and trap2 >= 2, (trap1, trap2)           # lines where float round() and half-up disagree
CHECK_TOTALS = """
def totals(rel, exp):
    rows = read_csv(rel)
    got = {r.get('invoice', ''): r for r in rows}
    if sorted(got) != sorted(exp): fail(f'{rel} invoices {sorted(got)}')
    for inv, vals in exp.items():
        for col, v in zip(('net', 'vat', 'gross'), vals):
            if col not in got[inv]: fail(f'{rel} has no {col} column ({list(got[inv])})')
            if abs(num(got[inv][col], col) - v) > 0.001: fail(f'{rel} {inv} {col}: {got[inv][col]} != {v:.2f} (round half up per line)')
"""
task("calc_vat_tool", """
Build a tool that totals invoice files like the ones in invoices/A/. For each line, net = qty * unit_price rounded
half up to cents, and VAT = net * vat_rate rounded half up to cents; an invoice's net, vat and gross (net + vat) are
the sums over its lines. Use it on the invoices in invoices/A/ and write totals_A.csv with the columns invoice, net,
vat and gross.""",
     {**b1, **b2},
     CHECK_TOTALS + f"""
totals('totals_A.csv', {t1!r})
from_program([(v, 2) for vals in {t1!r}.values() for v in vals], 'batch A')
installed, ran = set(), set()
for task_, ev in logs():
    for e in ev:
        if e.get('kind') == 'invoke' and e.get('ok') is True: ran.add(e.get('tool'))
if not ran: fail('no tool ran')
b = [ev for task_, ev in logs() if 'invoices/B' in task_]
if not b: fail('the second task did not run')
ran_b = {{e.get('tool') for e in b[0] if e.get('kind') == 'invoke' and e.get('ok') is True}}
if not ran_b: fail('the second task did not use a tool')
totals('totals_B.csv', {t2!r})
from_program([(v, 2) for vals in {t2!r}.values() for v in vals], 'batch B')
""", prompt2="""Total the invoices in invoices/B/ the same way and write totals_B.csv with the columns invoice, net,
vat and gross.""")
