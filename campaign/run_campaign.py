"""Test campaign: every demo task N times from a clean registry, with the fallback off and on.

Each run is a fresh `python -m mini run` process (a new session). The composition task restarts mini with the
registry that this campaign's own runs built live (never a seed): after an email run and a pdf run pass,
their registries are merged into a snapshot that each compose run starts from.

usage: python campaign/run_campaign.py --tasks email,weather,pdf,compose --runs 5 --fallback off --name A
Results: campaign/out/<name>/results.jsonl (one line per run) and the full run logs in runs/.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from campaign import grade  # noqa: E402

ROOT = grade.ROOT
PY = os.path.join(ROOT, ".venv", "bin", "python")
STATE = os.path.join(ROOT, "state")
TASKS = {
    "email": ("Send an email to anna@example.local saying the meeting moved to Friday.", []),
    "weather": ("What will the weather be in Prague tomorrow?", []),
    "pdf": ("Convert this PDF to Markdown and add a short summary at the top.", ["demo/report.pdf"]),
    "compose": ("Summarize this PDF and email the summary to anna@example.local.", ["demo/report.pdf"]),
}


def services_up():
    subprocess.run([os.path.join(ROOT, "demo", "services.sh"), "up"], check=True, capture_output=True)
    for _ in range(30):
        try:
            grade.mailpit_messages()
            return
        except OSError:
            time.sleep(1)


def run_one(task, i, fallback, name, registry_from=None):
    text, attach = TASKS[task]
    shutil.rmtree(os.path.join(STATE, "registry"), ignore_errors=True)
    if registry_from:
        shutil.copytree(registry_from, os.path.join(STATE, "registry"))
    else:
        os.makedirs(os.path.join(STATE, "registry"))
    services_up()
    grade.mailpit_clear()
    label = f"{name}-{task}-{i}"
    argv = [PY, "-m", "mini", "run", text, "--auto", "--no-confirm", "--fallback", fallback, "--label", label, "--quiet"]
    for a in attach:
        argv += ["--attach", os.path.join(ROOT, a)]
    t0 = time.time()
    p = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=4200)
    run_dirs = sorted(d for d in os.listdir(os.path.join(ROOT, "runs")) if d.endswith(label))
    run_dir = os.path.join(ROOT, "runs", run_dirs[-1])
    summary = json.load(open(os.path.join(run_dir, "summary.json")))
    try:
        passed, details = grade.GRADERS[task](summary, run_dir)
    except Exception as e:   # noqa: BLE001 - a grader failure is recorded, not hidden
        passed, details = False, {"grader_error": f"{type(e).__name__}: {e}"}
    attempts = [c["attempts"] for c in summary["created"]]
    rec = {"campaign": name, "task": task, "i": i, "fallback": fallback, "run_id": summary["run_id"],
           "agent_status": summary["status"], "graded_pass": passed, "grade": details, "secs": round(time.time() - t0, 1),
           "tools": [{"name": c["name"], "version": c["version"], "by": c["created_by"]["provider"],
                      "attempts": c["attempts"], "restarts": c["restarts"]} for c in summary["created"]],
           "attempts_local": sum(a["local"] for a in attempts), "attempts_fallback": sum(a["fallback"] for a in attempts),
           "restarts": sum(c["restarts"] for c in summary["created"]), "repairs": summary["repairs"],
           "escalations": summary["escalations"], "fallback_usage": summary["fallback"],
           "watchdogs": summary["watchdogs"], "local_tokens": summary["local_tokens"], "error": summary["error"],
           "answer": (summary.get("answer") or "")[:500], "exit": p.returncode}
    return rec, run_dir


def snapshot(run_dir, dest):
    """Copy the registry as it is now (right after a passing run) into dest, merging tool directories."""
    src = os.path.join(STATE, "registry")
    os.makedirs(dest, exist_ok=True)
    for name in os.listdir(src):
        if not os.path.exists(os.path.join(dest, name)):
            shutil.copytree(os.path.join(src, name), os.path.join(dest, name))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", default="email,weather,pdf,compose")
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--fallback", choices=["on", "off"], default="off")
    ap.add_argument("--name", required=True)
    ap.add_argument("--compose-registry", help="registry snapshot to start compose runs from")
    a = ap.parse_args()
    out = os.path.join(ROOT, "campaign", "out", a.name)
    os.makedirs(out, exist_ok=True)
    res_path = os.path.join(out, "results.jsonl")
    snap = a.compose_registry or os.path.join(out, "compose-registry")
    tasks = a.tasks.split(",")
    for i in range(1, a.runs + 1):
        for task in tasks:
            if task == "compose":
                if not os.path.isdir(snap) or len(os.listdir(snap)) < 2:
                    rec = {"campaign": a.name, "task": task, "i": i, "fallback": a.fallback, "skipped":
                           "no registry snapshot with an email tool and a pdf tool from passing runs yet"}
                    print(json.dumps(rec), flush=True)
                    open(res_path, "a").write(json.dumps(rec) + "\n")
                    continue
                rec, run_dir = run_one(task, i, a.fallback, a.name, registry_from=snap)
            else:
                rec, run_dir = run_one(task, i, a.fallback, a.name)
                if rec["graded_pass"] and task in ("email", "pdf") and not a.compose_registry:
                    tag = os.path.join(out, f"snap-{task}")
                    if not os.path.exists(tag):
                        snapshot(run_dir, snap)
                        open(tag, "w").write(rec["run_id"])
            print(json.dumps({k: rec.get(k) for k in ("task", "i", "agent_status", "graded_pass", "secs",
                                                      "attempts_local", "escalations", "error")}), flush=True)
            open(res_path, "a").write(json.dumps(rec) + "\n")
    shutil.rmtree(os.path.join(STATE, "registry"), ignore_errors=True)
    os.makedirs(os.path.join(STATE, "registry"))


if __name__ == "__main__":
    main()
