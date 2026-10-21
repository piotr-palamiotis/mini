"""The Config tab: switches, dropdowns and fields that save in place; the raw TOML tab shows the real file."""
import asyncio
import os
import tempfile
import tomllib
import unittest

from textual.widgets import Input, Select, Static, Switch, TabbedContent, TextArea

from mini.harness import config as config_mod
from mini.ui.app import MiniApp
from mini.ui.settingsform import FIELDS, wid
from tests.helpers import temp_config

TOML = '''# my notes
[free]
enabled = true   # keep this comment
model = "mini-free"

[routing]
start = "local"
local_budget_s = 900   # local time per task
'''


def make_app():
    d = tempfile.mkdtemp(prefix="mini-form-")
    path = os.path.join(d, "mini.toml")
    open(path, "w").write(TOML)
    cfg = config_mod.load(path)
    cfg["paths"]["state"], cfg["paths"]["runs"] = os.path.join(d, "state"), os.path.join(d, "runs")
    cfg["local"]["backend"] = "fake"
    return MiniApp(cfg, backend_factory=lambda: None), path


async def open_config(app, pilot):
    app.query_one("#views", TabbedContent).active = "tab-cfg"
    await pilot.pause()


def msg(app):
    return str(app.query_one("#cfg-msg", Static).render())


class SettingsFormTest(unittest.TestCase):
    def test_every_field_is_in_the_defaults(self):
        cfg = temp_config()
        for _, fields in FIELDS:
            for section, key, *_ in fields:
                self.assertIn(key, cfg[section], f"{section}.{key}")

    def test_form_saves_changes_in_place(self):
        app, path = make_app()

        async def go():
            async with app.run_test(size=(180, 60)) as pilot:
                await open_config(app, pilot)
                self.assertTrue(app.query_one("#" + wid("free", "enabled"), Switch).value)
                self.assertEqual(app.query_one("#" + wid("routing", "local_budget_s"), Input).value, "900")
                app.query_one("#" + wid("free", "enabled"), Switch).value = False
                app.query_one("#" + wid("paid", "enabled"), Switch).value = False
                app.query_one("#" + wid("routing", "local_budget_s"), Input).value = "0"
                app.query_one("#" + wid("approvals", "mode"), Select).value = "auto"
                await pilot.click("#cfg-save-form")
                await pilot.pause()
                self.assertIn("saved", msg(app))
                self.assertFalse(app.cfg["free"]["enabled"])
                self.assertEqual(app.cfg["routing"]["local_budget_s"], 0)
                self.assertIn("auto", app.query_one("#cfg-text", TextArea).text, "the raw tab follows the file")
        asyncio.run(go())
        text = open(path).read()
        self.assertIn("# my notes", text)
        self.assertIn("enabled = false   # keep this comment", text)
        self.assertIn("local_budget_s = 0   # local time per task", text)
        data = tomllib.loads(text)
        self.assertFalse(data["paid"]["enabled"])
        self.assertEqual(data["approvals"]["mode"], "auto")
        self.assertNotIn("caps", data, "unchanged settings are not written")

    def test_bad_numbers_are_refused_and_nothing_is_written(self):
        app, path = make_app()

        async def go():
            async with app.run_test(size=(180, 60)) as pilot:
                await open_config(app, pilot)
                app.query_one("#" + wid("free", "enabled"), Switch).value = False
                app.query_one("#" + wid("caps", "task_turns"), Input).value = "lots"
                await pilot.click("#cfg-save-form")
                await pilot.pause()
                self.assertIn("not saved", msg(app))
                self.assertIn("is not valid", msg(app))
                await pilot.click("#cfg-reset")
                await pilot.pause()
                self.assertTrue(app.query_one("#" + wid("free", "enabled"), Switch).value, "reset shows the saved value")
        asyncio.run(go())
        self.assertEqual(open(path).read(), TOML)

    def test_raw_tab_shows_the_file_and_rejects_broken_toml(self):
        app, path = make_app()

        async def go():
            async with app.run_test(size=(180, 60)) as pilot:
                await open_config(app, pilot)
                raw = app.query_one("#cfg-text", TextArea)
                self.assertEqual(raw.text, TOML, "the file as written, comments included")
                raw.text = "[free\nenabled = "
                app.save_config()
                self.assertIn("not saved", msg(app))
        asyncio.run(go())
        self.assertEqual(open(path).read(), TOML)


if __name__ == "__main__":
    unittest.main()
