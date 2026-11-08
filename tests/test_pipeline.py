"""End-to-end harness test with a scripted model and the real sandbox (needs podman + mini-sandbox:base).
The tool here is a throwaway fixture for testing the harness; it is never installed in the real registry."""
import json
import os
import tempfile
import unittest

from mini import bundled
from mini.agent.core import Agent
from mini.harness.registry import Registry
from tests.helpers import spec_steps, call, fake_backend, fence, step, temp_config

TOOL_BAD = '''
import os
def run(inputs):
    path = inputs["path"]
    with open(path) as f:
        return {"lines": len(f.read().split("\\n"))}
'''
TOOL_FIX_OLD = 'return {"lines": len(f.read().split("\\n"))}'
TOOL_FIX_NEW = 'return {"lines": len(f.read().splitlines())}'
TESTS = '''
import pytest
from tool import run

def test_counts(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("one\\ntwo\\nthree\\n")
    assert run({"path": str(p)}) == {"lines": 3}

def test_empty(tmp_path):
    p = tmp_path / "e.txt"
    p.write_text("")
    assert run({"path": str(p)}) == {"lines": 0}

def test_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        run({"path": str(tmp_path / "nope.txt")})
'''
SPEC = dict(name="count_text_lines", description="Count the lines of a text file.",
            input_schema={"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}},
            output_schema={"type": "object", "properties": {"lines": {"type": "integer"}}},
            permissions={"network": [], "packages": {}},
            cases=[{"situation": "a file of two lines", "expected": "2", "quote": "How many lines does a.txt have"},
                   {"situation": "an empty file", "expected": "raises ValueError", "quote": "empty files are rejected"}])


def script(path="a.txt"):
    """path: the attached file, as the agent is told it (its absolute path)."""
    return [
        step(call("understand", goal="count lines", inputs=[path], outputs=["line count"], side_effects=[],
                  ambiguities=[], plan="1. count")),
        step(call("decide_coverage", reasoning="registry empty", use=[], missing=["count the lines of a text file"])),
        *spec_steps(SPEC),
        step(content=fence("tool.py", TOOL_BAD)),
        step(content=fence("test_tool.py", TESTS)),
        step(call("replace", path="tool.py", old=TOOL_FIX_OLD, new=TOOL_FIX_NEW), call("run_tests")),
        step(call("record_lesson", problem="line count off by one", cause="split on newline counts the empty tail",
                  avoid="str.split('\\n') for counting lines", try_instead="str.splitlines()")),
        step(call("invoke_tool", name="count_text_lines", inputs={"path": path})),
        step(call("finish", answer="The file has 2 lines.")),
    ]


def attachment(text):
    src = os.path.join(tempfile.mkdtemp(prefix="mini-attach-"), "a.txt")
    open(src, "w").write(text)
    return src


class PipelineTest(unittest.TestCase):
    def test_create_fix_install_invoke(self):
        cfg = temp_config()
        src = attachment("x\ny\n")
        agent = Agent(cfg, backend=fake_backend(script(src)), label="pipeline")
        s = agent.run_task("How many lines does a.txt have?", [src])
        self.assertEqual(s["status"], "success", s["error"])
        self.assertEqual(s["created"][0]["attempts"], {"local": 2})
        m = agent.registry.get("count_text_lines")
        self.assertTrue(m["test_results"]["passed"])
        self.assertEqual(m["created_by"]["provider"], "local")
        events = [json.loads(l) for l in open(os.path.join(agent.run_dir, "log.jsonl"))]
        kinds = [e["kind"] for e in events]
        for k in ("registry_state", "gap", "spec", "code", "test_run", "approval", "install", "invoke", "run_end"):
            self.assertIn(k, kinds)
        tests = [e for e in events if e["kind"] == "test_run"]
        self.assertEqual([t["passed"] for t in tests], [False, True])
        # an edge case that quotes the task is kept; one that quotes words the task does not have is dropped
        edges = [e for e in events if e["kind"] == "edge_cases"][0]
        self.assertEqual(edges["kept"], ["a file of two lines -> 2"])
        self.assertEqual(edges["dropped"], ["an empty file"])
        self.assertEqual([e.get("phase") for e in events if e["kind"] == "phase" and str(e.get("phase")).startswith("spec_")],
                         ["spec_design", "spec_io", "spec_run", "spec_edges"])
        # the failed first attempt is written down and the fix starts in a fresh conversation built from it
        note = [e for e in events if e["kind"] == "attempt_note"]
        self.assertEqual(len(note), 1)
        self.assertIn("first version", note[0]["text"])
        self.assertIn("test_counts", note[0]["text"])
        self.assertIn("fresh_context", [e.get("phase") for e in events if e["kind"] == "phase"])
        inv = [e for e in events if e["kind"] == "invoke" and "output" in e][0]
        self.assertEqual(inv["output"], {"lines": 2})
        # install happened only after a passing test run
        self.assertLess(kinds.index("install"), len(kinds))
        self.assertTrue(tests[-1]["t"] <= [e for e in events if e["kind"] == "install"][0]["t"])

    def test_lessons_persist_into_next_run(self):
        cfg = temp_config()
        src = attachment("x\n")
        a1 = Agent(cfg, backend=fake_backend(script(src)), label="lesson1")
        a1.run_task("count lines", [src])
        stored = a1.lessons.all()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["outcome"], "fixed")
        self.assertEqual(stored[0]["try_instead"], "str.splitlines()")
        self.assertTrue(any("AssertionError" in sig for sig, n in stored[0]["errors"]))
        # a fresh session (new Agent, empty registry) building a similar tool sees the lesson in its spec prompt
        a1.registry.wipe()
        backend = fake_backend(script(src))
        a2 = Agent(cfg, backend=backend, label="lesson2")
        seen = []
        orig = backend.chat
        backend.chat = lambda model, messages, *x, **k: (seen.append(messages[-1]["content"]), orig(model, messages, *x, **k))[1]
        a2.run_task("count lines", [src])
        self.assertTrue(any("str.splitlines()" in m and "Lessons from earlier attempts" in m for m in seen))

    def test_rollback(self):
        cfg = temp_config()
        src = attachment("x\n")
        agent = Agent(cfg, backend=fake_backend(script(src)), label="rollback")
        agent.run_task("count", [src])
        reg = agent.registry
        m1 = reg.get("count_text_lines")
        reg.install({**{k: v for k, v in m1.items() if not k.startswith("_")}, "description": "v2"}, m1["_dir"])
        self.assertEqual(reg.get("count_text_lines")["version"], 2)
        reg.set_active("count_text_lines", 1)
        self.assertEqual(reg.get("count_text_lines")["version"], 1)
        self.assertEqual(reg.versions("count_text_lines"), [1, 2])

    def test_staged_tool_is_used_by_its_own_task_only(self):
        cfg = temp_config(**{"tools.auto_accept": False})
        src = attachment("x\ny\n")
        agent = Agent(cfg, backend=fake_backend(script(src)), label="staged")
        s = agent.run_task("How many lines does a.txt have?", [src])
        self.assertEqual(s["status"], "success", s["error"])
        inv = [json.loads(l) for l in open(os.path.join(agent.run_dir, "log.jsonl"))]
        self.assertIn({"lines": 2}, [e.get("output") for e in inv if e["kind"] == "invoke"])
        other = Registry(cfg.abspath("state"))
        self.assertEqual(set(other.names()), set(bundled.names()))   # only the seeded tools
        self.assertEqual([x["run_id"] for x in other.staged()], [s["run_id"]])
        self.assertEqual(other.accept("count_text_lines", s["run_id"]), 1)
        self.assertEqual(set(Registry(cfg.abspath("state")).names()), {"count_text_lines", *bundled.names()})


if __name__ == "__main__":
    unittest.main()
