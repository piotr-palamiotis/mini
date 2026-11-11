"""run_code gets the packages it names: approved once per folder, kept in .mini/packages.json."""
import json
import os
import types
import unittest
from unittest import mock

from mini.agent.core import Agent
from mini.core import run_code
from tests.helpers import git_folder, temp_config


class RunCodePackagesTest(unittest.TestCase):
    def agent(self, approve=True):
        cfg = temp_config()
        if not approve:
            cfg["approvals"]["auto_approve"] = [k for k in cfg["approvals"]["auto_approve"] if k != "permissions"]
        self.folder = git_folder({"a.txt": "x\n"})
        return Agent(cfg, label="pkgs", workspace=self.folder)

    def test_new_packages_are_approved_once_and_kept_for_the_folder(self):
        a = self.agent()
        built = []
        with mock.patch.object(a.sandbox, "ensure_image", side_effect=lambda pk: built.append(pk) or a.cfg["sandbox"]["image"]):
            image, problem = run_code.ensure_packages(a, {"pip": ["pyyaml"]})
            self.assertEqual(problem, "")
            self.assertEqual(json.load(open(os.path.join(self.folder, run_code.PACKAGES_FILE))), {"apt": [], "pip": ["pyyaml"]})
            run_code.ensure_packages(a, {})                     # a later run without packages keeps the folder's image
            self.assertEqual(built[-1], {"apt": [], "pip": ["pyyaml"]})

    def test_a_quoted_key_is_read_as_the_key_and_an_unknown_one_is_refused(self):
        a = self.agent()
        with mock.patch.object(a.sandbox, "ensure_image", return_value="x"):
            self.assertEqual(run_code.ensure_packages(a, {'"pip"': ["pyyaml"]}), ("x", ""))
            self.assertEqual(run_code.project_packages(self.folder)["pip"], ["pyyaml"])
            image, problem = run_code.ensure_packages(a, {"python": ["toml"]})
        self.assertIsNone(image)
        self.assertIn('takes the keys "pip" and "apt"', problem)

    def test_refused_packages_do_not_run(self):
        a = self.agent(approve=False)
        with mock.patch.object(a.sandbox, "ensure_image", return_value="x"):
            image, problem = run_code.ensure_packages(a, {"pip": ["pillow"]})
        self.assertIsNone(image)
        self.assertIn("refused", problem)
        self.assertFalse(os.path.exists(os.path.join(self.folder, run_code.PACKAGES_FILE)))

    def test_a_missing_module_names_the_package_to_add(self):
        a = self.agent()
        self.assertIn('packages {"pip": ["pyyaml"]}', run_code._module_hint(a, "yaml"))

    def test_the_headless_build_replaces_the_other_one(self):
        a = self.agent()
        with mock.patch.object(a.sandbox, "ensure_image", return_value="x"):
            run_code.ensure_packages(a, {"pip": ["opencv-python", "pyzbar"]})
            run_code.ensure_packages(a, {"pip": ["opencv-python-headless"]})
            self.assertEqual(run_code.project_packages(self.folder)["pip"], ["opencv-python-headless", "pyzbar"])
            run_code.ensure_packages(a, {"pip": ["opencv-python"]})       # the headless one stays
            self.assertEqual(run_code.project_packages(self.folder)["pip"], ["opencv-python-headless", "pyzbar"])


class ImportProbeTest(unittest.TestCase):
    """A script that catches its own import error: the harness imports the modules in the image and names the cause."""
    SCRIPT = ("import os\nimport pyzbar\ntry:\n    from pyzbar.pyzbar import decode\nexcept ImportError:\n"
              "    print('Could not find decode')\n")

    def setUp(self):
        self.folder = git_folder({"a.txt": "x\n", "helper.py": "X = 1\n"})
        self.a = Agent(temp_config(), label="probe", workspace=self.folder)
        self.runs = []

        def fake(argv, box, **kw):
            self.runs.append(argv)
            if "probe.py" in argv[1]:
                return {"exit_code": 0, "output": 'MINI_PROBE {"direct": {}, "sub": {"pyzbar.pyzbar": '
                        '"ImportError: Unable to find zbar shared library", "pyzbar.locations": "ValueError: odd"}}\n'}
            return {"exit_code": 0, "output": "Could not find decode\n"}
        self.fake = fake

    def run_script(self, script):
        call = types.SimpleNamespace(agent=self.a, content="```python\n" + script + "```\n")
        with mock.patch.object(self.a.sandbox, "run", side_effect=self.fake), \
                mock.patch.object(self.a.sandbox, "ensure_image", return_value="img"):
            return run_code.run(call, {})

    def test_a_caught_missing_library_is_named_once_per_image(self):
        out = self.run_script(self.SCRIPT)
        self.assertIn("importing pyzbar.pyzbar fails: ImportError: Unable to find zbar shared library", out)
        self.assertIn('{"apt": ["libzbar0t64"]}', out)
        self.assertNotIn("pyzbar.locations", out)              # a submodule's other errors are not the cause
        self.assertEqual(json.loads(self.runs[1][2]), ["pyzbar", "pyzbar.pyzbar"])
        out = self.run_script(self.SCRIPT)                      # the next run reuses the answer: no second probe
        self.assertEqual(len(self.runs), 3)
        self.assertIn("libzbar0t64", out)

    def test_stdlib_and_the_folders_own_modules_are_not_probed(self):
        self.run_script("import os, json\nimport helper\nprint(helper.X)\n")
        self.assertEqual(len(self.runs), 1)


if __name__ == "__main__":
    unittest.main()
