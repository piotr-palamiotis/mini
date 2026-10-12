"""Setup and doctor without touching the real system: a fake gateway (local HTTP server), a scripted UI, units
and the env file in temp dirs."""
import json
import os
import stat
import tempfile
import threading
import tomllib
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from mini import setup
from mini.harness import config as config_mod
from tests.helpers import temp_config

KEY = "k-123"


class Gateway(BaseHTTPRequestHandler):
    models = ["mini-free"]

    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer " + KEY:
            self.send_response(401)
            self.end_headers()
            return
        body = json.dumps({"data": [{"id": m} for m in self.models]}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class ScriptUI:
    def __init__(self, yes=(), text=(), secret=(), choice=()):
        self._yes, self._text, self._secret, self._choice = list(yes), list(text), list(secret), list(choice)
        self.said = []

    def say(self, t):
        self.said.append(t)

    def yes(self, prompt, default=True):
        return self._yes.pop(0) if self._yes else default

    def text(self, prompt, default=None):
        return self._text.pop(0) if self._text else default

    def secret(self, prompt):
        return self._secret.pop(0)

    def choice(self, prompt, options, default=None):
        return self._choice.pop(0) if self._choice else default


class SetupTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Gateway)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_port}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mini-setup-")
        self.cfg = temp_config()
        self.cfg.path = os.path.join(self.tmp, "mini.toml")
        open(self.cfg.path, "w").write('[free]\nenabled = true   # keep me\nmodel = "mini-free"\n\n[paid]\nenabled = true\n')
        for t in ("free", "paid"):
            self.cfg[t].update(enabled=True, url=self.url, api_key_env="MINI_TEST_GW_KEY")
        self.env = mock.patch.object(config_mod, "ENV_FILE", os.path.join(self.tmp, "env"))
        self.env.start()
        os.environ.pop("MINI_TEST_GW_KEY", None)

    def tearDown(self):
        self.env.stop()
        os.environ.pop("MINI_TEST_GW_KEY", None)

    def test_set_values_keeps_comments(self):
        config_mod.set_values(self.cfg.path, {"free": {"enabled": False, "extra": 2}, "routing": {"start": "local"}})
        text = open(self.cfg.path).read()
        self.assertIn("enabled = false   # keep me", text)
        self.assertEqual(tomllib.loads(text)["free"]["extra"], 2)
        self.assertEqual(tomllib.loads(text)["routing"]["start"], "local")

    def test_tier_status(self):
        st = setup.tier_status(self.cfg)
        self.assertEqual(st["free"], (False, "API key missing or wrong"))
        os.environ["MINI_TEST_GW_KEY"] = KEY
        st = setup.tier_status(self.cfg)
        self.assertEqual(st["free"], (True, "mini-free"))
        self.assertEqual(st["paid"], (False, "no model or combo named mini-paid"))
        self.cfg["paid"]["enabled"] = False
        self.assertEqual(setup.tier_status(self.cfg)["paid"], (None, "off"))
        self.cfg["free"]["url"] = "http://127.0.0.1:9/v1"
        self.assertEqual(setup.tier_status(self.cfg)["free"], (False, "gateway not running"))

    def test_gateway_key_is_checked_and_saved_privately(self):
        with mock.patch.object(setup, "ENV_FILE", os.path.join(self.tmp, "env")):
            with self.assertRaises(setup.SetupError):
                setup.set_gateway_key(self.cfg, ScriptUI(secret=["wrong"]))
            setup.set_gateway_key(self.cfg, ScriptUI(secret=[KEY]))
        p = os.path.join(self.tmp, "env")
        self.assertEqual(open(p).read(), f"MINI_TEST_GW_KEY={KEY}\n")
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        os.environ.pop("MINI_TEST_GW_KEY")
        self.assertEqual(config_mod.secret("MINI_TEST_GW_KEY"), KEY, "mini reads it back from the env file")

    def test_missing_combo_can_be_renamed_or_turned_off(self):
        setup.setup_combo(self.cfg, "paid", ScriptUI(choice=["use another model or combo name for paid"], text=["other"]))
        self.assertEqual(self.cfg["paid"]["model"], "other")
        setup.setup_combo(self.cfg, "paid", ScriptUI(choice=["turn the paid tier off"]))
        self.assertFalse(tomllib.load(open(self.cfg.path, "rb"))["paid"]["enabled"])

    def test_foreign_unit_is_not_replaced_without_a_yes(self):
        units = os.path.join(self.tmp, "units")
        os.makedirs(units)
        open(os.path.join(units, "x.service"), "w").write("[Service]\nExecStart=/bin/true\n")
        with mock.patch.object(setup, "UNIT_DIR", units), mock.patch.object(setup, "_run") as run:
            with self.assertRaises(setup.SetupError):
                setup.write_unit("x", "d", "/bin/false", {}, ScriptUI(yes=[False]))
            self.assertEqual(open(os.path.join(units, "x.service")).read(), "[Service]\nExecStart=/bin/true\n")
            run.assert_not_called()
            setup.write_unit("x", "d", "/bin/false", {"A": "1"}, ScriptUI(yes=[True]))
            text = open(os.path.join(units, "x.service")).read()
            self.assertIn(setup.UNIT_MARK, text)
            self.assertIn("Environment=A=1", text)
            self.assertTrue(os.path.exists(os.path.join(units, "x.service.bak")))
            setup.write_unit("x", "d", "/bin/false", {}, ScriptUI())          # ours now: no question
            self.assertIn(["systemctl", "--user", "enable", "--now", "x"], [c.args[0] for c in run.call_args_list])

    def test_wizard_fixes_rechecks_and_records(self):
        state = {"fixed": False}

        def fix(ui):
            state["fixed"] = True

        def fake_checks(cfg, ui):
            return [setup.Check("a", "thing a", state["fixed"], "broken", fix=fix, fix_text="repair a"),
                    setup.Check("b", "thing b", False, "needs root", manual="sudo do-b"),
                    setup.Check("c", "thing c", False, "optional", fix=fix, fix_text="repair c")]
        ui = ScriptUI(yes=[False, True, False])      # cloud: no; fix a: yes; fix c: no
        with mock.patch.object(setup, "checks", fake_checks):
            self.assertTrue(setup.needs_setup(self.cfg))
            rc = setup.wizard(self.cfg, ui)
        self.assertEqual(rc, 1, "b and c are still open")
        self.assertTrue(state["fixed"])
        self.assertTrue(any("sudo do-b" in s for s in ui.said))
        self.assertFalse(setup.needs_setup(self.cfg))
        self.assertEqual(json.load(open(setup.marker(self.cfg)))["open"], ["b", "c"])
        self.assertFalse(tomllib.load(open(self.cfg.path, "rb"))["free"]["enabled"], "no cloud: tiers turned off")


if __name__ == "__main__":
    unittest.main()
