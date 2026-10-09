"""End-to-end harness test with a scripted model and the real sandbox (needs podman + mini-sandbox:base).
The tool here is a throwaway fixture for testing the harness; it is never installed in the real registry."""
import json
import os
import unittest

from mini.agent.core import Agent
from tests.helpers import call, fake_backend, fence, step, temp_config

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
            permissions={"network": [], "packages": {}}, edge_cases=["normal file", "empty file", "missing file"])


def script():
    return [
        step(call("understand", goal="count lines", inputs=["in/a.txt"], outputs=["line count"], side_effects=[],
                  ambiguities=[], plan="1. count")),
        step(call("decide_coverage", reasoning="registry empty", use=[], missing=["count the lines of a text file"])),
        step(call("write_spec", **SPEC)),
        step(content=fence("tool.py", TOOL_BAD)),
        step(content=fence("test_tool.py", TESTS)),
        step(call("replace", path="tool.py", old=TOOL_FIX_OLD, new=TOOL_FIX_NEW), call("run_tests")),
        step(call("record_lesson", problem="line count off by one", cause="split on newline counts the empty tail",
                  avoid="str.split('\\n') for counting lines", try_instead="str.splitlines()")),
        step(call("invoke_tool", name="count_text_lines", inputs={"path": "in/a.txt"})),
        step(call("finish", answer="The file has 2 lines.")),
    ]


class PipelineTest(unittest.TestCase):
    def test_create_fix_install_invoke(self):
        cfg = temp_config()
        agent = Agent(cfg, backend=fake_backend(script()), label="pipeline")
        src = os.path.join(agent.run_dir, "a.txt")
        open(src, "w").write("x\ny\n")
        s = agent.run_task("How many lines does a.txt have?", [src])
        self.assertEqual(s["status"], "success", s["error"])
        self.assertEqual(s["created"][0]["attempts"], {"local": 2, "fallback": 0})
        m = agent.registry.get("count_text_lines")
        self.assertTrue(m["test_results"]["passed"])
        self.assertEqual(m["created_by"]["provider"], "local")
        events = [json.loads(l) for l in open(os.path.join(agent.run_dir, "log.jsonl"))]
        kinds = [e["kind"] for e in events]
        for k in ("registry_state", "gap", "spec", "code", "test_run", "approval", "install", "invoke", "run_end"):
            self.assertIn(k, kinds)
        tests = [e for e in events if e["kind"] == "test_run"]
        self.assertEqual([t["passed"] for t in tests], [False, True])
        inv = [e for e in events if e["kind"] == "invoke" and "output" in e][0]
        self.assertEqual(inv["output"], {"lines": 2})
        # install happened only after a passing test run
        self.assertLess(kinds.index("install"), len(kinds))
        self.assertTrue(tests[-1]["t"] <= [e for e in events if e["kind"] == "install"][0]["t"])

    def test_lessons_persist_into_next_run(self):
        cfg = temp_config()
        a1 = Agent(cfg, backend=fake_backend(script()), label="lesson1")
        src = os.path.join(a1.run_dir, "a.txt")
        open(src, "w").write("x\n")
        a1.run_task("count lines", [src])
        stored = a1.lessons.all()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["outcome"], "fixed")
        self.assertEqual(stored[0]["try_instead"], "str.splitlines()")
        self.assertTrue(any("AssertionError" in sig for sig, n in stored[0]["errors"]))
        # a fresh session (new Agent, empty registry) building a similar tool sees the lesson in its spec prompt
        a1.registry.wipe()
        backend = fake_backend(script())
        a2 = Agent(cfg, backend=backend, label="lesson2")
        seen = []
        orig = backend.chat
        backend.chat = lambda model, messages, *x, **k: (seen.append(messages[-1]["content"]), orig(model, messages, *x, **k))[1]
        a2.run_task("count lines", [src])
        self.assertTrue(any("str.splitlines()" in m and "Lessons from earlier attempts" in m for m in seen))

    def test_rollback(self):
        cfg = temp_config()
        agent = Agent(cfg, backend=fake_backend(script()), label="rollback")
        src = os.path.join(agent.run_dir, "a.txt")
        open(src, "w").write("x\n")
        agent.run_task("count", [src])
        reg = agent.registry
        m1 = reg.get("count_text_lines")
        reg.install({**{k: v for k, v in m1.items() if not k.startswith("_")}, "description": "v2"}, m1["_dir"])
        self.assertEqual(reg.get("count_text_lines")["version"], 2)
        reg.set_active("count_text_lines", 1)
        self.assertEqual(reg.get("count_text_lines")["version"], 1)
        self.assertEqual(reg.versions("count_text_lines"), [1, 2])


if __name__ == "__main__":
    unittest.main()
