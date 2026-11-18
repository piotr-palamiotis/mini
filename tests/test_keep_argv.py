"""A command-line script run with no arguments did no work: run_code says so and keep_tool keeps nothing."""
import unittest

from mini.agent.core import Agent
from mini.core import run_code
from tests.helpers import git_folder, temp_config

CLI = ("import sys\n\ndef make(text, out):\n    open(out, 'w').write(text)\n\n"
       "if __name__ == '__main__':\n    if len(sys.argv) < 3:\n        print('Usage: make.py <text> <out>')\n"
       "    else:\n        make(sys.argv[1], sys.argv[2])\n")


class KeepArgvTest(unittest.TestCase):
    def test_a_usage_only_run_is_idle(self):
        self.assertTrue(run_code.argv_idle(CLI, "Usage: make.py <text> <out>\n", []))
        self.assertTrue(run_code.argv_idle(CLI, "", []))
        self.assertFalse(run_code.argv_idle(CLI, "Usage: make.py <text> <out>\n", ["a.txt"]))   # it wrote a file
        self.assertFalse(run_code.argv_idle(CLI, "total 42\n", []))                            # it printed a result
        self.assertFalse(run_code.argv_idle("print('Usage: x')", "Usage: x\n", []))           # no sys.argv

    def test_keep_tool_refuses_a_usage_only_run_without_planning(self):
        a = Agent(temp_config(), label="keep_argv", workspace=git_folder({"a.txt": "x\n"}))
        a.last_script = {"script": CLI, "ok": True, "record": "/nowhere", "written": [],
                         "output": "Usage: make.py <text> <out>\n"}
        text = a._keep(None, "make a file from text", "task", "", [])
        self.assertIn("Nothing to keep", text)
        self.assertIn("without sys.argv", text)


if __name__ == "__main__":
    unittest.main()
