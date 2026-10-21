"""The modal form of a `form`-mode tool run from a slash command (docs/redesign.md 4b).

One field per input of the tool's input schema (harness/toolui.fields: string -> text, number / integer -> number,
boolean -> switch, enum -> dropdown, array -> list with one item per line, a file input -> path field that completes
from the project folder, a secret -> masked); ui hints set labels, order, multiline text and the file filter.
Inline arguments of the command prefill it. Enter in a field (or Ctrl+S) runs the tool, Esc cancels. The screen
returns the inputs through dismiss(), or None when cancelled; what the form answered goes to the tool as inputs, so
io.ask does not ask for it again.
"""
from rich.markup import escape
from textual import on
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.suggester import Suggester
from textual.widgets import Button, Input, Label, Select, Static, TextArea

from ..harness import toolui
from . import commands, settingsform


class PathSuggester(Suggester):
    """Inline completion of paths relative to the project folder (→ accepts), files matching the field's filter."""

    def __init__(self, folder, globs=()):
        super().__init__(use_cache=False, case_sensitive=True)
        self.folder, self.globs = folder, list(globs)

    async def get_suggestion(self, value):
        for p in commands.paths(self.folder, value, self.globs):
            if p != value and p.startswith(value):
                return p
        return None


def fid(name):
    return "tf-f-" + name


def widget(field, value, folder):
    """The widget of one field, holding its initial value (toolui.initial)."""
    v = toolui.initial(field, value)
    hint = field.help or ""
    if field.kind in ("list", "json") or field.multiline:
        ta = TextArea(v or "", id=fid(field.name), classes="long", soft_wrap=True, show_line_numbers=False)
        ta.placeholder = hint + (" (one per line)" if field.kind == "list" else "")
        return ta
    if field.kind == "switch":
        return settingsform.control("switch", v, fid(field.name))
    if field.kind == "select":
        return settingsform.control("select", v, fid(field.name), field.choices, allow_blank=not field.required)
    kind = {"integer": "int", "number": "float"}.get(field.kind, "text")
    extra = {"password": True} if field.kind == "secret" else {}
    if field.kind == "file":
        extra["suggester"] = PathSuggester(folder, field.filter)
        hint = hint or "a file in the project folder"
        hint += f" ({', '.join(field.filter)})" if field.filter else ""
    return settingsform.control(kind, v, fid(field.name), placeholder=hint, **extra)


def raw_value(w):
    if isinstance(w, TextArea):
        return w.text
    if isinstance(w, Select):
        return None if w.value is Select.NULL else w.value
    return w.value                                 # Input, Switch


class ToolForm(ModalScreen):
    """Form for one tool command. dismiss(inputs) on run, dismiss(None) on Esc."""
    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("ctrl+s", "run", "Run")]
    DEFAULT_CSS = """
    ToolForm { align: center middle; }
    #tf-box { width: 100; max-height: 90%; height: auto; border: thick $primary; background: $surface; padding: 1 2; }
    #tf-title { text-style: bold; color: $primary; }
    #tf-desc { color: $text-muted; }
    #tf-keys { margin-bottom: 1; }
    #tf-fields { height: auto; max-height: 60vh; }
    #tf-fields .row { height: 1; margin-bottom: 1; }
    #tf-fields .lbl { width: 30; }
    #tf-fields Input { border: none; height: 1; padding: 0 1; width: 1fr; background: $boost; }
    #tf-fields Switch { border: none; height: 1; padding: 0 1; }
    #tf-fields Select { width: 1fr; height: 1; }
    #tf-fields SelectCurrent { border: none; height: 1; padding: 0 1; background: $boost; }
    #tf-fields .long { height: 4; border: none; padding: 0 1; background: $boost; margin-bottom: 1; }
    #tf-msg { height: auto; color: $error; }
    #tf-buttons { height: auto; margin-top: 1; }
    #tf-buttons Button { margin-right: 1; }
    """

    def __init__(self, command, prefill=None, folder=None):
        super().__init__()
        self.command, self.prefill, self.folder = command, dict(prefill or {}), folder
        self.fields = toolui.fields(command.schema, command.ui)

    def compose(self):
        with Vertical(id="tf-box"):
            yield Label(f"/{self.command.name}", id="tf-title")
            yield Static(escape(self.command.description), id="tf-desc")
            yield Static("[dim]Enter in a field or Ctrl+S runs   Esc cancels   → accepts a suggested path   "
                         "* required[/dim]", id="tf-keys")
            with VerticalScroll(id="tf-fields"):
                for f in self.fields:
                    w = widget(f, self.prefill.get(f.name), self.folder)
                    label = f.label + (" *" if f.required else "")
                    if isinstance(w, TextArea):
                        yield Label(label, classes="lbl")
                        yield w
                    else:
                        yield settingsform.row(label, w)
            yield Static(id="tf-msg")
            with Horizontal(id="tf-buttons"):
                yield Button("Run", id="tf-run", variant="primary")
                yield Button("Cancel", id="tf-cancel")

    def on_mount(self):
        for f in self.fields:                     # the first field nobody filled in gets the cursor
            if self.prefill.get(f.name) is None:
                self.query_one("#" + fid(f.name)).focus()
                return
        if self.fields:
            self.query_one("#" + fid(self.fields[0].name)).focus()

    def values(self):
        """(inputs, errors) from the widgets."""
        raw = {f.name: raw_value(self.query_one("#" + fid(f.name))) for f in self.fields}
        return toolui.to_inputs(self.fields, raw)

    def action_run(self):
        inputs, errors = self.values()
        if errors:
            self.query_one("#tf-msg", Static).update(escape("; ".join(errors)))
            return
        self.dismiss(inputs)

    def action_cancel(self):
        self.dismiss(None)

    @on(Input.Submitted)
    def submitted(self, ev):
        ev.stop()
        self.action_run()

    @on(Button.Pressed, "#tf-run")
    def pressed_run(self):
        self.action_run()

    @on(Button.Pressed, "#tf-cancel")
    def pressed_cancel(self):
        self.action_cancel()
