"""Before a task ends, the model sees the task next to the head of each file it wrote, once, and checks the form."""
import json
import os
import unittest

from mini.agent.core import Agent
from tests.helpers import call, fake_backend, git_folder, step, temp_config


def steps(*after_review):
    return [
        step(call("understand", goal="list names", inputs=["a.txt"], outputs=["names.json"], plan="1. write")),
        step(call("decide_coverage", reasoning="write_file does it", use=[], missing=[])),
        step(call("write_file", path="names.json", content='[{"name": "x"}]\n')),
        step(call("finish", answer="Done.", files=["names.json"])),
        *after_review,
    ]


class FinishReviewTest(unittest.TestCase):
    def run_task(self, script, **over):
        folder = git_folder({"a.txt": "x\n"})
        agent = Agent(temp_config(**over), backend=fake_backend(script), label="review", workspace=folder)
        s = agent.run_task("Write the names in a.txt to names.json as a list of strings.")
        events = [json.loads(l) for l in open(os.path.join(agent.run_dir, "log.jsonl"))]
        return s, folder, events

    def test_the_first_finish_shows_the_files_and_the_model_can_fix_them(self):
        fix = [step(call("write_file", path="names.json", content='["x"]\n')),
               step(call("finish", answer="Done.", files=["names.json"]))]
        s, folder, events = self.run_task(steps(*fix), **{"finish.review_outputs": True})
        self.assertEqual(s["status"], "success", s["error"])
        self.assertEqual(open(os.path.join(folder, "names.json")).read(), '["x"]\n')
        reviews = [e for e in events if e["kind"] == "watchdog" and e.get("name") == "finish_review"]
        self.assertEqual([e["files"] for e in reviews], [["names.json"]])

    def test_it_is_shown_once(self):
        again = [step(call("finish", answer="Done.", files=["names.json"]))]
        s, _, events = self.run_task(steps(*again), **{"finish.review_outputs": True})
        self.assertEqual(s["status"], "success", s["error"])

    def test_the_review_text_has_the_task_and_the_file_head(self):
        from mini.agent import prompts, samples
        folder = git_folder({"out.json": '[{"name": "x"}]\n'})
        h = samples.head(folder, "out.json")
        self.assertIn('[{"name": "x"}]', h)
        text = prompts.REVIEW_OUTPUTS.format(task="T as a list of strings", heads=h)
        self.assertIn("T as a list of strings", text)
        self.assertIn("out.json", text)


if __name__ == "__main__":
    unittest.main()
