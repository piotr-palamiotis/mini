"""mini command line.

  python -m mini ui                                   Textual UI (default)
  python -m mini run "task" [--attach FILE] [--auto] [--no-confirm] [--model local|free|paid] [--label L]
  python -m mini tools list|staged|show NAME [VERSION]|review NAME|accept NAME|discard NAME|rollback NAME VERSION|wipe
                     review/accept/discard take --run RUN when more than one task staged the tool
  python -m mini tool NAME [--field VALUE ...]        run an accepted tool; it asks for what is not given
  python -m mini tools shims                          (re)write the PATH commands of accepted tools
  python -m mini settings list|show KEY|set KEY|delete KEY
  python -m mini preset list|add NAME [--gpu vulkan|cuda|cpu --device D --model M --ctx N --args A]|use NAME|show [NAME]
                                                      llama-server presets (GPU, model, context) in [presets.*]
  python -m mini undo [RUN] [FILE ...] [--list]       put back what a task changed (default: the newest task), or
                                                      only the files named, from its checkpoint (refs/mini/<run>)
  python -m mini setup                                guided setup: sandbox, local model, cloud gateway
  python -m mini doctor                               check the setup, change nothing

mini works in the folder it is started in, which must be a git repository (it offers `git init`). Before a task
changes a file, the folder is checkpointed in a hidden ref; the branch history is never touched.

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
    argv = list(sys.argv[1:] if argv is None else argv)
    head = argv[:2] if argv[:1] == ["--config"] else []
    rest = argv[len(head):]
    if rest[:1] == ["tool"]:                         # tool arguments belong to the tool's own parser
        if len(rest) < 2 or rest[1].startswith("-"):
            print("usage: mini tool NAME [--field VALUE ...]", file=sys.stderr)
            return 2
        from .toolcli import run_tool
        return run_tool(config_mod.load(head[1] if head else None), rest[1], rest[2:])
    ap = argparse.ArgumentParser(prog="mini")
    ap.add_argument("--config")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run")
    r.add_argument("task")
    r.add_argument("--attach", action="append", default=[])
    r.add_argument("--auto", action="store_true")
    r.add_argument("--auto-approve", default="confirm,plan,permissions,install,cap")
    r.add_argument("--no-confirm", action="store_true")
    r.add_argument("--model", choices=["local", "free", "paid"], help="tier to start on (default: routing.start)")
    r.add_argument("--no-escalate", action="store_true", help="stay on the start tier")
    r.add_argument("--label", default="")
    r.add_argument("--quiet", action="store_true")
    g = sub.add_parser("tools", aliases=["registry"])
    g.add_argument("action", choices=["list", "staged", "show", "review", "accept", "discard", "rollback", "shims", "wipe"])
    g.add_argument("name", nargs="?")
    g.add_argument("version", nargs="?", type=int)
    g.add_argument("--run", help="the run that staged the tool (when there is more than one)")
    g.add_argument("--force", action="store_true", help="accept even if a newer version was accepted meanwhile")
    st = sub.add_parser("settings")
    st.add_argument("action", choices=["list", "show", "set", "delete"])
    st.add_argument("key", nargs="?")
    pr = sub.add_parser("preset")
    pr.add_argument("action", choices=["list", "add", "use", "show"])
    pr.add_argument("name", nargs="?")
    pr.add_argument("--gpu", choices=["vulkan", "cuda", "cpu"])
    pr.add_argument("--device")
    pr.add_argument("--model")
    pr.add_argument("--ctx", type=int)
    pr.add_argument("--args", help="llama-server arguments (default: [llamacpp] extra_args)")
    un = sub.add_parser("undo")
    un.add_argument("items", nargs="*", metavar="RUN|FILE", help="a run id (default: the newest task), then files")
    un.add_argument("--list", action="store_true", help="list the checkpoints in this folder")
    sub.add_parser("setup")
    sub.add_parser("doctor")
    le = sub.add_parser("lessons")
    le.add_argument("action", choices=["list", "wipe"])
    sub.add_parser("ui")
    a = ap.parse_args(argv)
    cfg = config_mod.load(a.config)

    if a.cmd == "run":
        from .agent.core import Agent
        from .setup import needs_setup
        if needs_setup(cfg):
            print("note: mini is not set up on this machine yet; `mini setup` helps, `mini doctor` checks", file=sys.stderr)
        if a.auto:
            cfg["approvals"]["mode"] = "auto"
            cfg["approvals"]["auto_approve"] = [k for k in a.auto_approve.split(",") if k]
        if a.no_confirm:
            cfg["approvals"]["confirm_first"] = False
        if a.no_escalate:
            cfg["routing"]["auto_escalate"] = False
        from .harness import checkpoint, workspace as ws
        folder, why = ws.resolve(cfg)
        if why:
            print(why, file=sys.stderr)
            return 2
        if not checkpoint.offer_init(folder, checkpoint.terminal_ask):
            return 2
        agent = Agent(cfg, label=a.label, start_tier=a.model, workspace=folder)
        if not a.quiet:
            from .harness.eventlog import RunLog
            agent.log.subscribe(lambda ev: ev["kind"] != "delta" and print(RunLog.format(ev)[:1500], flush=True))
        s = agent.run_task(a.task, a.attach)
        print(json.dumps({k: s[k] for k in ("run_id", "status", "answer", "error", "secs", "created", "escalations",
                                            "tiers", "watchdogs", "outputs", "checkpoint")}, indent=1))
        return 0 if s["status"] == "success" else 1
    if a.cmd in ("tools", "registry"):
        return tools_cmd(cfg, a)
    if a.cmd == "settings":
        from .toolcli import settings_cmd
        return settings_cmd(cfg, a.action, a.key)
    if a.cmd == "lessons":
        from .harness.lessons import LessonStore, format_lessons
        store = LessonStore(cfg.abspath("state"))
        if a.action == "wipe":
            store.wipe()
            print("lessons wiped")
        else:
            print(format_lessons(store.all()) or "(no lessons yet)")
        return 0
    if a.cmd == "undo":
        return undo_cmd(cfg, a.items, a.list)
    if a.cmd == "preset":
        from .setup import preset_cmd
        return preset_cmd(cfg, a.action, a.name, {k: getattr(a, k) for k in ("gpu", "device", "model", "ctx", "args")})
    if a.cmd == "setup":
        from .setup import wizard
        return wizard(cfg)
    if a.cmd == "doctor":
        from .setup import doctor
        return doctor(cfg)
    from .setup import first_run_offer
    first_run_offer(cfg)
    from .ui.app import run_ui
    return run_ui(cfg)


def undo_cmd(cfg, items, list_only):
    from .harness import checkpoint, workspace as ws
    folder, why = ws.resolve(cfg)
    if why or not checkpoint.is_repo(folder):
        print(why or checkpoint.guard_note(folder), file=sys.stderr)
        return 2
    try:
        if list_only:
            for run, when, subject in checkpoint.checkpoints(folder):
                print(f"{run:<36} {when}  {subject[:80]}")
            return 0
        run, files = checkpoint.undo_args(folder, items)
        run, done = checkpoint.undo(folder, run, files)
    except checkpoint.GitError as e:
        print(e, file=sys.stderr)
        return 1
    if not done:
        print(f"{run}: nothing to undo")
    for rel, how in done:
        print(f"{how}: {rel}")
    return 0


def tools_cmd(cfg, a):
    from .harness.registry import RegistryError
    reg = Registry(cfg.abspath("state"))
    reg.seed_bundled()
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
            from .toolcli import write_shim
            v = reg.accept(a.name, pick_run(), force=a.force)
            print(f"{a.name}: accepted as v{v}; command: {write_shim(cfg, a.name)}")
        elif a.action == "shims":
            from .toolcli import sync_shims
            written, removed = sync_shims(cfg)
            for p in written:
                print("command:", p)
            for p in removed:
                print("removed:", p)
        elif a.action == "discard":
            run = pick_run()
            reg.discard(a.name, run)
            print(f"{a.name}: staged version from run {run} discarded")
        elif a.action == "rollback":
            reg.set_active(a.name, a.version)
            print(f"{a.name}: active version is now {a.version}")
        elif a.action == "wipe":
            from .toolcli import sync_shims
            reg.wipe()
            sync_shims(cfg)
            print("registry wiped")
    except (RegistryError, ValueError) as e:
        print(e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
