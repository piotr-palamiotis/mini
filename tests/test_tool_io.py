"""Tools that ask: a fixture tool written here (not by a model) with a saved setting (one secret field), questions,
a follow-up, a confirmation and an output file. Driven from a pty (asks, saves the setting, asks less the
second time), one-line with every value given (asks nothing), through its PATH command, and inside a task
(questions become dialogs; unattended they become an error for the agent). Needs podman + mini-sandbox:base."""
import json
import os
import pty
import select
import stat
import subprocess
import sys
import tempfile
import time
import unittest

from mini.agent.core import Agent
from mini.harness.approvals import Decision
from mini.harness.config import ROOT
from mini.harness.registry import Registry
from mini.harness.settings import SettingsStore
from mini.toolcli import write_shim
from tests.helpers import call, fake_backend, step, temp_config

sys.path.insert(0, os.path.join(ROOT, "sandbox"))
from mini_io import MissingInput, ScriptedIO  # noqa: E402

TOOL = '''import os


def run(inputs, io):
    profile = io.setting("writer_profile")
    title = io.ask("title", "Title", "text")
    body = io.ask("body", "Text", "text")
    if io.ask("add_footer", "Add the footer?", "confirm"):
        body += "\\n-- " + profile["footer"]
    if not io.ask("go", "Write " + title + "?", "confirm"):
        raise RuntimeError("cancelled")
    os.makedirs("out", exist_ok=True)
    path = "out/" + title + ".txt"
    with open(path, "w") as f:
        f.write(body)
    io.say("written " + path)
    return {"file": path, "chars": len(body), "token_len": len(profile.get("token", ""))}
'''
MANIFEST = {
    "name": "note_writer", "description": "Write a titled note file.", "kind": "code",
    "input_schema": {"type": "object", "required": ["title", "body"], "properties": {
        "title": {"type": "string", "description": "file title"}, "body": {"type": "string"},
        "add_footer": {"type": "boolean"}, "go": {"type": "boolean"}}},
    "output_schema": {"type": "object", "properties": {"file": {"type": "string"}, "chars": {"type": "integer"}}},
    "settings": [{"key": "writer_profile", "description": "Writer profile", "secret": ["token"],
                  "schema": {"type": "object", "required": ["footer", "token"], "properties": {
                      "footer": {"type": "string", "description": "Footer line"},
                      "token": {"type": "string", "description": "Access token"}}}}],
    "interaction": ["profile missing: ask and save it", "title", "text", "footer?", "confirm", "write"],
    "permissions": {"network": [], "packages": {}}, "edge_cases": [], "files": ["tool.py"],
    "created_by": {"provider": "test", "model": "none"}, "test_results": {"passed": True, "summary": "fixture"},
}
SECRET = "tok-7f3a9c"


def install(cfg):
    d = tempfile.mkdtemp(prefix="mini-fixture-")
    open(os.path.join(d, "tool.py"), "w").write(TOOL)
    Registry(cfg.abspath("state"), run_id="fixture", auto_accept=True).install(MANIFEST, d)


def write_cfg(cfg):
    cfg.path = os.path.join(tempfile.mkdtemp(prefix="mini-cfg-"), "mini.toml")
    from mini.harness.config import dumps
    open(cfg.path, "w").write(dumps({k: v for k, v in cfg.items()}))
    return cfg


def drive(argv, cwd, answers, timeout=180):
    """Run argv in a pty; answer each prompt (substring -> reply) in order. Returns the whole output."""
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(cwd)
        os.execvpe(argv[0], argv, {**os.environ, "PYTHONPATH": ROOT})
    out, pending, t0 = b"", list(answers), time.time()
    while time.time() - t0 < timeout:
        r, _, _ = select.select([fd], [], [], 0.5)
        if r:
            try:
                data = os.read(fd, 4096)
            except OSError:
                break
            if not data:
                break
            out += data
        while pending and pending[0][0].encode() in out.split(b"\x00")[-1]:
            want, reply = pending.pop(0)
            out += b"\x00"                                 # answered up to here
            os.write(fd, (reply + "\n").encode())
    os.waitpid(pid, 0)
    return out.replace(b"\x00", b"").decode(errors="replace"), pending


class ScriptedIOTest(unittest.TestCase):
    def test_inputs_skip_questions_and_missing_answers_raise(self):
        io = ScriptedIO(answers={"b": "2"}, inputs={"a": "1"}, settings={"s": {"x": 1}})
        self.assertEqual(io.ask("a", "A?"), "1")
        self.assertEqual(io.ask("b", "B?", "number"), 2)
        self.assertEqual(io.asked, ["b"])
        self.assertEqual(io.setting("s"), {"x": 1})
        with self.assertRaises(MissingInput):
            io.ask("c", "C?")
        with self.assertRaises(KeyError):
            io.setting("nope")


class ToolIOTest(unittest.TestCase):
    def setUp(self):
        self.cfg = write_cfg(temp_config())
        install(self.cfg)
        self.cwd = tempfile.mkdtemp(prefix="mini-cwd-")
        self.py = [sys.executable, "-m", "mini", "--config", self.cfg.path, "tool", "note_writer"]

    def test_terminal_asks_then_remembers_the_setting(self):
        out, left = drive(self.py, self.cwd, [("Footer line", "best regards"), ("Access token", SECRET),
                                              ("Title", "first"), ("Text", "hello"), ("footer?", "y"),
                                              ("Write first?", "y")])
        self.assertEqual(left, [], out)
        self.assertIn("written out/first.txt", out)
        self.assertNotIn(SECRET, out, "a secret is not echoed")
        self.assertEqual(open(os.path.join(self.cwd, "first.txt")).read(), "hello\n-- best regards")
        rec = SettingsStore(self.cfg.abspath("state")).get("writer_profile")
        self.assertEqual(rec["value"], {"footer": "best regards", "token": SECRET})
        p = os.path.join(self.cfg.abspath("state"), "settings", "writer_profile.json")
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        for base, _, files in os.walk(self.cfg.abspath("runs")):
            for f in files:
                self.assertNotIn(SECRET, open(os.path.join(base, f), errors="replace").read(), f"secret in {f}")

        out2, left2 = drive(self.py + ["--title", "second"], self.cwd,
                            [("Text", "again"), ("footer?", "n"), ("Write second?", "y")])
        self.assertEqual(left2, [], out2)
        self.assertNotIn("Footer line", out2, "the saved setting is not asked again")
        self.assertNotIn("Title", out2, "a value given as an argument is not asked")
        self.assertEqual(open(os.path.join(self.cwd, "second.txt")).read(), "again")

    def test_one_line_asks_nothing(self):
        SettingsStore(self.cfg.abspath("state")).save("writer_profile", {"footer": "f", "token": "t"},
                                                      MANIFEST["settings"][0])
        r = subprocess.run(self.py + ["--title", "x", "--body", "b", "--no-add-footer", "--go", "--json"],
                           cwd=self.cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                           env={**os.environ, "PYTHONPATH": ROOT}, timeout=180)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"file": "out/x.txt", "chars": 1, "token_len": 1})
        self.assertTrue(os.path.exists(os.path.join(self.cwd, "x.txt")))
        r = subprocess.run(self.py + ["--title", "y", "--body", "b"], cwd=self.cwd, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, env={**os.environ, "PYTHONPATH": ROOT}, timeout=180)
        self.assertEqual(r.returncode, 1)
        self.assertIn("pass --add-footer", r.stderr, "not interactive: says which value is missing")

    def test_path_command(self):
        SettingsStore(self.cfg.abspath("state")).save("writer_profile", {"footer": "f", "token": "t"},
                                                      MANIFEST["settings"][0])
        self.cfg["tools"]["bin_dir"] = tempfile.mkdtemp(prefix="mini-bin-")
        shim = write_shim(self.cfg, "note_writer")
        self.assertEqual(os.path.basename(shim), "note_writer")
        r = subprocess.run([shim, "--title", "z", "--body", "b", "--add-footer", "--go"], cwd=self.cwd,
                           capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=180)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("chars: 6", r.stdout)
        self.assertEqual(open(os.path.join(self.cwd, "z.txt")).read(), "b\n-- f")


class InTaskTest(unittest.TestCase):
    def script(self):
        return [step(call("understand", goal="write a note", inputs=[], outputs=["file"], plan="use note_writer")),
                step(call("decide_coverage", reasoning="installed", use=["note_writer"], missing=[])),
                step(call("invoke_tool", name="note_writer", inputs={"title": "t", "body": "b"})),
                step(call("finish", answer="done", files=["out/t.txt"]))]

    def test_questions_become_dialogs(self):
        cfg = temp_config(**{"approvals.mode": "ask", "approvals.confirm_first": False})
        install(cfg)
        seen = []

        def handler(kind, title, details, options):
            seen.append((kind, title, tuple(options)))
            if kind != "question":
                return Decision(True, options[0], "", "user")
            answers = {"Footer line": "ft", "Access token": SECRET}
            for k, v in answers.items():
                if k in title:
                    return Decision(True, "answer", v, "user")
            return Decision(True, "yes", "", "user")
        agent = Agent(cfg, backend=fake_backend(self.script()), approval_handler=handler, label="ask")
        s = agent.run_task("write a note")
        self.assertEqual(s["status"], "success", s["error"])
        self.assertEqual([t for k, t, o in seen if k == "question"],
                         ["Writer profile: Footer line", "Writer profile: Access token", "Add the footer?", "Write t?"])
        self.assertEqual(open(os.path.join(agent.work, "out", "t.txt")).read(), "b\n-- ft")
        log = open(os.path.join(agent.run_dir, "log.jsonl")).read()
        self.assertNotIn(SECRET, log)
        self.assertFalse(os.path.exists(os.path.join(agent.work, ".mini", "settings.json")), "settings removed after the run")

    def test_unattended_question_is_an_error_for_the_agent(self):
        cfg = temp_config(**{"approvals.confirm_first": False})
        install(cfg)
        SettingsStore(cfg.abspath("state")).save("writer_profile", {"footer": "f", "token": "t"}, MANIFEST["settings"][0])
        agent = Agent(cfg, backend=fake_backend(self.script()), label="auto")
        agent.run_task("write a note")
        res = [json.loads(l) for l in open(os.path.join(agent.run_dir, "log.jsonl"))]
        text = next(e["text"] for e in res if e["kind"] == "tool_result" and e["name"] == "invoke_tool")
        self.assertIn("pass it in the tool inputs", text)


if __name__ == "__main__":
    unittest.main()
