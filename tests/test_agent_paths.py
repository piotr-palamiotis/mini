"""A bad path from the model is an error message back to the model, never a crash of the run."""
import os
import unittest

from mini.agent.core import Agent
from tests.helpers import temp_config


class AgentPathTest(unittest.TestCase):
    def test_write_to_a_directory_is_refused_politely(self):
        a = Agent(temp_config(), label="paths")
        os.makedirs(os.path.join(a.folder, "out"))
        out = a.act("write_file", {"path": "out", "content": "x"})[0]
        self.assertIn("is a directory, not a file", out)
        self.assertTrue(os.path.isdir(os.path.join(a.folder, "out")))

    def test_write_to_a_file_still_works(self):
        a = Agent(temp_config(), label="paths")
        self.assertIn("Wrote out/a.txt", a.act("write_file", {"path": "out/a.txt", "content": "hi"})[0])
        self.assertEqual(open(os.path.join(a.folder, "out", "a.txt")).read(), "hi")


if __name__ == "__main__":
    unittest.main()


class FinishCheckTest(unittest.TestCase):
    def check(self, outputs, produced=()):
        a = Agent(temp_config(), label="finish")
        for name in produced:
            a.act("write_file", {"path": name, "content": "x"})
        return a._check_finish({"answer": "done"}, {"outputs": outputs}, 0)

    def test_a_list_in_the_answer_is_not_a_promised_file(self):
        self.assertEqual(self.check(["A list of files in this folder."]), "")

    def test_a_promised_file_must_exist(self):
        self.assertIn("produces a file", self.check(["Save the summary to a file."]))
        self.assertIn("produces a file", self.check(["report.csv"]))
        self.assertEqual(self.check(["report.csv"], produced=["report.csv"]), "")

    def test_a_tool_built_in_the_task_must_have_run(self):
        a = Agent(temp_config(), label="finish")
        a.created.append({"name": "ini_to_json", "version": 1})
        self.assertIn("never ran successfully", a._check_finish({"answer": "done"}, {"outputs": []}, 0))
        self.assertEqual(a._check_finish({"answer": "done"}, {"outputs": []}, 2), "")   # gives way after 2 rejections
        a.invoked_ok.add("ini_to_json")
        self.assertEqual(a._check_finish({"answer": "done"}, {"outputs": []}, 0), "")
        a.created.append({"name": "convert_ini_json", "version": 1})       # a second copy; one of them ran
        self.assertEqual(a._check_finish({"answer": "done"}, {"outputs": []}, 0), "")
        a.created.append({"name": "draw_bar_chart", "version": 1})         # a tool for another job must run too
        self.assertIn("draw_bar_chart was built", a._check_finish({"answer": "done"}, {"outputs": []}, 0))

    def test_a_folder_of_outputs_counts_as_written(self):
        a = Agent(temp_config(), label="finish")
        a.act("write_file", {"path": "thumbs/a.jpg", "content": "x"})
        self.assertEqual(a._check_finish({"answer": "done", "files": ["thumbs/", "thumbs/a.jpg"]}, {"outputs": []}, 0), "")
        os.makedirs(os.path.join(a.folder, "empty"))
        self.assertIn("empty folder", a._check_finish({"answer": "done", "files": ["empty/"]}, {"outputs": []}, 0))
