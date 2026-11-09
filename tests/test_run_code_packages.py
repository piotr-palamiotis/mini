"""run_code gets the packages it names: approved once per folder, kept in .mini/packages.json."""
import json
import os
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


if __name__ == "__main__":
    unittest.main()
