"""Run a registered tool for the user, outside a task: from the terminal (mini tool NAME, the shims) and from a slash
command in the UI. The tool runs in the sandbox exactly as it does inside a task: same permissions, network
allowlist and saved settings; the files it writes land at the same relative path in the folder, after a checkpoint.

invoke() is the shared part. slash() wraps it for the UI: the run is a tool block in the event stream (its sandbox
command and its questions nest in it), questions are cards whatever the approval mode (the user is there; they ran
it), and it ends with a run_end event, so the UI adds it to the conversation and the folder's journal records it.
"""
import json
import os
import shutil
import time

from . import checkpoint, gates, journal, toolui, workspace as ws
from .approvals import ApprovalGate
from .ask import GateAsker
from .eventlog import JournalSink, RunLog
from .sandbox import PolicyError, Sandbox
from .settings import MissingSetting, SettingsStore


def masked(m, inputs):
    """inputs with the values of secret properties (toolui.is_secret) replaced, for the stream and the logs."""
    props = (m.get("input_schema") or {}).get("properties") or {}
    return {k: "***" if isinstance(props.get(k), dict) and toolui.is_secret(props[k]) else v for k, v in inputs.items()}


def invoke(cfg, m, folder, work, inputs, log, asker, task, via, checkpointed=True):
    """Run the tool on inputs already copied into work. Returns (output, sandbox result, written, refused, write
    error or ""). PolicyError or MissingSetting when it did not run."""
    name = m["name"]
    store = SettingsStore(cfg.abspath("state"))
    settings = store.resolve(m, asker)
    network = store.network(m, settings)
    log.emit("invoke", tool=name, version=m["version"], inputs=masked(m, inputs), settings=sorted(settings) or None,
             via=via)
    out, res = Sandbox(cfg, log).invoke_tool(m, m["_dir"], work, inputs, on_message=asker, settings=settings,
                                             network=network)
    items, refused = ws.changes(folder, work)
    written, problem = [], ""
    if items:
        try:
            with gates.FOLDER.hold(on_wait=lambda: log.emit("gate", gate="folder", state="waiting")):
                cp = checkpoint.Checkpoint(folder, log.run_id, task) if checkpointed else None
                if cp:
                    cp.before_change([rel for rel, _ in items])
                written = ws.export(folder, items)
                if cp:
                    cp.after_change(written)
        except (checkpoint.GitError, PolicyError, OSError) as e:
            problem = str(e)
    log.emit("files_changed", paths=written, refused=refused or None)
    log.emit("invoke", tool=name, version=m["version"], ok="error" not in out, output=out, exit_code=res["exit_code"])
    return out, res, written, refused, problem


def result_text(out, written, refused=(), problem=""):
    """The output as `key: value` lines (what the terminal prints and the tool block shows)."""
    lines = []
    for k, v in out.items():
        text = v if isinstance(v, str) else json.dumps(v, indent=1, default=str)
        lines.append(f"{k}:" + ("\n" + text if "\n" in text else " " + text))
    extra = [p for p in written if p not in {x for x in out.values() if isinstance(x, str)}]
    if extra:
        lines.append("files: " + ", ".join(extra))
    if problem:
        lines.append(f"files not written: {problem}")
    lines += [f"not written: {rel} ({why})" for rel, why in refused]
    return "\n".join(lines)


def slash(cfg, workspace, m, inputs, task, approval_handler, sinks=()):
    """Run a tool from a slash command. sinks get the event stream (the UI's handler); approval_handler answers
    the question cards. Returns the run's summary (the run_end event's fields)."""
    t0 = time.time()
    memory = bool(workspace) and checkpoint.is_repo(workspace)
    log = RunLog(journal.runs_dir(workspace) if memory else cfg.abspath("runs"), label=f"tool-{m['name']}")
    for s in sinks:
        log.subscribe(s)
    if memory:
        journal.ensure_excluded(workspace)
        log.subscribe(JournalSink(workspace, log))
    folder = workspace or os.path.join(log.dir, "files")      # without a folder: an empty one of the run
    os.makedirs(folder, exist_ok=True)
    gate = ApprovalGate({"approvals": {**cfg["approvals"], "mode": "ask"}}, log, approval_handler)
    work = os.path.join(log.dir, "work")                       # the tool's /work: copies of the files it is given
    os.makedirs(work, exist_ok=True)
    bid = log.start("tool", name=m["name"], args=masked(m, inputs), via="slash")
    status, text, written, error = "failed", "", [], ""
    try:
        staged = ws.copy_in(folder, work, inputs, outside=True)    # the user named them: any file may come in
        out, _res, written, refused, problem = invoke(cfg, m, folder, work, staged, log,
                                                      GateAsker(gate, log, work, m["name"], folder), task, "slash",
                                                      checkpointed=memory)
        text = result_text(out, written, refused, problem)
        if "error" in out:
            error = f"{m['name']} failed: {out.get('message') or out['error']}"
        else:
            status = "success"
    except MissingSetting as e:
        error = f"{m['name']} cannot run: {e}"
    except (PolicyError, OSError) as e:
        error = f"{m['name']} was not run: {e}"
    finally:
        shutil.rmtree(work, ignore_errors=True)
    log.end(bid, "ok" if status == "success" else "error", text=text or error)
    summary = {"run_id": log.run_id, "task": task, "status": status, "answer": text if status == "success" else "",
               "error": error, "secs": round(time.time() - t0, 1), "outputs": sorted(written), "created": [],
               "workspace": workspace, "checkpoint": checkpoint.REF + log.run_id if memory and written else None,
               "via": "slash", "tool": m["name"]}
    log.emit("run_end", **summary)
    log.close()
    return summary
