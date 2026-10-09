"""Run the demo path once, the way it will be shown: empty registry -> email -> weather -> restart mini ->
composition in a fresh session (reusing the registry the first two runs built). Every run is graded."""
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from campaign import run_campaign as rc  # noqa: E402

name = sys.argv[1] if len(sys.argv) > 1 else "DEMO1"
out = os.path.join(rc.ROOT, "campaign", "out", name)
os.makedirs(out, exist_ok=True)
reg = os.path.join(rc.STATE, "registry")
shutil.rmtree(reg, ignore_errors=True)
os.makedirs(reg)
keep = os.path.join(out, "registry-after-build")
for task in ("email", "weather", "compose"):
    if task == "compose":
        rec, run_dir = rc.run_one(task, 1, "off", name, registry_from=keep)
    else:
        # email starts from the empty registry; weather starts from what email left (as in the live demo)
        rec, run_dir = rc.run_one(task, 1, "off", name, registry_from=keep if task == "weather" else None)
        shutil.rmtree(keep, ignore_errors=True)
        shutil.copytree(reg, keep)
    print(json.dumps({k: rec.get(k) for k in ("task", "agent_status", "graded_pass", "secs", "attempts_local",
                                              "repairs", "error", "grade")}), flush=True)
    open(os.path.join(out, "results.jsonl"), "a").write(json.dumps(rec) + "\n")
shutil.rmtree(reg, ignore_errors=True)
os.makedirs(reg)
