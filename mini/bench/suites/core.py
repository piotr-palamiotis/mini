"""core: everyday work in a folder (from bench2 make_tasks.py)."""
import tempfile
from .. import Suite

SUITE = Suite('core')
import os, random, shutil, textwrap



def task(name, prompt, files, check):
    files = {rel: textwrap.dedent(body).lstrip("\n") if isinstance(body, str) and body.startswith("\n") else body
             for rel, body in files.items()}
    SUITE.add(name, prompt.strip(), files, "import os, sys, re, json, subprocess\nW, A = sys.argv[1], open(sys.argv[2]).read()\n"
              "def fail(m): print('FAIL', m); sys.exit(1)\n" + textwrap.dedent(check) + "\nprint('PASS')\n")



rnd = random.Random(7)

# 1. data report: revenue per region from a CSV
rows, tot = [], {}
for i in range(60):
    r = rnd.choice(["north", "south", "east", "west"]); q = rnd.randint(1, 20); p = rnd.choice([2.5, 4.0, 9.99, 12.0])
    rows.append(f"{i+1},{r},item{rnd.randint(1,9)},{q},{p}"); tot[r] = tot.get(r, 0) + q * p
task("csv_revenue",
     "sales.csv has one row per sale. Write revenue.md: a markdown table with columns region and revenue "
     "(qty * price, two decimals), one row per region, sorted by revenue from highest to lowest, then a final line "
     "`Total: X` with the grand total.",
     {"sales.csv": "id,region,product,qty,price\n" + "\n".join(rows) + "\n"},
     f"""
exp = {{k: round(v, 2) for k, v in {tot!r}.items()}}
p = os.path.join(W, 'revenue.md')
if not os.path.exists(p): fail('no revenue.md')
t = open(p).read()
got = [(m.group(1).lower(), float(m.group(2).replace(',', ''))) for m in re.finditer(r'\\|\\s*\\**(north|south|east|west)\\**\\s*\\|\\s*\\$?([\\d,]+\\.?\\d*)', t, re.I)]
if sorted(r for r, _ in got) != sorted(exp): fail(f'regions {{got}}')
for r, v in got:
    if abs(v - exp[r]) > 0.01: fail(f'{{r}} {{v}} != {{exp[r]}}')
if [r for r, _ in got] != sorted(exp, key=exp.get, reverse=True): fail('order')
m = re.search(r'Total:\\s*\\**\\$?([\\d,]+\\.?\\d*)', t)
if not m or abs(float(m.group(1).replace(',', '')) - round(sum(exp.values()), 2)) > 0.02: fail('total')
""")

# 2. search: list TODO comments
task("todo_list",
     "Find every TODO comment in this project's source files and write TODO.md with one line per TODO in the form "
     "`path:line: text` (path relative to the project, text after `TODO:`), sorted by path then line.",
     {"app/main.py": "\nimport util\n\n# TODO: read the port from the config\nPORT = 8000\n\ndef main():\n    # TODO: handle SIGTERM\n    util.serve(PORT)\n",
      "app/util.py": "\ndef serve(port):\n    print('serving', port)  # TODO: real server\n\n\ndef todos():\n    return 'TODO list feature'  # not a comment marker\n",
      "lib/parse.c": "\n#include <stdio.h>\n/* TODO: support hex literals */\nint parse(const char *s) {\n    return 0; // TODO: error codes\n}\n",
      "docs/notes.txt": "The TODO comments are tracked in TODO.md.\n"},
     """
exp = ['app/main.py:3: read the port from the config', 'app/main.py:7: handle SIGTERM', 'app/util.py:2: real server',
       'lib/parse.c:2: support hex literals', 'lib/parse.c:4: error codes']
p = os.path.join(W, 'TODO.md')
if not os.path.exists(p): fail('no TODO.md')
lines = [re.sub(r'\\s*\\*/\\s*$', '', l.strip().lstrip('-* ').strip('`')) for l in open(p) if re.match(r'\\s*[-*]?\\s*`?\\w+/\\w+\\.\\w+:\\d+', l)]
if lines != exp: fail(lines)
""")

# 3. fix a bug, tests given
task("fix_dates",
     "`python3 -m unittest` fails. Fix the bug in dates.py (do not change the tests).",
     {"dates.py": '''
def is_leap(year):
    """Gregorian leap year."""
    return year % 4 == 0 and year % 100 != 0


def days_in_month(year, month):
    """Days in month 1..12; raises ValueError for other months."""
    if not 1 <= month <= 12:
        raise ValueError(month)
    if month == 2:
        return 29 if is_leap(year) else 28
    return 30 if month in (4, 6, 9, 11) else 31
''',
      "test_dates.py": '''
import unittest
from dates import is_leap, days_in_month


class T(unittest.TestCase):
    def test_leap(self):
        self.assertTrue(is_leap(2024))
        self.assertFalse(is_leap(1900))
        self.assertTrue(is_leap(2000))

    def test_days(self):
        self.assertEqual(days_in_month(2000, 2), 29)
        self.assertEqual(days_in_month(2023, 4), 30)
        with self.assertRaises(ValueError):
            days_in_month(2023, 13)
'''},
     """
r = subprocess.run(['python3', '-c', 'from dates import is_leap as l, days_in_month as d; assert l(2000) and l(1600) and not l(2100) and l(2024) and not l(2023); assert d(1900,2)==28 and d(2000,2)==29'], cwd=W)
if r.returncode: fail('hidden')
if 'assertTrue(is_leap(2000))' not in open(os.path.join(W, 'test_dates.py')).read(): fail('tests changed')
""")

# 4. rename across files
task("rename_fn",
     "Rename the function get_usr to get_user everywhere in this project (definition and all calls). "
     "Afterwards `python3 cli.py 2` must still print `bob`.",
     {"users.py": "\nUSERS = {1: 'ann', 2: 'bob'}\n\n\ndef get_usr(uid):\n    return USERS[uid]\n\n\ndef get_usrs():\n    return list(USERS.values())\n",
      "cli.py": "\nimport sys\nfrom users import get_usr\n\nprint(get_usr(int(sys.argv[1])))\n",
      "report.py": "\nfrom users import get_usr, get_usrs\n\n\ndef report():\n    return [get_usr(1)] + get_usrs()\n"},
     """
src = ''.join(open(os.path.join(W, f)).read() for f in ('users.py', 'cli.py', 'report.py'))
if re.search(r'get_usr\\b', src): fail('get_usr left')
if 'def get_usrs' not in src: fail('get_usrs was renamed too')
r = subprocess.run(['python3', 'cli.py', '2'], cwd=W, capture_output=True, text=True)
if r.stdout.strip() != 'bob': fail('cli ' + r.stdout + r.stderr)
r = subprocess.run(['python3', '-c', 'import report; assert report.report()==["ann","ann","bob"]'], cwd=W)
if r.returncode: fail('report')
""")

# 5. question about a log: answer in the reply only
mods, errs = ["auth", "db", "cache", "api"], {}
log = []
for i in range(200):
    m = rnd.choice(mods); lvl = rnd.choices(["INFO", "WARN", "ERROR"], [6, 2, 2])[0]
    if lvl == "ERROR":
        errs[m] = errs.get(m, 0) + 1
    log.append(f"2026-10-01T10:{i // 60:02d}:{i % 60:02d} {lvl} [{m}] event {i}")
top = max(errs, key=errs.get)
task("log_question",
     "How many ERROR lines are in server.log, and which module (the name in brackets) has the most ERROR lines? "
     "Just answer; do not create files.",
     {"server.log": "\n".join(log) + "\n"},
     f"""
if str({sum(errs.values())}) not in A: fail('count {sum(errs.values())} not in answer')
if '{top}' not in A.lower(): fail('module {top}')
extra = set(os.listdir(W)) - {{'server.log', '.git', '.mini'}}
if extra: fail(f'created {{extra}}')
""")

# 6. build a tool, then use it
task("ini_tool",
     "Make a tool that converts an INI file to JSON (sections become objects, values stay strings), "
     "then use it to convert settings.ini to settings.json.",
     {"settings.ini": "[server]\nhost = 0.0.0.0\nport = 8080\n\n[db]\nurl = postgres://x/y\npool = 5\n"},
     """
p = os.path.join(W, 'settings.json')
if not os.path.exists(p): fail('no settings.json')
d = json.load(open(p))
if d != {'server': {'host': '0.0.0.0', 'port': '8080'}, 'db': {'url': 'postgres://x/y', 'pool': '5'}}: fail(d)
import glob
ok = []
for lg in glob.glob(os.path.join(W, '.mini', 'runs', '*', 'log.jsonl')):
    for line in open(lg):
        e = json.loads(line)
        if e.get('kind') == 'invoke' and e.get('ok') is True: ok.append(e.get('tool'))
if not ok: fail('the built tool was never invoked successfully (settings.json made another way)')
""")

# 7. multi-step edit: add a CLI flag across two files, guided by a spec
task("add_flag",
     "Add a `--upper` flag to greet.py: when given, the greeting is printed in upper case. Keep the default "
     "behaviour. Add a test for it in test_greet.py and make sure `python3 -m unittest` passes.",
     {"greet.py": "\nimport argparse\n\n\ndef greeting(name):\n    return f'Hello, {name}!'\n\n\ndef main(argv=None):\n    p = argparse.ArgumentParser()\n    p.add_argument('name')\n    a = p.parse_args(argv)\n    print(greeting(a.name))\n\n\nif __name__ == '__main__':\n    main()\n",
      "test_greet.py": "\nimport unittest\nfrom greet import greeting\n\n\nclass T(unittest.TestCase):\n    def test_plain(self):\n        self.assertEqual(greeting('Ann'), 'Hello, Ann!')\n"},
     """
def run(*a):
    return subprocess.run(['python3', 'greet.py', *a], cwd=W, capture_output=True, text=True).stdout.strip()
if run('Ann') != 'Hello, Ann!': fail('default ' + run('Ann'))
if run('--upper', 'Ann') != 'HELLO, ANN!' or run('Ann', '--upper') != 'HELLO, ANN!': fail('upper ' + run('--upper', 'Ann'))
t = open(os.path.join(W, 'test_greet.py')).read()
if 'upper' not in t.lower(): fail('no test')
r = subprocess.run(['python3', '-m', 'unittest'], cwd=W, capture_output=True, text=True)
if r.returncode: fail('unittest ' + r.stderr[-300:])
""")
