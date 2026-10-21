"""A bad path from the model is an error message back to the model, never a crash of the run."""
import os
import unittest

from mini.agent.core import Agent
from tests.helpers import temp_config


class AgentPathTest(unittest.TestCase):
    def test_write_to_a_directory_is_refused_politely(self):
        a = Agent(temp_config(), label="paths")
        out = a._write({"path": "out", "content": "x"}, "")
        self.assertIn("is a directory, not a file", out)
        self.assertTrue(os.path.isdir(os.path.join(a.work, "out")))

    def test_write_to_a_file_still_works(self):
        a = Agent(temp_config(), label="paths")
        self.assertIn("Wrote out/a.txt", a._write({"path": "out/a.txt", "content": "hi"}, ""))


if __name__ == "__main__":
    unittest.main()


class FinishCheckTest(unittest.TestCase):
    def check(self, outputs, produced=()):
        a = Agent(temp_config(), label="finish")
        for name in produced:
            a._write({"path": f"out/{name}", "content": "x"}, "")
        return a._check_finish({"answer": "done"}, {"outputs": outputs}, 0)

    def test_a_list_in_the_answer_is_not_a_promised_file(self):
        self.assertEqual(self.check(["A list of files in /host."]), "")

    def test_a_promised_file_must_exist(self):
        self.assertIn("produces a file", self.check(["Save the summary to a file."]))
        self.assertIn("produces a file", self.check(["report.csv"]))
        self.assertEqual(self.check(["report.csv"], produced=["report.csv"]), "")
