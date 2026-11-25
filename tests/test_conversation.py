"""A message that is conversation, not a task ("hi", "thanks", a question about the last run), is answered with
`reply`: no plan to confirm, no gap step, nothing run. A message with files attached is always a task."""
import json
import os
import tempfile
import unittest

from mini.agent.core import Agent
from tests.helpers import call, fake_backend, step, temp_config


class ConversationTest(unittest.TestCase):
    def agent(self, script, asked):
        cfg = temp_config()
        cfg["approvals"]["mode"] = "ask"                      # the confirm card would be shown

        def handler(kind, title, body, options, **kw):
            asked.append(kind)
            raise AssertionError(f"asked {kind}: {title}")
        return Agent(cfg, approval_handler=handler, backend=fake_backend(script))

    def test_a_greeting_is_answered_without_a_plan(self):
        asked = []
        a = self.agent([step(call("reply", text="Hi! What can I do for you?"))], asked)
        s = a.run_task("hi")
        self.assertEqual((s["status"], s["answer"], asked), ("success", "Hi! What can I do for you?", []))
        with open(os.path.join(a.run_dir, "log.jsonl")) as f:
            phases = [json.loads(ln).get("phase") for ln in f if '"phase"' in ln]
        self.assertIn("conversation", phases)
        self.assertNotIn("gap_detection", phases, "nothing after the reply")

    def test_an_empty_reply_or_one_with_files_is_sent_back(self):
        script = [step(call("reply", text="")),
                  step(call("reply", text="looks fine")),
                  step(call("understand", goal="g", inputs=["a.txt"], outputs=["n"], plan="1")),
                  step(call("decide_coverage", reasoning="none", use=[], missing=[])),
                  step(call("finish", answer="done"))]
        cfg = temp_config()
        a = Agent(cfg, backend=fake_backend(script))
        f = os.path.join(tempfile.mkdtemp(), "a.txt")
        open(f, "w").write("x\n")
        s = a.run_task("what is in this file?", attachments=[f])
        self.assertEqual((s["status"], s["answer"]), ("success", "done"))


if __name__ == "__main__":
    unittest.main()
