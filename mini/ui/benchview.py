"""The Bench view (/bench): benchmark results per preset, and starting a bench on one.

  [ presets            ] [ averages per task of the preset picked  ]
  [  (runs, average)   ] [ every run: when, task, rep, result, time ]
  [ suite v ] [ reps ] [ Run on PRESET ] [ Stop ]   status

A result is passed or failed and how long the run took; every run is its own row, and the averages are worked out
from the rows (bench/results.py). Only the rows of this mini version count: a new version starts afresh. A bench runs in the background (bench/run.py), outside the UI: it switches
llama-server to its preset while it runs, so it is not started while a task is running.
"""
import os

from rich.markup import escape
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Input, OptionList, Select, Static
from textual.widgets.option_list import Option

from .. import __version__, bench
from ..bench import results, run

POLL_S = 3.0


class BenchView(Vertical):
    DEFAULT_CSS = """
    BenchView { height: 1fr; }
    #bv-main { height: 1fr; }
    #bv-presets { width: 1fr; height: 1fr; }
    #bv-right { width: 3fr; height: 1fr; }
    #bv-head { height: auto; padding: 0 1; }
    #bv-tasks { height: 1fr; }
    #bv-runs { height: 1fr; }
    #bv-bar { height: auto; margin: 1 0 0 0; }
    #bv-suite { width: 24; }
    #bv-reps { width: 10; }
    #bv-msg { width: 1fr; padding: 1 1 0 1; }
    """

    def __init__(self, app_ref, **kw):
        super().__init__(**kw)
        self.mini = app_ref
        self.preset = None              # the preset shown
        self.seen = None                # (results mtime, job state) last drawn

    def compose(self):
        with Horizontal(id="bv-main"):
            yield OptionList(id="bv-presets")
            with Vertical(id="bv-right"):
                yield Static("", id="bv-head")
                yield DataTable(id="bv-tasks", cursor_type="row", zebra_stripes=True)
                yield DataTable(id="bv-runs", cursor_type="row", zebra_stripes=True)
        with Horizontal(id="bv-bar"):
            yield Select([("all suites", "all")] + [(s, s) for s in bench.SUITES], value="all", allow_blank=False,
                         id="bv-suite")
            yield Input("1", placeholder="reps", id="bv-reps", restrict=r"[0-9]*", max_length=2)
            yield Button("Run", id="bv-run", variant="success")
            yield Button("Stop", id="bv-stop", variant="error")
            yield Static("", id="bv-msg")

    def on_mount(self):
        self.query_one("#bv-tasks", DataTable).add_columns("task", "suite", "runs", "passed", "pass %", "average time")
        self.query_one("#bv-runs", DataTable).add_columns("when", "task", "rep", "result", "time")
        self.set_interval(POLL_S, self.poll)

    # ---------------------------------------------------------------- drawing
    def opened(self, preset=None):
        self.preset = preset or self.preset
        self.seen = None
        self.poll()
        self.query_one("#bv-presets", OptionList).focus()

    def presets(self, rs):
        """Presets with results first (in the order of their first run), then configured ones without any."""
        names = results.presets(rs)
        return names + [p for p in (self.mini.cfg.get("presets") or {}) if p not in names]

    def poll(self):
        p = results.path(self.mini.cfg)
        job = run.running(self.mini.cfg)
        key = (os.path.getmtime(p) if os.path.exists(p) else 0, (job or {}).get("n"), bool(job), self.preset)
        self.draw_status(job)
        if key == self.seen:
            return
        self.seen = key
        self.draw()

    def draw(self):
        rs = results.rows(self.mini.cfg)
        names = self.presets(rs)
        if self.preset not in names:
            active = self.mini.cfg["llamacpp"].get("preset")
            self.preset = active if active in names else (names[0] if names else None)
        ol = self.query_one("#bv-presets", OptionList)
        ol.clear_options()
        for n in names:
            a = results.average([r for r in rs if r.get("preset") == n])
            line = (f"{n}\n  {a['passed']}/{a['runs']} passed, {round(100 * a['rate'])}%, avg "
                    f"{results.fmt_secs(a['secs'])}") if a["runs"] else f"{n}\n  no runs"
            ol.add_option(Option(escape(line), id=n))
        if self.preset in names:
            ol.highlighted = names.index(self.preset)
        self.draw_preset(rs)
        self.query_one("#bv-run", Button).label = f"Run on {self.preset}" if self.preset else "Run"

    def draw_preset(self, rs):
        mine = [r for r in rs if r.get("preset") == self.preset]
        models = ", ".join(dict.fromkeys(r.get("model", "?") for r in mine)) or \
            ((self.mini.cfg.get("presets") or {}).get(self.preset) or {}).get("model", "")
        a = results.average(mine)
        self.query_one("#bv-head", Static).update(
            f"[b]{escape(self.preset or '(no presets)')}[/b]  {escape(models)}   [dim]mini {__version__}[/dim]\n"
            + (f"all runs: {escape(results.fmt_avg(a))}" if mine else
               f"no bench runs on this preset with mini {__version__} yet"))
        tt = self.query_one("#bv-tasks", DataTable)
        tt.clear()
        for t, s, avg in results.per_task(mine):
            tt.add_row(t, s, str(avg["runs"]), str(avg["passed"]), f"{round(100 * avg['rate'])}%",
                       results.fmt_secs(avg["secs"]))
        if mine:
            tt.add_row("[b]average[/b]", "", str(a["runs"]), str(a["passed"]), f"{round(100 * a['rate'])}%",
                       results.fmt_secs(a["secs"]))
        rt = self.query_one("#bv-runs", DataTable)
        rt.clear()
        for r in reversed(mine):                              # newest first
            rt.add_row(r.get("at", ""), r.get("task", ""), str(r.get("rep", "")),
                       "[green]passed[/green]" if r.get("passed") else "[red]failed[/red]", results.fmt_secs(r.get("secs")))

    def draw_status(self, job):
        msg = self.query_one("#bv-msg", Static)
        if job:
            msg.update(f"[b]bench running[/b] on {escape(job.get('preset', ''))}: "
                       + (f"{job.get('n')}/{job.get('total')} {escape(str(job.get('task')))}" if job.get("task") else "starting"))
        else:
            last = run.load_job(self.mini.cfg)
            if last.get("state") == "failed":
                msg.update(f"[red]last bench failed:[/red] {escape(last.get('error', ''))}")
            elif last.get("state") in ("done", "stopped"):
                msg.update(f"last bench on {escape(last.get('preset', ''))}: {last['state']}")
        self.query_one("#bv-stop", Button).disabled = not job
        self.query_one("#bv-run", Button).disabled = bool(job)

    # ---------------------------------------------------------------- events
    def on_option_list_option_highlighted(self, event):
        if event.option_list.id == "bv-presets" and event.option.id != self.preset:
            self.preset = event.option.id
            self.draw_preset(results.rows(self.mini.cfg))
            self.query_one("#bv-run", Button).label = f"Run on {self.preset}"

    def on_button_pressed(self, event):
        if event.button.id == "bv-run":
            event.stop()
            self.start()
        elif event.button.id == "bv-stop":
            event.stop()
            self.query_one("#bv-msg", Static).update("bench stopped" if run.stop(self.mini.cfg) else "no bench running")
            self.seen = None

    def start(self):
        msg = self.query_one("#bv-msg", Static)
        if not self.preset or self.preset not in (self.mini.cfg.get("presets") or {}):
            msg.update("[yellow]pick a preset[/yellow] (a bench runs on a preset)")
            return
        if any(s.busy for s in self.mini.sessions):
            msg.update("[yellow]a task is running[/yellow]: the bench switches llama-server, so wait until it ends")
            return
        suite = self.query_one("#bv-suite", Select).value
        reps = int(self.query_one("#bv-reps", Input).value or 1)
        names = [] if suite == "all" else [suite]
        try:
            job = run.start(self.mini.cfg, self.preset, names, max(1, reps), 1)
        except (run.BenchError, KeyError) as e:
            msg.update(f"[red]{escape(str(e))}[/red]")
            return
        n = len(bench.tasks(names)) * max(1, reps)
        msg.update(f"bench started: {n} runs on {escape(self.preset)}; log {escape(job['log'])}")
        self.seen = None
