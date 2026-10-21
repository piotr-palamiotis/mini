"""Run a registered tool from the terminal, and the commands on PATH that do the same.

  mini tool NAME [--field VALUE ...] [--json]     fields come from the tool's input schema; whatever is not
                                                   given, the tool asks for (when the terminal is interactive)
  NAME [--field VALUE ...]                         the same, through a shim in [tools] bin_dir

The tool runs in the sandbox exactly as it does inside a task: same permissions, same network allowlist, same
saved settings. Host files given as values are copied in; files the tool writes under out/ are copied to the
current directory.
"""
import argparse
import json
import os
import shutil
import sys

from .harness.ask import TerminalAsker, stage_file
from .harness.config import ROOT
from .harness.eventlog import RunLog
from .harness.registry import Registry
from .harness.sandbox import PolicyError, Sandbox
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


def _stage_values(work, value):
    """Host files named in values are copied into the sandbox's working directory."""
    if isinstance(value, str) and os.path.isfile(os.path.expanduser(value)):
        return stage_file(work, value)
    if isinstance(value, list):
        return [_stage_values(work, v) for v in value]
    return value


def _collect_outputs(work, out):
    """Copy what the tool wrote under out/ to the current directory. Returns {tool path: local path}."""
    moved = {}
    src = os.path.join(work, "out")
    for base, _, files in os.walk(src):
        for f in files:
            p = os.path.join(base, f)
            dest = os.path.join(os.getcwd(), os.path.relpath(p, src))
            stem, ext = os.path.splitext(dest)
            n = 1
            while os.path.exists(dest):
                dest, n = f"{stem}-{n}{ext}", n + 1
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy(p, dest)
            moved[os.path.relpath(p, work)] = dest
    return moved


def _show(out, moved):
    for k, v in out.items():
        if isinstance(v, str) and v in moved:
            v = moved[v]
        text = v if isinstance(v, str) else json.dumps(v, indent=1, default=str)
        print(f"{k}:" + ("\n" + text if "\n" in text else " " + text))
    extra = [d for p, d in moved.items() if p not in {x for x in out.values() if isinstance(x, str)}]
    if extra:
        print("files: " + ", ".join(extra))


def run_tool(cfg, name, argv):
    reg = Registry(cfg.abspath("state"))
    m = reg.get(name)
    if not m:
        staged = [s["run_id"] for s in reg.staged() if s["name"] == name]
        print(f"no accepted tool named {name}" + (f" (staged by run {', '.join(staged)}: mini tools review {name})"
                                                  if staged else ""), file=sys.stderr)
        return 2
    a = _parser(m).parse_args(argv)
    log = RunLog(cfg.abspath("runs"), label=f"tool-{name}")
    work = os.path.join(log.dir, "work")
    os.makedirs(os.path.join(work, "in"), exist_ok=True)
    inputs = dict(a._inputs or {})
    inputs.update({k: v for k, v in vars(a).items() if not k.startswith("_") and v is not None})
    inputs = {k: _stage_values(work, v) for k, v in inputs.items()}
    asker = TerminalAsker(work)
    store = SettingsStore(cfg.abspath("state"))
    try:
        settings = store.resolve(m, asker)
        network = store.network(m, settings)
    except MissingSetting as e:
        print(f"{name}: {e}. Save it with: mini settings set KEY (or run {name} in a terminal)", file=sys.stderr)
        return 2
    log.emit("invoke", tool=name, version=m["version"], inputs=inputs, settings=sorted(settings) or None, via="terminal")
    try:
        from .harness import workspace as ws
        out, res = Sandbox(cfg, log).invoke_tool(m, m["_dir"], work, inputs, on_message=asker, settings=settings,
                                                 network=network, workspace=ws.resolve(cfg)[0])
    except PolicyError as e:
        print(f"{name}: {e}", file=sys.stderr)
        return 1
    log.emit("invoke", tool=name, version=m["version"], ok="error" not in out, output=out, exit_code=res["exit_code"])
    if "error" in out:
        print(f"{name} failed: {out.get('message') or out['error']}", file=sys.stderr)
        return 1
    moved = _collect_outputs(work, out)
    if a._json:
        print(json.dumps(out, indent=1, default=str))
    else:
        _show(out, moved)
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
    names = Registry(cfg.abspath("state")).names()
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
