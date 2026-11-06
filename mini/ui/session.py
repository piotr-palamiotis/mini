"""One conversation tab: its own stream, history, model tier and running task. The sessions of an app run in
parallel; the local model and the folder's file changes are shared through harness/gates."""

import json
import os
import tempfile

FILE = "sessions.json"                              # in <folder>/.mini/: the tabs outlive the app


def sessions_path(workspace):
    from ..harness import journal
    return os.path.join(journal.meta_dir(workspace), FILE) if workspace else None


def load(path, attempts):
    """(sessions, active number) saved by an earlier app; ([], None) when there is nothing usable."""
    try:
        with open(path) as f:
            data = json.load(f)
        out = [Session(int(d["n"]), str(d["name"]), d.get("tier", "local"), attempts, history=d.get("history") or [],
                       pending=d.get("pending")) for d in data["sessions"]]
        return out, data.get("active")
    except (OSError, ValueError, KeyError, TypeError):
        return [], None


def save(path, sessions, active):
    """Write the tabs, atomically. Never raises: a full disk must not break a running task."""
    data = {"active": active.n, "sessions": [{"n": s.n, "name": s.name, "tier": s.tier, "history": s.history,
                                              "pending": s.pending} for s in sessions]}
    try:
        folder = os.path.dirname(os.path.dirname(path))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        from ..harness import journal
        journal.ensure_excluded(folder)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError:
        pass


class Session:
    def __init__(self, n, name, tier, attempts, history=None, pending=None):
        self.n, self.name = n, name
        self.pane_id, self.stream_id = f"s-{n}", f"stream-{n}"
        self.stream = None                          # the VerticalScroll, set once mounted
        self.tier = tier                            # the model for this session's tasks (local unless /model says otherwise)
        self.history = list(history or [])                           # finished tasks of this conversation; every new task is told about them
        self.pending = pending                      # the task text while one runs: after a restart it was interrupted
        self.attachments = []
        self.attempt = (0, attempts, tier)
        self.cap_hit = ""
        self.busy = False
        self.waiting = ""                           # "local" or "folder" while a gate holds this session back
        self.attention = False                      # something happened while this tab was in the background
        self.blocks = {}                            # open block id -> widget
        self.card = None                            # the question card waiting for an answer
        self.last_text = ""
        self.last_run = None                        # the newest checkpointed run of this session: /undo's default
        self.agent = None

    def label(self):
        return self.name + (" …" if self.waiting else " ●" if self.busy else "") + (" *" if self.attention else "")
