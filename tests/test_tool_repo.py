"""Each tool is its own git repo: a task stages its version on task/<run>, accept makes it the next tag on main,
discard deletes it, parallel tasks on one tool need an explicit choice. No model, no sandbox."""
import os
import subprocess
import tempfile
import unittest

from mini.harness.registry import Registry, RegistryError


def manifest(name="word_tool", desc="does a thing", n=1):
    return {"name": name, "description": desc, "input_schema": {}, "output_schema": {}, "permissions": {},
            "edge_cases": [], "kind": "code", "created_by": {"provider": "local", "model": "m"},
            "test_results": {"passed": True, "summary": f"{n} passed"}}


def files(body):
    d = tempfile.mkdtemp(prefix="mini-files-")
    open(os.path.join(d, "tool.py"), "w").write(f"def run(inputs):\n    return {{'v': {body!r}}}\n")
    open(os.path.join(d, "test_tool.py"), "w").write("def test_x():\n    assert True\n")
    os.makedirs(os.path.join(d, "__pycache__"))
    return d


class ToolRepoTest(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="mini-state-")

    def reg(self, run=None):
        return Registry(self.state, run_id=run)

    def log(self, name):
        return subprocess.run(["git", "-C", os.path.join(self.state, "tools", name), "log", "--format=%s", "main"],
                              capture_output=True, text=True).stdout.splitlines()

    def test_stage_then_accept(self):
        a = self.reg("run1")
        self.assertEqual(a.install(manifest(), files("one")), 1)
        self.assertEqual(a.names(), ["word_tool"])
        m = a.get("word_tool")
        self.assertTrue(m["_staged"])
        self.assertIn("'one'", open(os.path.join(m["_dir"], "tool.py")).read())
        self.assertFalse(os.path.exists(os.path.join(m["_dir"], "manifest.json")), "the manifest is not mounted")
        self.assertFalse(os.path.exists(os.path.join(m["_dir"], "__pycache__")))
        self.assertEqual(self.reg("run2").names(), [], "another run does not see a staged tool")
        self.assertEqual(self.reg().versions("word_tool"), [])
        self.assertEqual([s["run_id"] for s in self.reg().staged()], ["run1"])
        self.assertIn("+def run(inputs):", self.reg().diff("word_tool", "run1"))

        self.assertEqual(self.reg().accept("word_tool", "run1"), 1)
        r = self.reg()
        self.assertEqual(r.names(), ["word_tool"])
        self.assertEqual(r.get("word_tool")["version"], 1)
        self.assertFalse(r.get("word_tool")["_staged"])
        self.assertEqual(r.staged(), [])
        self.assertEqual(len(self.log("word_tool")), 1)

    def test_versions_and_rollback(self):
        self.reg("r1").install(manifest(), files("one"))
        self.reg().accept("word_tool", "r1")
        b = self.reg("r2")
        b.install(manifest(desc="better"), files("two"))
        self.assertEqual(b.get("word_tool")["description"], "better", "the run sees its own staged version")
        self.assertEqual(self.reg().get("word_tool")["version"], 1, "others still get v1")
        self.assertEqual(self.reg().accept("word_tool", "r2"), 2)
        r = self.reg()
        self.assertEqual(r.versions("word_tool"), [1, 2])
        r.set_active("word_tool", 1)
        self.assertEqual(r.get("word_tool")["version"], 1)
        self.assertIn("'one'", open(os.path.join(r.get("word_tool")["_dir"], "tool.py")).read())
        self.assertEqual(r.get("word_tool", 2)["description"], "better")
        self.assertEqual(len(self.log("word_tool")), 2, "one commit per accepted version on main")

    def test_parallel_tasks_need_a_choice(self):
        self.reg("r1").install(manifest(), files("one"))
        self.reg().accept("word_tool", "r1")
        self.reg("a").install(manifest(desc="from a"), files("a"))
        self.reg("b").install(manifest(desc="from b"), files("b"))
        self.assertEqual(self.reg().accept("word_tool", "a"), 2)
        self.assertTrue(next(s for s in self.reg().staged() if s["run_id"] == "b")["outdated"])
        with self.assertRaises(RegistryError):
            self.reg().accept("word_tool", "b")
        self.assertEqual(self.reg().accept("word_tool", "b", force=True), 3)
        self.assertEqual(self.reg().get("word_tool")["description"], "from b")

    def test_discard(self):
        self.reg("r1").install(manifest(), files("one"))
        self.reg().discard("word_tool", "r1")
        self.assertFalse(os.path.exists(os.path.join(self.state, "tools", "word_tool")), "never accepted: repo removed")
        self.reg("r2").install(manifest(), files("one"))
        self.reg().accept("word_tool", "r2")
        self.reg("r3").install(manifest(desc="worse"), files("x"))
        self.reg().discard("word_tool", "r3")
        self.assertEqual(self.reg().get("word_tool")["description"], "does a thing")
        with self.assertRaises(RegistryError):
            self.reg().accept("word_tool", "r3")

    def test_auto_accept(self):
        r = Registry(self.state, run_id="r1", auto_accept=True)
        self.assertEqual(r.install(manifest(), files("one")), 1)
        self.assertEqual(self.reg().versions("word_tool"), [1])
        self.assertEqual(self.reg().staged(), [])


if __name__ == "__main__":
    unittest.main()
