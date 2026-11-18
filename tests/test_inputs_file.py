"""Long data a script made reaches a tool through a saved JSON file, not through the model copying it."""
import json
import os
import types
import unittest
from unittest import mock

from mini.agent.core import Agent
from mini.core import registry_invoke, run_code
from mini.harness import workspace as ws
from tests.helpers import git_folder, temp_config


class InputsFileTest(unittest.TestCase):
    def setUp(self):
        self.folder = git_folder({"a.txt": "x\n"})
        self.a = Agent(temp_config(), label="inputs_file", workspace=self.folder)
        os.makedirs(self.a.run_dir, exist_ok=True)

    def test_json_a_script_saves_under_mini_data_is_kept_for_the_run(self):
        box = os.path.join(self.a.run_dir, "box")
        os.makedirs(os.path.join(box, run_code.DATA))
        json.dump({"rows": [1, 2]}, open(os.path.join(box, run_code.DATA, "sheets.json"), "w"))
        open(os.path.join(box, run_code.DATA, "notes.txt"), "w").write("not json")
        self.assertEqual(run_code._keep_data(self.a, box), ["sheets.json"])
        self.assertTrue(os.path.isfile(os.path.join(self.a.run_dir, "data", "sheets.json")))
        self.assertEqual(ws.changes(self.folder, box)[0], [])          # nothing of it lands in the user's folder

    def test_long_printed_json_is_found(self):
        rows = json.dumps({"east": [["2026-05-13", "delta", 5, 4.0, 20.0]] * 60}, indent=2)
        self.assertTrue(run_code._big_json("Totals:\n  east 334.8\n" + rows + "\n"))
        self.assertFalse(run_code._big_json('{"a": 1}\n' + "x" * 3000))
        self.assertFalse(run_code._big_json("[not json at all " + "y" * 3000))

    def invoke(self, args):
        tool = {"name": "xlsx_write", "version": 1, "input_schema": {
            "type": "object", "required": ["sheets"], "properties": {"sheets": {}, "path": {}}}}
        got = {}
        with mock.patch.object(self.a.registry, "get", return_value=tool), \
                mock.patch.object(registry_invoke, "_invoke_in",
                                  side_effect=lambda a, m, name, inputs, box: got.update(inputs) or "ran"):
            text = registry_invoke.run(types.SimpleNamespace(agent=self.a), {"name": "xlsx_write", **args})
        return text, got

    def test_invoke_tool_reads_the_saved_inputs_and_inputs_add_over_them(self):
        os.makedirs(os.path.join(self.a.run_dir, "data"))
        json.dump({"sheets": {"east": [[1]]}, "path": "a.xlsx"}, open(os.path.join(self.a.run_dir, "data", "in.json"), "w"))
        text, got = self.invoke({"inputs_file": ".mini/data/in.json", "inputs": {"path": "report.xlsx"}})
        self.assertEqual(text, "ran")
        self.assertEqual(got, {"sheets": {"east": [[1]]}, "path": "report.xlsx"})

    def test_a_missing_or_wrong_saved_file_is_named(self):
        text, got = self.invoke({"inputs_file": "nope.json"})
        self.assertIn("No saved data named 'nope.json'", text)
        os.makedirs(os.path.join(self.a.run_dir, "data"))
        json.dump([1, 2], open(os.path.join(self.a.run_dir, "data", "list.json"), "w"))
        text, got = self.invoke({"inputs_file": "list.json"})
        self.assertIn("must be the tool's inputs object", text)
        self.assertEqual(got, {})


if __name__ == "__main__":
    unittest.main()
