"""The shared config/mini.toml with this machine's ~/.config/mini/mini.toml laid over it; every change mini makes
goes to the machine file. One file named by --config / $MINI_CONFIG is read alone."""
import os
import tempfile
import tomllib
import unittest
from unittest import mock

from mini.harness import config as config_mod


class LayersTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp(prefix="mini-layers-")
        self.shared, self.user = os.path.join(d, "shared.toml"), os.path.join(d, "home", "mini", "mini.toml")
        open(self.shared, "w").write('[local]\nmodel = "shared"\ntemperature = 0.5\n\n[caps]\ntask_turns = 7\n')
        self.patch = mock.patch.multiple(config_mod, SHARED_FILE=self.shared, USER_FILE=self.user)
        self.patch.start()
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop("MINI_CONFIG", None)

    def tearDown(self):
        self.env.stop()
        self.patch.stop()

    def test_machine_file_is_laid_over_the_shared_one(self):
        os.makedirs(os.path.dirname(self.user))
        open(self.user, "w").write('[local]\nmodel = "mine"\n\n[free]\nmodel = "f"\n')
        cfg = config_mod.load()
        self.assertEqual((cfg["local"]["model"], cfg["local"]["temperature"], cfg["caps"]["task_turns"]), ("mine", 0.5, 7))
        self.assertTrue(cfg["free"]["enabled"], "a tier configured in the machine file is on")
        self.assertFalse(cfg["paid"]["enabled"])
        self.assertEqual(cfg.path, self.user)

    def test_changes_go_to_the_machine_file_only(self):
        cfg = config_mod.load()
        self.assertEqual(cfg["local"]["model"], "shared", "no machine file yet")
        config_mod.set_values(cfg.path, {"llamacpp": {"preset": "x"}})
        text = open(self.user).read()
        self.assertTrue(text.startswith("# mini settings for this machine"))
        self.assertEqual(tomllib.loads(text), {"llamacpp": {"preset": "x"}})
        self.assertNotIn("preset", open(self.shared).read())
        self.assertEqual(config_mod.reload(cfg)["llamacpp"]["preset"], "x")

    def test_one_named_file_is_read_alone(self):
        os.makedirs(os.path.dirname(self.user))
        open(self.user, "w").write('[local]\nmodel = "mine"\n')
        named = os.path.join(os.path.dirname(self.shared), "bench.toml")
        open(named, "w").write('[local]\nmodel = "bench"\n')
        for cfg in (config_mod.load(named), self._env_load(named)):
            self.assertEqual((cfg["local"]["model"], cfg["caps"]["task_turns"]), ("bench", 30), "no shared or machine file")
            self.assertEqual((cfg.path, cfg.source), (named, named))
            self.assertEqual(config_mod.reload(cfg)["local"]["model"], "bench")

    def _env_load(self, path):
        with mock.patch.dict(os.environ, {"MINI_CONFIG": path}):
            return config_mod.load()


if __name__ == "__main__":
    unittest.main()
