"""Bench results: one row per run of a task, in <state>/bench/results.jsonl. A row is kept as it was written (a run
done again is a new row). What a result says: passed or failed, and how long the run took (secs); the other fields
say what ran (preset, model, task, rep, seed, mini version and commit) and, for a failed run, the check's last words
(note).

Every row carries the mini version that made it (mini.__version__). A new version (a fix, a change to how mini works)
starts its statistics afresh: rows(cfg) returns only the current version's rows. The older rows stay in the file
(rows(cfg, version=ALL) reads them all), they are only left out. A row from before versions existed counts as
UNVERSIONED (older than any version)."""
import json
import os
import time

from .. import __version__

ALL = "all"
UNVERSIONED = "0.0.0"


def version_of(row):
    return row.get("version") or UNVERSIONED


def path(cfg):
    return os.path.join(cfg.abspath("state"), "bench", "results.jsonl")


def add(cfg, row):
    row = dict(at=time.strftime("%Y-%m-%d %H:%M"), version=__version__, **row)
    p = path(cfg)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a") as f:
        f.write(json.dumps(row) + "\n")
    return row


def rows(cfg, preset=None, version=None):
    """The rows of one preset (default: all presets) of one mini version (default: this one; ALL: every version)."""
    version = version or __version__
    out = []
    try:
        with open(path(cfg)) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if (preset is None or r.get("preset") == preset) and (version == ALL or version_of(r) == version):
                    out.append(r)
    except OSError:
        pass
    return out


def versions(rs):
    """The mini versions in rows, oldest first."""
    return sorted({version_of(r) for r in rs}, key=lambda v: tuple(int(x) if x.isdigit() else 0 for x in v.split(".")))


def presets(rs):
    """Preset names in the order of their first run."""
    return list(dict.fromkeys(r.get("preset", "?") for r in rs))


def average(rs):
    """{runs, passed, rate (0-1), secs (mean)} over rows; rate and secs are None without rows."""
    n = len(rs)
    ok = sum(1 for r in rs if r.get("passed"))
    return {"runs": n, "passed": ok, "rate": ok / n if n else None,
            "secs": round(sum(r.get("secs") or 0 for r in rs) / n) if n else None}


def per_task(rs):
    """[(task, suite, average)] in the order tasks were first run."""
    by = {}
    for r in rs:
        by.setdefault(r.get("task", "?"), []).append(r)
    return [(t, v[0].get("suite", ""), average(v)) for t, v in by.items()]


def fmt_secs(s):
    if s is None:
        return "-"
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


def fmt_avg(a):
    if not a["runs"]:
        return "no runs"
    return f"{a['passed']}/{a['runs']} passed ({round(100 * a['rate'])}%), average {fmt_secs(a['secs'])}"
