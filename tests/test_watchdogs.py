"""Every watchdog fires exactly at its configured threshold. No model, no sandbox."""
import unittest

from mini.harness.watchdogs import Watchdogs
from tests.helpers import temp_config


class FakeLog:
    def __init__(self):
        self.events = []

    def emit(self, kind, **kw):
        self.events.append((kind, kw))

    def triggers(self, name):
        return [kw for k, kw in self.events if k == "watchdog" and kw.get("name") == name]


def wd(**over):
    log = FakeLog()
    return Watchdogs(temp_config(**over), log), log


class WatchdogTest(unittest.TestCase):
    def test_identical_call_warns_then_blocks_at_3(self):
        w, log = wd()
        self.assertEqual([w.on_call("run_tests", {}) for _ in range(4)], ["ok", "warn", "block", "block"])
        self.assertEqual(w.on_call("replace", {"a": 1}), "ok")           # a different call resets the streak
        self.assertEqual([t["action"] for t in log.triggers("identical_call")], ["warn", "block", "block"])

    def test_identical_call_threshold_from_config(self):
        w, _ = wd(**{"watchdogs.identical_call_repeat": 5})
        self.assertEqual([w.on_call("x", {}) for _ in range(5)], ["ok", "ok", "ok", "warn", "block"])

    def test_passing_rerun_goes_to_review_after_2(self):
        w, log = wd()
        self.assertEqual(w.on_test(True, True, "h1"), "review")
        self.assertEqual(w.on_test(True, False, "h1"), "review")
        self.assertEqual(log.triggers("passing_test_rerun"), [])
        self.assertEqual(w.on_test(True, False, "h1"), "review")
        self.assertEqual(len(log.triggers("passing_test_rerun")), 1)

    def test_file_rewrites_step_back_after_3(self):
        w, log = wd()
        self.assertEqual([w.on_rewrite("tool.py") for _ in range(4)], ["ok", "ok", "ok", "step_back"])
        self.assertEqual(w.on_rewrite("test_tool.py"), "ok")
        self.assertEqual(log.triggers("file_rewrites")[0]["rewrites"], 4)

    def test_fail_streak_restarts_at_3_and_counts_attempts(self):
        w, log = wd()
        verdicts = [w.on_test(False, True, f"h{i}") for i in range(6)]
        self.assertEqual(verdicts, ["continue", "continue", "restart", "continue", "continue", "restart"])
        self.assertEqual(w.attempts, 6)                 # restarts do not reset the attempt count
        w.reset_conversation()                          # what a restart does
        self.assertEqual(w.attempts, 6)

    def test_unchanged_failing_rerun_is_not_an_attempt(self):
        w, _ = wd()
        w.on_test(False, True, "a")
        w.on_test(False, False, "a")
        w.on_test(False, False, "a")
        self.assertEqual(w.attempts, 1)
        self.assertEqual(w.fail_streak, 1)

    def test_attempt_budget_stops_at_10_through_restarts(self):
        w, log = wd()
        verdicts = []
        for i in range(10):
            v = w.on_test(False, True, f"h{i}")
            verdicts.append(v)
            if v == "restart":
                w.reset_conversation()
        self.assertEqual(verdicts[-1], "budget")
        self.assertEqual(w.attempts, 10)
        self.assertEqual(verdicts.count("restart"), 3)  # after attempts 3, 6, 9
        self.assertEqual(log.triggers("attempt_budget")[0]["attempts"], 10)
        events = [kw["n"] for k, kw in log.events if k == "attempt"]
        self.assertEqual(events, list(range(1, 11)))

    def test_parse_errors_resend_up_to_5(self):
        w, log = wd()
        bad = {"tool_calls": [{"name": "x", "arguments": {"_unparsed": "{"}}], "output_tokens": 10, "secs": 1}
        verdicts = [w.on_reply(bad) for _ in range(6)]
        self.assertEqual(verdicts, ["parse_error"] * 5 + ["parse_limit"])
        self.assertEqual(w.attempts, 0)

    def test_empty_reply_one_nudge(self):
        w, log = wd()
        empty = {"tool_calls": [], "content": "I will now...", "output_tokens": 5, "secs": 1}
        self.assertEqual(w.on_reply(empty), "empty")
        self.assertEqual(w.on_reply(empty), "empty_limit")
        good = {"tool_calls": [{"name": "x", "arguments": {}}], "output_tokens": 5, "secs": 1}
        self.assertIsNone(w.on_reply(good))
        self.assertEqual(w.on_reply(empty), "empty")                  # nudge budget resets after a good reply
        self.assertEqual(w.attempts, 0)

    def test_runaway_output(self):
        w, log = wd()
        self.assertEqual(w.on_reply({"tool_calls": [], "output_tokens": 8192, "secs": 30}), "runaway")
        self.assertEqual(w.on_reply({"tool_calls": [], "output_tokens": 10, "secs": 300}), "runaway")
        self.assertEqual(w.on_reply({"tool_calls": [], "done_reason": "length", "output_tokens": 10, "secs": 1}), "runaway")
        self.assertEqual(len(log.triggers("runaway_output")), 3)
        self.assertEqual(w.on_reply({"content": "x", "output_tokens": 10, "secs": 1}, expect_call=False), None)

    def test_time_and_turn_limits(self):
        w, log = wd(**{"caps.task_turns": 3})
        good = {"tool_calls": [{"name": "x", "arguments": {}}], "output_tokens": 5, "secs": 1}
        for _ in range(2):
            w.on_reply(good)
            self.assertFalse(w.time_or_turns_exceeded())
        w.on_reply(good)
        self.assertTrue(w.time_or_turns_exceeded())
        w.extend()
        self.assertFalse(w.time_or_turns_exceeded())
        w2, _ = wd(**{"caps.task_wall_clock_s": 0})
        self.assertTrue(w2.time_or_turns_exceeded())

    def test_swap_watch_trips_once_above_limit(self):
        from mini.harness.hwmon import SwapWatch
        readings = iter([1000, 1100, 1251, 1300, 2000])
        trips = []
        sw = SwapWatch(250, trips.append, reader=lambda: next(readings))
        sw.check()
        sw.check()
        sw.check()
        sw.check()
        self.assertEqual(trips, [251])


if __name__ == "__main__":
    unittest.main()
