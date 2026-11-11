"""A missing package: name it for run_code's packages input, and an installed tool that has it."""
import types
import unittest

from mini.core.run_code import _module_hint


class Reg:
    def __init__(self, tools):
        self.tools = tools

    def names(self):
        return sorted(self.tools)

    def get(self, n):
        return {"permissions": {"packages": {"pip": self.tools[n]}}}


class ModuleHintTest(unittest.TestCase):
    def test_the_tool_built_for_the_task_that_has_the_package(self):
        a = types.SimpleNamespace(registry=Reg({"get_image_metadata": ["Pillow>=10"], "xlsx_x": ["openpyxl"]}),
                                  built_names=["get_image_metadata"])
        h = _module_hint(a, "PIL")
        self.assertIn('packages {"pip": ["pillow"]}', h)
        self.assertIn("get_image_metadata already has it", h)

    def test_an_import_of_an_installed_tool_says_to_invoke_it(self):
        a = types.SimpleNamespace(registry=Reg({"generate_qr_code": ["qrcode"]}), built_names=["generate_qr_code"])
        h = _module_hint(a, "generate_qr_code")
        self.assertIn("installed tool", h)
        self.assertIn("invoke_tool", h)
        self.assertNotIn("pip", h)

    def test_no_tool_has_it(self):
        a = types.SimpleNamespace(registry=Reg({"xlsx_x": ["openpyxl"]}), built_names=[])
        self.assertNotIn("already has it", _module_hint(a, "yaml"))


class LibraryHintTest(unittest.TestCase):
    def test_a_missing_shared_library_names_the_debian_13_apt_package(self):
        from mini.core.run_code import _apt_note, _library_hint
        h = _library_hint("ImportError: Unable to find zbar shared library")
        self.assertIn('{"apt": ["libzbar0t64"]}', h)
        h = _library_hint("ImportError: libxcb.so.1: cannot open shared object file: No such file or directory")
        self.assertIn("libxcb1", h)
        self.assertIn("opencv-python-headless", h)
        self.assertIn("t64", _library_hint("OSError: libodd.so.2: cannot open shared object file"))
        self.assertEqual(_library_hint("Traceback: ValueError: bad row"), "")
        self.assertIn("libzbar0t64", _apt_note("E: Unable to locate package libzbar0"))
        self.assertEqual(_apt_note("exit 1"), "")


if __name__ == "__main__":
    unittest.main()
