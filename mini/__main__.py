"""mini command line.

  python -m mini ui                                   Textual UI (default)
  python -m mini run "task" [--attach FILE] [--auto] [--no-confirm] [--model local|free|paid] [--label L]
  python -m mini tools list|staged|show NAME [VERSION]|review NAME|accept NAME|discard NAME|rollback NAME VERSION|wipe
                     review/accept/discard take --run RUN when more than one task staged the tool
  python -m mini setup                                build the sandbox base image if missing

--auto is for unattended test runs: every approval request is decided by the auto policy from config
([approvals] auto_approve / auto_choice) and logged as decided_by=auto-policy.
"""
import argparse
import json
import sys

from .harness import config as config_mod
from .harness.registry import Registry
from .harness.sandbox import Sandbox


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mini")
    ap.add_argument("--config")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run")
    r.add_argument("task")
    r.add_argument("--attach", action="append", default=[])
    r.add_argument("--auto", action="store_true")
    r.add_argument("--auto-approve", default="confirm,permissions,install,cap")
    r.add_argument("--no-confirm", action="store_true")
    r.add_argument("--model", choices=["local", "free", "paid"], help="tier to start on (default: routing.start)")
    r.add_argument("--no-escalate", action="store_true", help="stay on the start tier")
    r.add_argument("--label", default="")
    r.add_argument("--quiet", action="store_true")
    g = sub.add_parser("tools", aliases=["registry"])
    g.add_argument("action", choices=["list", "staged", "show", "review", "accept", "discard", "rollback", "wipe"])
    g.add_argument("name", nargs="?")
    g.add_argument("version", nargs="?", type=int)
    g.add_argument("--run", help="the run that staged the tool (when there is more than one)")
    g.add_argument("--force", action="store_true", help="accept even if a newer version was accepted meanwhile")
    sub.add_parser("setup")
    le = sub.add_parser("lessons")
    le.add_argument("action", choices=["list", "wipe"])
    sub.add_parser("ui")
    a = ap.parse_args(argv)
    cfg = config_mod.load(a.config)

    if a.cmd == "run":
        from .agent.core import Agent
        if a.auto:
            cfg["approvals"]["mode"] = "auto"
            cfg["approvals"]["auto_approve"] = [k for k in a.auto_approve.split(",") if k]
        if a.no_confirm:
            cfg["approvals"]["confirm_first"] = False
        if a.no_escalate:
            cfg["routing"]["auto_escalate"] = False
        agent = Agent(cfg, label=a.label, start_tier=a.model)
        if not a.quiet:
            from .harness.eventlog import RunLog
            agent.log.subscribe(lambda ev: ev["kind"] != "delta" and print(RunLog.format(ev)[:1500], flush=True))
        s = agent.run_task(a.task, a.attach)
        print(json.dumps({k: s[k] for k in ("run_id", "status", "answer", "error", "secs", "created", "escalations",
                                            "tiers", "watchdogs", "outputs")}, indent=1))
        return 0 if s["status"] == "success" else 1
    if a.cmd in ("tools", "registry"):
        return tools_cmd(cfg, a)
    if a.cmd == "lessons":
        from .harness.lessons import LessonStore, format_lessons
        store = LessonStore(cfg.abspath("state"))
        if a.action == "wipe":
            store.wipe()
            print("lessons wiped")
        else:
            print(format_lessons(store.all()) or "(no lessons yet)")
        return 0
    if a.cmd == "setup":
        sb = Sandbox(cfg)
        if not sb.image_exists(cfg["sandbox"]["image"]):
            sb.build_base()
        print("sandbox image ready:", cfg["sandbox"]["image"])
        return 0
    from .ui.app import run_ui
    return run_ui(cfg)


def tools_cmd(cfg, a):
    from .harness.registry import RegistryError
    reg = Registry(cfg.abspath("state"))
    need = {"show", "review", "accept", "discard", "rollback"}
    if a.action in need and not a.name:
        print(f"usage: mini tools {a.action} NAME", file=sys.stderr)
        return 2
    staged = reg.staged()

    def pick_run():
        runs = [s["run_id"] for s in staged if s["name"] == a.name]
        if a.run:
            return a.run
        if len(runs) == 1:
            return runs[0]
        raise RegistryError(f"{a.name}: nothing staged" if not runs else
                            f"{a.name} is staged by several runs, pick one with --run: {', '.join(runs)}")
    try:
        if a.action == "list":
            for m in reg.list_tools():
                print(f"{m['name']:<28} v{m['version']} of {reg.versions(m['name'])}  "
                      f"{m['created_by']['provider']}/{m['created_by']['model']}  {m['description'][:70]}")
            if not reg.names():
                print("(no accepted tools)")
            if staged:
                print(f"\n{len(staged)} staged, waiting for review: mini tools staged")
        elif a.action == "staged":
            for s in staged:
                cb, tr = s["created_by"] or {}, s["test_results"] or {}
                print(f"{s['name']:<28} run {s['run_id']}  {cb.get('provider')}/{cb.get('model')}  "
                      f"tests: {tr.get('summary', '?')}" + ("  [a newer version was accepted since]" if s["outdated"] else ""))
            if not staged:
                print("(nothing staged)")
        elif a.action == "show":
            print(json.dumps({k: v for k, v in (reg.get(a.name, a.version) or {}).items() if not k.startswith("_")}, indent=1))
        elif a.action == "review":
            run = pick_run()
            s = next(x for x in staged if x["name"] == a.name and x["run_id"] == run)
            print(f"{a.name} staged by run {run}: {s['description']}\nbuilt by: {json.dumps(s['created_by'])}\n"
                  f"tests: {(s['test_results'] or {}).get('summary')}\n")
            print(reg.diff(a.name, run))
        elif a.action == "accept":
            v = reg.accept(a.name, pick_run(), force=a.force)
            print(f"{a.name}: accepted as v{v}")
        elif a.action == "discard":
            run = pick_run()
            reg.discard(a.name, run)
            print(f"{a.name}: staged version from run {run} discarded")
        elif a.action == "rollback":
            reg.set_active(a.name, a.version)
            print(f"{a.name}: active version is now {a.version}")
        elif a.action == "wipe":
            reg.wipe()
            print("registry wiped")
    except (RegistryError, ValueError) as e:
        print(e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
