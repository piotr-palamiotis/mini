"""Project memory: the .mini/ journal and run logs in the folder, excluded from git, fed into Agent.context()."""
import json
import os
import subprocess
import unittest

from mini.agent.core import Agent
from mini.harness import checkpoint as ckpt, journal
from tests.helpers import call, fake_backend, git, git_folder, step, temp_config


def summary(i, **over):
    return {"run_id": f"r{i}", "task": f"task {i}", "status": "success", "answer": f"done {i}", "error": "",
            "outputs": [f"f{i}.txt"], "checkpoint": f"refs/mini/r{i}", **over}


class ExcludeTest(unittest.TestCase):
    def test_exclude_is_written_once_and_hides_mini_from_git(self):
        d = git_folder({"a.txt": "a\n"})
        self.assertTrue(journal.ensure_excluded(d))
        self.assertTrue(journal.ensure_excluded(d))
        exclude = open(os.path.join(d, ".git", "info", "exclude")).read().splitlines()
        self.assertEqual(exclude.count("/.mini/"), 1)
        os.makedirs(os.path.join(d, ".mini", "in"))
        open(os.path.join(d, ".mini", "in", "x.pdf"), "w").write("x")
        self.assertNotIn(".mini", git(d, "status", "--porcelain"))

    def test_a_subfolder_of_a_repo_is_anchored_to_it(self):
        d = git_folder({"sub/a.txt": "a\n"})
        sub = os.path.join(d, "sub")
        journal.ensure_excluded(sub)
        os.makedirs(os.path.join(sub, ".mini"))
        open(os.path.join(sub, ".mini", "journal.jsonl"), "w").write("{}\n")
        self.assertIn("/sub/.mini/", open(os.path.join(d, ".git", "info", "exclude")).read())
        self.assertEqual(git(d, "status", "--porcelain"), "")

    def test_not_a_repo_is_left_alone(self):
        import tempfile
        d = tempfile.mkdtemp(prefix="mini-plain-")
        self.assertFalse(journal.ensure_excluded(d))

    def test_checkpoints_ignore_the_mini_folder(self):
        d = git_folder({"a.txt": "a\n"})
        journal.ensure_excluded(d)
        os.makedirs(os.path.join(d, ".mini"))
        open(os.path.join(d, ".mini", "note"), "w").write("n")
        cp = ckpt.Checkpoint(d, "r1", "t")
        cp.before_change(["a.txt"])
        self.assertNotIn(".mini", git(d, "ls-tree", "-r", "--name-only", cp.before))


class JournalTest(unittest.TestCase):
    def setUp(self):
        self.d = git_folder({"a.txt": "a\n"})

    def test_record_and_read_back(self):
        journal.record(self.d, summary(1))
        journal.record(self.d, summary(2, status="failed", error="boom", answer=""))
        es = journal.entries(self.d)
        self.assertEqual([e["run"] for e in es], ["r1", "r2"])
        self.assertEqual(es[1]["result"], "boom")
        self.assertEqual(es[0]["files"], ["f1.txt"])
        self.assertEqual(es[0]["log"], os.path.join(".mini", "runs", "r1"))

    def test_summary_is_capped_and_most_recent_first(self):
        for i in range(30):
            journal.record(self.d, summary(i, answer="x" * 5000, task="t" * 900))
        text = journal.summary_text(self.d)
        self.assertLessEqual(len(text), journal.MAX_CHARS)
        self.assertLessEqual(text.count("\n- "), journal.MAX_ENTRIES)
        self.assertIn("t" * 10, text)
        lines = text.splitlines()[1:]
        self.assertTrue(lines and all(len(x) < 700 for x in lines))

    def test_order(self):
        for i in range(3):
            journal.record(self.d, summary(i))
        text = journal.summary_text(self.d)
        self.assertLess(text.index("task 2"), text.index("task 0"))

    def test_empty_and_damaged_journal(self):
        self.assertEqual(journal.summary_text(self.d), "")
        os.makedirs(os.path.join(self.d, ".mini"))
        with open(os.path.join(self.d, ".mini", "journal.jsonl"), "w") as f:
            f.write("not json\n" + json.dumps(summary(1) | {"time": "t"}) + "\n")
        self.assertEqual(len(journal.entries(self.d)), 1)


class AgentTest(unittest.TestCase):
    def run_agent(self, folder, task, answer="all good"):
        agent = Agent(temp_config(), backend=fake_backend([step(call("finish", answer=answer))] * 20),
                      label="j", workspace=folder)
        return agent, agent.run_task(task)

    def test_run_logs_and_journal_live_in_the_folder_and_feed_the_next_task(self):
        d = git_folder({"a.txt": "a\n"})
        agent, s = self.run_agent(d, "summarise a.txt")
        self.assertTrue(agent.run_dir.startswith(os.path.join(d, ".mini", "runs")))
        self.assertTrue(os.path.exists(os.path.join(agent.run_dir, "log.jsonl")))
        es = journal.entries(d)
        self.assertEqual(len(es), 1)
        self.assertEqual((es[0]["run"], es[0]["task"]), (s["run_id"], "summarise a.txt"))
        self.assertEqual(git(d, "status", "--porcelain"), "", "the journal and logs are invisible to git")
        ctx = Agent(temp_config(), label="next", workspace=d).context()
        self.assertIn("Earlier tasks in this folder", ctx)
        self.assertIn("summarise a.txt", ctx)

    def test_no_folder_no_journal(self):
        self.assertNotIn("Earlier tasks", Agent(temp_config(), label="none").context())


if __name__ == "__main__":
    unittest.main()
