"""Project memory: <folder>/.mini/ holds a journal of the tasks done in the folder (journal.jsonl, one line per
task) and the run logs (runs/<run id>/). The folder is excluded from git through .git/info/exclude, so it is never
committed or checkpointed, but it stays visible in a file explorer. Stdlib only (plus the git command).
"""
import json
import os
import subprocess
import time

from .workspace import META

FILE = "journal.jsonl"
MAX_ENTRIES = 8                                 # entries shown to the agent, most recent first
MAX_CHARS = 2500                                # and characters of the whole section (about 600 tokens)
MAX_FILES = 8                                   # files named per entry
CUT_TASK, CUT_RESULT = 200, 300


def meta_dir(folder):
    return os.path.join(folder, META)


def runs_dir(folder):
    return os.path.join(meta_dir(folder), "runs")


def ensure_excluded(folder):
    """Add .mini/ to the repository's .git/info/exclude (once). Does nothing when the folder is not in a git
    repository. Returns True when the exclude file lists it afterwards."""
    try:
        r = subprocess.run(["git", "-C", folder, "rev-parse", "--git-path", "info/exclude", "--show-prefix"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    lines = r.stdout.splitlines()
    if r.returncode or len(lines) < 2:
        return False
    path = lines[0] if os.path.isabs(lines[0]) else os.path.join(folder, lines[0])
    rule = "/" + lines[1] + META + "/"          # anchored to the repository root, even when the folder is a subfolder
    try:
        text = open(path).read() if os.path.exists(path) else ""
        if rule not in text.splitlines():
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a") as f:
                f.write(("\n" if text and not text.endswith("\n") else "") + rule + "\n")
    except OSError:
        return False
    return True


def record(folder, summary):
    """Append one finished task to the journal. summary is the run's summary dict."""
    os.makedirs(meta_dir(folder), exist_ok=True)
    ensure_excluded(folder)
    result = summary.get("answer") if summary.get("status") == "success" else summary.get("error")
    entry = {"time": time.strftime("%Y-%m-%d %H:%M"), "run": summary.get("run_id"), "task": summary.get("task") or "",
             "status": summary.get("status"), "result": result or "", "files": list(summary.get("outputs") or []),
             "checkpoint": summary.get("checkpoint"),
             "log": os.path.relpath(os.path.join(runs_dir(folder), summary.get("run_id") or ""), folder)}
    with open(os.path.join(meta_dir(folder), FILE), "a") as f:
        f.write(json.dumps(entry) + "\n")


def entries(folder):
    """Journal entries, oldest first. A damaged line is skipped."""
    out = []
    try:
        with open(os.path.join(meta_dir(folder), FILE)) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict):
                    out.append(e)
    except OSError:
        pass
    return out


def summary_text(folder):
    """What happened in this folder before, for Agent.context(): the latest entries, most recent first, within
    MAX_ENTRIES and MAX_CHARS. "" when the folder has no journal."""
    cut = lambda v, n: " ".join(str(v or "").split())[:n]
    head = "Earlier tasks in this folder (most recent first; the journal is in .mini/journal.jsonl):"
    lines, used = [], len(head)
    for e in reversed(entries(folder)[-MAX_ENTRIES:]):
        files = [str(f) for f in e.get("files") or []]
        line = f"- {e.get('time', '?')} [{e.get('status', '?')}] {cut(e.get('task'), CUT_TASK)}"
        if e.get("result"):
            line += f" -> {cut(e['result'], CUT_RESULT)}"
        if files:
            line += "; files: " + ", ".join(files[:MAX_FILES]) + (f" (+{len(files) - MAX_FILES} more)" if len(files) > MAX_FILES else "")
        if used + len(line) + 1 > MAX_CHARS:
            break
        lines.append(line)
        used += len(line) + 1
    return head + "\n" + "\n".join(lines) if lines else ""
