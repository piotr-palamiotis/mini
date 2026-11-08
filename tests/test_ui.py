"""Textual UI driven headlessly: a task runs with a scripted model, approval dialogs appear and are answered,
the attempt counter and registry browser update, rollback works. Needs podman (the tool tests really run)."""
import asyncio
import os
import tempfile
import unittest

from mini import bundled
from mini.harness.registry import Registry
from textual.app import App
from textual.widgets import Markdown

from mini.ui.app import Block, MiniApp, QuestionCard, TextBlock
from tests.helpers import call, fake_backend, step, temp_config
from tests.test_pipeline import script


SHOTS = os.environ.get("MINI_SHOTS", tempfile.mkdtemp(prefix="mini-ui-"))


class UITest(unittest.TestCase):
    def test_task_with_dialogs(self):
        cfg = temp_config(**{"approvals.mode": "ask", "approvals.confirm_first": True})
        cfg.path = os.path.join(tempfile.mkdtemp(), "mini.toml")
        app = MiniApp(cfg, backend_factory=lambda: fake_backend(script()))
        seen, handled = [], set()

        async def go():
            async with app.run_test(size=(180, 50)) as pilot:
                src = os.path.join(tempfile.mkdtemp(), "a.txt")
                open(src, "w").write("x\ny\n")
                await pilot.press(*f"/attach {src}")
                await pilot.press("enter")
                await pilot.press(*"How many lines does a.txt have?")
                await pilot.press("enter")
                for _ in range(1500):
                    await pilot.pause(0.1)
                    card = app.card
                    if card is not None and card.active and id(card) not in handled:
                        handled.add(id(card))
                        seen.append(card.req)
                        if card.req in ("confirm", "install"):
                            app.save_screenshot(os.path.join(SHOTS, f"mini-ui-{card.req}.svg"))
                        await pilot.press("enter")
                    if not app.busy and app.agent and seen:
                        break
                app.save_screenshot(os.path.join(SHOTS, "mini-ui-done.svg"))
                self.assertIn("attempt 2/10", str(app.query_one("#status").render()))
                reg = Registry(cfg.abspath("state"))
                self.assertEqual(set(reg.names()), {"count_text_lines", *bundled.names()})
                self.assertEqual(app.query_one("#reg-table").row_count, 1 + len(bundled.names()))
                cards = list(app.query(QuestionCard))
                self.assertEqual([c.req for c in cards], ["confirm", "plan", "install"])
                self.assertTrue(all(c.final and c.final["status"] == "answered" for c in cards))
                self.assertFalse(app.blocks)                      # nothing left running
                self.assertTrue(any(b.done for b in app.query(Block)))
        asyncio.run(go())
        self.assertEqual(seen, ["confirm", "plan", "install"])

    def test_model_command_sticks_to_the_session(self):
        cfg = temp_config(**{"approvals.confirm_first": False, "paid.enabled": True})
        cfg.path = os.path.join(tempfile.mkdtemp(), "mini.toml")
        quick = lambda: fake_backend([step(call("understand", goal="g", inputs=[], outputs=["n"], plan="1")),
                                      step(call("decide_coverage", reasoning="none needed", use=[], missing=[])),
                                      step(call("finish", answer="done"))])
        app = MiniApp(cfg, backend_factory=quick)
        used = []

        async def go():
            async with app.run_test(size=(180, 50)) as pilot:
                await pilot.press(*"/model paid")
                await pilot.press("enter")
                self.assertIn("paid", str(app.query_one("#status").render()))
                for _ in range(2):
                    await pilot.press(*"say done")
                    await pilot.press("enter")
                    for _ in range(100):
                        await pilot.pause(0.1)
                        if not app.busy:
                            break
                    used.append(app.agent.start_tier)
                self.assertIn("paid", str(app.query_one("#status").render()))
        asyncio.run(go())
        self.assertEqual(used, ["paid", "paid"])           # /model is the session's model until changed


class ViewsTest(unittest.TestCase):
    def app(self, backend):
        cfg = temp_config(**{"approvals.confirm_first": False})
        cfg.path = os.path.join(tempfile.mkdtemp(), "mini.toml")
        return MiniApp(cfg, backend_factory=backend)

    def test_views_open_full_screen_and_escape_returns(self):
        app = self.app(lambda: fake_backend([]))

        async def go():
            async with app.run_test(size=(180, 50)) as pilot:
                prompt = app.query_one("#prompt")
                for cmd, pane in (("/registry", "tab-reg"), ("/config", "tab-cfg"), ("/tools", "tab-tools")):
                    await pilot.press(*cmd)
                    await pilot.press("enter")
                    self.assertEqual(app.query_one("#views").active, pane)
                    self.assertFalse(prompt.display)
                    await pilot.press("escape")
                    self.assertEqual(app.query_one("#views").active, "tab-stream")
                    self.assertTrue(prompt.display)
                    self.assertTrue(prompt.has_focus)
                await pilot.press(*"/tools")
                await pilot.press("enter")
                table = app.query_one("#tools-table")
                names = [k.value for k in table.rows]
                self.assertIn("read_file", names)
                row = table.get_row("read_file")
                self.assertEqual(row[1], "core")
                self.assertIn("path", row[2])
                adv = app.query_one("#cfg-advanced")
                self.assertTrue(adv.collapsed)
                self.assertIsNotNone(adv.query_one("#f-watchdogs-swap_watch"))
        asyncio.run(go())

    def test_task_keeps_running_under_a_view(self):
        import threading
        gate = threading.Event()

        class Slow:
            def __init__(self, inner):
                self.inner = inner

            def __getattr__(self, name):
                return getattr(self.inner, name)

            def chat(self, *a, **kw):
                gate.wait(30)
                return self.inner.chat(*a, **kw)

        quick = [step(call("understand", goal="g", inputs=[], outputs=["n"], plan="1")),
                 step(call("decide_coverage", reasoning="none needed", use=[], missing=[])),
                 step(call("finish", answer="done"))]
        app = self.app(lambda: Slow(fake_backend(quick)))

        async def go():
            async with app.run_test(size=(180, 50)) as pilot:
                await pilot.press(*"say done")
                await pilot.press("enter")
                await pilot.pause(0.3)
                self.assertTrue(app.busy)
                await pilot.press(*"/config")
                await pilot.press("enter")
                await pilot.press("escape")
                await pilot.press(*"/tools")
                await pilot.press("enter")
                self.assertTrue(app.busy)                              # opening a view cancels nothing
                gate.set()
                await pilot.press("escape")
                for _ in range(150):
                    await pilot.pause(0.1)
                    if not app.busy:
                        break
                self.assertFalse(app.busy)
                self.assertEqual(app.query_one("#views").active, "tab-stream")
        asyncio.run(go())


def start(block, **kw):
    return {"kind": "start", "id": "b1", "block": block, "parent": None, **kw}


def end(block, status="ok", secs=3.0, **kw):
    return {"kind": "end", "id": "b1", "block": block, "status": status, "secs": secs, **kw}


class BlockHost(App):
    def __init__(self, *widgets):
        super().__init__()
        self.widgets = widgets

    def compose(self):
        yield from self.widgets


class BlockTest(unittest.TestCase):
    def run_app(self, *widgets, body):
        app = BlockHost(*widgets)

        async def go():
            async with app.run_test(size=(100, 40)) as pilot:
                await body(pilot)
        asyncio.run(go())

    def test_command_block_tails_then_collapses(self):
        b = Block(start("command", argv=["make", "test"]))

        async def body(pilot):
            for i in range(20):
                b.add(f"line {i}")
            shown = b.render_text().plain
            self.assertIn("line 19", shown)
            self.assertIn("line 12", shown)
            self.assertNotIn("line 11", shown)                    # the last 8 only
            b.finish(end("command", "ok", 2.5, lines=20))
            one = b.render_text().plain
            self.assertEqual(one.count("\n"), 0)
            self.assertIn("$ make test", one)
            self.assertIn("ok", one)
            self.assertIn("2.5s", one)
            self.assertIn("20 lines", one)
            await pilot.click(Block)                              # expand
            self.assertIn("line 0", b.render_text().plain)
            await pilot.click(Block)
            self.assertNotIn("line 0", b.render_text().plain)
        self.run_app(b, body=body)

    def test_failed_block_stays_open(self):
        b = Block(start("tool", name="run_code", args={"code": "1/0"}))

        async def body(pilot):
            b.add("Traceback")
            b.finish(end("tool", "error"))
            self.assertTrue(b.expanded)
            self.assertIn("Traceback", b.render_text().plain)
        self.run_app(b, body=body)

    def test_thinking_streams_then_collapses(self):
        b = Block(start("thinking"))

        async def body(pilot):
            b.add("let me think\nabout it")
            self.assertIn("Thinking", b.render_text().plain)
            self.assertIn("about it", b.render_text().plain)
            b.finish(end("thinking", secs=12.4))
            self.assertEqual(b.render_text().plain, "▸ Thought for 12s")
            b.action_toggle()
            self.assertIn("let me think", b.render_text().plain)
        self.run_app(b, body=body)

    def test_text_block_renders_markdown(self):
        b = TextBlock()

        async def body(pilot):
            b.add("# Title\n")
            b.add("some **bold** text")
            await pilot.pause(0.3)
            self.assertEqual(b.text, "# Title\nsome **bold** text")
            self.assertIsInstance(b, Markdown)
        self.run_app(b, body=body)


class CardTest(unittest.TestCase):
    def ask(self, ev, keys):
        """Show a card, press the keys, return the Decision (None if it did not answer)."""
        got = []
        card = QuestionCard({"kind": "start", "id": "q1", "block": "question", "parent": None, "details": {}, **ev})

        async def go():
            async with BlockHost(card).run_test(size=(100, 40)) as pilot:
                card.ask(got.append)
                await pilot.pause()
                for k in keys:
                    await pilot.press(k)
        asyncio.run(go())
        return got[0] if got else None

    CH = dict(req="question", title="pick", options=["a", "b", "c", "cancel"], choices=["a", "b", "c"], multi=False)
    MULTI = dict(CH, options=["answer", "cancel"], multi=True)
    APPROVE = dict(req="install", title="install x", options=["approve", "refuse"], choices=[], multi=False)
    FREE = dict(req="question", title="name?", options=["answer", "cancel"], choices=[], multi=False)

    def test_single_select(self):
        d = self.ask(self.CH, ["down", "enter"])
        self.assertEqual((d.approved, d.choice, d.picks), (True, "b", ["b"]))
        d = self.ask(self.CH, ["3", "enter"])
        self.assertEqual(d.picks, ["c"])

    def test_single_select_free_text_row(self):
        d = self.ask(self.CH, list("x y") + ["enter"])
        self.assertEqual((d.choice, d.note, d.picks), ("answer", "x y", ["x y"]))
        self.assertIsNone(self.ask(self.CH, ["down", "down", "down", "enter"]))     # empty text row sends nothing

    def test_checklist(self):
        d = self.ask(self.MULTI, ["space", "down", "down", "space", "enter"])
        self.assertEqual((d.choice, d.picks), ("answer", ["a", "c"]))
        d = self.ask(self.MULTI, ["2", "space", "o", "t", "h", "e", "r", "enter"])
        self.assertEqual(d.picks, ["b", "other"])
        d = self.ask(self.MULTI, ["2", "enter"])                                       # nothing ticked: the cursor row
        self.assertEqual(d.picks, ["b"])

    def test_approval_fixed_options_and_note(self):
        d = self.ask(self.APPROVE, ["enter"])
        self.assertEqual((d.approved, d.choice), (True, "approve"))
        d = self.ask(self.APPROVE, ["2", "enter"])
        self.assertEqual((d.approved, d.choice), (False, "refuse"))
        d = self.ask(self.APPROVE, list("ok") + ["enter"])
        self.assertEqual((d.choice, d.note), ("approve", "ok"))
        d = self.ask(self.APPROVE, ["down", "t", "x", "backspace", "enter"])
        self.assertEqual(d.note, "t")

    def test_free_question_and_escape(self):
        d = self.ask(self.FREE, list("Bob") + ["enter"])
        self.assertEqual((d.choice, d.note), ("answer", "Bob"))
        self.assertIsNone(self.ask(self.FREE, ["enter"]))
        d = self.ask(self.APPROVE, ["escape"])
        self.assertEqual((d.approved, d.choice), (False, "refuse"))
        d = self.ask(self.CH, ["escape"])
        self.assertEqual((d.approved, d.choice), (False, "cancel"))

    def test_answered_card_stays_with_the_answer(self):
        card = QuestionCard({"kind": "start", "id": "q", "block": "question", "parent": None, "details": {}, **self.CH})
        card.finish({"status": "answered", "choice": "b", "picks": ["b"], "answer": "", "decided_by": "user"})
        self.assertIn("answered: b", card.render_text().plain)


if __name__ == "__main__":
    unittest.main()
