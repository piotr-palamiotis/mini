"""The Config tab as a form: switches, dropdowns and fields instead of a TOML text box.

FIELDS says what is shown. Saving writes only the keys that changed, in place (comments and layout of the TOML
file stay), through config.set_values. Backends and presets are not here: `mini preset` and `mini setup` own them.
"""
from rich.markup import escape
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Input, Label, Select, Static, Switch

# (section, key, label, kind[, options]); kind: switch | select | int | float | text
FIELDS = [
    ("Model tiers", [
        ("free", "enabled", "Free cloud tier (through OmniRoute)", "switch"),
        ("paid", "enabled", "Paid cloud tier (through OmniRoute)", "switch"),
        ("routing", "start", "A task starts on", "select", ("local", "free", "paid")),
        ("routing", "auto_escalate", "Move local -> free by itself when the local model is stuck", "switch"),
        ("routing", "local_budget_s", "Local model time per task before it moves on, seconds (0 = no limit)", "int"),
        ("local", "temperature", "Local model temperature", "float"),
    ]),
    ("Approvals", [
        ("approvals", "confirm_first", "Ask me to confirm the plan before a task runs", "switch"),
        ("approvals", "mode", "Approval mode (auto answers by policy, for unattended runs)", "select", ("ask", "auto")),
        ("tools", "auto_accept", "Accept tools a task builds without a review step", "switch"),
    ]),
    ("Thinking (the model reasons before it answers, per step)", [
        ("thinking", "parse", "Understanding the task", "switch"),
        ("thinking", "gap", "Deciding what is missing", "switch"),
        ("thinking", "spec", "Writing a tool's spec", "switch"),
        ("thinking", "code", "Writing a tool's code", "switch"),
        ("thinking", "fix", "Fixing a failing tool", "switch"),
        ("thinking", "execute", "Running the task", "switch"),
        ("thinking", "write", "Writing documents", "switch"),
    ]),
    ("Limits", [
        ("caps", "attempts_per_tool", "Attempts to build one tool", "int"),
        ("caps", "task_wall_clock_s", "Seconds per tool build / run phase before I am asked", "int"),
        ("caps", "task_turns", "Model turns per tool build / run phase before I am asked", "int"),
        ("caps", "run_hard_wall_clock_s", "Hard stop for a whole run, seconds", "int"),
        ("caps", "tokens_per_run", "Local tokens per run", "int"),
        ("caps", "reply_max_tokens", "Longest single model reply, tokens", "int"),
        ("caps", "reply_max_s", "Longest single model reply, seconds", "int"),
        ("caps", "max_repairs_per_task", "New tool versions after a failed real run", "int"),
        ("consult", "enabled", "Ask for advice when the attempts run out", "switch"),
    ]),
    ("Watchdogs", [
        ("watchdogs", "swap_watch", "Abort a run when swap use grows", "switch"),
        ("watchdogs", "swap_growth_mib", "Swap growth that aborts it, MiB", "int"),
        ("watchdogs", "identical_call_repeat", "Same tool call repeated before it is blocked", "int"),
        ("watchdogs", "fail_streak_restart", "Failing test runs in a row before a fresh start", "int"),
        ("watchdogs", "parse_error_retries", "Unparseable replies tolerated in a row", "int"),
    ]),
    ("Your folder and the sandbox", [
        ("sandbox", "engine", "Container engine", "select", ("podman", "docker")),
        ("sandbox", "memory", "Memory per tool run (e.g. 1g)", "text"),
        ("sandbox", "cpus", "CPUs per tool run", "text"),
        ("sandbox", "timeout_s", "Seconds per tool run", "int"),
    ]),
    ("Memory", [
        ("lessons", "enabled", "Keep lessons from failures between runs", "switch"),
        ("lessons", "max_in_prompt", "Lessons shown to the model", "int"),
    ]),
]


def wid(section, key):
    return f"f-{section}-{key}"


def widgets(cfg):
    """The form, as one scrollable column of widgets."""
    rows = []
    for title, fields in FIELDS:
        rows.append(Label(title, classes="grp"))
        for section, key, label, kind, *opt in fields:
            value = cfg[section][key]
            if kind == "switch":
                control = Switch(bool(value), id=wid(section, key))
            elif kind == "select":
                options = list(opt[0])
                if value not in options:
                    options.append(value)
                control = Select([(o, o) for o in options], value=value, allow_blank=False, id=wid(section, key))
            else:
                control = Input(str(value), id=wid(section, key), classes="num" if kind in ("int", "float") else "")
            rows.append(Horizontal(Label(label, classes="lbl"), control, classes="row"))
    rows.append(Static("Backends and presets: `mini preset list|use`, `mini setup`. Anything else: the Raw TOML tab.",
                       classes="note"))
    return VerticalScroll(*rows, id="cfg-form")


def collect(app, cfg):
    """({section: {key: value}} for the fields that differ from cfg, [errors])."""
    updates, errors = {}, []
    for _, fields in FIELDS:
        for section, key, label, kind, *_opt in fields:
            w = app.query_one("#" + wid(section, key))
            raw = w.value
            try:
                if kind == "int":
                    value = int(str(raw).strip())
                    if value < 0:
                        raise ValueError
                elif kind == "float":
                    value = float(str(raw).strip())
                elif kind == "text":
                    value = str(raw).strip()
                    if not value:
                        raise ValueError
                else:
                    value = raw
            except ValueError:
                errors.append(f"{label}: '{raw}' is not valid")
                continue
            if value != cfg[section][key]:
                updates.setdefault(section, {})[key] = value
    return updates, errors


def refresh(app, cfg):
    """Show cfg's values in the form (after a save or an edit of the raw file)."""
    for _, fields in FIELDS:
        for section, key, *_ in fields:
            w = app.query_one("#" + wid(section, key))
            v = cfg[section][key]
            if isinstance(w, Switch):
                w.value = bool(v)
            elif isinstance(w, Select):
                w.value = v
            else:
                w.value = str(v)


def summary(updates):
    return ", ".join(f"{escape(s)}.{escape(k)} = {escape(str(v))}" for s, kv in updates.items() for k, v in kv.items())
