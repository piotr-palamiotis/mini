"""Before a task ends, the model checks each file it wrote against the task's words: finish carries one check per
file, and a file that does not match, or has no check, keeps the task going (at most 3 rounds)."""
import json
import os
import unittest

from mini.agent.core import Agent
from tests.helpers import call, fake_backend, git_folder, step, temp_config

ASKS = "a list of strings"
OK = [{"file": "names.json", "sample": '"x"', "task_asks": ASKS, "file_has": "a JSON array of texts", "matches": True}]
BAD = [{"file": "names.json", "sample": '{"name": "x"}', "task_asks": ASKS, "file_has": "a list of objects",
        "matches": False}]
COPIED = [dict(BAD[0], file_has=ASKS, matches=True)]
MADE_UP = [dict(OK[0], sample='"y"')]


def finish(checks=None):
    return step(call("finish", answer="Done.", files=["names.json"], **({"checks": checks} if checks else {})))


def steps(*after_review):
    return [
        step(call("understand", goal="list names", inputs=["a.txt"], outputs=["names.json"], plan="1. write")),
        step(call("decide_coverage", reasoning="write_file does it", use=[], missing=[])),
        step(call("write_file", path="names.json", content='[{"name": "x"}]\n')),
        finish(),
        *after_review,
    ]


class FinishReviewTest(unittest.TestCase):
    def run_task(self, script, **over):
        folder = git_folder({"a.txt": "x\n"})
        agent = Agent(temp_config(**{"finish.review_outputs": True, **over}), backend=fake_backend(script), label="review",
                      workspace=folder)
        s = agent.run_task("Write the names in a.txt to names.json as " + ASKS + ".")
        events = [json.loads(l) for l in open(os.path.join(agent.run_dir, "log.jsonl"))]
        reviews = [e for e in events if e["kind"] == "watchdog" and e.get("name") == "finish_review"]
        results = [e["text"] for e in reviews]
        return s, folder, results, reviews

    def test_a_file_that_does_not_match_is_fixed_before_the_task_ends(self):
        fix = [finish(BAD), step(call("write_file", path="names.json", content='["x"]\n')), finish(OK)]
        s, folder, results, reviews = self.run_task(steps(*fix))
        self.assertEqual(s["status"], "success", s["error"])
        self.assertEqual(open(os.path.join(folder, "names.json")).read(), '["x"]\n')
        self.assertEqual(len(reviews), 2)
        self.assertIn('[{"name": "x"}]', results[0])                    # the head of the file, next to the task
        self.assertIn(ASKS, results[0])
        self.assertIn("a list of objects", results[1])

    def test_a_check_must_quote_the_file_and_describe_it_in_its_own_words(self):
        fix = [finish(MADE_UP), finish(COPIED), step(call("write_file", path="names.json", content='["x"]\n')),
               finish(OK)]
        s, folder, results, reviews = self.run_task(steps(*fix))
        self.assertEqual(s["status"], "success", s["error"])
        self.assertIn("sample is not in the file", results[1])
        self.assertIn("repeats the task's words", results[2])
        self.assertEqual(open(os.path.join(folder, "names.json")).read(), '["x"]\n')

    def test_a_fresh_judge_sends_back_a_file_whose_form_differs(self):
        judge = lambda ok, why: step(call("judge_form", matches=ok, why=why))
        fix = [finish([dict(OK[0], sample='{"name": "x"}', file_has="objects with a name")]),
               judge(False, "the task asks for strings; the file has objects."),
               step(call("write_file", path="names.json", content='["x"]\n')),
               finish(OK), judge(True, "a list of strings.")]
        s, folder, results, reviews = self.run_task(steps(*fix), **{"finish.judge": True})
        self.assertEqual(s["status"], "success", s["error"])
        self.assertIn("the task asks for strings; the file has objects.", results[1])
        self.assertEqual(open(os.path.join(folder, "names.json")).read(), '["x"]\n')
        self.assertEqual(len(reviews), 2)

    def test_a_file_with_no_check_is_named(self):
        stray = [step(call("write_file", path="test.txt", content="t\n")), finish(OK)]
        s, _, results, reviews = self.run_task(steps(*stray, step(call("delete_file", path="test.txt")), finish(OK)))
        self.assertEqual(s["status"], "success", s["error"])
        self.assertTrue(any("no check for test.txt" in r for r in results), results)

    def test_after_three_rounds_the_task_may_end(self):
        s, _, _, reviews = self.run_task(steps(finish(), finish(), finish()))
        self.assertEqual(s["status"], "success", s["error"])
        self.assertEqual(len(reviews), 3)

    def test_off_in_config_means_no_review(self):
        folder = git_folder({"a.txt": "x\n"})
        agent = Agent(temp_config(), backend=fake_backend(steps()), label="review", workspace=folder)
        self.assertEqual(agent.run_task("Write names.json.")["status"], "success")


if __name__ == "__main__":
    unittest.main()
