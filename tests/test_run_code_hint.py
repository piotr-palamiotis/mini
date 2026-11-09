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

    def test_no_tool_has_it(self):
        a = types.SimpleNamespace(registry=Reg({"xlsx_x": ["openpyxl"]}), built_names=[])
        self.assertNotIn("already has it", _module_hint(a, "yaml"))


if __name__ == "__main__":
    unittest.main()
