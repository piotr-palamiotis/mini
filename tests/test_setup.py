"""Setup and doctor without touching the real system: a fake llama-server (local HTTP server), a scripted UI and
units in temp dirs."""
import json
import os
import tempfile
import threading
import tomllib
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from mini import setup
from mini.harness import config as config_mod
from tests.helpers import temp_config

class FakeLlama(BaseHTTPRequestHandler):
    models = ["good"]

    def do_GET(self):
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
        cls.srv = HTTPServer(("127.0.0.1", 0), FakeLlama)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_port}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mini-setup-")
        self.cfg = temp_config()
        self.cfg.path = os.path.join(self.tmp, "mini.toml")
        open(self.cfg.path, "w").write('[llamacpp]\nctx = 4096   # keep me\n')

    def test_set_values_keeps_comments(self):
        config_mod.set_values(self.cfg.path, {"llamacpp": {"ctx": 8192, "extra": 2}, "routing": {"start": "local"}})
        text = open(self.cfg.path).read()
        self.assertIn("ctx = 8192   # keep me", text)
        self.assertEqual(tomllib.loads(text)["llamacpp"]["extra"], 2)
        self.assertEqual(tomllib.loads(text)["routing"]["start"], "local")

    def test_local_status(self):
        self.cfg["local"].update(backend="openai", url=self.url, model="good")
        self.assertEqual(setup.local_status(self.cfg), (True, "good"))
        self.cfg["llamacpp"]["preset"] = "p"
        self.cfg["local"]["model"] = "missing"
        self.assertEqual(setup.local_status(self.cfg), (False, "missing (preset p): not in the models folder"))
        self.cfg["local"]["url"] = "http://127.0.0.1:9/v1"
        self.assertEqual(setup.local_status(self.cfg)[1], "missing (preset p): llama-server is not running")

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
        ui = ScriptUI(yes=[True, False])             # fix a: yes; fix c: no
        with mock.patch.object(setup, "checks", fake_checks):
            self.assertTrue(setup.needs_setup(self.cfg))
            rc = setup.wizard(self.cfg, ui)
        self.assertEqual(rc, 1, "b and c are still open")
        self.assertTrue(state["fixed"])
        self.assertTrue(any("sudo do-b" in s for s in ui.said))
        self.assertFalse(setup.needs_setup(self.cfg))
        self.assertEqual(json.load(open(setup.marker(self.cfg)))["open"], ["b", "c"])

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
        self.cfg["local"].update(backend="openai", url="http://127.0.0.1:9/v1", model="good")
        cs = setup.llama_checks(self.cfg)
        self.assertEqual([c.key for c in cs], ["llama_install"])
        self.assertIn(setup.LLAMA_TAG, cs[0].fix_text)
        with mock.patch.object(setup, "llama_server", return_value="/x/llama-server"), \
                mock.patch.object(setup, "llama_commit", return_value="0000000"), \
                mock.patch.object(setup, "unit_state", return_value=(False, False, False, False)):
            cs = {c.key: c for c in setup.llama_checks(self.cfg)}
            self.assertFalse(cs["llama_version"].ok)
            self.assertIn("0000000", cs["llama_version"].detail)
            self.assertFalse(cs["models"].ok)
            self.assertIn("scripts/ollama-export", cs["models"].manual)
            self.gguf(os.path.join(d, "good.gguf"), "knownarch")
            self.gguf(os.path.join(d, "odd.gguf"), "newarch")
            with mock.patch.object(setup, "llama_supports", side_effect=lambda srv, a: a == "knownarch"):
                cs = {c.key: c for c in setup.llama_checks(self.cfg)}
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

    def test_preset_overlays_llamacpp_settings(self):
        self.cfg["llamacpp"].update(models_dir="/m", device="Vulkan0", port=8080, ctx=4096, models_max=1, extra_args="--jinja")
        setup.save_preset(self.cfg, "rtx", {"gpu": "cuda", "device": "CUDA0", "model": "g", "ctx": 8192})
        self.assertEqual(self.cfg["presets"]["rtx"], {"gpu": "cuda", "device": "CUDA0", "model": "g", "ctx": 8192})
        self.assertEqual(tomllib.loads(open(self.cfg.path).read())["presets"]["rtx"]["device"], "CUDA0")
        self.cfg["llamacpp"]["preset"] = "rtx"
        self.assertEqual(setup.router_args(self.cfg, "/b/llama-server"),
                         "/b/llama-server --models-dir /m --models-max 1 --host 127.0.0.1 --port 8080 "
                         "--device CUDA0 -ngl 999 -c 8192 --jinja --no-webui")
        self.assertEqual(setup.llama_gpu(self.cfg), "cuda")
        self.assertTrue(setup.llama_build_dir(self.cfg).endswith("build-cuda"))
        self.assertTrue(setup.llama_build_dir(self.cfg, "vulkan").endswith("/build"))

    def test_preset_rejects_bad_input(self):
        for name, vals in (("bad name", {}), ("ok", {"gpu": "metal"})):
            with self.assertRaises(setup.SetupError):
                setup.save_preset(self.cfg, name, vals)

    def test_use_preset_restores_the_old_one_when_it_fails(self):
        setup.save_preset(self.cfg, "a", {"gpu": "cuda"})
        with mock.patch.object(setup, "llama_server", return_value="/x"), \
                mock.patch.object(setup, "setup_router", side_effect=setup.SetupError("boom")):
            with self.assertRaises(setup.SetupError):
                setup.use_preset(self.cfg, "a", ScriptUI())
        self.assertEqual(self.cfg["llamacpp"]["preset"], "")
        self.assertEqual(tomllib.loads(open(self.cfg.path).read())["llamacpp"]["preset"], "")
        with self.assertRaises(setup.SetupError):
            setup.use_preset(self.cfg, "nope", ScriptUI())

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

    def test_the_active_presets_model_is_the_model(self):
        from mini.agent.core import Agent
        path = os.path.join(self.tmp, "one.toml")
        open(path, "w").write('[local]\nmodel = "plain"\n\n[llamacpp]\nport = 8099\npreset = "big"\n\n'
                              '[presets.big]\nmodel = "big-model"\nctx = 1024\n')
        cfg = config_mod.load(path)
        self.assertEqual((cfg["local"]["model"], cfg["local"]["url"]), ("big-model", "http://127.0.0.1:8099/v1"))
        self.assertEqual(Agent(cfg, label="one").model, "big-model", "what a task asks the server for")
        cfg["llamacpp"]["preset"] = ""
        self.assertEqual(config_mod.reload(cfg)["local"]["model"], "big-model", "reload reads the file again")
        open(path, "w").write('[local]\nmodel = "plain"\n')
        self.assertEqual(config_mod.load(path)["local"]["model"], "plain", "no preset: [local] model")

    def test_choose_model_saves_to_the_active_preset(self):
        self.gguf(os.path.join(self.tmp, "models", "good.gguf"), "a")
        self.cfg["llamacpp"].update(models_dir=os.path.join(self.tmp, "models"), preset="p")
        self.cfg["presets"] = {"p": {"model": "old"}}
        self.cfg["local"].update(url=self.url, model="old")
        with mock.patch.object(setup, "load_test", return_value=(True, "")):
            setup.choose_model(self.cfg, ScriptUI(choice=["good"]))
        data = tomllib.loads(open(self.cfg.path).read())
        self.assertEqual((data["presets"]["p"]["model"], self.cfg["local"]["model"]), ("good", "good"))
        self.assertNotIn("model", data.get("local", {}))

    def test_use_preset_points_mini_at_its_model(self):
        setup.save_preset(self.cfg, "a", {"gpu": "cuda", "model": "m-a"})
        with mock.patch.object(setup, "llama_server", return_value="/x"), mock.patch.object(setup, "setup_router"):
            setup.use_preset(self.cfg, "a", setup.QuietUI(lambda m: None))
        self.assertEqual((self.cfg["llamacpp"]["preset"], self.cfg["local"]["model"]), ("a", "m-a"))


class PresetMakerTest(unittest.TestCase):
    """GPUs as physical cards, the backend they share, and the preset maker (a model + GPUs -> a preset)."""
    DEVICES = [("CUDA0", "NVIDIA GeForce RTX 3070", 7893), ("CUDA1", "NVIDIA GeForce RTX 3070", 7893),
               ("Vulkan0", "AMD Radeon RX 6700 XT (RADV NAVI22)", 12272), ("Vulkan1", "NVIDIA GeForce RTX 3070", 8438),
               ("Vulkan2", "NVIDIA GeForce RTX 3070", 8438), ("Vulkan3", "Intel(R) HD Graphics 610 (KBL GT1)", 5890)]

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mini-pm-")
        self.cfg = temp_config()
        self.cfg.path = os.path.join(self.tmp, "mini.toml")
        self.cfg["llamacpp"].update(models_dir=os.path.join(self.tmp, "models"), ctx=4096, extra_args="--jinja")
        os.makedirs(self.cfg["llamacpp"]["models_dir"])
        for name, size in (("small", 1 << 20), ("big", 20 << 30)):
            with open(os.path.join(self.cfg["llamacpp"]["models_dir"], name + ".gguf"), "wb") as f:
                f.truncate(size)                                   # sparse: the size counts, not the bytes
        self.cards = setup.gpu_cards(self.DEVICES)

    def maker(self, ui, **kw):
        with mock.patch.object(setup, "listing_server", return_value=("cuda", "/b/llama-server")), \
                mock.patch.object(setup, "gpu_devices", return_value=self.DEVICES):
            return setup.new_preset(self.cfg, ui, **kw)

    def test_devices_become_cards(self):
        self.assertEqual([(c["label"][:20], c["cuda"], c["vulkan"], c["integrated"]) for c in self.cards], [
            ("AMD Radeon RX 6700 X", None, "Vulkan0", False), ("NVIDIA GeForce RTX 3", "CUDA0", "Vulkan1", False),
            ("NVIDIA GeForce RTX 3", "CUDA1", "Vulkan2", False), ("Intel(R) HD Graphics", None, "Vulkan3", True)])

    def test_the_backend_the_cards_share(self):
        amd, n1, n2, intel = self.cards
        self.assertEqual(setup.gpu_plan([n1, n2])[:3], ("cuda", "CUDA0,CUDA1", ""), "NVIDIA only: CUDA, even split")
        backend, device, split, note = setup.gpu_plan([amd, n1])
        self.assertEqual((backend, device, split), ("vulkan", "Vulkan0,Vulkan1", "1.55,1"), "split by VRAM")
        self.assertIn("no CUDA", note)
        self.assertEqual(setup.gpu_plan([])[:2], ("cpu", ""))

    def test_pick_a_model_and_gpus(self):
        ui = ScriptUI(choice=["small  (0.0 GB)"], checklist=[[setup.card_label(self.cards[1]), setup.card_label(self.cards[2])]],
                      text=["8192", "two"], yes=[False])
        self.assertEqual(self.maker(ui), "two")
        p = tomllib.loads(open(self.cfg.path).read())["presets"]["two"]
        self.assertEqual(p, {"gpu": "cuda", "device": "CUDA0,CUDA1", "model": "small", "ctx": 8192,
                             "extra_args": "--jinja --split-mode layer"})
        self.assertEqual(self.cfg["llamacpp"]["preset"], "", "saved, not switched to: the user said no")

    def test_the_nvidia_cards_are_ticked_first_and_the_integrated_never(self):
        seen = []

        class UI(ScriptUI):
            def checklist(self, prompt, options, checked=()):
                seen.append(list(checked))
                return list(checked)
        self.maker(UI(text=["4096", "x"], yes=[False]), model="small")
        self.assertEqual(seen, [[setup.card_label(c) for c in self.cards[1:3]]])

    def test_a_model_too_big_for_the_cards_asks_before_saving(self):
        ui = ScriptUI(checklist=[[setup.card_label(self.cards[1])]], text=["4096", "nope"], yes=[False])
        with self.assertRaises(setup.SetupError):
            self.maker(ui, model="big")
        self.assertTrue(any("will not fit" in t for t in ui.said))
        self.assertNotIn("nope", self.cfg.get("presets", {}))
        name = self.maker(ScriptUI(), model="big", cards=[0, 1, 2], ctx=4096, name="all", use=False)   # flags: no question
        self.assertEqual(self.cfg["presets"][name]["device"], "Vulkan0,Vulkan1,Vulkan2")


if __name__ == "__main__":
    unittest.main()


class EnsureLocalTest(unittest.TestCase):
    def cfg(self):
        from mini.harness import config as config_mod
        return config_mod.load(None)

    def test_starts_an_installed_stopped_unit(self):
        from unittest import mock
        from mini import setup
        calls, said = [], []
        up = iter([(0, None), (200, {})])
        with mock.patch.object(setup, "http_json", lambda *a, **k: next(up)), \
             mock.patch.object(setup, "unit_state", lambda n: (True, False, True, True)), \
             mock.patch.object(setup, "sh", lambda cmd, **k: calls.append(cmd) or setup.subprocess.CompletedProcess(cmd, 0, "", "")):
            self.assertTrue(setup.ensure_local(self.cfg(), said.append, wait_s=5))
        self.assertIn(["systemctl", "--user", "start", "llama-server"], calls)

    def test_leaves_a_unit_mini_did_not_write(self):
        from unittest import mock
        from mini import setup
        with mock.patch.object(setup, "http_json", lambda *a, **k: (0, None)), \
             mock.patch.object(setup, "unit_state", lambda n: (True, False, True, False)), \
             mock.patch.object(setup, "sh", side_effect=AssertionError("must not start")):
            self.assertFalse(setup.ensure_local(self.cfg(), lambda m: None))
