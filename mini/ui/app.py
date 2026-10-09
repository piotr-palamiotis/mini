"""Textual UI: chat, live log, attempt counter, cap indicator, registry browser, config screen, approval dialogs.

The agent runs in a worker thread. Approval requests block that thread until the user answers in a modal dialog.
"""
import json
import os
import threading
import tomllib

from rich.markup import escape
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (Button, DataTable, Footer, Header, Input, Label, RichLog, Static, TabbedContent,
                             TabPane, TextArea)

from ..harness import config as config_mod
from ..harness.approvals import Decision
from ..harness.eventlog import RunLog
from ..harness.registry import Registry

KIND_STYLE = {"watchdog": "bold yellow", "cap": "bold red", "error": "bold red", "test_run": "cyan",
              "install": "bold green", "approval": "magenta", "fallback": "bold blue", "gap": "bold",
              "registry_state": "dim", "sandbox": "dim", "phase": "bold white", "attempt": "cyan"}


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
                    val = v if isinstance(v, str) else json.dumps(v, indent=1)
                    yield Static(f"[b]{escape(str(k))}[/b]: {escape(val)}")
            yield Input(placeholder="optional note / hint / clarification for the agent", id="note")
            with Horizontal(id="buttons"):
                for i, o in enumerate(self.options):
                    yield Button(o, id=f"opt-{i}", variant="success" if i == 0 else
                                 "error" if o in ("refuse", "stop", "cancel") else "default")

    @on(Button.Pressed)
    def pressed(self, ev):
        choice = self.options[int(ev.button.id.split("-")[1])]
        note = self.query_one("#note", Input).value
        self.dismiss(Decision(choice not in ("refuse", "stop", "cancel"), choice, note, "user"))


class MiniApp(App):
    TITLE = "mini"
    CSS = """
    #status { height: 1; background: $boost; padding: 0 1; }
    #main { height: 1fr; }
    #left { width: 45%; }
    #chat { height: 1fr; border: round $primary; }
    #live { height: 3; color: $text-muted; padding: 0 1; }
    #prompt { dock: bottom; }
    #log { height: 1fr; }
    #reg-table { height: 12; }
    #reg-detail { height: 1fr; }
    #reg-buttons { height: auto; }
    #cfg-text { height: 1fr; }
    #cfg-msg { height: 2; }
    """
    BINDINGS = [Binding("ctrl+r", "refresh_registry", "Refresh registry"), Binding("ctrl+c", "quit", "Quit")]

    def __init__(self, cfg, backend_factory=None):
        super().__init__()
        self.cfg = cfg
        self.backend_factory = backend_factory      # tests inject a scripted model
        self.agent = None
        self.attachments = []
        self.attempt = (0, cfg["caps"]["attempts_per_tool"], "local")
        self.cap_hit = ""
        self.spend = 0.0
        self.busy = False
        self.live_text = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="status")
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield RichLog(id="chat", wrap=True, markup=True)
                yield Static(id="live")
                yield Input(placeholder="Give mini a task.  /attach PATH  /registry  /help", id="prompt")
            with TabbedContent(initial="tab-log"):
                with TabPane("Live log", id="tab-log"):
                    yield RichLog(id="log", wrap=True, markup=False, max_lines=5000)
                with TabPane("Registry", id="tab-reg"):
                    yield DataTable(id="reg-table", cursor_type="row")
                    with Horizontal(id="reg-buttons"):
                        yield Input(placeholder="version", id="reg-version", restrict=r"[0-9]*", max_length=4)
                        yield Button("Roll back to version", id="rollback", variant="warning")
                    yield RichLog(id="reg-detail", wrap=True)
                with TabPane("Config", id="tab-cfg"):
                    yield TextArea(config_mod.dumps(self.cfg), language="toml", id="cfg-text")
                    yield Static(id="cfg-msg")
                    yield Button("Save config", id="cfg-save", variant="primary")
        yield Footer()

    def on_mount(self):
        self.update_status()
        self.refresh_registry()
        self.chat("[b]mini[/b] ready. Local model: " + self.cfg["local"]["model"] +
                  f" via {self.cfg['local']['backend']}. Fallback: " +
                  (f"{self.cfg['fallback']['provider']} ({self.cfg['fallback']['model']})" if self.cfg["fallback"]["enabled"] else "disabled"))
        self.query_one("#prompt", Input).focus()

    # ------------------------------------------------------------ status
    def update_status(self):
        n, budget, who = self.attempt
        parts = [f"model {self.cfg['local']['model']}",
                 f"[b]attempt {n}/{budget}[/b]" + (" (fallback)" if who == "fallback" else ""),
                 f"fallback ${self.spend:.3f}/{self.cfg['fallback']['spend_usd_per_run']}" if self.cfg["fallback"]["enabled"] else "fallback off",
                 "[b yellow]running[/b yellow]" if self.busy else "idle"]
        if self.cap_hit:
            parts.append(f"[b white on red] CAP: {escape(self.cap_hit)} [/]")
        self.query_one("#status", Static).update("  |  ".join(parts))

    def chat(self, text):
        self.query_one("#chat", RichLog).write(text)

    # ------------------------------------------------------------ events from the agent thread
    def on_agent_event(self, ev):
        k = ev["kind"]
        if k == "delta":
            self.live_text = (self.live_text + ev["text"])[-400:]
            self.query_one("#live", Static).update(Text(self.live_text.replace("\n", " ")[-300:], style="dim italic"))
            return
        self.live_text = ""
        line = RunLog.format(ev)
        self.query_one("#log", RichLog).write(Text(line[:4000], style=KIND_STYLE.get(k, "")))
        if k == "attempt":
            prov = ev.get("provider", "local")
            budget = ev.get("budget", self.cfg["caps"]["attempts_per_tool"])
            self.attempt = (ev["n"], budget, prov)
        elif k == "phase" and ev.get("phase") in ("create", "repair"):
            self.attempt = (0, self.cfg["caps"]["attempts_per_tool"], "local")
            self.chat(f"[b]building a new tool[/b] for: {escape(str(ev.get('need') or ev.get('tool')))}")
        elif k == "cap" or (k == "watchdog" and ev.get("name") in ("attempt_budget", "time_turn_limit", "hardware_swap")):
            self.cap_hit = ev.get("cap") or ev.get("name")
            self.chat(f"[b red]cap reached:[/b red] {escape(self.cap_hit)}")
        elif k == "fallback" and ev.get("phase") == "reply":
            self.spend = ev.get("total_spend_usd", self.spend)
            self.chat(f"[b blue]fallback reply[/b blue] ({ev.get('output_tokens')} tokens, ${ev.get('cost_usd')})")
        elif k == "gap":
            self.chat(f"[b]gap detection:[/b] use {ev.get('use') or '-'}; missing {ev.get('missing') or '-'}")
        elif k == "test_run":
            self.chat(("[green]tests passed[/green] " if ev["passed"] else "[red]tests failed[/red] ") + escape(ev.get("summary", "")))
        elif k == "install":
            self.chat(f"[b green]installed[/b green] {ev['tool']} v{ev['version']} (built by {ev['created_by']['provider']})")
            self.refresh_registry()
        elif k == "watchdog":
            self.chat(f"[yellow]watchdog[/yellow] {escape(ev.get('name', ''))}")
        elif k == "run_end":
            self.busy = False
            if ev["status"] == "success":
                self.chat(f"[b green]done[/b green] in {ev['secs']} s\n{escape(ev.get('answer') or '')}")
            else:
                self.chat(f"[b red]{ev['status']}[/b red]: {escape(ev.get('error') or '')}")
            if ev.get("outputs"):
                self.chat("output files: " + ", ".join(ev["outputs"]) + f"  (in runs/{ev['run_id']}/work/out/)")
            self.refresh_registry()
        self.update_status()

    def approval_handler(self, kind, title, details, options):
        """Called from the agent thread; blocks it until the user answers."""
        done, box = threading.Event(), {}

        def show():
            def got(decision):
                box["d"] = decision
                done.set()
            self.push_screen(ApprovalScreen(kind, title, details, options), got)
        self.call_from_thread(show)
        done.wait()
        return box["d"]

    # ------------------------------------------------------------ input
    @on(Input.Submitted, "#prompt")
    def submitted(self, ev):
        text = ev.value.strip()
        ev.input.value = ""
        if not text:
            return
        if text.startswith("/attach "):
            p = os.path.expanduser(text[8:].strip())
            if os.path.isfile(p):
                self.attachments.append(os.path.abspath(p))
                self.chat(f"attached {escape(p)}")
            else:
                self.chat(f"[red]no such file:[/red] {escape(p)}")
            return
        if text == "/registry":
            self.query_one(TabbedContent).active = "tab-reg"
            return
        if text == "/help":
            self.chat("/attach PATH   attach a file to the next task\n/registry      open the registry browser\n"
                      "Config tab: edit and save the TOML config (caps, watchdogs, backend, fallback).")
            return
        if self.busy:
            self.chat("[yellow]a task is running[/yellow]")
            return
        self.chat(f"[b cyan]you:[/b cyan] {escape(text)}" + (f"  [dim](attached: {', '.join(os.path.basename(a) for a in self.attachments)})[/dim]" if self.attachments else ""))
        self.busy, self.cap_hit, self.spend = True, "", 0.0
        self.update_status()
        self.run_task(text, list(self.attachments))
        self.attachments = []

    @work(thread=True, exclusive=True)
    def run_task(self, text, attachments):
        from ..agent.core import Agent
        cfg = self.cfg if self.backend_factory else config_mod.load(self.cfg.path)
        self.agent = Agent(cfg, approval_handler=self.approval_handler,
                           backend=self.backend_factory() if self.backend_factory else None)
        self.agent.log.subscribe(lambda ev: self.call_from_thread(self.on_agent_event, ev))
        self.agent.run_task(text, attachments)

    # ------------------------------------------------------------ registry browser
    def action_refresh_registry(self):
        self.refresh_registry()

    def refresh_registry(self):
        reg = Registry(self.cfg.abspath("state"))
        t = self.query_one("#reg-table", DataTable)
        t.clear(columns=True)
        t.add_columns("tool", "active", "versions", "built by", "tests", "attempts")
        for m in reg.list_tools():
            cb = m.get("created_by", {})
            by = f"{cb.get('provider')}/{cb.get('model')}" + (" [FALLBACK]" if cb.get("fallback") else "")
            tr = m.get("test_results") or {}
            t.add_row(m["name"], f"v{m['version']}", ",".join(map(str, reg.versions(m["name"]))), by,
                      ("pass: " if tr.get("passed") else "FAIL: ") + tr.get("summary", "")[:30],
                      json.dumps(m.get("attempts_used")), key=m["name"])
        if not reg.names():
            self.query_one("#reg-detail", RichLog).clear().write("(registry is empty)")

    @on(DataTable.RowHighlighted, "#reg-table")
    def show_tool(self, ev):
        if ev.row_key is None:
            return
        reg = Registry(self.cfg.abspath("state"))
        m = reg.get(ev.row_key.value)
        self.selected_tool = ev.row_key.value
        d = self.query_one("#reg-detail", RichLog)
        d.clear()
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

    # ------------------------------------------------------------ config screen
    @on(Button.Pressed, "#cfg-save")
    def save_config(self):
        text = self.query_one("#cfg-text", TextArea).text
        msg = self.query_one("#cfg-msg", Static)
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            msg.update(f"[red]not saved: {escape(str(e))}[/red]")
            return
        if data.get("local", {}).get("num_ctx") != self.cfg["local"]["num_ctx"]:
            msg.update("[yellow]context size changed: this needs the owner's approval (VRAM budget). Saved anyway.[/yellow]")
        else:
            msg.update(f"[green]saved to {escape(self.cfg.path)}; applies to the next task[/green]")
        with open(self.cfg.path, "w") as f:
            f.write(text)
        self.cfg = config_mod.load(self.cfg.path)
        self.update_status()


def run_ui(cfg):
    MiniApp(cfg).run()
    return 0
