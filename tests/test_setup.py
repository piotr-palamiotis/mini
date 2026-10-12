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
    def __init__(self, yes=(), text=(), secret=(), choice=(), checklist=()):
        self._yes, self._text, self._secret, self._choice = list(yes), list(text), list(secret), list(choice)
        self._checklist = list(checklist)
        self.said = []

    def checklist(self, prompt, options, checked=()):
        return self._checklist.pop(0) if self._checklist else list(checked)

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

    def test_gguf_header(self):
        import struct
        from mini.harness.gguf import architecture, metadata
        p = os.path.join(self.tmp, "m.gguf")

        def kv_str(k, v):
            return struct.pack("<Q", len(k)) + k.encode() + struct.pack("<I", 8) + struct.pack("<Q", len(v)) + v.encode()
        arr = struct.pack("<Q", 3) + b"a.b" + struct.pack("<I", 9) + struct.pack("<IQ", 4, 2) + struct.pack("<II", 1, 2)
        with open(p, "wb") as f:
            f.write(b"GGUF" + struct.pack("<IQQ", 3, 0, 3) + arr + kv_str("general.architecture", "testarch")
                    + kv_str("general.name", "N"))
        self.assertEqual(architecture(p), "testarch")
        self.assertEqual(metadata(p, ("general.name",)), {"general.name": "N"})

    def test_backend_checklist_and_order(self):
        self.cfg["local"].update(model="m1", use=[])
        self.cfg["backends"] = {}
        ui = ScriptUI(checklist=[[setup.BACKENDS["llamacpp"], setup.BACKENDS["custom"]]], choice=[setup.BACKENDS["custom"]])
        setup.choose_backends(self.cfg, ui)
        data = tomllib.load(open(self.cfg.path, "rb"))
        self.assertEqual(data["local"]["use"], ["custom", "llamacpp"])
        self.assertEqual(data["backends"]["llamacpp"], {"backend": "openai", "url": "http://127.0.0.1:8080/v1", "model": "m1"})
        self.assertNotIn("ollama", setup.BACKENDS)

    def gguf(self, path, arch):
        import struct
        os.makedirs(os.path.dirname(path), exist_ok=True)
        k, v = "general.architecture", arch
        with open(path, "wb") as f:
            f.write(b"GGUF" + struct.pack("<IQQ", 3, 0, 1) + struct.pack("<Q", len(k)) + k.encode()
                    + struct.pack("<I", 8) + struct.pack("<Q", len(v)) + v.encode())

    def test_models_folder_layout(self):
        d = os.path.join(self.tmp, "models")
        self.gguf(os.path.join(d, "plain.gguf"), "a")
        self.gguf(os.path.join(d, "vis", "vis.gguf"), "b")
        self.gguf(os.path.join(d, "vis", "mmproj-vis.gguf"), "clip")
        self.gguf(os.path.join(d, "mmproj-stray.gguf"), "clip")
        os.makedirs(os.path.join(d, "empty"))
        m = setup.models_in(d)
        self.assertEqual(sorted(m), ["plain", "vis"])
        self.assertEqual(m["vis"]["mmproj"], os.path.join(d, "vis", "mmproj-vis.gguf"))
        self.assertIsNone(m["plain"]["mmproj"])

    def test_llama_checks(self):
        d = os.path.join(self.tmp, "models")
        self.cfg["llamacpp"].update(dir=os.path.join(self.tmp, "llama.cpp"), models_dir=d)
        entry = {"backend": "openai", "url": "http://127.0.0.1:9/v1", "model": "good"}
        cs = setup.backend_checks(self.cfg, "llamacpp", entry)
        self.assertEqual([c.key for c in cs], ["llama_install"])
        self.assertIn(setup.LLAMA_TAG, cs[0].fix_text)
        with mock.patch.object(setup, "llama_server", return_value="/x/llama-server"), \
                mock.patch.object(setup, "llama_commit", return_value="0000000"), \
                mock.patch.object(setup, "unit_state", return_value=(False, False, False, False)):
            cs = {c.key: c for c in setup.backend_checks(self.cfg, "llamacpp", entry)}
            self.assertFalse(cs["llama_version"].ok)
            self.assertIn("0000000", cs["llama_version"].detail)
            self.assertFalse(cs["models"].ok)
            self.assertIn("scripts/ollama-export", cs["models"].manual)
            self.gguf(os.path.join(d, "good.gguf"), "knownarch")
            self.gguf(os.path.join(d, "odd.gguf"), "newarch")
            with mock.patch.object(setup, "llama_supports", side_effect=lambda srv, a: a == "knownarch"):
                cs = {c.key: c for c in setup.backend_checks(self.cfg, "llamacpp", entry)}
        self.assertTrue(cs["models"].ok)
        self.assertEqual(cs["models"].detail, "good, odd")
        self.assertFalse(cs["model_arch"].ok)
        self.assertIn("odd (newarch)", cs["model_arch"].detail)
        self.assertFalse(cs["router"].ok)

    def test_router_command(self):
        self.cfg["llamacpp"].update(models_dir="/m", device="Vulkan0", port=8080, ctx=4096, models_max=1,
                                    extra_args="--jinja")
        self.assertEqual(setup.router_args(self.cfg, "/b/llama-server"),
                         "/b/llama-server --models-dir /m --models-max 1 --host 127.0.0.1 --port 8080 "
                         "--device Vulkan0 -ngl 999 -c 4096 --jinja --no-webui")

    def test_ollama_export_prints_moves(self):
        import subprocess
        store = os.path.join(self.tmp, "store")
        man = os.path.join(store, "manifests", "registry.ollama.ai", "library")
        os.makedirs(os.path.join(man, "vis"))
        os.makedirs(os.path.join(man, "txt"))
        os.makedirs(os.path.join(store, "blobs"))
        L = lambda kind, d: {"mediaType": f"application/vnd.ollama.image.{kind}", "digest": d}
        json.dump({"layers": [L("model", "sha256:aa"), L("projector", "sha256:pp"), L("draft", "sha256:dd"),
                              L("license", "sha256:ll")]}, open(os.path.join(man, "vis", "1b"), "w"))
        json.dump({"layers": [L("model", "sha256:bb"), L("projector", "sha256:pp")]}, open(os.path.join(man, "txt", "2b"), "w"))
        r = subprocess.run([os.path.join(config_mod.ROOT, "scripts", "ollama-export"), "/m", "--store", store, "--owner", "u"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = [l for l in r.stdout.splitlines() if l.startswith(("mv ", "ln ", "chown "))]
        b = os.path.join(store, "blobs")
        self.assertEqual(lines, [
            f"mv {b}/sha256-bb /m/txt-2b/txt-2b.gguf", f"mv {b}/sha256-pp /m/txt-2b/mmproj-txt-2b.gguf",
            f"mv {b}/sha256-aa /m/vis-1b/vis-1b.gguf", "ln /m/txt-2b/mmproj-txt-2b.gguf /m/vis-1b/mmproj-vis-1b.gguf",
            f"mv {b}/sha256-dd /m-drafts/vis-1b-draft.gguf", "chown -R u: /m /m-drafts"])

    def test_task_falls_back_to_the_next_backend(self):
        from mini.agent.core import Agent
        os.environ["MINI_TEST_GW_KEY"] = KEY
        self.cfg["local"]["use"] = ["llamacpp", "custom"]
        self.cfg["backends"] = {"llamacpp": {"backend": "openai", "url": "http://127.0.0.1:9/v1", "model": "x"},
                                "custom": {"backend": "openai", "url": self.url, "model": "mini-free",
                                           "api_key_env": "MINI_TEST_GW_KEY"}}
        a = Agent(self.cfg, label="fallback")
        self.assertEqual(a.local_backend, "custom")
        self.assertEqual((a.model, a.cfg["local"]["url"]), ("mini-free", self.url))
        self.assertEqual(self.cfg["local"]["use"], ["llamacpp", "custom"], "the shared config is not changed")
        st = setup.tier_status(self.cfg)
        self.assertEqual(st["local"], (True, "mini-free via custom (also: llamacpp); llamacpp is down"))


if __name__ == "__main__":
    unittest.main()
