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

    def test_garbled_package_shapes_are_read_as_names(self):
        a = self.agent()
        with mock.patch.object(a.sandbox, "ensure_image", return_value="x"):
            self.assertEqual(run_code.ensure_packages(a, {'"pip"': [{'"PyYAML"': [], '"toml"': []}]}), ("x", ""))
        self.assertEqual(run_code.project_packages(self.folder)["pip"], ["PyYAML", "toml"])

    def test_a_garbled_key_that_starts_with_a_parameter_is_that_parameter(self):
        from mini.core import validate
        args, problem = validate(run_code.SCHEMA, {'packages={"pip"': {'"pip"': ["toml"]}})
        self.assertEqual(problem, "")
        self.assertEqual(args, {"packages": {'"pip"': ["toml"]}})
        args, problem = validate(run_code.SCHEMA, {'packages={"pip"': ["matplotlib", "openpyxl"]})   # reg1 sales
        self.assertEqual((args, problem), ({"packages": {"pip": ["matplotlib", "openpyxl"]}}, ""))
        self.assertIn("unknown argument", validate(run_code.SCHEMA, {"pkgs": {}})[1])

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


class SameResultTest(unittest.TestCase):
    """Different scripts that keep writing the same file: the fix is not reaching the cause."""
    def test_the_third_script_with_the_same_result_is_told_to_look_at_its_values(self):
        folder = git_folder({"a.txt": "x\n"})
        a = Agent(temp_config(), label="same", workspace=folder)

        def fake(argv, box, **kw):
            open(os.path.join(box, "decoded.json"), "w").write('[{"raw": "S"}]')
            open(os.path.join(box, "q.png"), "wb").write(b"\x89PNG\x00\xff\xfe")
            return {"exit_code": 0, "output": "Saved decoded.json\n"}
        outs = []
        for i in range(3):
            call = types.SimpleNamespace(agent=a, content=f"```python\nprint({i})\n```\n")
            with mock.patch.object(a.sandbox, "run", side_effect=fake):
                outs.append(run_code.run(call, {}))
        self.assertNotIn("unchanged", outs[0])
        self.assertIn("same content the folder already had (unchanged): decoded.json, q.png", outs[1])
        self.assertNotIn("different scripts", outs[1])
        self.assertIn('3 different scripts have now written exactly the same decoded.json:\n[{"raw": "S"}]', outs[2])
        self.assertIn("repr()", outs[2])
        self.assertNotIn("same q.png", outs[2])                 # a binary file is never reported as stuck
        call = types.SimpleNamespace(agent=a, content="```python\nprint(0)\n```\n")
        with mock.patch.object(a.sandbox, "run", side_effect=fake):
            self.assertNotIn("4 different", run_code.run(call, {}))   # the same script again is not a new attempt


class KeptToolRedoneTest(unittest.TestCase):
    """A script that does again what a tool kept in this task does: run the tool instead."""
    def test_same_function_or_same_kind_of_file_names_the_kept_tool_twice_at_most(self):
        folder = git_folder({"a.txt": "x\n"})
        a = Agent(temp_config(), label="kept", workspace=folder)
        a.kept_jobs = {"make_qr": run_code.job_of("def make(t, p):\n    pass\n", ["qr/test.png"]),
                       "read_qr": run_code.job_of("def decode_qr_image(p):\n    return p\ndef main():\n    pass\n", [])}

        def fake(argv, box, **kw):
            if "png" in open(os.path.join(box, run_code.SCRIPT)).read():
                os.makedirs(os.path.join(box, "qr"), exist_ok=True)
                open(os.path.join(box, "qr", "SKU1.png"), "wb").write(b"\x89PNG")
            return {"exit_code": 0, "output": "ok\n"}

        def run(script):
            call = types.SimpleNamespace(agent=a, content="```python\n" + script + "```\n")
            with mock.patch.object(a.sandbox, "run", side_effect=fake):
                return run_code.run(call, {})
        self.assertIn("job of make_qr", run("x = 'png'\n"))
        self.assertIn("job of read_qr", run("def decode_qr_image(p):\n    return 1\n"))
        self.assertNotIn("job of", run("def main():\n    print(1)\n"))   # main is everyone's
        run("y = 'png'\n")
        self.assertNotIn("job of make_qr", run("z = 'png'\n"))         # told twice, not more


if __name__ == "__main__":
    unittest.main()
