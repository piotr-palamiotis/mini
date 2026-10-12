"""Textual UI driven headlessly: a task runs with a scripted model, approval dialogs appear and are answered,
the attempt counter and registry browser update, rollback works. Needs podman (the tool tests really run)."""
import asyncio
import os
import tempfile
import unittest

from mini.harness.registry import Registry
from mini.ui.app import ApprovalScreen, MiniApp
from tests.helpers import call, fake_backend, step, temp_config
from tests.test_pipeline import script


SHOTS = os.environ.get("MINI_SHOTS", tempfile.mkdtemp(prefix="mini-ui-"))


class UITest(unittest.TestCase):
    def test_task_with_dialogs(self):
        cfg = temp_config(**{"approvals.mode": "ask", "approvals.confirm_first": True})
        cfg.path = os.path.join(tempfile.mkdtemp(), "mini.toml")
        app = MiniApp(cfg, backend_factory=lambda: fake_backend(script()))
        seen, handled = [], set()

        async def go():
            async with app.run_test(size=(180, 50)) as pilot:
                src = os.path.join(tempfile.mkdtemp(), "a.txt")
                open(src, "w").write("x\ny\n")
                await pilot.press(*f"/attach {src}")
                await pilot.press("enter")
                await pilot.press(*"How many lines does a.txt have?")
                await pilot.press("enter")
                for _ in range(600):
                    await pilot.pause(0.1)
                    if isinstance(app.screen, ApprovalScreen) and id(app.screen) not in handled:
                        handled.add(id(app.screen))
                        seen.append(app.screen.kind)
                        if app.screen.kind in ("confirm", "install"):
                            app.save_screenshot(os.path.join(SHOTS, f"mini-ui-{app.screen.kind}.svg"))
                        await pilot.click("#opt-0")
                    if not app.busy and app.agent and seen:
                        break
                app.save_screenshot(os.path.join(SHOTS, "mini-ui-done.svg"))
                self.assertIn("attempt 2/10", str(app.query_one("#status").render()))
                reg = Registry(cfg.abspath("state"))
                self.assertEqual(reg.names(), ["count_text_lines"])
                self.assertEqual(app.query_one("#reg-table").row_count, 1)
        asyncio.run(go())
        self.assertEqual(seen, ["confirm", "install"])

    def test_model_command_paid_lasts_one_task(self):
        cfg = temp_config(**{"approvals.confirm_first": False, "paid.enabled": True})
        cfg.path = os.path.join(tempfile.mkdtemp(), "mini.toml")
        quick = lambda: fake_backend([step(call("understand", goal="g", inputs=[], outputs=["n"], plan="1")),
                                      step(call("decide_coverage", reasoning="none needed", use=[], missing=[])),
                                      step(call("finish", answer="done"))])
        app = MiniApp(cfg, backend_factory=quick)
        used = []

        async def go():
            async with app.run_test(size=(180, 50)) as pilot:
                await pilot.press(*"/model paid")
                await pilot.press("enter")
                self.assertIn("paid", str(app.query_one("#status").render()))
                for _ in range(2):
                    await pilot.press(*"say done")
                    await pilot.press("enter")
                    for _ in range(100):
                        await pilot.pause(0.1)
                        if not app.busy:
                            break
                    used.append(app.agent.start_tier)
                self.assertIn("local", str(app.query_one("#status").render()))
        asyncio.run(go())
        self.assertEqual(used, ["paid", "local"])


if __name__ == "__main__":
    unittest.main()
