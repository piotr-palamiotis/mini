"""A new task is told about the earlier tasks of the conversation, so "retry" has something to refer to."""
import asyncio
import os
import tempfile
import unittest

from mini.agent.core import Agent
from tests.helpers import temp_config


def past(n, **over):
    return {"task": f"task {n}", "status": "success", "answer": f"answer {n}", "error": "", "created": [], "outputs": [], **over}


class HistoryTest(unittest.TestCase):
    def test_context_lists_earlier_tasks(self):
        cfg = temp_config()
        h = [past(1, status="stopped", answer="", error="swap grew by 349 MiB; run aborted", created=["list_files"])]
        ctx = Agent(cfg, history=h).context()
        self.assertIn("Earlier in this conversation", ctx)
        self.assertIn("the user asked: task 1 -> stopped; error: swap grew by 349 MiB; run aborted; tools built: ['list_files']", ctx)
        self.assertNotIn("Earlier in this conversation", Agent(cfg).context(), "a first task has no history")

    def test_context_is_bounded(self):
        cfg = temp_config()
        ctx = Agent(cfg, history=[past(i, answer="x" * 5000) for i in range(10)]).context()
        self.assertNotIn("task 3 ", ctx)
        self.assertIn("task 4 ", ctx)
        self.assertIn("task 9 ", ctx)
        self.assertLess(len(ctx), 6 * 1000)

    def test_ui_remembers_and_forgets(self):
        from mini.ui.app import MiniApp
        cfg = temp_config()
        cfg.path = os.path.join(tempfile.mkdtemp(), "mini.toml")
        app = MiniApp(cfg, backend_factory=lambda: None)

        async def go():
            async with app.run_test(size=(120, 40)) as pilot:
                app.on_agent_event({"kind": "run_end", "t": 1.0, "status": "stopped", "task": "list files", "error": "boom",
                                    "answer": "", "created": [], "outputs": [], "secs": 1, "run_id": "r1"})
                self.assertEqual([h["task"] for h in app.history], ["list files"])
                await pilot.press(*"/new")
                await pilot.press("enter")
                self.assertEqual(app.history, [])
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
