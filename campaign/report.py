"""Build the test report tables from campaign/out/*/results.jsonl and the run logs.

usage: python campaign/report.py A B INJ > docs/test-report-tables.md
"""
import json
import os
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name):
    p = os.path.join(ROOT, "campaign", "out", name, "results.jsonl")
    return [json.loads(l) for l in open(p)] if os.path.exists(p) else []


def med(xs):
    return round(statistics.median(xs), 1) if xs else "-"


def log_events(run_id):
    p = os.path.join(ROOT, "runs", run_id, "log.jsonl")
    return [json.loads(l) for l in open(p)] if os.path.exists(p) else []


def natural_failures(recs):
    """Count failure types that happen on their own: missing packages, failing tests, real-run tool failures."""
    c = {"missing_package_seen": 0, "missing_package_resolved": 0, "failing_test_runs": 0,
         "tools_passing_after_failure": 0, "real_run_tool_errors": 0, "repairs": 0}
    for r in recs:
        if not r.get("run_id"):
            continue
        ev = log_events(r["run_id"])
        missing = False
        for i, e in enumerate(ev):
            if e["kind"] == "test_run":
                if not e["passed"]:
                    c["failing_test_runs"] += 1
                if "ModuleNotFoundError" in (e.get("text") or "") or "No module named" in (e.get("text") or ""):
                    if not missing:
                        c["missing_package_seen"] += 1
                    missing = True
                elif missing and e["passed"]:
                    c["missing_package_resolved"] += 1
                    missing = False
            if e["kind"] == "invoke" and e.get("ok") is False:
                c["real_run_tool_errors"] += 1
        for t in r.get("tools", []):
            if t["attempts"]["local"] > 1 or t["attempts"]["fallback"]:
                c["tools_passing_after_failure"] += 1
        c["repairs"] += r.get("repairs", 0)
    return c


def table(recs, title):
    out = [f"### {title}", "",
           "| task | fallback | runs | graded pass | agent said success | median attempts | restarts | repairs | "
           "escalations | escalation fixed | median time (s) | fallback spend ($) |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    keys = sorted({(r["task"], r["fallback"]) for r in recs if "skipped" not in r},
                  key=lambda k: (["email", "weather", "pdf", "compose"].index(k[0]), k[1]))
    for task, fb in keys:
        rs = [r for r in recs if r["task"] == task and r["fallback"] == fb and "skipped" not in r]
        esc = [r for r in rs if r["escalations"]]
        out.append(f"| {task} | {fb} | {len(rs)} | {sum(r['graded_pass'] for r in rs)}/{len(rs)} | "
                   f"{sum(r['agent_status'] == 'success' for r in rs)}/{len(rs)} | "
                   f"{med([r['attempts_local'] for r in rs])} | {sum(r['restarts'] for r in rs)} | "
                   f"{sum(r['repairs'] for r in rs)} | {len(esc)} | {sum(r['graded_pass'] for r in esc)}/{len(esc)} | "
                   f"{med([r['secs'] for r in rs])} | {sum(r['fallback_usage']['spend_usd'] for r in rs):.3f} |")
    skipped = [r for r in recs if "skipped" in r]
    if skipped:
        out.append(f"\nSkipped: {len(skipped)} compose runs ({skipped[0]['skipped']}).")
    wd = {}
    for r in recs:
        for k, v in (r.get("watchdogs") or {}).items():
            wd[k] = wd.get(k, 0) + v
    out += ["", "Watchdog triggers (all runs above): " + (", ".join(f"{k} {v}" for k, v in sorted(wd.items())) or "none")]
    nat = natural_failures(recs)
    out += ["", "Failures that occurred on their own: " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in nat.items())]
    out += ["", "| run | task | fb | graded | agent | attempts (local/fallback) | restarts | repairs | time (s) | error / notes |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    for r in recs:
        if "skipped" in r:
            continue
        g = r["grade"]
        note = (r.get("error") or "")[:90]
        if r["task"] == "pdf":
            note = note or f"facts {g.get('facts_found')}/{g.get('facts_total')}, scan facts {g.get('scan_facts_found')}/{g.get('scan_facts_total')}, summary {g.get('summary_at_top')}"
        if r["task"] == "weather":
            note = note or f"answer temps {g.get('temps_in_answer')} vs ref {g.get('reference')}"
        out.append(f"| {r['run_id']} | {r['task']} | {r['fallback']} | {'PASS' if r['graded_pass'] else 'FAIL'} | "
                   f"{r['agent_status']} | {r['attempts_local']}/{r['attempts_fallback']} | {r['restarts']} | "
                   f"{r['repairs']} | {r['secs']} | {note.replace('|', '/')} |")
    return "\n".join(out)


def inject_table(recs):
    out = ["### Failure injection", "", "| run | scenario | agent status | honest report | false success claim | "
           "tool errors seen | faults hit | repairs | time (s) | answer / error |", "|---|---|---|---|---|---|---|---|---|---|"]
    for r in recs:
        txt = (r.get("answer") or r.get("error") or "")[:110].replace("|", "/").replace("\n", " ")
        out.append(f"| {r['run_id']} | {r['scenario']} | {r['agent_status']} | {r['honest_report']} | "
                   f"{r['false_success_claim']} | {r['tool_errors_seen']} | {r['faults_hit']} | {r['repairs']} | "
                   f"{r['secs']} | {txt} |")
    return "\n".join(out)


if __name__ == "__main__":
    for name in sys.argv[1:]:
        recs = load(name)
        if recs and "scenario" in recs[0]:
            print(inject_table(recs))
        elif recs:
            print(table(recs, f"Campaign {name}"))
        print()
