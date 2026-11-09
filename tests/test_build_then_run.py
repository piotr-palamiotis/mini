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
        self.creator = types.SimpleNamespace(create=lambda need, task, ctx: (self.built.append(need), manifest("pdf_invoices"))[1])
        self.a = types.SimpleNamespace(core=types.SimpleNamespace(covering=lambda x: None), built_names=[],
                                       invoked_any=set(), need_reminded=False, log=Log(), last_script=None,
                                       wd=types.SimpleNamespace(reset_conversation=lambda: None))

    def need(self, text):
        return Agent._need(self.a, self.creator, {"need": text}, "task", "", [])

    def test_a_second_build_waits_until_the_first_tool_ran(self):
        self.assertIn("New tool installed", self.need("make PDF invoices"))
        for _ in range(2):                                  # refused every time, not once
            out = self.need("make PDF invoices, again")
            self.assertIn("pdf_invoices was built and installed for this task and has not run yet", out)
        self.assertEqual(len(self.built), 1)
        self.a.invoked_any.add("pdf_invoices")              # it ran: another capability may be built now
        self.assertIn("built and installed for this task. Call", self.need("read PDF text"))   # the old one-time nudge
        self.assertIn("New tool installed", self.need("read PDF text"))
        self.assertEqual(len(self.built), 2)


if __name__ == "__main__":
    unittest.main()
