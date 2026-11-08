"""Slash commands (docs/redesign.md 4b): the command registry, parsing, autocomplete, ui hints and their validation,
form fields from the input schema, prefill, and tools run from the prompt in ask and form mode (the run tests need
podman, like test_tool_io)."""
import asyncio
import os
import tempfile
import types
import unittest

from textual.app import App
from textual.widgets import Input, Switch, TextArea

from mini import bundled, core
from mini.agent.creator import Creator
from mini.harness import journal, toolui
from mini.harness.registry import Registry
from mini.ui.app import Block, MiniApp, QuestionCard
from mini.ui.commands import CORE, Commands
from mini.ui.toolform import ToolForm, fid
from tests.helpers import git_folder, temp_config
from tests.test_tool_io import MANIFEST, SECRET, install

EMAIL = {"name": "send_email", "description": "Send an email.", "version": 1, "input_schema": {
    "type": "object", "required": ["to", "subject"], "properties": {
        "to": {"type": "string", "description": "receiver"}, "subject": {"type": "string"},
        "body": {"type": "string"}, "attachments": {"type": "array", "items": {"type": "string", "format": "file"}},
        "priority": {"type": "string", "enum": ["low", "normal", "high"]}, "copies": {"type": "integer"},
        "html": {"type": "boolean"}, "password": {"type": "string", "format": "password"},
        "headers": {"type": "object"}}},
    "ui": {"mode": "form", "order": ["subject", "to"],
           "fields": {"body": {"label": "Message", "multiline": True}, "attachments": {"filter": "*.pdf"}}}}


def tools(*extra):
    return [EMAIL, {"name": "status", "description": "clashes with /status", "version": 1, "input_schema": {}}, *extra]


class RegistryTest(unittest.TestCase):
    def test_core_commands_and_tools_from_one_description(self):
        c = Commands(tools())
        for name in ("model", "new", "attach", "status", "undo", "registry", "config", "tools", "help"):
            self.assertIsNone(c.get(name).tool, name)
        email = c.get("send-email")                                     # kebab-case for send_email
        self.assertEqual((email.tool, email.mode, email.description), ("send_email", "form", "Send an email."))
        self.assertEqual(c.get("tool-status").tool, "status")           # a core name keeps its command
        self.assertEqual(c.get("status").tool, None)
        self.assertEqual([x.name for x in c.matching("st")][:1], ["status"])
        self.assertIn("/send-email", c.help_text())
        self.assertEqual(len(CORE), 14)

    def test_mode_defaults(self):
        c = Commands([{"name": "no_inputs", "description": "", "input_schema": {}},
                      {"name": "has_inputs", "description": "", "input_schema": {"properties": {"a": {"type": "string"}}}},
                      {"name": "talks", "description": "", "ui": {"mode": "ask"},
                       "input_schema": {"properties": {"a": {"type": "string"}}}}])
        self.assertEqual([c.get(n).mode for n in ("no-inputs", "has-inputs", "talks")], ["ask", "form", "ask"])

    def test_parse_flags_values_and_positionals(self):
        c = Commands(tools())
        cmd, args, problem = c.parse('/send-email --to anna@example.com --subject "Hi there" --copies 2 --html '
                                     '--attachments a.pdf b.pdf --priority=high --headers \'{"X":"1"}\'')
        self.assertEqual(problem, "")
        self.assertEqual(args, {"to": "anna@example.com", "subject": "Hi there", "copies": 2, "html": True,
                                "attachments": ["a.pdf", "b.pdf"], "priority": "high", "headers": {"X": "1"}})
        self.assertEqual(c.parse("/send-email --no-html")[1], {"html": False})
        self.assertIn("no --cc", c.parse("/send-email --cc x")[2])
        self.assertIn("not a number", c.parse("/send-email --copies many")[2])
        self.assertIn("one of", c.parse("/send-email --priority urgent")[2])
        self.assertIn("needs a value", c.parse("/send-email --to")[2])
        self.assertEqual(c.parse("/model paid")[1], {"tier": "paid"})
        self.assertIn("one of", c.parse("/model fast")[2])
        self.assertEqual(c.parse("/undo r1 a.txt")[1], {"target": ["r1", "a.txt"]})
        self.assertEqual(c.parse("/attach my notes.txt")[1], {"path": "my notes.txt"})
        self.assertIn("unknown command", c.parse("/nope")[2])

    def test_what_counts_as_a_command(self):
        c = Commands()
        self.assertTrue(c.is_command("/help"))
        self.assertTrue(c.is_command("/nope"))                          # reported as unknown
        self.assertFalse(c.is_command("/tmp/a.txt: count its lines"))   # a task that starts with a path
        self.assertFalse(c.is_command("help"))

    def test_line_masks_secrets(self):
        line = Commands(tools()).get("send-email").line({"to": "a b", "password": "pw", "html": False, "body": "x\ny"})
        self.assertEqual(line, "/send-email --to 'a b' --password *** --no-html --body 'x y'")


class CompletionTest(unittest.TestCase):
    def setUp(self):
        self.c = Commands(tools())
        self.d = tempfile.mkdtemp()
        for rel in ("report.pdf", "notes.txt", "docs/a.pdf", ".hidden", ".git/config"):
            os.makedirs(os.path.dirname(os.path.join(self.d, rel)) or self.d, exist_ok=True)
            open(os.path.join(self.d, rel), "w").close()

    def labels(self, text):
        return [label for _, label, _ in self.c.complete(text, self.d)]

    def test_names_with_descriptions_filtered_as_you_type(self):
        self.assertEqual(len(self.labels("/")), len(self.c.all()))
        got = self.c.complete("/mo", self.d)
        self.assertEqual(got[0][:2], ("/model ", "/model"))
        self.assertIn("session", got[0][2])
        self.assertEqual(self.labels("/send"), ["/send-email"])
        self.assertEqual(self.labels("task"), [])

    def test_flags_then_enum_values(self):
        self.assertIn("--subject", self.labels("/send-email "))
        self.assertEqual(self.labels("/send-email --s"), ["--subject"])
        self.assertEqual(self.labels("/send-email --h"), ["--html", "--headers"])
        self.assertEqual(self.labels("/send-email --no"), ["--no-html"])
        self.assertNotIn("--to", self.labels("/send-email --to x --"))  # given once already
        self.assertEqual(self.labels("/send-email --priority "), ["low", "normal", "high"])
        self.assertEqual(self.labels("/send-email --priority h"), ["high"])
        self.assertEqual(self.labels("/model "), ["local", "free", "paid"])
        self.assertEqual(self.labels("/model paid "), [])
        self.assertEqual(self.c.complete("/model p", self.d)[0][0], "/model paid ")

    def test_file_paths_relative_to_the_folder(self):
        self.assertEqual(self.labels("/attach "), ["docs/", "notes.txt", "report.pdf"])   # no hidden, no .git
        self.assertEqual(self.labels("/attach ."), [".hidden"])
        self.assertEqual(self.labels("/attach docs/"), ["docs/a.pdf"])
        self.assertEqual(self.labels("/send-email --attachments "), ["docs/", "report.pdf"])   # the *.pdf filter
        self.assertEqual(self.c.complete("/attach no", self.d)[0][0], "/attach notes.txt ")
        self.assertEqual(self.c.complete("/attach do", self.d)[0][0], "/attach docs/")        # into the folder


class HintsTest(unittest.TestCase):
    schema = EMAIL["input_schema"]

    def test_valid_hints(self):
        self.assertIsNone(toolui.validate(EMAIL["ui"], self.schema))
        self.assertIsNone(toolui.validate(None, self.schema))
        self.assertIsNone(toolui.validate({"mode": "ask"}, {}))

    def test_hints_may_only_name_schema_properties(self):
        bad = [({"mode": "modal"}, "mode"), ({"order": ["cc"]}, "cc"), ({"fields": {"cc": {}}}, "cc"),
               ({"fields": {"to": {"colour": "red"}}}, "unknown hint"), ({"layout": 1}, "unknown key"),
               ({"fields": {"copies": {"multiline": True}}}, "multiline"), ({"fields": {"html": {"filter": "*"}}}, "filter"),
               ({"order": ["to", "to"]}, "twice"), ({"fields": {"to": {"label": 3}}}, "wrong type"), ("form", "object")]
        for ui, why in bad:
            problem = toolui.validate(ui, self.schema)
            self.assertIsNotNone(problem, ui)
            self.assertIn(why, problem, ui)

    def test_tidy_drops_what_validate_refuses(self):
        bad = [{"mode": "modal"}, {"order": ["cc"]}, {"fields": {"cc": {}}}, {"fields": {"to": {"colour": "red"}}},
               {"layout": 1}, {"fields": {"copies": {"multiline": True}}}, {"fields": {"html": {"filter": "*"}}},
               {"order": ["to", "to"]}, {"fields": {"to": {"label": 3}}}, "form"]
        for ui in bad:
            self.assertIsNone(toolui.validate(toolui.tidy(ui, self.schema), self.schema), ui)
        self.assertEqual(toolui.tidy({"mode": "form", "fields": {"to": {"label": "To", "type": "email"}}}, self.schema),
                         {"mode": "form", "fields": {"to": {"label": "To"}}})
        self.assertEqual(toolui.tidy(EMAIL["ui"], self.schema), EMAIL["ui"])

    def test_bundled_tools_carry_valid_hints(self):
        for name in bundled.names():
            m = bundled.manifest(name)
            self.assertIsNone(toolui.validate(m.get("ui"), m["input_schema"]), name)
        self.assertEqual(bundled.manifest("pdf_read")["ui"]["fields"]["path"]["filter"], "*.pdf")

    def test_the_creator_drops_hints_outside_the_schema(self):
        agent = types.SimpleNamespace(registry=types.SimpleNamespace(names=lambda: []), core=core.load())
        spec = {"name": "send_email", "description": "Send an email through a mail server.",
                "input_schema": self.schema, "output_schema": {"type": "object"}, "edge_cases": ["no receiver"],
                "ui": {"fields": {"receiver": {"label": "To"}}}}
        cr = Creator(agent)
        self.assertIsNone(cr._validate_spec(spec, new=True))
        self.assertEqual(spec["ui"], {})
        spec["ui"] = EMAIL["ui"]
        self.assertIsNone(cr._validate_spec(spec, new=True))
        self.assertEqual(cr._clean_spec({**spec, "settings": []})["ui"], EMAIL["ui"])


class FieldsTest(unittest.TestCase):
    def test_fields_from_the_schema_and_hints(self):
        f = {x.name: x for x in toolui.fields(EMAIL["input_schema"], EMAIL["ui"])}
        self.assertEqual(list(f)[:2], ["subject", "to"])                      # ui.order first, then schema order
        kinds = {k: x.kind for k, x in f.items()}
        self.assertEqual(kinds, {"subject": "text", "to": "text", "body": "text", "attachments": "list",
                                 "priority": "select", "copies": "integer", "html": "switch", "password": "secret",
                                 "headers": "json"})
        self.assertTrue(f["body"].multiline)
        self.assertEqual(f["body"].label, "Message")
        self.assertEqual((f["attachments"].files, f["attachments"].filter), (True, ["*.pdf"]))
        self.assertEqual(f["priority"].choices, ["low", "normal", "high"])
        self.assertTrue(f["to"].required and not f["body"].required)
        self.assertEqual(toolui.fields({"properties": {"path": {"type": "string"}, "out_file": {"type": "string"},
                                                       "n": {"type": "number"}}})[0].kind, "file")
        self.assertEqual([x.kind for x in toolui.fields({"properties": {"out_file": {"type": "string"},
                                                                         "n": {"type": "number"}}})], ["file", "number"])

    def test_values_become_inputs(self):
        fs = toolui.fields(EMAIL["input_schema"], EMAIL["ui"])
        raw = {"subject": " Hi ", "to": "a@b", "body": "line 1\nline 2\n", "attachments": "a.pdf\n\nb.pdf\n",
               "priority": None, "copies": "3", "html": True, "password": " p ", "headers": '{"X": 1}'}
        inputs, errors = toolui.to_inputs(fs, raw)
        self.assertEqual(errors, [])
        self.assertEqual(inputs, {"subject": "Hi", "to": "a@b", "body": "line 1\nline 2\n",
                                  "attachments": ["a.pdf", "b.pdf"], "copies": 3, "html": True, "password": " p ",
                                  "headers": {"X": 1}})
        _, errors = toolui.to_inputs(fs, {"subject": "", "to": "x", "copies": "lots", "headers": "{"})
        self.assertEqual(errors, ["subject is required", "copies: 'lots' is not a number", "headers: '{' is not valid JSON"])

    def test_prefill(self):
        f = {x.name: x for x in toolui.fields(EMAIL["input_schema"], EMAIL["ui"])}
        self.assertEqual(toolui.initial(f["attachments"], ["a.pdf", "b.pdf"]), "a.pdf\nb.pdf")
        self.assertEqual(toolui.initial(f["copies"], 2), "2")
        self.assertEqual(toolui.initial(f["priority"], None), None)
        self.assertEqual(toolui.initial(f["html"], None), False)
        self.assertEqual(toolui.initial(f["headers"], {"X": 1}), '{"X": 1}')


class FormHost(App):
    def __init__(self, form):
        super().__init__()
        self.form, self.result = form, "unset"

    def on_mount(self):
        self.push_screen(self.form, lambda r: setattr(self, "result", r))


class FormTest(unittest.TestCase):
    def run_form(self, prefill, body):
        cmd = Commands(tools()).get("send-email")
        host = FormHost(ToolForm(cmd, prefill, tempfile.mkdtemp()))

        async def go():
            async with host.run_test(size=(140, 50)) as pilot:
                await pilot.pause()
                await body(pilot, host.form)
        asyncio.run(go())
        return host.result

    def test_inline_arguments_prefill_and_enter_runs(self):
        async def body(pilot, form):
            self.assertEqual(form.query_one("#" + fid("to"), Input).value, "anna@example.com")
            self.assertEqual(form.query_one("#" + fid("attachments"), TextArea).text, "a.pdf")
            self.assertTrue(form.query_one("#" + fid("html"), Switch).value)
            self.assertTrue(form.query_one("#" + fid("password"), Input).password)              # masked
            self.assertTrue(form.query_one("#" + fid("subject"), Input).has_focus)              # the first empty one
            await pilot.press(*"Hello")
            await pilot.press("enter")
        result = self.run_form({"to": "anna@example.com", "attachments": ["a.pdf"], "html": True}, body)
        self.assertEqual(result, {"subject": "Hello", "to": "anna@example.com", "attachments": ["a.pdf"], "html": True})

    def test_required_fields_and_escape(self):
        async def body(pilot, form):
            await pilot.press("ctrl+s")
            self.assertIn("subject is required", str(form.query_one("#tf-msg").render()))
            await pilot.press("escape")
        self.assertIsNone(self.run_form({}, body))


class SlashUITest(unittest.TestCase):
    """The prompt: autocomplete keys, and tools run from it (podman)."""

    def app(self, folder=None):
        cfg = temp_config(**{"approvals.confirm_first": False})
        cfg.path = os.path.join(tempfile.mkdtemp(), "mini.toml")
        install(cfg)
        app = MiniApp(cfg, backend_factory=lambda: None)
        if folder:
            app.workspace, app.git_ok = folder, True
        return app

    def test_autocomplete_keys(self):
        app = self.app()

        async def go():
            async with app.run_test(size=(160, 50)) as pilot:
                prompt = app.query_one("#prompt", Input)
                await pilot.press("/")
                self.assertIn("/note-writer", [label for _, label, _ in app.sugg])     # registry tools too
                self.assertIn("/pdf-read", [label for _, label, _ in app.sugg])        # and the bundled ones
                await pilot.press(*"mo")
                await pilot.press("tab")
                self.assertEqual(prompt.value, "/model ")
                self.assertEqual([label for _, label, _ in app.sugg], ["local", "free", "paid"])
                self.assertIsNone(app.sugg_i)                                           # Enter would run /model
                await pilot.press("down", "down")
                await pilot.press("enter")                                              # picks "free"
                self.assertEqual(prompt.value, "/model free ")
                await pilot.press("escape")                                             # closes the list
                self.assertFalse(app.sugg)
                prompt.value = "/he"
                await pilot.pause()
                await pilot.press("enter")                                              # picks /help
                self.assertEqual(prompt.value, "/help ")
                await pilot.press("enter")                                              # runs it
                self.assertEqual(prompt.value, "")
                self.assertFalse(app.sugg)
                notes = " ".join(str(w.render()) for w in app.query(".note"))
                self.assertIn("/note-writer", notes)
        asyncio.run(go())

    def with_ui(self, app, ui):
        """note_writer again, as a new version with these ui hints."""
        reg = Registry(app.cfg.abspath("state"), run_id="hints", auto_accept=True)
        m = reg.get("note_writer")
        reg.install({**{k: v for k, v in m.items() if not k.startswith("_")}, "ui": ui}, m["_dir"])

    def run_slash(self, app, text, answer, form=None):
        """Type text, fill the form (form(pilot, screen)) when one opens, answer the cards (answer(card)), wait for
        the run to end. Returns the card titles."""
        titles = []

        async def go():
            async with app.run_test(size=(160, 50)) as pilot:
                app.query_one("#prompt", Input).value = text
                await pilot.press("escape", "enter")
                if form:
                    await pilot.pause(0.2)
                    self.assertIsInstance(app.screen, ToolForm)
                    await form(pilot, app.screen)
                for _ in range(900):
                    await pilot.pause(0.1)
                    card = app.card
                    if card is not None and card.active:
                        titles.append(card.ev["title"])
                        card.answer(answer(card))
                    if not app.busy and app.history:
                        break
                self.assertFalse(app.busy)
                self.blocks = [b for b in app.query(Block) if b.kind == "tool"]
                self.cards = list(app.query(QuestionCard))
        asyncio.run(go())
        return titles

    def test_ask_mode_tool_asks_in_cards_and_joins_the_conversation(self):
        from mini.harness.approvals import Decision
        folder = git_folder({"README": "x\n"})
        app = self.app(folder)
        self.with_ui(app, {"mode": "ask"})
        replies = {"Footer line": "ft", "Access token": SECRET}

        def answer(card):
            for k, v in replies.items():
                if k in card.ev["title"]:
                    return Decision(True, "answer", v, "user")
            return Decision(True, "yes", "", "user")
        titles = self.run_slash(app, "/note-writer --title hello --body text", answer)
        self.assertEqual(titles, ["Writer profile: Footer line", "Writer profile: Access token", "Add the footer?",
                                  "Write hello?"])                              # title and body were given: not asked
        self.assertEqual(open(os.path.join(folder, "out", "hello.txt")).read(), "text\n-- ft")
        self.assertEqual(len(self.blocks), 1)
        b = self.blocks[0]
        self.assertEqual((b.ev["name"], b.status), ("note_writer", "ok"))
        self.assertIn("out/hello.txt", b.result)
        h = app.history[-1]
        self.assertEqual((h["via"], h["status"], h["outputs"]), ("slash", "success", ["out/hello.txt"]))
        self.assertTrue(h["task"].startswith("/note-writer --title hello"))
        self.assertIsNotNone(app.last_run)                                      # /undo puts it back
        e = journal.entries(folder)[-1]
        self.assertEqual((e["task"], e["files"]), (h["task"], ["out/hello.txt"]))
        from mini.agent.core import Agent
        self.assertIn("ran the tool /note-writer", Agent._past(h))
        logs = os.path.join(journal.runs_dir(folder), e["run"])
        for f in os.listdir(logs):
            self.assertNotIn(SECRET, open(os.path.join(logs, f), errors="replace").read(), f)

    def test_form_mode_answers_are_inputs(self):
        from mini.harness.approvals import Decision
        from mini.harness.settings import SettingsStore
        folder = git_folder({"README": "x\n"})
        app = self.app(folder)
        self.with_ui(app, {"mode": "form", "order": ["title"], "fields": {"body": {"multiline": True}}})
        SettingsStore(app.cfg.abspath("state")).save("writer_profile", {"footer": "f", "token": "t"},
                                                     MANIFEST["settings"][0])

        async def form(pilot, screen):
            self.assertEqual(screen.query_one("#" + fid("title"), Input).value, "formed")     # prefilled inline
            screen.query_one("#" + fid("body"), TextArea).text = "from the form"
            screen.query_one("#" + fid("go"), Switch).value = True
            await pilot.press("ctrl+s")
        titles = self.run_slash(app, "/note-writer --title formed", lambda c: Decision(True, "yes", "", "user"), form)
        self.assertEqual(titles, [])                                            # the form answered everything
        self.assertEqual(open(os.path.join(folder, "out", "formed.txt")).read(), "from the form")
        self.assertEqual(app.history[-1]["outputs"], ["out/formed.txt"])


if __name__ == "__main__":
    unittest.main()
