"""Test helpers: a temporary config with its own state/runs dirs, a scripted model, auto approvals."""
import os
import tempfile

from mini.harness import config as config_mod
from mini.harness.backends import Endpoint, FakeBackend


def temp_config(**over):
    d = tempfile.mkdtemp(prefix="mini-test-")
    cfg = config_mod.load()
    cfg["paths"]["state"] = os.path.join(d, "state")
    cfg["paths"]["runs"] = os.path.join(d, "runs")
    cfg["approvals"]["mode"] = "auto"
    cfg["approvals"]["auto_approve"] = ["confirm", "permissions", "install", "escalate", "cap"]
    cfg["fallback"]["enabled"] = False
    for path, v in over.items():
        sec, key = path.split(".")
        cfg[sec][key] = v
    return cfg


def call(_name, **args):
    return {"name": _name, "arguments": args}


def step(*calls, content="", thinking="", **extra):
    return {"content": content, "thinking": thinking, "tool_calls": list(calls), **extra}


def fake_backend(script):
    return FakeBackend(Endpoint("fake", "http://fake", options={"script": script}))


def fence(name, body):
    return f"```python {name}\n{body.strip()}\n```\n"
