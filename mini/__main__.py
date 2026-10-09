"""mini command line.

  python -m mini ui                                   Textual UI (default)
  python -m mini run "task" [--attach FILE] [--auto] [--no-confirm] [--fallback on|off] [--label L]
  python -m mini registry list|show NAME [VERSION]|rollback NAME VERSION|wipe
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
    r.add_argument("--auto-approve", default="confirm,permissions,install,escalate,cap")
    r.add_argument("--no-confirm", action="store_true")
    r.add_argument("--fallback", choices=["on", "off"])
    r.add_argument("--label", default="")
    r.add_argument("--quiet", action="store_true")
    g = sub.add_parser("registry")
    g.add_argument("action", choices=["list", "show", "rollback", "wipe"])
    g.add_argument("name", nargs="?")
    g.add_argument("version", nargs="?", type=int)
    sub.add_parser("setup")
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
        if a.fallback:
            cfg["fallback"]["enabled"] = a.fallback == "on"
        agent = Agent(cfg, label=a.label)
        if not a.quiet:
            from .harness.eventlog import RunLog
            agent.log.subscribe(lambda ev: ev["kind"] != "delta" and print(RunLog.format(ev)[:1500], flush=True))
        s = agent.run_task(a.task, a.attach)
        print(json.dumps({k: s[k] for k in ("run_id", "status", "answer", "error", "secs", "created", "escalations",
                                            "fallback", "watchdogs", "outputs")}, indent=1))
        return 0 if s["status"] == "success" else 1
    if a.cmd == "registry":
        reg = Registry(cfg.abspath("state"))
        if a.action == "list":
            for m in reg.list_tools():
                print(f"{m['name']:<28} v{m['version']} of {reg.versions(m['name'])}  "
                      f"{m['created_by']['provider']}/{m['created_by']['model']}  {m['description'][:70]}")
            if not reg.names():
                print("(registry is empty)")
        elif a.action == "show":
            print(json.dumps(reg.get(a.name, a.version), indent=1))
        elif a.action == "rollback":
            reg.set_active(a.name, a.version)
            print(f"{a.name}: active version is now {a.version}")
        elif a.action == "wipe":
            reg.wipe()
            print("registry wiped")
        return 0
    if a.cmd == "setup":
        sb = Sandbox(cfg)
        if not sb.image_exists(cfg["sandbox"]["image"]):
            sb.build_base()
        print("sandbox image ready:", cfg["sandbox"]["image"])
        return 0
    from .ui.app import run_ui
    return run_ui(cfg)


if __name__ == "__main__":
    sys.exit(main())
