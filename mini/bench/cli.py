"""mini bench list|run|results|status|stop (see mini/__main__.py)."""
import os
import sys

from .. import __version__, bench
from . import results, run


def bench_cmd(cfg, a):
    try:
        if a.action == "list":
            return list_cmd(a.names)
        if a.action == "run":
            return run_cmd(cfg, a)
        if a.action == "results":
            print(results_text(cfg, a.preset, a.runs, a.all_versions))
            return 0
        if a.action == "status":
            print(status_text(cfg))
            return 0
        if a.action == "stop":
            print("bench stopped" if run.stop(cfg) else "no bench is running")
            return 0
    except (KeyError, run.BenchError) as e:
        print(f"bench: {e.args[0] if e.args else e}", file=sys.stderr)
        return 1
    return 2


def list_cmd(names):
    ts = bench.tasks(names)
    for s in bench.SUITES:
        mine = [t for t in ts if t.suite == s]
        if mine:
            print(f"{s}:")
            for t in mine:
                extra = " (+ follow-up)" if t.prompt2 else ""
                extra += f" (needs {', '.join(t.needs)})" if t.needs else ""
                print(f"  {t.name:<18} {t.summary()}{extra}")
    print(f"{len(ts)} tasks")
    return 0


def run_cmd(cfg, a):
    """--preset A,B runs the tasks on A, then on B (default: the active preset)."""
    presets = [p for p in (a.preset or cfg["llamacpp"].get("preset") or "").split(",") if p]
    known = cfg.get("presets") or {}
    for p in presets or [""]:
        if p not in known:
            raise run.BenchError(f"no preset {p!r}: give --preset (mini preset list)")
    ts = bench.tasks(a.names)
    if a.background:
        job = run.start(cfg, presets, a.names, a.reps, a.seed)
        print(f"bench started in the background: {len(ts)} tasks x {a.reps} on {', then '.join(presets)}; "
              f"log {job['log']}\nmini bench status follows it, mini bench results shows what is done")
        return 0
    if run.running(cfg):
        raise run.BenchError("a bench is already running in the background (mini bench stop ends it)")
    rows = []
    for p in presets:
        rows += run.run_bench(cfg, p, ts, a.reps, a.seed, say=lambda m: print(m, flush=True))
    print(results.fmt_avg(results.average(rows)))
    return 0 if rows and all(r["passed"] for r in rows) else 1


def results_text(cfg, preset=None, every=False, all_versions=False):
    """The statistics of this mini version (all_versions: of each version, newest first)."""
    everything = results.rows(cfg, preset, results.ALL)
    if all_versions:
        vs = list(reversed(results.versions(everything))) or [__version__]
    else:
        vs = [__version__]
    out = []
    for v in vs:
        rs = [r for r in everything if results.version_of(r) == v]
        out.append(f"mini {v}" + (" (this version)" if v == __version__ else ""))
        if not rs:
            older = len(everything) - len(rs)
            out.append("  no bench results for this version yet (mini bench run)"
                       + (f"; {older} from older versions: --all-versions" if older else ""))
        for p in results.presets(rs):
            mine = [r for r in rs if r.get("preset") == p]
            models = ", ".join(dict.fromkeys(r.get("model", "?") for r in mine))
            out.append(f"  {p}  ({models})  {results.fmt_avg(results.average(mine))}")
            for t, s, avg in results.per_task(mine):
                out.append(f"    {t:<18} {s:<8} {results.fmt_avg(avg)}")
                if every:
                    for r in [r for r in mine if r.get("task") == t]:
                        out.append(f"        {r.get('at', '')}  rep {r.get('rep')}  "
                                   f"{'passed' if r.get('passed') else 'FAILED'}  {results.fmt_secs(r.get('secs'))}")
        out.append("")
    return "\n".join(out).rstrip()


def status_text(cfg):
    job = run.load_job(cfg)
    if not job:
        return "no bench has run in the background"
    live = run.running(cfg)
    head = (f"bench on {job.get('preset')}: {job.get('state')}" + ("" if live or job.get("state") != "running"
                                                                  else " (its process is gone)"))
    if job.get("task"):
        head += f"; run {job.get('n')}/{job.get('total')}: {job.get('task')} (rep {job.get('rep')})"
    if job.get("error"):
        head += f"\n{job['error']}"
    log = job.get("log")
    if log and os.path.exists(log):
        with open(log) as f:
            tail = f.readlines()[-5:]
        head += "\n" + "".join(tail).rstrip()
    return head
