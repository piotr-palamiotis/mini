"""A tool built for the task runs before anything else is built: no second build of the same thing."""
import types
import unittest

from mini.agent.core import Agent


class Log:
    def __init__(self):
        self.events = []

    def emit(self, kind, **kw):
        self.events.append((kind, kw))


class BuildThenRunTest(unittest.TestCase):
    def setUp(self):
        self.built = []
        manifest = lambda n: {"name": n, "description": n, "version": 1, "created_by": {"provider": "local"},
                              "input_schema": {"type": "object", "properties": {}}, "output_schema": {}}
        self.creator = types.SimpleNamespace(create=lambda need, task, ctx: (self.built.append(need), manifest("pdf_invoices" if len(self.built) == 1 else "pdf_text"))[1])
        self.a = types.SimpleNamespace(core=types.SimpleNamespace(covering=lambda x: None), built_names=[],
                                       registry=types.SimpleNamespace(get=lambda n: {"description": "Make PDF invoices per customer."}),
                                       invoked_any=set(), need_reminded=False, log=Log(), last_script=None,
                                       wd=types.SimpleNamespace(reset_conversation=lambda: None))
        self.a._same_need = lambda tool, need: Agent._same_need(self.a, tool, need)

    def need(self, text):
        return Agent._need(self.a, self.creator, {"need": text}, "task", "", [])

    def test_a_second_build_of_the_same_thing_waits_until_the_first_ran(self):
        self.assertIn("New tool installed", self.need("make PDF invoices"))
        for _ in range(2):                                  # refused every time, not once
            out = self.need("make the PDF invoices per customer, again")
            self.assertIn("pdf_invoices was built and installed for this task and has not run yet", out)
        self.assertEqual(len(self.built), 1)
        self.a.invoked_any.add("pdf_invoices")              # it ran: once more, the reminder to use it
        self.assertIn("built and installed for this task. Call", self.need("make the PDF invoices per customer"))

    def test_a_different_capability_is_built_at_once(self):
        self.need("make PDF invoices")
        self.assertIn("New tool installed", self.need("read the text out of PDF files"))
        self.assertEqual(len(self.built), 2)

if __name__ == "__main__":
    unittest.main()


class KeepDuplicateTest(unittest.TestCase):
    def test_keeping_what_a_tool_made_for_this_task_does_is_refused_once(self):
        a = types.SimpleNamespace(built_names=["yaml_to_toml"], keep_reminded=False, log=Log(),
                                  last_script={"ok": True, "script": "x", "record": "/r"},
                                  registry=types.SimpleNamespace(get=lambda n: {"description": "Convert a YAML file to TOML."}))
        a._same_need = lambda tool, need: Agent._same_need(a, tool, need)
        out = Agent._keep(a, None, "convert a YAML file to a TOML file", "task", "", [])
        self.assertIn("yaml_to_toml was made for this task and already does this", out)
        self.assertTrue(a.keep_reminded)
