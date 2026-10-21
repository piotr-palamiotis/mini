"""The event stream: thinking, text, tool, command and question blocks (start, chunk, end) in order and with their
fields; live sandbox output, line by line while the command runs (a fake engine, no container); io.ask and
ask_user with choices and multi."""
import json
import os
import stat
import sys
import tempfile
import time
import unittest

from mini.agent.core import Agent
from mini.harness.approvals import ApprovalGate, Decision
from mini.harness.ask import GateAsker
from mini.harness.config import ROOT
from mini.harness.eventlog import EventBus, Sink
from mini.harness.sandbox import Sandbox
from tests.helpers import call, fake_backend, git_folder, step, temp_config

sys.path.insert(0, os.path.join(ROOT, "sandbox"))
from mini_io import ScriptedIO  # noqa: E402

# stands in for podman: `run ...` prints two stdout lines and one stderr line, half a second apart; `rm` does nothing
ENGINE = f"""#!{sys.executable}
import sys, time
if sys.argv[1] != "run":
    sys.exit(0)
if "sleep" in sys.argv:
    time.sleep(30)
print("one", flush=True)
time.sleep(0.5)
print("warn", file=sys.stderr, flush=True)
time.sleep(0.5)
print("two", flush=True)
sys.exit(3)
"""


class Recorder(Sink):
    def __init__(self):
        self.events = []

    def __call__(self, ev):
        self.events.append(dict(ev, at=time.time()))

    def blocks(self, block):
        return [e for e in self.events if e.get("block") == block]


class StreamTest(unittest.TestCase):
    def test_block_events_in_order(self):
        script = [step(call("understand", goal="g", inputs=[], outputs=["n"], plan="1"), thinking="let me see this"),
                  step(call("decide_coverage", reasoning="core does it", use=[], missing=[])),
                  step(call("ask_user", question="Which?", options=["red", "green", "blue"], multi=True),
                       content="asking now"),
                  step(call("finish", answer="done"))]

        def handler(kind, title, details, options):
            return Decision(True, "answer", "2, 3", "user") if kind == "question" else Decision(True, options[0])
        cfg = temp_config(**{"approvals.mode": "ask", "approvals.confirm_first": False})
        a = Agent(cfg, backend=fake_backend(script), approval_handler=handler, label="events",
                  workspace=git_folder({"a.txt": "a"}))
        rec = a.log.subscribe(Recorder())
        self.assertEqual(a.run_task("pick colours")["status"], "success")

        think = rec.blocks("thinking")
        self.assertEqual([e["kind"] for e in think[:1] + think[-1:]], ["start", "end"])
        first = think[0]["id"]
        mine = [e for e in think if e["id"] == first]
        self.assertEqual([e["kind"] for e in mine], ["start"] + ["chunk"] * (len(mine) - 2) + ["end"])
        self.assertEqual("".join(e["text"] for e in mine if e["kind"] == "chunk").split(), "let me see this".split())
        self.assertEqual((mine[0]["step"], mine[-1]["status"]), ("parse", "ok"))
        self.assertEqual(mine[-1]["chars"], len("".join(e["text"] for e in mine if e["kind"] == "chunk")))
        self.assertTrue(all(e["elapsed"] >= 0 for e in mine if e["kind"] == "chunk"))
        self.assertTrue(rec.blocks("text"), "the answer text streams as a text block")

        tool = [e for e in rec.blocks("tool") if e.get("name") == "ask_user" or e["kind"] == "end"]
        start, end = tool[0], next(e for e in tool if e["kind"] == "end" and e["id"] == tool[0]["id"])
        self.assertEqual((start["args"]["question"], end["status"]), ("Which?", "ok"))
        self.assertIn("The user picked: green; blue", end["text"])
        q = rec.blocks("question")
        self.assertEqual([e["kind"] for e in q], ["start", "end"])
        self.assertEqual(q[0]["parent"], start["id"], "the question runs inside the ask_user tool block")
        self.assertEqual((q[0]["choices"], q[0]["multi"], q[0]["title"]), (["red", "green", "blue"], True, "Which?"))
        self.assertEqual((q[1]["status"], q[1]["picks"], q[1]["answer"], q[1]["decided_by"]),
                         ("answered", ["green", "blue"], "2, 3", "user"))
        order = [e["id"] for e in rec.events if e["kind"] in ("start", "end") and e["id"] in (start["id"], q[0]["id"])]
        self.assertEqual(order, [start["id"], q[0]["id"], q[0]["id"], start["id"]], "properly nested")

        logged = [json.loads(line) for line in open(os.path.join(a.run_dir, "log.jsonl"))]
        self.assertFalse([e for e in logged if e["kind"] == "chunk"], "chunks go to subscribers only")
        self.assertEqual(sum(e["kind"] == "start" for e in logged), sum(e["kind"] == "start" for e in rec.events))
        self.assertTrue(any(e["kind"] == "run_end" for e in logged))
        journal = open(os.path.join(a.workspace, ".mini", "journal.jsonl")).read()
        self.assertIn('"task": "pick colours"', journal, "the journal sink recorded the task from run_end")


class LiveSandboxTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp(prefix="mini-engine-")
        self.engine = os.path.join(d, "engine")
        open(self.engine, "w").write(ENGINE)
        os.chmod(self.engine, os.stat(self.engine).st_mode | stat.S_IXUSR)
        cfg = temp_config(**{"sandbox.engine": self.engine})
        self.bus, self.rec = EventBus(), Recorder()
        self.bus.subscribe(self.rec)
        self.sb, self.work = Sandbox(cfg, self.bus), tempfile.mkdtemp()

    def test_lines_arrive_while_it_runs(self):
        res = self.sb.run(["python", "-c", "x"], self.work, purpose="live")
        ev = self.rec.blocks("command")
        self.assertEqual([e["kind"] for e in ev], ["start", "chunk", "chunk", "chunk", "end"])
        self.assertEqual((ev[0]["argv"], ev[0]["purpose"], ev[0]["parent"]), (["python", "-c", "x"], "live", None))
        chunks = ev[1:4]
        self.assertEqual([(c["text"], c["stream"]) for c in chunks], [("one", "stdout"), ("warn", "stderr"), ("two", "stdout")])
        self.assertLess(chunks[0]["elapsed"], chunks[1]["elapsed"])
        self.assertLess(chunks[1]["elapsed"], chunks[2]["elapsed"])
        self.assertGreater(ev[-1]["at"] - chunks[0]["at"], 0.6, "the first line came long before the end")
        self.assertEqual({k: ev[-1][k] for k in ("status", "exit_code", "lines", "timed_out")},
                         {"status": "failed", "exit_code": 3, "lines": 3, "timed_out": False})
        self.assertEqual((res["exit_code"], res["output"]), (3, "one\ntwo\n\nwarn\n"))

    def test_timeout_ends_the_block(self):
        res = self.sb.run(["python", "-c", "sleep"], self.work, timeout=1)
        end = self.rec.blocks("command")[-1]
        self.assertEqual((end["kind"], end["status"], end["timed_out"]), ("end", "timeout", True))
        self.assertTrue(res["timed_out"])
        self.assertLess(end["secs"], 10)


class AskChoicesTest(unittest.TestCase):
    def test_scripted_io_multi(self):
        io = ScriptedIO(answers={"c": "red, blue", "d": ["green"]})
        self.assertEqual(io.ask("c", "Colours?", "choice", ["red", "green", "blue"], multi=True), ["red", "blue"])
        self.assertEqual(io.ask("d", "Colours?", "choice", ["red", "green"], multi=True), ["green"])
        with self.assertRaises(ValueError):
            ScriptedIO(answers={"e": "pink"}).ask("e", "?", "choice", ["red"], multi=True)
        with self.assertRaises(ValueError):
            io.ask("f", "?", "text", multi=True)

    def test_tool_question_with_choices_through_the_gate(self):
        seen = []

        def handler(kind, title, details, options):
            seen.append((tuple(options), details.get("options")))
            return Decision(True, "answer", "1,3", "user") if options[0] == "answer" else Decision(True, "beta", "", "user")
        bus, rec = EventBus(), Recorder()
        bus.subscribe(rec)
        gate = ApprovalGate(temp_config(**{"approvals.mode": "ask"}), bus, handler)
        ask = GateAsker(gate, bus, tempfile.mkdtemp(), "t")
        msg = {"op": "ask", "key": "k", "prompt": "Pick", "kind": "choice", "choices": ["alpha", "beta", "gamma"]}
        self.assertEqual(ask(dict(msg, multi=True)), {"answer": ["alpha", "gamma"]})
        self.assertEqual(ask(msg), {"answer": "beta"})
        self.assertEqual(seen[0], (("answer", "cancel"), "1) alpha\n2) beta\n3) gamma"))
        self.assertEqual(seen[1][0], ("alpha", "beta", "gamma", "cancel"))
        starts = [e for e in rec.blocks("question") if e["kind"] == "start"]
        ends = [e for e in rec.blocks("question") if e["kind"] == "end"]
        self.assertEqual([(s["multi"], s["choices"]) for s in starts], [(True, msg["choices"]), (False, msg["choices"])])
        self.assertEqual([e["picks"] for e in ends], [["alpha", "gamma"], ["beta"]])

    def test_cancel_is_a_cancelled_question(self):
        bus, rec = EventBus(), Recorder()
        bus.subscribe(rec)
        gate = ApprovalGate(temp_config(**{"approvals.mode": "ask"}), bus,
                            lambda *a: Decision(False, "cancel", "", "user"))
        r = GateAsker(gate, bus, tempfile.mkdtemp(), "t")({"op": "ask", "key": "k", "prompt": "Pick", "kind": "choice",
                                                           "choices": ["a", "b"], "multi": True})
        self.assertEqual(r, {"error": "the user cancelled"})
        self.assertEqual((rec.events[-1]["kind"], rec.events[-1]["status"], rec.events[-1]["picks"]), ("end", "cancelled", None))


if __name__ == "__main__":
    unittest.main()
