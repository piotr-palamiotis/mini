"""run_code is the workbench: a script that worked is kept as a tool whose test is that run (real sandbox)."""
import json
import os
import unittest

from mini.agent.core import Agent
from tests.helpers import call, fake_backend, fence, git_folder, step, temp_config

SCRIPT = '''
import csv
rows = list(csv.DictReader(open("sales.csv")))
total = sum(float(r["qty"]) * float(r["price"]) for r in rows)
open("total.txt", "w").write(f"{total:.2f}\\n")
open("scratch.txt", "w").write("a check the script made on the side\\n")
print("total", round(total, 2))
'''
TOOL = '''
import csv
def run(inputs, io):
    src = io.ask("csv_path", "CSV file", "file")
    dest = io.ask("out_path", "where to write the total", "text")
    rows = list(csv.DictReader(open(src)))
    total = sum(float(r["qty"]) * float(r["price"]) for r in rows)
    open(dest, "w").write(f"{total:.2f}\\n")
    return {"total": round(total, 2), "written": dest}
'''
PLAN = dict(name="csv_total", description="Sum qty * price over a CSV and write the total to a text file.",
            input_schema={"type": "object", "required": ["csv_path", "out_path"], "properties": {
                "csv_path": {"type": "string", "description": "the CSV"}, "out_path": {"type": "string"}}},
            output_schema={"type": "object", "properties": {"total": {"type": "number"}}},
            example_inputs={"csv_path": "sales.csv", "out_path": "total.txt"}, outputs=["total.txt"])


def script():
    return [
        step(call("understand", goal="total of sales", inputs=["sales.csv"], outputs=["total.txt"], plan="1. sum")),
        step(call("decide_coverage", reasoning="a script does it", use=[], missing=[])),
        step(call("run_code", files=["sales.csv"]), content=fence("script.py", SCRIPT)),
        step(call("keep_tool", what="sum a CSV's qty * price")),
        step(call("write_tool_plan", **PLAN)),
        step(content=fence("tool.py", TOOL)),
        step(call("finish", answer="The total is 12.50.", files=["total.txt"])),
    ]


class KeepTest(unittest.TestCase):
    def test_a_script_that_worked_becomes_a_tool_tested_on_its_run(self):
        folder = git_folder({"sales.csv": "qty,price\n2,1.25\n4,2.5\n"})
        agent = Agent(temp_config(), backend=fake_backend(script()), label="keep", workspace=folder)
        s = agent.run_task("Write the total of sales.csv (qty * price) to total.txt and keep it as a tool.")
        self.assertEqual(s["status"], "success", s["error"])
        self.assertEqual(open(os.path.join(folder, "total.txt")).read(), "12.50\n")
        m = agent.registry.get("csv_total")
        self.assertTrue(m["test_results"]["passed"])
        golden = os.path.join(m["_dir"], "golden")
        self.assertEqual(json.load(open(os.path.join(golden, "example.json"))), PLAN["example_inputs"])
        self.assertEqual(open(os.path.join(golden, "out", "total.txt")).read(), "12.50\n")
        self.assertTrue(os.path.exists(os.path.join(golden, "in", "sales.csv")))
        self.assertFalse(os.path.exists(os.path.join(golden, "out", "scratch.txt")))   # not the tool's result
        events = [json.loads(l) for l in open(os.path.join(agent.run_dir, "log.jsonl"))]
        tests = [e for e in events if e["kind"] == "test_run"]
        self.assertEqual([t["passed"] for t in tests], [True])          # the harness's test, first try
        self.assertTrue(any(e["kind"] == "invoke" and e.get("ok") is True and e["tool"] == "csv_total" for e in events))
        self.assertIn(("", "total.txt"), agent.kept_jobs["csv_total"]["shapes"])  # a later script writing it is told

    def test_nothing_to_keep_without_a_successful_script(self):
        a = Agent(temp_config(), label="keep0", workspace=git_folder({"a.txt": "x\n"}))
        self.assertIn("Nothing to keep", a._keep(None, "x", "task", "", []))

    def test_a_plan_the_model_never_gets_right_is_not_kept_and_the_task_goes_on(self):
        bad = dict(PLAN, example_inputs={"nope": 1})
        steps = script()[:4] + [step(call("write_tool_plan", **bad))] * 4 + [
            step(call("finish", answer="The total is 12.50.", files=["total.txt"]))]
        folder = git_folder({"sales.csv": "qty,price\n2,1.25\n4,2.5\n"})
        agent = Agent(temp_config(), backend=fake_backend(steps), label="keepbad", workspace=folder)
        s = agent.run_task("Write the total of sales.csv (qty * price) to total.txt.")
        self.assertEqual(s["status"], "success", s["error"])
        self.assertEqual(open(os.path.join(folder, "total.txt")).read(), "12.50\n")
        self.assertIsNone(agent.registry.get("csv_total"))

    def test_a_tool_the_model_never_writes_mid_task_is_not_built_and_the_task_goes_on(self):
        from unittest import mock
        from mini.agent.turns import ModelGaveUp
        steps = script()[:2] + [step(call("need_tool", need="read sales from shop.db")),
                                step(call("write_file", path="total.txt", content="12.50\n")),
                                step(call("finish", answer="The total is 12.50.", files=["total.txt"]))]
        folder = git_folder({"sales.csv": "qty,price\n2,1.25\n4,2.5\n"})
        agent = Agent(temp_config(), backend=fake_backend(steps), label="needgaveup", workspace=folder)
        with mock.patch("mini.agent.core.Creator.create", side_effect=ModelGaveUp("no usable test_tool.py after 3 replies")):
            s = agent.run_task("Write the total of sales.csv (qty * price) to total.txt.")
        self.assertEqual(s["status"], "success", s["error"])
        events = [json.loads(l) for l in open(os.path.join(agent.run_dir, "log.jsonl"))]
        said = [e["text"] for e in events if e["kind"] == "tool_result" and e.get("name") == "need_tool"]
        self.assertIn("Not built: no usable test_tool.py", said[0])
        self.assertIn("run_code", said[0])

    def test_a_rerun_that_writes_the_same_file_again_can_be_kept(self):
        steps = script()
        steps.insert(3, steps[2])               # the same script again: total.txt is unchanged in the folder
        folder = git_folder({"sales.csv": "qty,price\n2,1.25\n4,2.5\n"})
        agent = Agent(temp_config(), backend=fake_backend(steps), label="keeprerun", workspace=folder)
        s = agent.run_task("Write the total of sales.csv (qty * price) to total.txt and keep it as a tool.")
        self.assertEqual(s["status"], "success", s["error"])
        m = agent.registry.get("csv_total")
        self.assertTrue(m and m["test_results"]["passed"])
        self.assertEqual(open(os.path.join(m["_dir"], "golden", "out", "total.txt")).read(), "12.50\n")


class SchemaShapeTest(unittest.TestCase):
    def test_fields_next_to_type_go_into_properties(self):
        from mini.agent.creator import object_schema
        got = object_schema({"type": "object", "required": ["data"], "data": {"type": "object"},
                             "out": {"description": "path", "type": "string"}, "properties": {}})
        self.assertEqual(got, {"type": "object", "required": ["data"],
                               "properties": {"data": {"type": "object"}, "out": {"description": "path", "type": "string"}}})
        ok = {"type": "object", "properties": {"a": {"type": "string"}}, "additionalProperties": {"type": "string"}}
        self.assertEqual(object_schema(dict(ok)), ok)


class PlanRedoesTheRunTest(unittest.TestCase):
    def test_an_example_naming_a_file_the_script_never_used_is_rejected(self):
        from mini.agent.creator import Creator
        script = "import yaml\nopen('test.toml','w').write(convert(open('test.yaml').read()))\n"
        plan = dict(PLAN, example_inputs={"csv_path": "app.yaml", "out_path": "test.toml"}, outputs=["test.toml"])
        err = Creator._check_plan(types_ns(), dict(plan), ["test.toml"], script)
        self.assertIn("does not use app.yaml", err)
        self.assertIn("test.yaml", err)

    def test_a_sample_the_script_made_is_not_an_output(self):
        from mini.agent.creator import Creator
        script = "make_sample('made.png')\nprint(read('made.png'))\n"
        plan = dict(PLAN, input_schema={"type": "object", "properties": {"image": {"type": "string"}}},
                    example_inputs={"image": "made.png"}, outputs=[])
        self.assertIsNone(Creator._check_plan(types_ns(), dict(plan), ["made.png"], script))
        err = Creator._check_plan(types_ns(), dict(plan), ["made.png", "result.txt"], script)
        self.assertIn("result.txt", err)
        self.assertIn("is an input", err)

    def test_a_sample_input_the_script_made_is_the_tests_input(self):
        import tempfile
        from mini.agent.creator import _self_made_inputs
        g = tempfile.mkdtemp()
        for part, rel, text in (("out", "sample.yaml", "a: 1\n"), ("out", "sample.toml", "a = 1\n"),
                                ("in", "app.yaml", "b: 2\n")):
            os.makedirs(os.path.join(g, part), exist_ok=True)
            open(os.path.join(g, part, rel), "w").write(text)
        self.assertEqual(_self_made_inputs({"path": "sample.yaml", "out": "sample.toml", "other": ["app.yaml", 3]}, g,
                                           ["sample.toml"]), ["sample.yaml"])
        self.assertEqual(_self_made_inputs({"out": "sample.toml"}, g, None), [])
        open(os.path.join(g, "out", "made.png"), "w").write("png")
        self.assertEqual(_self_made_inputs({"image": "made.png"}, g, []), ["made.png"])    # a tool that writes no file
        self.assertTrue(os.path.exists(os.path.join(g, "in", "sample.yaml")))
        self.assertFalse(os.path.exists(os.path.join(g, "out", "sample.yaml")))
        self.assertTrue(os.path.exists(os.path.join(g, "out", "sample.toml")))


def types_ns():
    """A stand-in for the creator: _check_plan only needs _validate_spec from it."""
    import types
    return types.SimpleNamespace(_validate_spec=lambda d, new=True: None)


if __name__ == "__main__":
    unittest.main()


class CommandScriptTest(unittest.TestCase):
    def test_a_command_running_a_py_file_in_the_folder_is_a_script_run(self):
        import tempfile
        from mini.core.run_code import _script_of
        box = tempfile.mkdtemp()
        open(os.path.join(box, "calc.py"), "w").write("print(1)\n")
        got = _script_of(["python3", "calc.py", "a.json"], box)
        self.assertTrue(got.startswith("# run as: python3 calc.py a.json\n") and got.endswith("print(1)\n"))
        self.assertIsNone(_script_of(["wc", "-l", "a.txt"], box))

    def test_a_task_that_asks_for_a_tool(self):
        from mini.agent.core import ASKS_FOR_TOOL
        self.assertTrue(ASKS_FOR_TOOL.search("Build a tool that totals invoice files"))
        self.assertTrue(ASKS_FOR_TOOL.search("Make a tool that converts an INI file to JSON"))
        self.assertFalse(ASKS_FOR_TOOL.search("Rename get_usr to get_user. Use the tools you have."))
