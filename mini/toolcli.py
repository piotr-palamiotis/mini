"""Run a registered tool from the terminal, and the commands on PATH that do the same.

  mini tool NAME [--field VALUE ...] [--json]     fields come from the tool's input schema; whatever is not
                                                   given, the tool asks for (when the terminal is interactive)
  NAME [--field VALUE ...]                         the same, through a shim in [tools] bin_dir

The tool runs in the sandbox exactly as it does inside a task: same permissions, same network allowlist, same
saved settings, same folder rules: the current directory is the folder (a git repository), files given as values
are copied in, and every file the tool writes lands at the same relative path in it, after a checkpoint
(`mini undo` puts it back).
"""
import argparse
import json
import os
import shutil
import sys

from .harness import checkpoint, toolrun, workspace as ws
from .harness.ask import TerminalAsker
from .harness.config import ROOT
from .harness.eventlog import RunLog
from .harness.registry import Registry
from .harness.sandbox import PolicyError
from .harness.settings import MissingSetting, SettingsStore

SHIM_MARK = "# mini tool shim"


def _parser(m):
    props = (m.get("input_schema") or {}).get("properties") or {}
    ap = argparse.ArgumentParser(prog=m["name"], description=m.get("description"),
                                 epilog="Values you leave out are asked for. Saved settings: "
                                        + (", ".join(d["key"] for d in m.get("settings") or []) or "none"))
    for k, p in props.items():
        p = p if isinstance(p, dict) else {}
        flag, help_ = "--" + k.replace("_", "-"), (p.get("description") or "") + (f" (one of: {', '.join(map(str, p['enum']))})" if p.get("enum") else "")
        t = p.get("type")
        if t == "boolean":
            ap.add_argument(flag, dest=k, action=argparse.BooleanOptionalAction, default=None, help=help_)
        elif t == "array":
            ap.add_argument(flag, dest=k, nargs="*", default=None, help=help_)
        elif t == "object":
            ap.add_argument(flag, dest=k, type=json.loads, default=None, help=help_ + " (JSON)")
        else:
            ap.add_argument(flag, dest=k, type={"integer": int, "number": float}.get(t, str), default=None, help=help_)
    ap.add_argument("--json", dest="_json", action="store_true", help="print the raw JSON result")
    ap.add_argument("--inputs", dest="_inputs", type=json.loads, default=None, help="all inputs as one JSON object")
    return ap


def run_tool(cfg, name, argv):
    reg = Registry(cfg.abspath("state"))
    reg.seed_bundled()
    m = reg.get(name)
    if not m:
        staged = [s["run_id"] for s in reg.staged() if s["name"] == name]
        print(f"no accepted tool named {name}" + (f" (staged by run {', '.join(staged)}: mini tools review {name})"
                                                  if staged else ""), file=sys.stderr)
        return 2
    a = _parser(m).parse_args(argv)
    folder, why = ws.resolve(cfg)
    if why:
        print(f"{name}: {why}", file=sys.stderr)
        return 2
    if not checkpoint.offer_init(folder, checkpoint.terminal_ask):
        return 2
    log = RunLog(cfg.abspath("runs"), label=f"tool-{name}")
    work = os.path.join(log.dir, "work")                # the tool's /work: copies of the files it is given
    os.makedirs(work, exist_ok=True)
    try:
        return _run(cfg, m, name, a, folder, log, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _run(cfg, m, name, a, folder, log, work):
    inputs = dict(a._inputs or {})
    inputs.update({k: v for k, v in vars(a).items() if not k.startswith("_") and v is not None})
    try:
        inputs = ws.copy_in(folder, work, inputs, outside=True)    # the user named them: any file may come in
    except PolicyError as e:
        print(f"{name}: {e}", file=sys.stderr)
        return 2
    asker = TerminalAsker(work, folder=folder)
    try:
        out, _res, written, refused, problem = toolrun.invoke(cfg, m, folder, work, inputs, log, asker,
                                                              f"mini tool {name}", "terminal")
    except MissingSetting as e:
        print(f"{name}: {e}. Save it with: mini settings set KEY (or run {name} in a terminal)", file=sys.stderr)
        return 2
    except PolicyError as e:
        print(f"{name}: {e}", file=sys.stderr)
        return 1
    if problem:
        print(f"{name}: files not written: {problem}", file=sys.stderr)
    for rel, why in refused:
        print(f"not written: {rel} ({why})", file=sys.stderr)
    if "error" in out:
        print(f"{name} failed: {out.get('message') or out['error']}", file=sys.stderr)
        return 1
    if a._json:
        print(json.dumps(out, indent=1, default=str))
    elif out or written:
        print(toolrun.result_text(out, written))
    return 0


# ---------------------------------------------------------------- shims
def bin_dir(cfg):
    return os.path.expanduser(cfg["tools"]["bin_dir"])


def _is_shim(path):
    try:
        with open(path, "rb") as f:
            return SHIM_MARK.encode() in f.read(300)
    except OSError:
        return False


def shim_path(cfg, name):
    """<bin_dir>/<name>, or <bin_dir>/mini-<name> when the plain name is already another command."""
    d = bin_dir(cfg)
    plain = os.path.join(d, name)
    other = shutil.which(name)
    if _is_shim(plain) or (not os.path.exists(plain) and (other is None or _is_shim(other))):
        return plain
    return os.path.join(d, "mini-" + name)


def write_shim(cfg, name):
    p = shim_path(cfg, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    py = os.path.join(ROOT, ".venv", "bin", "python")
    py = py if os.path.exists(py) else sys.executable
    conf = f' --config "{cfg.path}"' if cfg.path and os.path.exists(cfg.path) else ""
    with open(p, "w") as f:
        f.write(f"#!/bin/sh\n{SHIM_MARK} for {name} (written by `mini tools accept`; `mini tools shims` rewrites it)\n"
                f'PYTHONPATH="{ROOT}${{PYTHONPATH:+:$PYTHONPATH}}" exec "{py}" -m mini{conf} tool {name} "$@"\n')
    os.chmod(p, 0o755)
    return p


def sync_shims(cfg):
    """A shim for every accepted tool; shims of tools that are gone are removed. Returns (written, removed)."""
    reg = Registry(cfg.abspath("state"))
    reg.seed_bundled()
    names = reg.names()
    written = [write_shim(cfg, n) for n in names]
    removed = []
    d = bin_dir(cfg)
    if os.path.isdir(d):
        for f in os.listdir(d):
            p = os.path.join(d, f)
            if _is_shim(p) and p not in written:
                os.remove(p)
                removed.append(p)
    return written, removed


# ---------------------------------------------------------------- settings
def settings_cmd(cfg, action, key):
    store = SettingsStore(cfg.abspath("state"))
    if action == "list":
        for r in store.list():
            print(f"{r['key']:<24} {r['description'][:50]:<50} saved by {r.get('saved_by') or '?'}  {r['saved_at']}")
        if not store.list():
            print("(no saved settings)")
        return 0
    if not key:
        print(f"usage: mini settings {action} KEY", file=sys.stderr)
        return 2
    rec = store.get(key)
    if rec is None and action != "set":
        print(f"no setting {key}", file=sys.stderr)
        return 1
    if action == "show":
        print(json.dumps({**{k: v for k, v in rec.items() if k != "value"}, "value": store.masked(rec)}, indent=1))
    elif action == "delete":
        store.delete(key)
        print(f"{key} deleted; tools that need it will ask again")
    elif action == "set":
        decl = None
        if rec:
            decl = {"key": key, "description": rec["description"], "schema": rec["schema"], "secret": rec["secret"]}
        else:
            for m in Registry(cfg.abspath("state")).list_tools():
                decl = next((d for d in m.get("settings") or [] if d["key"] == key), decl)
        if not decl:
            print(f"no tool declares a setting {key}", file=sys.stderr)
            return 1
        if rec:
            store.delete(key)
        try:
            store.resolve({"name": "mini settings", "settings": [decl]}, TerminalAsker(os.getcwd()))
        except MissingSetting as e:
            if rec:
                store.save(key, rec["value"], decl, rec.get("saved_by", ""))
            print(f"not saved: {e}", file=sys.stderr)
            return 1
        print(f"{key} saved")
    return 0
