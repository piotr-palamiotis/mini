"""A call that gives one tool the inputs of another is told which tool they fit."""
import types
import unittest

from mini.core import registry_invoke


class Reg:
    def __init__(self, tools):
        self.tools = tools

    def names(self):
        return sorted(self.tools)

    def get(self, n):
        t = self.tools.get(n)
        return t and {"name": n, "description": n, "version": 1, "created_by": {"provider": "local"},
                      "input_schema": {"type": "object", "required": t[0], "properties": {k: {"type": "string"} for k in t[1]}},
                      "output_schema": {"type": "object", "properties": {}}}


class InvokeHintTest(unittest.TestCase):
    def test_inputs_of_the_tool_built_for_this_task(self):
        a = types.SimpleNamespace(registry=Reg({"xlsx_write": (["path", "sheets"], ["path", "sheets"]),
                                                "write_excel_workbook": (["output_path", "sheets"], ["output_path", "sheets"])}),
                                  built_names=["write_excel_workbook"], core=set())
        call = types.SimpleNamespace(agent=a)
        out = registry_invoke.run(call, {"name": "xlsx_write", "inputs": {"output_path": "r.xlsx", "sheets": {}}})
        self.assertIn("Missing required inputs for xlsx_write: path", out)
        self.assertIn("are the inputs of write_excel_workbook (built for this task)", out)

    def test_no_hint_when_no_other_tool_fits(self):
        a = types.SimpleNamespace(registry=Reg({"xlsx_write": (["path"], ["path", "sheets"])}), built_names=[], core=set())
        out = registry_invoke.run(types.SimpleNamespace(agent=a), {"name": "xlsx_write", "inputs": {"sheets": {}}})
        self.assertNotIn("are the inputs of", out)


if __name__ == "__main__":
    unittest.main()


class FailedTest(unittest.TestCase):
    def test_an_output_with_an_error_field_of_its_own_is_a_success(self):
        from mini.harness.toolrun import failed
        self.assertFalse(failed({"valid": True, "error": None}, {"exit_code": 0}))
        self.assertFalse(failed({"rows": 3, "error": "none found in row 2"}, {"exit_code": 0}))
        self.assertTrue(failed({"error": "ValueError", "message": "bad"}, {"exit_code": 1}))
        self.assertTrue(failed({"error": "ValueError", "message": "bad"}))
