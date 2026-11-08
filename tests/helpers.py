"""Test helpers: a temporary config with its own state/runs dirs, a scripted model, auto approvals."""
import os
import tempfile

from mini.harness import config as config_mod
from mini.harness.backends import Endpoint, FakeBackend


def temp_config(**over):
    d = tempfile.mkdtemp(prefix="mini-test-")
    cfg = config_mod.load(os.path.join(d, "no-such.toml"))      # the built-in defaults, never the user's settings
    cfg["paths"]["state"] = os.path.join(d, "state")
    cfg["paths"]["runs"] = os.path.join(d, "runs")
    cfg["approvals"]["mode"] = "auto"
    cfg["approvals"]["auto_approve"] = ["confirm", "plan", "permissions", "install", "cap"]
    cfg["local"]["backend"] = "fake"                  # never reach a real model server from a test
    cfg["local"]["use"] = []
    cfg["llamacpp"]["preset"] = ""                    # tests set up their own llama.cpp settings, not the user's preset
    cfg["tools"]["auto_accept"] = True                # tests that check staging turn this off
    cfg["free"]["enabled"] = False
    cfg["paid"]["enabled"] = False
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


def git_folder(files=None, commit=True):
    """A throwaway git repository in a temp dir with the given {relative path: text} files, committed. Returns its
    real path."""
    import subprocess
    d = os.path.realpath(tempfile.mkdtemp(prefix="mini-repo-"))
    run = lambda *a: subprocess.run(["git", "-C", d, *a], check=True, capture_output=True, text=True).stdout
    run("init", "-q")
    run("config", "user.name", "test")
    run("config", "user.email", "test@example.com")
    run("config", "commit.gpgsign", "false")
    for rel, text in (files or {}).items():
        os.makedirs(os.path.dirname(os.path.join(d, rel)) or d, exist_ok=True)
        with open(os.path.join(d, rel), "w") as f:
            f.write(text)
    if commit and files:
        run("add", "-A")
        run("commit", "-q", "-m", "start")
    return d


def git(folder, *args):
    import subprocess
    return subprocess.run(["git", "-C", folder, *args], check=True, capture_output=True, text=True).stdout


def spec_steps(spec):
    """The four spec phases a scripted model answers, from one spec dict."""
    return [step(call("write_design", name=spec["name"], description=spec["description"],
                      how_it_works=spec.get("how_it_works", ["do it"]))),
            step(call("write_io", input_schema=spec["input_schema"], output_schema=spec["output_schema"],
                      settings=spec.get("settings", []))),
            step(call("write_run", interaction=spec.get("interaction", ["run"]), ui=spec.get("ui", {}),
                      permissions=spec.get("permissions", {"network": [], "packages": {}}))),
            step(call("write_edge_cases", cases=spec.get("cases", [])))]
