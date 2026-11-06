"""Textual UI: one scrolling stream (answers as markdown, thinking, tool and command blocks, inline question cards)
with the prompt fixed below it, plus the attempt counter, cap indicator and the full-screen views (/registry,
/config, /tools). A view replaces the stream on screen and Esc returns to it; the task keeps running underneath.

Slash commands (commands.py) run here, never through the agent: `/` first in the prompt opens the autocomplete list
(command names, then --field flags and enum values, then paths), core commands are cmd_<name>() methods, and a tool
opens its form (toolform.py) or starts at once with its questions as cards (ask mode). A tool run is a tool block in
the stream and a finished task of the conversation like any other (harness/toolrun.slash).

The agent runs in a worker thread. The UI consumes the event stream (start / chunk / end, see harness/eventlog.py)
directly: each block event drives one widget in the stream. A question block becomes a card at the bottom of the
stream; the approval handler blocks the agent thread until the card is answered.
"""
import json
import os
import threading
import time
import tomllib

from rich.markup import escape
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (Button, DataTable, Footer, Header, Input, Label, Markdown, OptionList, RichLog, Static,
                             TabbedContent, TabPane, TextArea)

from ..harness import checkpoint
from ..harness import config as config_mod
from ..harness import workspace as ws
from . import commands, settingsform
from . import session as session_mod
from .session import Session
from .toolform import ToolForm
from ..harness.approvals import Decision
from ..harness.config import TIERS
from ..harness.registry import Registry, is_bundled

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
TAIL = 8                                            # live lines shown while a block runs
KEEP = 2000                                         # lines kept per block
NEGATIVE = ("refuse", "stop", "cancel")


def schema_summary(schema):
    """`path, offset?` for an input schema: its properties, optional ones marked."""
    props, req = (schema or {}).get("properties") or {}, set((schema or {}).get("required") or [])
    return ", ".join(k + ("" if k in req else "?") for k in props) or "(none)"


def clip(text, n):
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 1] + "…"


class Block(Static, can_focus=True):
    """A thinking, tool or command block. Running: open, with a spinner, elapsed time and the tail of its live
    lines. Finished: one summary line, expandable by click or Enter; failed tool and command blocks stay open."""
    BINDINGS = [Binding("enter,space", "toggle", "Expand / collapse", show=False)]
    DEFAULT_CSS = """
    Block { height: auto; padding: 0 1; }
    Block:focus { background: $boost; }
    """

    def __init__(self, ev):
        super().__init__()
        self.ev, self.kind = ev, ev["block"]
        self.t0 = time.monotonic()
        self.text, self.lines, self.dropped = "", [], 0           # thinking: text; tool and command: lines
        self.status = self.secs = self.result = None
        self.expanded = False

    @property
    def done(self):
        return self.status is not None

    def label(self):
        if self.kind == "thinking":
            return "Thinking"
        if self.kind == "command":
            return "$ " + clip(" ".join(map(str, self.ev.get("argv") or [])) or self.ev.get("purpose") or "", 90)
        args = ", ".join(f"{k}={clip(v if isinstance(v, str) else json.dumps(v, default=str), 40)}"
                         for k, v in (self.ev.get("args") or {}).items())
        return f"{self.ev.get('name')}({clip(args, 90)})"

    def count(self):
        return len(self.text.splitlines()) if self.kind == "thinking" else self.dropped + len(self.lines)

    def add(self, text):
        if self.kind == "thinking":
            self.text += text
        else:
            self.lines.append(text.rstrip("\n"))
            if len(self.lines) > KEEP:
                del self.lines[0]
                self.dropped += 1
        self.paint()

    def finish(self, ev):
        self.status, self.secs, self.result = ev.get("status", "ok"), ev.get("secs", 0), ev.get("text")
        self.expanded = self.kind != "thinking" and self.status != "ok"           # failed ones stay open
        self.paint()

    def body(self):
        if self.kind == "thinking":
            return self.text.splitlines() if self.expanded or not self.done else []
        lines = self.lines
        if self.result and not lines:
            lines = str(self.result).splitlines()
        return lines if self.expanded else lines[-TAIL:] if not self.done else []

    def render_text(self):
        if self.done:
            if self.kind == "thinking":
                head = Text(f"{'▾' if self.expanded else '▸'} Thought for {self.secs:.0f}s", style="dim italic")
            else:
                ok = self.status == "ok"
                head = Text(f"{'▾' if self.expanded else '▸'} {'✓' if ok else '✗'} ", style="green" if ok else "bold red")
                head.append(self.label(), style="bold" if not ok else "")
                head.append(f"  {self.status}  {self.secs:.1f}s  {self.count()} lines", style="dim" if ok else "red")
        else:
            elapsed = time.monotonic() - self.t0
            head = Text(f"{SPINNER[int(elapsed * 10) % len(SPINNER)]} ", style="yellow")
            head.append("Thinking…" if self.kind == "thinking" else self.label(), style="italic" if self.kind == "thinking" else "bold")
            head.append(f"  {elapsed:.0f}s", style="dim")
        out = Text()
        out.append_text(head)
        body = self.body()
        if self.done and self.expanded:
            body = body[-300:]
        elif not self.done:
            body = body[-TAIL:]
        for line in body:
            out.append("\n  " + line, style="dim italic" if self.kind == "thinking" else "dim")
        return out

    def paint(self):
        if self.is_attached:
            self.update(self.render_text())

    def on_mount(self):
        self.paint()

    def action_toggle(self):
        self.expanded = not self.expanded
        self.paint()

    def on_click(self):
        if self.done:
            self.action_toggle()


class TextBlock(Markdown):
    """The answer, streamed: markdown re-rendered at most every 80 ms while it grows."""
    DEFAULT_CSS = "TextBlock { margin: 0 1; padding: 0; }"

    def __init__(self):
        super().__init__("")
        self.text, self.pending = "", False

    def add(self, text):
        self.text += text
        if not self.pending:
            self.pending = True
            self.set_timer(0.08, self.flush) if self.is_attached else None

    def flush(self):
        self.pending = False
        if self.is_attached:
            self.update(self.text)

    def on_mount(self):
        self.flush()


class QuestionCard(Static, can_focus=True):
    """A question or approval inline in the stream. Rows: the asker's choices (or the fixed approval options) and
    always a free-text row added here. Arrows or number keys move, Space ticks (checklist), Enter sends, typing
    starts the text row, Esc cancels (a refusal). Once answered the card stays in the history with the answer."""
    DEFAULT_CSS = """
    QuestionCard { height: auto; border: round $warning; padding: 0 1; margin: 1 0; }
    QuestionCard.-done { border: round $primary-darken-2; color: $text-muted; }
    """

    def __init__(self, ev):
        super().__init__()
        self.ev, self.req = ev, ev.get("req", "")
        self.choices, self.multi = list(ev.get("choices") or []), bool(ev.get("multi"))
        opts = list(ev.get("options") or [])
        if self.choices:
            self.rows = self.choices
        elif self.req == "question":
            self.rows = []                      # only the text row; Esc cancels
        else:
            self.rows = opts                    # approvals keep their fixed options; the text row is a note
        self.options = opts
        self.details = {k: v for k, v in (ev.get("details") or {}).items()
                        if not (self.multi and k in ("options", "how"))}
        self.secret = bool(self.details.pop("_secret", False))
        self.cursor, self.ticked, self.text = 0, set(), ""
        self.on_answer, self.active, self.final = None, False, None

    def ask(self, on_answer):
        self.on_answer, self.active = on_answer, True
        self.paint()
        self.focus()

    # ---------------------------------------------------------------- keys
    def handle_key(self, key, char=None):
        """Returns True when the key was used."""
        if not self.active:
            return False
        n = len(self.rows)
        if key == "escape":
            self.answer(Decision(False, "refuse" if "refuse" in self.options else "cancel" if "cancel" in self.options
                                 else self.options[-1], "", "user"))
        elif key == "up":
            self.cursor = max(0, self.cursor - 1)
        elif key == "down":
            self.cursor = min(n, self.cursor + 1)
        elif key == "enter":
            d = self.decision()
            if d is None:
                return True
            self.answer(d)
            return True
        elif key == "backspace":
            self.text = self.text[:-1]
            self.cursor = n
        elif key == "space" and self.cursor < n and not self.text:
            if self.multi:
                self.ticked ^= {self.cursor}
        elif char and char.isprintable():
            if char in "123456789" and not self.text and int(char) <= n:
                self.cursor = int(char) - 1
            else:
                self.cursor, self.text = n, self.text + char
        else:
            return False
        self.paint()
        return True

    def on_key(self, event):
        if self.handle_key(event.key, event.character):
            event.stop()
            event.prevent_default()

    def decision(self):
        n, text = len(self.rows), self.text.strip()
        if self.choices and self.multi:
            picks = [self.choices[i] for i in sorted(self.ticked)]
            if not picks and not text:
                picks = [self.choices[self.cursor]] if self.cursor < n else []
            picks += [text] if text else []
            return Decision(True, "answer", ", ".join(picks), "user", picks=picks) if picks else None
        if self.choices:
            if self.cursor < n:
                return Decision(True, self.choices[self.cursor], text, "user", picks=[self.choices[self.cursor]])
            return Decision(True, "answer", text, "user", picks=[text]) if text else None
        if self.req == "question":
            return Decision(True, "answer", text, "user") if text else None
        choice = self.rows[self.cursor] if self.cursor < n else self.rows[0]     # the text row sends the first option
        return Decision(choice not in NEGATIVE, choice, text, "user")

    def answer(self, decision):
        self.active = False
        cb, self.on_answer = self.on_answer, None
        self.paint()
        try:
            self.app.query_one("#prompt", Input).focus()
        except Exception:                                    # noqa: BLE001 - the card may outlive the screen
            pass
        if cb:
            cb(decision)

    def finish(self, ev):
        self.final = ev
        self.active = False
        self.add_class("-done")
        self.paint()

    # ---------------------------------------------------------------- view
    def render_text(self):
        out = Text()
        title = self.ev.get("title", "")
        out.append(f"{'?' if self.req == 'question' else self.req.upper()} {title}", style="bold yellow" if not self.final else "bold")
        for k, v in self.details.items():
            val = v if isinstance(v, str) else json.dumps(v, default=str)
            out.append(f"\n  {k}: ", style="dim bold")
            out.append(val if len(val) <= 1200 else val[:1200] + "…", style="dim")
        if self.final:
            st = self.final.get("status")
            ans = ", ".join(map(str, self.final.get("picks") or [])) or self.final.get("answer") or self.final.get("choice") or ""
            note = self.final.get("answer") if self.final.get("picks") or self.final.get("choice") not in ("answer", None) else ""
            out.append(f"\n  -> {st}: " + (str(ans) if st == "answered" else self.final.get("choice") or ""),
                       style="green" if st == "answered" else "red")
            if note and note != ans and st == "answered":
                out.append(f" (note: {note})", style="dim")
            if self.final.get("decided_by") not in (None, "user"):
                out.append(f"  [{self.final['decided_by']}]", style="dim")
            return out
        if not self.active:
            out.append("\n  waiting…", style="dim")
            return out
        for i, row in enumerate(self.rows):
            mark = ("[x] " if i in self.ticked else "[ ] ") if self.multi else ""
            out.append(f"\n{'❯' if self.cursor == i else ' '} {i + 1}. {mark}{row}", style="bold" if self.cursor == i else "")
        shown = "*" * len(self.text) if self.secret else self.text
        hint = "your answer" if self.req == "question" and not self.choices else "other / note"
        at = self.cursor == len(self.rows)
        out.append(f"\n{'❯' if at else ' '} ", style="bold" if at else "")
        out.append(shown + ("▌" if at else "") if self.text or at else hint, style="" if self.text or at else "dim italic")
        out.append("\n  ↑↓ / 1-9 move" + ("   Space ticks" if self.multi else "") + "   Enter sends   Esc cancels", style="dim")
        return out

    def paint(self):
        if self.is_attached:
            self.update(self.render_text())

    def on_mount(self):
        self.paint()


class ApprovalScreen(ModalScreen):
    """One approval dialog. Returns a Decision through dismiss()."""
    DEFAULT_CSS = """
    ApprovalScreen { align: center middle; }
    #box { width: 90; max-height: 90%; border: thick $warning; background: $surface; padding: 1 2; }
    #title { text-style: bold; color: $warning; margin-bottom: 1; }
    #details { height: auto; max-height: 30; }
    #buttons { height: auto; margin-top: 1; }
    #buttons Button { margin-right: 1; }
    """

    def __init__(self, kind, title, details, options):
        super().__init__()
        self.kind, self.title_text, self.details, self.options = kind, title, details, options

    def compose(self):
        with Vertical(id="box"):
            yield Label(f"[{self.kind.upper()}] {self.title_text}", id="title")
            with VerticalScroll(id="details"):
                for k, v in self.details.items():
                    if str(k).startswith("_"):
                        continue
                    val = v if isinstance(v, str) else json.dumps(v, indent=1)
                    yield Static(f"[b]{escape(str(k))}[/b]: {escape(val)}")
            yield Input(placeholder="your answer" if self.kind == "question" else
                        "optional note / hint / clarification for the agent", id="note",
                        password=bool(self.details.get("_secret")))
            with Horizontal(id="buttons"):
                for i, o in enumerate(self.options):
                    yield Button(o, id=f"opt-{i}", variant="success" if i == 0 else
                                 "error" if o in ("refuse", "stop", "cancel") else "default")

    @on(Button.Pressed)
    def pressed(self, ev):
        choice = self.options[int(ev.button.id.split("-")[1])]
        note = self.query_one("#note", Input).value
        self.dismiss(Decision(choice not in ("refuse", "stop", "cancel"), choice, note, "user"))


class PromptInput(Input):
    """The prompt. While the autocomplete list is open, Tab, Enter, the arrows and Esc go to it first."""

    async def _on_key(self, event):
        if self.app.completion_key(event.key):
            event.stop()
            event.prevent_default()
            return
        await super()._on_key(event)


class MiniApp(App):
    TITLE = "mini"
    CSS = """
    #status { height: 1; background: $boost; padding: 0 1; }
    #col { height: 1fr; }
    #views { height: 1fr; }
    #sessions { height: 1fr; }
    .stream { height: 1fr; }
    #prompt { height: 3; }
    #complete { height: auto; max-height: 12; display: none; border: none; padding: 0 1; background: $boost; }
    #complete.-show { display: block; }
    #views > ContentTabs { display: none; }
    #reg-table { height: 12; }
    #tools-table { height: 14; }
    #tools-detail { height: 1fr; }
    #reg-detail { height: 1fr; }
    #reg-buttons { height: auto; }
    #cfg-text { height: 1fr; }
    #cfg-msg { height: 2; }
    #cfg-form { height: 1fr; padding: 0 1; }
    #cfg-form .grp { text-style: bold; color: $accent; margin-top: 1; }
    #cfg-form .row { height: 1; }
    #cfg-form .lbl { width: 1fr; }
    #cfg-form Switch { border: none; height: 1; padding: 0 1; }
    #cfg-form Input { border: none; height: 1; padding: 0 1; width: 22; background: $boost; }
    #cfg-form Select { width: 22; height: 1; }
    #cfg-form SelectCurrent { border: none; height: 1; padding: 0 1; background: $boost; }
    #cfg-form Collapsible { height: auto; padding: 0; border: none; }
    #cfg-form .note { color: $text-muted; margin-top: 1; }
    #cfg-buttons { height: auto; }
    #cfg-buttons Button { margin-right: 1; }
    """
    BINDINGS = [Binding("ctrl+r", "refresh_registry", "Refresh registry"), Binding("ctrl+c", "quit", "Quit"),
                Binding("escape", "back", "Back to the stream"), Binding("ctrl+t", "new_session", "New session"),
                Binding("alt+w", "close_session", "Close session")] + [
        Binding(f"alt+{i}", f"goto_session({i})", show=False) for i in range(1, 10)]
    VIEWS = {"registry": "tab-reg", "config": "tab-cfg", "tools": "tab-tools"}

    def __init__(self, cfg, backend_factory=None, sessions_file=None):
        super().__init__()
        self.cfg = cfg
        self.backend_factory = backend_factory      # tests inject a scripted model
        self.workspace, self.workspace_note = (None, "") if backend_factory else ws.resolve(cfg)   # the launch folder
        self.git_ok = self.workspace is None or checkpoint.is_repo(self.workspace)     # tasks wait for a repository
        self.sessions_file = sessions_file or session_mod.sessions_path(self.workspace)    # None: nothing is kept
        saved, active = session_mod.load(self.sessions_file, cfg["caps"]["attempts_per_tool"]) if self.sessions_file else ([], None)
        self.sessions = saved or [Session(1, "1", "local", cfg["caps"]["attempts_per_tool"])]
        self.active = next((x for x in self.sessions if x.n == active), self.sessions[0])
        for x in self.sessions:
            if not self.tier_on(x.tier):
                x.tier = "local"                         # a cloud tier switched off since
        self._ctx = None                            # the session an agent event belongs to, while it is handled
        self.session_n = max(x.n for x in self.sessions)
        self.commands = commands.Commands()         # core commands now, the registry's tools from refresh_registry()
        self.sugg, self.sugg_i = [], None           # autocomplete: [(prompt text, label, description)], highlighted

    @property
    def s(self):
        """The session the code is working for: the one an event belongs to, else the one on screen."""
        return self._ctx or self.active

    @property
    def agent(self):
        return self.s.agent

    @agent.setter
    def agent(self, v):
        self.s.agent = v

    @property
    def history(self):
        return self.s.history

    @history.setter
    def history(self, v):
        self.s.history = v

    @property
    def attachments(self):
        return self.s.attachments

    @attachments.setter
    def attachments(self, v):
        self.s.attachments = v

    @property
    def attempt(self):
        return self.s.attempt

    @attempt.setter
    def attempt(self, v):
        self.s.attempt = v

    @property
    def cap_hit(self):
        return self.s.cap_hit

    @cap_hit.setter
    def cap_hit(self, v):
        self.s.cap_hit = v

    @property
    def busy(self):
        return self.s.busy

    @busy.setter
    def busy(self, v):
        self.s.busy = v

    @property
    def blocks(self):
        return self.s.blocks

    @blocks.setter
    def blocks(self, v):
        self.s.blocks = v

    @property
    def card(self):
        return self.s.card

    @card.setter
    def card(self, v):
        self.s.card = v

    @property
    def last_text(self):
        return self.s.last_text

    @last_text.setter
    def last_text(self, v):
        self.s.last_text = v

    @property
    def last_run(self):
        return self.s.last_run

    @last_run.setter
    def last_run(self, v):
        self.s.last_run = v

    @property
    def next_tier(self):
        return self.s.tier

    @next_tier.setter
    def next_tier(self, v):
        self.s.tier = v

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="status")
        with Vertical(id="col"):
            with TabbedContent(initial="tab-stream", id="views"):
                with TabPane("Stream", id="tab-stream"):
                    with TabbedContent(initial=self.active.pane_id, id="sessions"):
                        for x in self.sessions:
                            with TabPane(x.label(), id=x.pane_id):
                                yield VerticalScroll(id=x.stream_id, classes="stream")
                with TabPane("Registry", id="tab-reg"):
                    yield DataTable(id="reg-table", cursor_type="row")
                    with Horizontal(id="reg-buttons"):
                        yield Input(placeholder="version", id="reg-version", restrict=r"[0-9]*", max_length=4)
                        yield Button("Roll back to version", id="rollback", variant="warning")
                        yield Button("Accept staged", id="accept", variant="success")
                        yield Button("Discard staged", id="discard", variant="error")
                    yield RichLog(id="reg-detail", wrap=True, min_width=1)
                with TabPane("Tools", id="tab-tools"):
                    yield DataTable(id="tools-table", cursor_type="row")
                    yield RichLog(id="tools-detail", wrap=True, min_width=1)
                with TabPane("Config", id="tab-cfg"):
                    with TabbedContent(id="cfg-tabs"):
                        with TabPane("Settings", id="cfg-tab-form"):
                            yield settingsform.widgets(self.cfg)
                            with Horizontal(id="cfg-buttons"):
                                yield Button("Save settings", id="cfg-save-form", variant="primary")
                                yield Button("Reset", id="cfg-reset")
                        with TabPane("Raw TOML", id="cfg-tab-raw"):
                            yield TextArea(self.config_file_text(), language="toml", id="cfg-text")
                            yield Button("Save config", id="cfg-save", variant="primary")
                    yield Static(id="cfg-msg")
            yield OptionList(id="complete")
            yield PromptInput(placeholder="Give mini a task.  / for commands and tools  /help", id="prompt")
        yield Footer()

    def on_mount(self):
        self.query_one("#complete", OptionList).can_focus = False       # the prompt keeps the keys
        self.update_status()
        try:
            Registry(self.cfg.abspath("state")).seed_bundled()     # the bundled tools are slash commands from the start
        except Exception as e:   # noqa: BLE001 - the stream must come up; the first task seeds them again
            self.chat(f"[yellow]bundled tools not installed: {escape(str(e))}[/yellow]")
        self.refresh_registry()
        self.refresh_tools()
        for x in self.sessions:
            self.replay(x)
            self.stream_of(x).anchor()
        self.persist()
        self.stream.anchor()                        # follow the newest block until the user scrolls up
        self.set_interval(0.1, self.tick)
        if len(self.sessions) > 1 or self.history:
            self.chat(f"[dim]restored {len(self.sessions)} session(s) from the last time[/dim]")
        self.chat("[b]mini[/b] ready. " + "  ".join(
            f"{t}: {self.cfg[t]['model']}" for t in TIERS if self.tier_on(t)) +
            "\nEvery session starts on local; /model changes the current session's model (only configured tiers)."
            " /new opens another session (ctrl+t), /exit quits.")
        self.chat(f"working in [b]{escape(self.workspace)}[/b]: files change in place; every task is checkpointed in "
                  f"git, /undo puts it back." if self.workspace else escape(self.workspace_note) if self.workspace_note else
                  "no folder: tasks work in an empty folder of their run.")
        self.check_tiers()
        if not self.git_ok:
            self.offer_git_init()
        self.query_one("#prompt", Input).focus()

    @work(thread=True)
    def check_tiers(self):
        from ..setup import ensure_local, tier_status
        try:
            ensure_local(self.cfg, lambda m: self.call_from_thread(self.chat, f"[dim]{escape(m)}[/dim]"))
            st = tier_status(self.cfg)
        except Exception as e:   # noqa: BLE001 - a status probe must never break the UI
            st = {"status": (False, str(e))}
        mark = {True: "[green]ok[/green]", False: "[red]!![/red]", None: "[dim]--[/dim]"}
        line = "   ".join(f"{t} {mark[ok]} {escape(text)}" for t, (ok, text) in st.items())
        bad = any(ok is False for ok, _ in st.values())
        self.call_from_thread(self.chat, line + ("\n[dim]mini doctor shows how to fix it; mini setup fixes it[/dim]"
                                                 if bad else ""))

    # ------------------------------------------------------------ status
    def update_status(self):
        s = self.active
        n, budget, who = s.attempt
        tier = who if s.busy else s.tier
        parts = [f"session [b]{escape(s.name)}[/b]",
                 f"[b]{tier}[/b] {self.cfg[tier]['model']}" + ("" if s.busy else " (next task)"),
                 f"[b]attempt {n}/{budget}[/b]",
                 ("[b yellow]waiting for the " + ("local model" if s.waiting == "local" else "folder") + "[/b yellow]")
                 if s.waiting else "[b yellow]running[/b yellow]" if s.busy else "idle"]
        if s.cap_hit:
            parts.append(f"[b white on red] CAP: {escape(s.cap_hit)} [/]")
        self.query_one("#status", Static).update("  |  ".join(parts))
        for t in self.sessions:
            self.relabel(t)

    def persist(self):
        """Keep the tabs on disk (<folder>/.mini/sessions.json) so they outlive the app."""
        if self.sessions_file:
            session_mod.save(self.sessions_file, self.sessions, self.active)

    def replay(self, s):
        """Show a restored session's earlier tasks in its stream; a task that was running when the app closed is
        reported as interrupted."""
        for h in s.history:
            self.stream_of(s).mount(Static(f"[b cyan]you:[/b cyan] {escape(str(h.get('task') or ''))}", classes="note"))
            if h.get("status") == "success":
                if h.get("answer") and h.get("via") != "slash":
                    block = TextBlock()
                    self.stream_of(s).mount(block)
                    block.add(h["answer"])
            else:
                self.stream_of(s).mount(Static(f"[b red]{escape(str(h.get('status')))}[/b red]: "
                                               f"{escape(str(h.get('error') or ''))}", classes="note"))
        if s.pending:
            self.stream_of(s).mount(Static(f"[b cyan]you:[/b cyan] {escape(s.pending)}\n[yellow]interrupted: mini was closed "
                                           f"while this task ran. Files it changed stay; /undo puts them back.[/yellow]",
                                           classes="note"))
            s.history.append({"task": s.pending, "status": "interrupted", "error": "mini was closed while it ran"})
            s.pending = None

    def stream_of(self, s):
        if s.stream is None:
            s.stream = self.query_one("#" + s.stream_id, VerticalScroll)
        return s.stream

    def relabel(self, s):
        try:
            self.query_one("#sessions", TabbedContent).get_tab(s.pane_id).label = s.label()
        except Exception:   # noqa: BLE001 - the tab may not be mounted yet
            pass

    def tier_on(self, tier):
        return tier == "local" or bool(self.cfg[tier].get("enabled", True))

    @property
    def stream(self):
        s = self.s
        if s.stream is None:
            s.stream = self.query_one("#" + s.stream_id, VerticalScroll)
        return s.stream

    def chat(self, text):
        self.stream.mount(Static(text, classes="note"))

    def tick(self):
        """Spinners and elapsed times of the running blocks."""
        for s in self.sessions:
            for w in s.blocks.values():
                if isinstance(w, Block):
                    w.paint()

    # ------------------------------------------------------------ events from the agent thread
    def on_agent_event(self, session, ev):
        """An event of one session's agent: handled with that session as the one the code works for."""
        self._ctx = session
        try:
            self.handle_event(ev)
            if session is not self.active and ev["kind"] in ("run_end", "start") and (
                    ev["kind"] == "run_end" or ev.get("block") == "question"):
                session.attention = True
            if ev["kind"] == "gate":
                session.waiting = ev["gate"] if ev["state"] == "waiting" else ""
            elif ev["kind"] in ("model_reply", "files_changed", "run_end"):
                session.waiting = ""
        finally:
            self._ctx = None
        self.update_status()

    def handle_event(self, ev):
        """The event stream: block events drive the stream widgets, the other kinds update the counters and add a
        note line for what the user should see."""
        k = ev["kind"]
        if k in ("start", "chunk", "end"):
            self.on_block(ev)
            return
        if k == "run_start":
            self.attempt = (0, self.cfg["caps"]["attempts_per_tool"], ev.get("tier", "local"))
        elif k == "attempt":
            budget = ev.get("budget", self.cfg["caps"]["attempts_per_tool"])
            self.attempt = (ev["n"], budget, ev.get("tier", self.attempt[2]))
        elif k == "tier":
            self.attempt = (0, self.cfg["caps"]["attempts_per_tool"], ev["tier"])
            self.chat(f"[b blue]model switched[/b blue] {ev['previous']} -> {ev['tier']} ({escape(ev['model'])}): "
                      f"{escape(ev.get('why', ''))}" + ("\n[dim]task data now leaves this machine[/dim]" if ev.get("leaves_machine") else ""))
        elif k == "phase" and ev.get("phase") in ("create", "repair"):
            self.attempt = (0, self.cfg["caps"]["attempts_per_tool"], self.attempt[2])
            self.chat(f"[b]building a new tool[/b] for: {escape(str(ev.get('need') or ev.get('tool')))}")
        elif k == "cap" or (k == "watchdog" and ev.get("name") in ("attempt_budget", "time_turn_limit", "hardware_swap")):
            self.cap_hit = ev.get("cap") or ev.get("name")
            self.chat(f"[b red]cap reached:[/b red] {escape(self.cap_hit)}")
        elif k == "gap":
            self.chat(f"[b]gap detection:[/b] use {ev.get('use') or '-'}; missing {ev.get('missing') or '-'}")
        elif k == "test_run":
            self.chat(("[green]tests passed[/green] " if ev["passed"] else "[red]tests failed[/red] ") + escape(ev.get("summary", "")))
        elif k == "install":
            self.chat(f"[b green]installed[/b green] {ev['tool']} v{ev['version']} (built by {ev['created_by']['provider']})")
            self.refresh_registry()
        elif k == "gate":
            self.chat("[dim]waiting for the local model: another session is using it[/dim]" if ev["gate"] == "local" else
                      "[dim]waiting for another session's file change to finish[/dim]")
        elif k == "watchdog":
            self.chat(f"[yellow]watchdog[/yellow] {escape(ev.get('name', ''))}")
        elif k == "run_end":
            self.busy = False
            self.close_blocks()
            self.history.append({x: ev.get(x) for x in ("task", "status", "answer", "error", "created", "outputs", "via")})
            self.s.pending = None
            self.persist()
            if ev.get("checkpoint"):
                self.last_run = ev.get("run_id")
            if ev["status"] == "success":
                self.chat(f"[b green]done[/b green] in {ev['secs']} s")
                answer = (ev.get("answer") or "").strip()
                if ev.get("via") == "slash":                          # the tool block already shows its result
                    answer = ""
                if answer and answer not in self.last_text:           # the finish answer, unless it was streamed
                    block = TextBlock()
                    self.stream.mount(block)
                    block.add(answer)
            else:
                self.chat(f"[b red]{ev['status']}[/b red]: {escape(ev.get('error') or '')}")
            if ev.get("outputs"):
                self.chat("files changed: " + escape(", ".join(ev["outputs"]))
                          + ("  [dim](/undo puts them back)[/dim]" if ev.get("checkpoint") else ""))
            self.refresh_registry()
        self.update_status()

    def on_block(self, ev):
        k, bid, block = ev["kind"], ev["id"], ev["block"]
        w = self.blocks.get(bid)
        if k == "start":
            w = (TextBlock() if block == "text" else QuestionCard(ev) if block == "question" else Block(ev))
            self.blocks[bid] = w
            if block == "text":
                self.last_text = ""
            if block == "question":
                self.card = w
                self.show_view("tab-stream")
            self.stream.mount(w)
        elif w is None:
            return
        elif k == "chunk":
            w.add(ev["text"])
            if block == "text":
                self.last_text += ev["text"]
        elif k == "end":
            self.blocks.pop(bid, None)
            if isinstance(w, TextBlock):
                w.flush()
            else:
                w.finish(ev)
            if w is self.card:
                self.card = None

    def close_blocks(self):
        """A run ended with blocks still open (stopped, cancelled): close them so no spinner is left running."""
        for bid, w in list(self.blocks.items()):
            self.on_block({"kind": "end", "id": bid, "block": w.ev["block"] if hasattr(w, "ev") else "text",
                           "status": "stopped", "secs": round(time.monotonic() - getattr(w, "t0", time.monotonic()), 1)})

    def approval_handler(self, kind, title, details, options, session=None):
        """Called from the agent thread; blocks it until the user answers the card the question block opened."""
        done, box = threading.Event(), {}
        session = session or self.active

        def got(decision):
            box["d"] = decision
            done.set()

        def show():
            if session.card is not None:
                session.card.ask(got)
            else:                                           # no card (should not happen): the old dialog
                self.push_screen(ApprovalScreen(kind, title, details, options), got)
        self.call_from_thread(show)
        done.wait()
        return box["d"]

    # ------------------------------------------------------------ input
    @on(Input.Submitted, "#prompt")
    def submitted(self, ev):
        text = ev.value.strip()
        ev.input.value = ""
        self.hide_completion()
        if not text:
            return
        if self.commands.is_command(text):
            self.run_command(text)
            return
        self.show_view("tab-stream")
        if not self.can_start():
            return
        self.chat(f"[b cyan]you:[/b cyan] {escape(text)}" + (f"  [dim](attached: {', '.join(os.path.basename(a) for a in self.attachments)})[/dim]" if self.attachments else ""))
        self.busy, self.cap_hit = True, ""
        self.s.pending = text
        self.persist()
        tier = self.next_tier
        self.attempt = (0, self.cfg["caps"]["attempts_per_tool"], tier)
        self.update_status()
        self.run_task(self.s, text, list(self.attachments), tier)
        self.attachments = []

    def can_start(self):
        """Whether a task or a tool may start now (one at a time, in a git folder); says why not."""
        if self.busy:
            self.chat("[yellow]a task is running[/yellow]")
            return False
        if self.workspace_note and not self.workspace:
            self.chat(f"[yellow]{escape(self.workspace_note)}[/yellow]")
            return False
        if not self.git_ok:
            self.offer_git_init()
            return False
        return True

    # ------------------------------------------------------------ slash commands
    def run_command(self, text):
        """A slash command, in the harness: a core command's cmd_<name>(), or a tool in its ui mode."""
        c, args, problem = self.commands.parse(text)
        if c is None or c.name not in self.VIEWS:
            self.show_view("tab-stream")
        if c is None:
            self.chat(f"[yellow]{escape(problem)}[/yellow]")
            return
        if problem:
            self.chat(f"[yellow]{escape(problem)}[/yellow]   usage: {escape(c.usage())}")
            return
        if not c.tool:
            getattr(self, "cmd_" + c.name)(args)
            return
        if not self.can_start():
            return
        if c.mode == "form":
            self.push_screen(ToolForm(c, args, self.folder()),
                             lambda inputs: self.start_tool(c, inputs) if inputs is not None else self.chat(
                                 f"[dim]/{c.name} cancelled[/dim]"))
        else:
            self.start_tool(c, args)

    def start_tool(self, c, inputs):
        if not self.can_start():                             # a task may have started while the form was open
            return
        line = c.line(inputs)
        self.chat(f"[b cyan]you:[/b cyan] {escape(line)}")
        self.busy, self.cap_hit = True, ""
        self.s.pending = line
        self.persist()
        self.update_status()
        self.run_tool(self.s, c.tool, inputs, line)

    def run_tool(self, session, name, inputs, line):
        self.run_worker(lambda: self._run_tool(session, name, inputs, line), thread=True, group=f"run-{session.n}",
                        exclusive=True)

    def _run_tool(self, session, name, inputs, line):
        from ..harness import toolrun
        cfg = self.cfg if self.backend_factory else config_mod.load(self.cfg.path)
        sink = lambda ev: self.call_from_thread(self.on_agent_event, session, ev)
        m = Registry(cfg.abspath("state")).get(name)
        if not m:
            sink({"kind": "run_end", "task": line, "status": "failed", "error": f"no accepted tool named {name}",
                  "secs": 0, "via": "slash"})
            return
        toolrun.slash(cfg, self.workspace, m, inputs, line, lambda *a: self.approval_handler(*a, session=session),
                      sinks=[sink])

    def folder(self):
        """The project folder: where paths complete from and relative paths point."""
        return self.workspace or os.getcwd()

    def cmd_model(self, args):
        tier = args.get("tier")
        on = [t for t in TIERS if self.tier_on(t)]
        if tier is None:
            self.chat(f"this session's model: [b]{self.next_tier}[/b]. Choose: /model {'|'.join(on)}")
        elif not self.tier_on(tier):
            self.chat(f"[yellow]{tier} is not configured[/yellow] (only {', '.join(on)}); `mini setup` adds a cloud tier")
        else:
            self.next_tier = tier
            self.persist()
            self.chat(f"this session's tasks run on [b]{tier}[/b] ({escape(self.cfg[tier]['model'])})"
                      + ("\n[dim]task data leaves this machine[/dim]" if tier != "local" else ""))
        self.update_status()

    def cmd_clear(self, args):
        self.history = []
        self.persist()
        self.chat("conversation cleared: earlier tasks of this session are forgotten")

    def cmd_new(self, args):
        self.run_worker(self.add_session(args.get("name")), exclusive=False)

    async def add_session(self, name=None):
        self.session_n += 1
        s = Session(self.session_n, name or str(self.session_n), "local", self.cfg["caps"]["attempts_per_tool"])
        s.stream = VerticalScroll(id=s.stream_id, classes="stream")
        self.sessions.append(s)
        self.persist()
        tabs = self.query_one("#sessions", TabbedContent)
        await tabs.add_pane(TabPane(s.label(), s.stream, id=s.pane_id))
        tabs.active = s.pane_id
        self.switch_to(s)
        s.stream.anchor()
        self.chat(f"session [b]{escape(s.name)}[/b]: runs on local; /model changes it; alt+1..9 or /session switches")

    def switch_to(self, s):
        self.active = s
        self.persist()
        s.attention = False
        self.show_view("tab-stream")
        self.update_status()

    @on(TabbedContent.TabActivated, "#sessions")
    def session_tab_activated(self, ev):
        for s in self.sessions:
            if s.pane_id == ev.pane.id and s is not self.active:
                self.switch_to(s)

    def find_session(self, ref):
        ref = str(ref or "").strip()
        for s in self.sessions:
            if ref == s.name or ref == str(s.n):
                return s
        if ref.isdigit() and 1 <= int(ref) <= len(self.sessions):
            return self.sessions[int(ref) - 1]
        return None

    def cmd_session(self, args):
        ref = args.get("target")
        if ref is None:
            self.chat("sessions: " + ", ".join(f"{i}:{escape(s.name)} ({s.tier}{', running' if s.busy else ''})"
                                               for i, s in enumerate(self.sessions, 1)))
            return
        s = self.find_session(ref)
        if s is None:
            self.chat(f"[yellow]no session {escape(str(ref))}[/yellow]")
            return
        self.query_one("#sessions", TabbedContent).active = s.pane_id

    def cmd_rename(self, args):
        self.active.name = args["name"]
        self.persist()
        self.update_status()

    def cmd_close(self, args):
        s = self.find_session(args["target"]) if args.get("target") else self.active
        if s is None:
            self.chat(f"[yellow]no session {escape(str(args.get('target')))}[/yellow]")
        elif s.busy:
            self.chat(f"[yellow]session {escape(s.name)} is running a task[/yellow]")
        elif len(self.sessions) == 1:
            if self.sessions_file:                           # closing the last session ends mini and forgets it
                try:
                    os.remove(self.sessions_file)
                except OSError:
                    pass
                self.sessions_file = None
            self.exit()
        else:
            self.run_worker(self.remove_session(s), exclusive=False)

    async def remove_session(self, s):
        i = self.sessions.index(s)
        self.sessions.remove(s)
        self.persist()
        tabs = self.query_one("#sessions", TabbedContent)
        if s is self.active:
            nxt = self.sessions[min(i, len(self.sessions) - 1)]
            tabs.active = nxt.pane_id
            self.switch_to(nxt)
        await tabs.remove_pane(s.pane_id)

    def action_new_session(self):
        self.run_command("/new")

    def action_close_session(self):
        self.run_command("/close")

    def action_goto_session(self, i):
        if 1 <= i <= len(self.sessions):
            self.query_one("#sessions", TabbedContent).active = self.sessions[i - 1].pane_id

    def cmd_attach(self, args):
        p = os.path.join(self.folder(), os.path.expanduser(args["path"]))     # an absolute path stays as it is
        if os.path.isfile(p):
            self.attachments.append(os.path.abspath(p))
            self.chat(f"attached {escape(args['path'])}")
        else:
            self.chat(f"[red]no such file:[/red] {escape(args['path'])}")

    def cmd_status(self, args):
        self.check_tiers()

    def cmd_undo(self, args):
        self.undo(args.get("target") or [])

    def cmd_registry(self, args):
        self.show_view("tab-reg")

    def cmd_config(self, args):
        self.show_view("tab-cfg")

    def cmd_tools(self, args):
        self.show_view("tab-tools")

    def cmd_exit(self, args):
        self.exit()

    def cmd_help(self, args):
        self.chat(escape(self.commands.help_text()) + "\n/config: switches and fields for the settings (saved in "
                  "place), or the raw TOML.")

    # ------------------------------------------------------------ autocomplete
    @on(Input.Changed, "#prompt")
    def prompt_changed(self, ev):
        self.update_completion(ev.value)

    def update_completion(self, value):
        """Suggestions for the prompt text while it is a command being typed. The first is highlighted while a name
        or a word is being typed; on an empty argument nothing is, so Enter runs the command (Tab picks)."""
        self.sugg = self.commands.complete(value, self.folder()) if value.startswith("/") else []
        if self.sugg and len(self.sugg) == 1 and self.sugg[0][0].rstrip() == value.rstrip() and value.endswith(" "):
            self.sugg = []                                   # nothing left to offer
        ol = self.query_one("#complete", OptionList)
        ol.clear_options()
        if not self.sugg:
            self.sugg_i = None
            ol.remove_class("-show")
            return
        for _, label, desc in self.sugg:
            t = Text(label, style="bold")
            if desc:
                t.append("  " + clip(desc, 100), style="dim")
            ol.add_option(t)
        ol.add_class("-show")
        self.set_highlight(0 if " " not in value or value.rpartition(" ")[2] else None)

    def set_highlight(self, i):
        self.sugg_i = i
        ol = self.query_one("#complete", OptionList)
        ol.highlighted = i
        if i is not None:
            ol.scroll_to_highlight()

    def hide_completion(self):
        self.sugg, self.sugg_i = [], None
        ol = self.query_one("#complete", OptionList)
        ol.clear_options()
        ol.remove_class("-show")

    def pick(self, i):
        prompt = self.query_one("#prompt", Input)
        prompt.value = self.sugg[i][0]
        prompt.cursor_position = len(prompt.value)
        self.update_completion(prompt.value)               # the next step: flags, values, the folder's entries

    def completion_key(self, key):
        """A key for the open autocomplete list. True when it was used: arrows move, Tab picks (the first when none
        is highlighted), Enter picks the highlighted one unless the prompt already says it, Esc closes the list."""
        if not self.sugg:
            return False
        n = len(self.sugg)
        if key in ("down", "up"):
            i = self.sugg_i
            self.set_highlight(0 if i is None and key == "down" else n - 1 if i is None else (i + (1 if key == "down" else -1)) % n)
            return True
        if key == "tab":
            self.pick(self.sugg_i or 0)
            return True
        if key == "enter":
            value = self.query_one("#prompt", Input).value
            if self.sugg_i is None or self.sugg[self.sugg_i][0].rstrip() == value.rstrip():
                return False                                 # the prompt is complete: Enter runs it
            self.pick(self.sugg_i)
            return True
        if key == "escape":
            self.hide_completion()
            return True
        return False

    @on(OptionList.OptionSelected, "#complete")
    def clicked_suggestion(self, ev):
        self.pick(ev.option_index)
        self.query_one("#prompt", Input).focus()

    def run_task(self, session, text, attachments, tier):
        self.run_worker(lambda: self._run_task(session, text, attachments, tier), thread=True, group=f"run-{session.n}",
                        exclusive=True)

    def _run_task(self, session, text, attachments, tier):
        from ..agent.core import Agent
        cfg = self.cfg if self.backend_factory else config_mod.load(self.cfg.path)
        session.agent = Agent(cfg, approval_handler=lambda *a: self.approval_handler(*a, session=session), start_tier=tier,
                              history=session.history, workspace=self.workspace,
                              tier_backends={tier: self.backend_factory()} if self.backend_factory else None)
        session.agent.log.subscribe(lambda ev: self.call_from_thread(self.on_agent_event, session, ev))
        session.agent.run_task(text, attachments)

    def offer_git_init(self):
        """The git-required guard: say so and offer `git init`; no task runs until the user agrees."""
        def got(d):
            if d.approved and d.choice == "git init":
                try:
                    checkpoint.init(self.workspace)
                    self.git_ok = True
                    self.chat(f"[b green]git init[/b green] done in {escape(self.workspace)}")
                except checkpoint.GitError as e:
                    self.chat(f"[red]git init failed:[/red] {escape(str(e))}")
            else:
                self.chat("[yellow]no repository, so mini does not work in this folder. Give a task to be asked again.[/yellow]")
        self.push_screen(ApprovalScreen("git", "This folder is not a git repository",
                                        {"why": checkpoint.guard_note(self.workspace)}, ("git init", "not now")), got)

    def undo(self, args):
        """Put back what a task changed, from its checkpoint."""
        if self.busy:
            self.chat("[yellow]a task is running[/yellow]")
            return
        if not self.workspace or not self.git_ok:
            self.chat("nothing to undo: no checkpoints without a git repository")
            return
        try:
            run, files = checkpoint.undo_args(self.workspace, args)
            if run is None and not args and self.last_run:
                run = self.last_run                          # default: this session's last task or tool run
            run, done = checkpoint.undo(self.workspace, run, files)
        except checkpoint.GitError as e:
            self.chat(f"[red]undo:[/red] {escape(str(e))}")
            return
        self.chat(f"[b]undo {escape(run)}[/b]: " + (escape(", ".join(f"{rel} {how}" for rel, how in done)) or "nothing to undo"))

    # ------------------------------------------------------------ full-screen views
    def show_view(self, pane):
        """Show one view full screen (the stream, or /registry, /config, /tools). Only the display changes: the
        running task, its blocks and its question card stay where they are."""
        self.query_one("#views", TabbedContent).active = pane
        self.query_one("#prompt", Input).display = pane == "tab-stream"
        if pane == "tab-stream":
            self.query_one("#prompt", Input).focus()
        elif pane == "tab-tools":
            self.refresh_tools()

    def check_action(self, action, parameters):
        if action == "back":
            return self.query_one("#views", TabbedContent).active != "tab-stream"    # on the stream Esc is left to the card
        return True

    def action_back(self):
        self.show_view("tab-stream")

    # ------------------------------------------------------------ tools view
    def tool_rows(self):
        """[(name, description, source, schema summary, input schema)] of the core capabilities, then the registry's."""
        from .. import core
        rows = [(c.name, c.description, "core", schema_summary(c.schema), c.schema) for c in core.load().caps.values()]
        reg = Registry(self.cfg.abspath("state"))
        for m in reg.list_tools():
            src = "bundled" if is_bundled(m["name"]) else "registry"
            rows.append((m["name"], m.get("description", ""), f"{src} v{m['version']}",
                         schema_summary(m.get("input_schema")), m.get("input_schema") or {}))
        return rows

    def refresh_tools(self):
        t = self.query_one("#tools-table", DataTable)
        t.clear(columns=True)
        t.add_columns("name", "source", "inputs", "description")
        self.tool_schemas = {}
        for name, desc, src, summ, schema in self.tool_rows():
            t.add_row(name, src, summ, clip(desc, 80), key=name)
            self.tool_schemas[name] = (desc, src, schema)

    @on(DataTable.RowHighlighted, "#tools-table")
    def show_tool_spec(self, ev):
        if ev.row_key is None or ev.row_key.value not in getattr(self, "tool_schemas", {}):
            return
        desc, src, schema = self.tool_schemas[ev.row_key.value]
        d = self.query_one("#tools-detail", RichLog)
        d.clear()
        d.write(f"{ev.row_key.value} ({src})\n{desc}\n")
        d.write(json.dumps(schema, indent=1))

    # ------------------------------------------------------------ registry browser
    def action_refresh_registry(self):
        self.refresh_registry()

    def refresh_registry(self):
        reg = Registry(self.cfg.abspath("state"))
        self.commands = commands.Commands.load(reg)          # every accepted tool is a slash command
        t = self.query_one("#reg-table", DataTable)
        t.clear(columns=True)
        t.add_columns("tool", "active", "versions", "built by", "tests", "attempts")
        for m in reg.list_tools():
            cb = m.get("created_by", {})
            by = f"{cb.get('provider')}/{cb.get('model')}" + (f" (from {cb['escalated_from']})" if cb.get("escalated_from") else "")
            tr = m.get("test_results") or {}
            t.add_row(m["name"], f"v{m['version']}", ",".join(map(str, reg.versions(m["name"]))), by,
                      ("pass: " if tr.get("passed") else "FAIL: ") + tr.get("summary", "")[:30],
                      json.dumps(m.get("attempts_used")), key=m["name"])
        for st in reg.staged():
            cb, tr = st["created_by"] or {}, st["test_results"] or {}
            t.add_row(st["name"], "[staged]", f"run {st['run_id']}" + (" (outdated)" if st["outdated"] else ""),
                      f"{cb.get('provider')}/{cb.get('model')}", ("pass: " if tr.get("passed") else "FAIL: ") +
                      tr.get("summary", "")[:30], "", key=f"staged:{st['name']}:{st['run_id']}")
        if not reg.names() and not reg.staged():
            self.query_one("#reg-detail", RichLog).clear().write("(registry is empty)")
        if self.query_one("#views", TabbedContent).active == "tab-tools":
            self.refresh_tools()

    @on(DataTable.RowHighlighted, "#reg-table")
    def show_tool(self, ev):
        if ev.row_key is None:
            return
        reg = Registry(self.cfg.abspath("state"))
        d = self.query_one("#reg-detail", RichLog)
        d.clear()
        key = ev.row_key.value
        if key.startswith("staged:"):
            _, name, run = key.split(":", 2)
            self.selected_tool, self.selected_run = name, run
            d.write(f"{name} staged by run {run}. Accept makes it the next version; discard deletes it.\n")
            d.write(reg.diff(name, run))
            return
        self.selected_tool, self.selected_run = key, None
        m = reg.get(key)
        d.write(json.dumps({k: v for k, v in m.items() if not k.startswith("_")}, indent=1))

    @on(Button.Pressed, "#rollback")
    def rollback(self):
        name = getattr(self, "selected_tool", None)
        v = self.query_one("#reg-version", Input).value
        if not name or not v:
            self.chat("[yellow]select a tool and type a version to roll back to[/yellow]")
            return
        try:
            Registry(self.cfg.abspath("state")).set_active(name, int(v))
            self.chat(f"[b]rollback:[/b] {name} active version is now v{v}")
        except ValueError as e:
            self.chat(f"[red]{escape(str(e))}[/red]")
        self.refresh_registry()

    @on(Button.Pressed, "#accept")
    def accept_staged(self):
        self._staged_action("accept")

    @on(Button.Pressed, "#discard")
    def discard_staged(self):
        self._staged_action("discard")

    def _staged_action(self, action):
        from ..harness.registry import RegistryError
        name, run = getattr(self, "selected_tool", None), getattr(self, "selected_run", None)
        if not run:
            self.chat("[yellow]select a [staged] row first[/yellow]")
            return
        reg = Registry(self.cfg.abspath("state"))
        try:
            if action == "accept":
                outdated = any(s["outdated"] for s in reg.staged() if s["name"] == name and s["run_id"] == run)
                from ..toolcli import write_shim
                v = reg.accept(name, run, force=outdated and getattr(self, "confirm_replace", None) == (name, run))
                self.chat(f"[b green]accepted[/b green] {name} as v{v}; command: {escape(write_shim(self.cfg, name))}")
            else:
                reg.discard(name, run)
                self.chat(f"discarded the staged {name} from run {run}")
            self.confirm_replace = None
        except RegistryError as e:
            self.confirm_replace = (name, run)           # a second press replaces the newer version
            self.chat(f"[yellow]{escape(str(e))}[/yellow] Press Accept again to replace it.")
        self.selected_run = None
        self.refresh_registry()

    # ------------------------------------------------------------ config screen
    def config_file_text(self):
        try:
            with open(self.cfg.path) as f:
                return f.read()
        except OSError:
            return config_mod.dumps(self.cfg)

    def reload_config(self):
        """Take the file's values into the running UI; the next task uses them."""
        self.cfg = config_mod.load(self.cfg.path)
        for s in self.sessions:                              # a tier that is no longer configured falls back to local
            if s.tier not in TIERS or not self.tier_on(s.tier):
                s.tier = "local"
        settingsform.refresh(self, self.cfg)
        self.query_one("#cfg-text", TextArea).text = self.config_file_text()
        self.update_status()

    @on(Button.Pressed, "#cfg-save-form")
    def save_settings(self):
        msg = self.query_one("#cfg-msg", Static)
        updates, errors = settingsform.collect(self, self.cfg)
        if errors:
            msg.update("[red]not saved: " + escape("; ".join(errors)) + "[/red]")
            return
        if not updates:
            msg.update("nothing changed")
            return
        try:
            config_mod.set_values(self.cfg.path, updates)
        except (OSError, tomllib.TOMLDecodeError) as e:
            msg.update(f"[red]not saved: {escape(str(e))}[/red]")
            return
        self.reload_config()
        msg.update(f"[green]saved: {settingsform.summary(updates)}; applies to the next task[/green]")

    @on(Button.Pressed, "#cfg-reset")
    def reset_settings(self):
        settingsform.refresh(self, self.cfg)
        self.query_one("#cfg-msg", Static).update("form reset to the saved values")

    @on(Button.Pressed, "#cfg-save")
    def save_config(self):
        text = self.query_one("#cfg-text", TextArea).text
        msg = self.query_one("#cfg-msg", Static)
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            msg.update(f"[red]not saved: {escape(str(e))}[/red]")
            return
        with open(self.cfg.path, "w") as f:
            f.write(text)
        self.reload_config()
        msg.update(f"[green]saved to {escape(self.cfg.path)}; applies to the next task[/green]")


def run_ui(cfg):
    MiniApp(cfg).run()
    return 0
