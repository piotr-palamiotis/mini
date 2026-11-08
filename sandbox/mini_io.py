"""The io object a tool gets as run(inputs, io). Harness code, mounted read-only at /mini/mini_io.py.

  io.ask(key, prompt, kind="text", choices=None, default=None, multi=False)
      The value for `key`: from inputs when the caller gave it (one-line use), else the user is asked.
      kind: text | number | confirm (returns bool) | choice (one of choices; with multi=True a list of them, the
      user ticks several) | file (a path in the working directory) | secret (not echoed).
  io.setting(key)      A saved setting declared in the spec (a dict, or the value itself). The harness asks the
                       user for missing settings before the tool starts.
  io.say(text)         Show a line to the user while the tool runs.

ScriptedIO is the same interface for tests: answers come from a dict, nothing is asked, `asked` records the
questions in order, and a missing answer raises MissingInput like an unattended run does.
"""
import json
import sys


class MissingInput(Exception):
    """A question had no answer: unattended run, or the user cancelled."""


KINDS = ("text", "number", "confirm", "choice", "file", "secret")


def _coerce(kind, value, choices, multi=False):
    if kind == "choice" and multi:
        items = value.split(",") if isinstance(value, str) else list(value or [])
        return [_coerce(kind, str(v).strip() if isinstance(v, str) else v, choices) for v in items
                if not isinstance(v, str) or v.strip()]
    if kind == "number" and isinstance(value, str):
        value = float(value) if "." in value else int(value)
    if kind == "confirm" and isinstance(value, str):
        value = value.strip().lower() in ("y", "yes", "true", "1")
    if kind == "choice" and choices and value not in choices:
        raise ValueError(f"{value!r} is not one of {choices}")
    return value


def _check(kind, choices, multi):
    if kind not in KINDS:
        raise ValueError(f"unknown question kind {kind!r}; use one of {KINDS}")
    if multi and (kind != "choice" or not choices):
        raise ValueError("multi=True needs kind=\"choice\" and the choices")


class IO:
    """Live channel to the harness: JSON lines on a private pipe (the tool's own prints go to stderr)."""

    def __init__(self, inputs, settings, out, inp):
        self.inputs, self.settings, self._out, self._in = inputs, settings, out, inp

    def _send(self, msg):
        self._out.write(json.dumps(msg) + "\n")
        self._out.flush()

    def ask(self, key, prompt, kind="text", choices=None, default=None, multi=False):
        _check(kind, choices, multi)
        if key in self.inputs and self.inputs[key] is not None:
            return _coerce(kind, self.inputs[key], choices, multi)
        self._send({"op": "ask", "key": key, "prompt": prompt, "kind": kind, "choices": choices, "default": default,
                    "multi": bool(multi)})
        line = self._in.readline()
        if not line:
            raise MissingInput(f"no answer for {key!r}: the harness closed the channel")
        reply = json.loads(line)
        if "answer" not in reply:
            raise MissingInput(reply.get("error") or f"no answer for {key!r}")
        value = _coerce(kind, reply["answer"], choices, multi)
        self.inputs[key] = value
        return value

    def setting(self, key):
        if key not in self.settings:
            raise KeyError(f"setting {key!r} is not available: declare it in the spec's settings")
        return self.settings[key]

    def say(self, text):
        self._send({"op": "say", "text": str(text)})


class ScriptedIO:
    """For tests: answers by key, settings by key; records every question."""

    def __init__(self, answers=None, settings=None, inputs=None):
        self.answers, self.settings, self.inputs = dict(answers or {}), dict(settings or {}), dict(inputs or {})
        self.asked, self.said = [], []

    def ask(self, key, prompt, kind="text", choices=None, default=None, multi=False):
        _check(kind, choices, multi)
        if key in self.inputs and self.inputs[key] is not None:
            return _coerce(kind, self.inputs[key], choices, multi)
        self.asked.append(key)
        if key not in self.answers:
            raise MissingInput(f"no answer for {key!r}")
        return _coerce(kind, self.answers[key], choices, multi)

    def setting(self, key):
        if key not in self.settings:
            raise KeyError(f"setting {key!r} is not available: declare it in the spec's settings")
        return self.settings[key]

    def say(self, text):
        self.said.append(str(text))
        print(f"[io.say] {text}")       # pytest shows it under a failing test: a swallowed error stays visible


def channel(inputs, settings):
    """The live IO for the runner: the protocol keeps the real stdout; print() goes to stderr."""
    import os
    out = os.fdopen(os.dup(1), "w")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return IO(inputs, settings, out, sys.stdin)
