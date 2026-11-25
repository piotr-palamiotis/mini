"""Benchmarks inside mini: tasks generate the same fixtures every time, a run records passed/failed and its time,
results are averaged per preset, and the Bench view shows them."""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from mini import bench
from mini.bench import results, run
from mini.harness import config as config_mod
from tests.helpers import fake_backend, temp_config


def bench_cfg():
    cfg = temp_config()
    cfg.path = os.path.join(tempfile.mkdtemp(), "mini.toml")
    cfg["presets"] = {"big": {"model": "big-model", "device": "CUDA0"}, "small": {"model": "small-model"}}
    cfg["llamacpp"]["preset"] = "big"
    return cfg


class TasksTest(unittest.TestCase):
    def test_every_suite_loads_and_names_are_unique(self):
        ts = bench.tasks()
        self.assertEqual(len({t.name for t in ts}), len(ts))
        self.assertEqual({t.suite for t in ts}, set(bench.SUITES))
        for t in ts:
            self.assertTrue(t.prompt and t.files and "print('PASS')" in t.check, t.name)

    def test_selection_by_suite_and_task(self):
        got = [t.name for t in bench.tasks(["expert", "csv_revenue"])]
        self.assertEqual(got[:3], ["expert_bugfix", "expert_schedule", "expert_fifo"])
        self.assertEqual(got[-1], "csv_revenue")
        with self.assertRaises(KeyError):
            bench.tasks(["no_such_task"])

    def test_a_check_fails_on_the_untouched_fixture(self):
        for name in ("csv_revenue", "expert_fifo", "hard_logs"):
            t = bench.tasks([name])[0]
            w = tempfile.mkdtemp()
            run.write_fixture(t, w)
            check = os.path.join(w, "..", name + ".check.py")
            with open(check, "w") as f:
                f.write(t.check)
            out = os.path.join(w, "..", name + ".out")
            open(out, "w").close()
            r = subprocess.run([sys.executable, "-I", check, w, out], capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0, name)
            self.assertIn("FAIL", r.stdout)


class ResultsTest(unittest.TestCase):
    def test_every_run_is_a_row_and_averages_come_from_the_rows(self):
        cfg = bench_cfg()
        for task, ok, secs in (("a", True, 100), ("a", False, 300), ("b", True, 50)):
            results.add(cfg, dict(preset="big", model="m", suite="core", task=task, rep=0, passed=ok, secs=secs))
        results.add(cfg, dict(preset="small", model="s", suite="core", task="a", rep=0, passed=True, secs=10))
        rs = results.rows(cfg, "big")
        self.assertEqual(len(rs), 3)
        self.assertEqual(results.average(rs), {"runs": 3, "passed": 2, "rate": 2 / 3, "secs": 150})
        per = {t: a for t, _, a in results.per_task(rs)}
        self.assertEqual((per["a"]["runs"], per["a"]["passed"], per["a"]["secs"]), (2, 1, 200))
        self.assertEqual(results.presets(results.rows(cfg)), ["big", "small"])
        self.assertEqual(results.fmt_avg(results.average(rs)), "2/3 passed (67%), average 2m30s")


    def test_a_new_mini_version_starts_the_statistics_afresh(self):
        cfg = bench_cfg()
        with mock.patch.object(results, "__version__", "0.1.0"):
            results.add(cfg, dict(preset="big", model="m", suite="core", task="a", rep=0, passed=False, secs=100))
        with open(results.path(cfg), "a") as f:          # a row from before versions: older than any version
            f.write(json.dumps(dict(preset="big", model="m", suite="core", task="a", rep=1, passed=False, secs=50)) + "\n")
        with mock.patch.object(results, "__version__", "0.1.1"):
            row = results.add(cfg, dict(preset="big", model="m", suite="core", task="a", rep=0, passed=True, secs=10))
            self.assertEqual(row["version"], "0.1.1")
            self.assertEqual([r["secs"] for r in results.rows(cfg)], [10])
            self.assertEqual(len(results.rows(cfg, version=results.ALL)), 3)
            self.assertEqual(results.versions(results.rows(cfg, version=results.ALL)), ["0.0.0", "0.1.0", "0.1.1"])
            self.assertEqual(len(results.rows(cfg, version="0.1.0")), 1)
            from mini.bench import cli
            with mock.patch.object(cli, "__version__", "0.1.1"):
                text = cli.results_text(cfg)
                self.assertIn("mini 0.1.1 (this version)", text)
                self.assertIn("1/1 passed", text)
                self.assertNotIn("0.1.0", text)
                every = cli.results_text(cfg, all_versions=True)
                self.assertLess(every.index("mini 0.1.1"), every.index("mini 0.1.0"))
                self.assertIn("0/1 passed", every)


class RunTest(unittest.TestCase):
    def test_bench_config_pins_what_changes_a_model(self):
        cfg = bench_cfg()
        cfg["thinking"]["gap"] = False
        cfg["local"]["temperature"] = 1.3
        root = tempfile.mkdtemp()
        b = config_mod.load(run.bench_config(cfg, "small", 7, root))
        self.assertEqual(b["llamacpp"]["preset"], "small")
        self.assertEqual(b["local"]["model"], "small-model")
        self.assertEqual((b["local"]["seed"], b["local"]["temperature"], b["thinking"]["gap"]), (7, 0.6, True))
        self.assertEqual(b["paths"]["state"], os.path.join(root, "state"))

    def test_a_run_records_passed_and_time_and_switches_the_preset_back(self):
        cfg = bench_cfg()
        task = bench.tasks(["log_question"])[0]
        calls, switched = [], []

        def fake_mini(args, cwd, config, timeout, out=None):
            calls.append(args[0])
            if args[0] == "run":                          # a model that answers right: count the ERROR lines
                text = open(os.path.join(cwd, "server.log")).read()
                errs = [l for l in text.splitlines() if " ERROR " in l]
                mods = {}
                for l in errs:
                    m = l.split("[", 1)[1].split("]", 1)[0]
                    mods[m] = mods.get(m, 0) + 1
                with open(out, "w") as f:
                    f.write(json.dumps({"answer": f"{len(errs)} ERROR lines; most: {max(mods, key=mods.get)}"}))
                return 0
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch.object(run, "_mini", fake_mini), mock.patch.object(run, "free_space", lambda root, say: None), \
                mock.patch("mini.setup.use_preset", lambda c, name, ui: switched.append(name)):
            rows = run.run_bench(cfg, "small", [task], reps=2, seed=1, say=lambda m: None)
        self.assertEqual(switched, ["small", "big"])
        self.assertEqual([(r["task"], r["rep"], r["seed"], r["preset"], r["model"]) for r in rows],
                         [("log_question", 0, 1, "small", "small-model"), ("log_question", 1, 2, "small", "small-model")])
        self.assertTrue(all(r["passed"] and isinstance(r["secs"], int) for r in rows), rows)
        self.assertEqual(len(results.rows(cfg, "small")), 2)

    def test_several_presets_run_one_after_another(self):
        from mini.bench import cli
        cfg = bench_cfg()
        order = []
        a = mock.Mock(preset="big,small", names=["csv_revenue"], reps=2, seed=1, background=False)
        with mock.patch.object(run, "run_bench", lambda c, p, ts, reps, seed, say: order.append((p, reps)) or []), \
                mock.patch.object(run, "running", lambda c: None):
            cli.run_cmd(cfg, a)
        self.assertEqual(order, [("big", 2), ("small", 2)])
        a.preset = "big,nope"
        with self.assertRaises(run.BenchError):
            cli.run_cmd(cfg, a)

    def test_a_run_needs_a_preset(self):
        cfg = bench_cfg()
        with self.assertRaises(run.BenchError):
            run.run_bench(cfg, "nope", bench.tasks(["csv_revenue"]), say=lambda m: None)


class PackageCacheTest(unittest.TestCase):
    def test_image_builds_keep_their_downloads_on_the_host(self):
        from mini.harness.sandbox import Sandbox, package_cache
        cfg = temp_config()
        self.assertEqual(package_cache(cfg), os.path.join(cfg.abspath("state"), "package-cache"))
        sb = Sandbox(cfg)
        seen = []
        sb.image_exists = lambda image: False
        sb._build = lambda args, timeout, purpose, input=None, **info: seen.append((args, input)) or (0, "")
        sb.ensure_image({"pip": ["qrcode"], "apt": ["zbar-tools"]})
        args, cf = seen[0]
        self.assertIn(f"{sb.cache}/wheels:/wheels", args)
        self.assertIn("--no-index --find-links /wheels qrcode ||", cf)          # the cache first, then the network
        self.assertIn("pip wheel --no-cache-dir -w /wheels qrcode", cf)
        self.assertIn("--no-download zbar-tools ||", cf)
        cfg["sandbox"]["package_cache"] = ""                                 # off: plain installs, no volumes
        sb = Sandbox(cfg)
        sb.image_exists, seen[:] = (lambda image: False), []
        sb._build = lambda args, timeout, purpose, input=None, **info: seen.append((args, input)) or (0, "")
        sb.ensure_image({"pip": ["qrcode"]})
        self.assertNotIn("-v", seen[0][0])
        self.assertIn("RUN pip install --no-cache-dir qrcode", seen[0][1])

    def test_bench_runs_share_mini_package_cache(self):
        cfg = bench_cfg()
        b = config_mod.load(run.bench_config(cfg, "big", 1, tempfile.mkdtemp()))
        self.assertEqual(b["sandbox"]["package_cache"], os.path.join(cfg.abspath("state"), "package-cache"))


class BenchViewTest(unittest.TestCase):
    def test_bench_view_shows_every_run_and_the_average_per_preset(self):
        from mini.ui.app import MiniApp
        cfg = bench_cfg()
        cfg["approvals"]["confirm_first"] = False
        for task, ok, secs in (("hard_logs", True, 900), ("hard_logs", False, 600), ("hard_fifo", True, 300)):
            results.add(cfg, dict(preset="big", model="big-model", suite="hard", task=task, rep=0, passed=ok, secs=secs))
        results.add(cfg, dict(preset="small", model="small-model", suite="hard", task="hard_logs", rep=0, passed=False,
                              secs=60))
        app = MiniApp(cfg, backend_factory=lambda: fake_backend([]))
        started = []

        async def go():
            async with app.run_test(size=(180, 50)) as pilot:
                await pilot.press(*"/bench")
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(app.query_one("#views").active, "tab-bench")
                runs = app.query_one("#bv-runs")
                self.assertEqual(runs.row_count, 3)                      # every run of the active preset
                tasks = app.query_one("#bv-tasks")
                last = tasks.get_row_at(tasks.row_count - 1)
                self.assertIn("average", str(last[0]))
                self.assertEqual((last[2], last[3], last[4], last[5]), ("3", "2", "67%", "10m00s"))
                ol = app.query_one("#bv-presets")
                ol.highlighted = 1                                       # the other preset
                await pilot.pause()
                self.assertEqual(runs.row_count, 1)
                self.assertIn("small", str(app.query_one("#bv-head").render()))
                with mock.patch.object(run, "start", lambda c, p, n, r, s: started.append((p, n, r)) or
                                       {"log": "x.log"}):
                    app.query_one("#bv-run").press()
                    await pilot.pause()
                self.assertEqual(started, [("small", [], 1)])
                await pilot.press("escape")
                self.assertEqual(app.query_one("#views").active, "tab-stream")

        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
