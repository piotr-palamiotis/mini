"""Watchdogs: the code-enforced caps. Each check returns an action; the agent loop must obey it.
Every trigger is logged as a "watchdog" event. Thresholds come from config [watchdogs] and [caps].

| Watchdog                | Trigger                                 | Action                                      |
| identical call          | same call N (3) times in a row          | warn on N-1, block on N and after          |
| passing test rerun      | N (2) reruns with no change, passing    | go straight to review                       |
| file rewrites           | more than N (3) full rewrites of a file | force a step back: explain what still fails |
| fail streak             | N (3) failed test runs in a row         | restart fresh with a summary (counts)       |
| attempt budget          | N (10) attempts on one tool             | offer fallback, else stop                   |
| unparseable tool call   | parse/JSON error                        | "resend as one small call", up to N (5)     |
| empty reply / lost call | no tool call                            | one nudge                                   |
| runaway output          | reply_max_tokens or reply_max_s         | "make smaller changes"                      |
| time and turn limits    | task_wall_clock_s or task_turns         | ask the user                                |
| hardware                | swap growth > N MiB                     | abort the run (hwmon.SwapWatch)             |
"""
import json
import time


class Watchdogs:
    def __init__(self, cfg, log):
        self.w, self.caps, self.log = cfg["watchdogs"], cfg["caps"], log
        self.reset_task()

    # ------------------------------------------------------------ scope resets
    def reset_task(self):
        self.task_t0 = time.time()
        self.turns = 0
        self.reset_tool()

    def reset_tool(self):
        self.attempts = 0
        self.fail_streak = 0
        self.rewrites = {}
        self.green_reruns = 0
        self.last_green_hash = None
        self.reset_conversation()

    def reset_conversation(self):
        self.last_call, self.call_streak = None, 0
        self.parse_errors = 0
        self.empty_nudged = False

    def trigger(self, name, **kw):
        self.log.emit("watchdog", name=name, **kw)

    # ------------------------------------------------------------ per reply
    def on_reply(self, reply, expect_call=True):
        """Returns None, 'runaway', 'parse_error', 'parse_limit', 'empty', or 'empty_limit'.
        expect_call=False: a text-only turn (the harness asked for a fenced file), only runaway output counts."""
        self.turns += 1
        if reply.get("done_reason") in ("length", "timeout") or reply.get("output_tokens", 0) >= self.caps["reply_max_tokens"] \
                or reply.get("secs", 0) >= self.caps["reply_max_s"]:
            self.trigger("runaway_output", output_tokens=reply.get("output_tokens"), secs=reply.get("secs"),
                         limit_tokens=self.caps["reply_max_tokens"], limit_s=self.caps["reply_max_s"])
            return "runaway"
        if not expect_call:
            return None
        bad = [c for c in reply.get("tool_calls", []) if "_unparsed" in (c.get("arguments") or {})] or reply.get("raw_error")
        if bad:
            self.parse_errors += 1
            self.trigger("unparseable_call", count=self.parse_errors, limit=self.w["parse_error_retries"],
                         error=str(reply.get("raw_error") or "")[:300])
            return "parse_limit" if self.parse_errors > self.w["parse_error_retries"] else "parse_error"
        if not reply.get("tool_calls"):
            if self.empty_nudged:
                self.parse_errors += 1
                self.trigger("empty_reply", nudged_before=True, count=self.parse_errors)
                return "parse_limit" if self.parse_errors > self.w["parse_error_retries"] else "empty_limit"
            self.empty_nudged = True
            self.trigger("empty_reply", nudged_before=False)
            return "empty"
        self.parse_errors = 0
        self.empty_nudged = False
        return None

    def on_call(self, name, args):
        """Returns 'ok', 'warn' or 'block' for one tool call."""
        key = name + json.dumps(args, sort_keys=True, default=str)
        self.call_streak = self.call_streak + 1 if key == self.last_call else 1
        self.last_call = key
        n = self.w["identical_call_repeat"]
        if self.call_streak >= n:
            self.trigger("identical_call", call=name, streak=self.call_streak, limit=n, action="block")
            return "block"
        if self.call_streak == n - 1:
            self.trigger("identical_call", call=name, streak=self.call_streak, limit=n, action="warn")
            return "warn"
        return "ok"

    def on_rewrite(self, path):
        """A full rewrite of a file. Returns 'ok' or 'step_back'."""
        self.rewrites[path] = self.rewrites.get(path, 0) + 1
        if self.rewrites[path] > self.w["file_rewrites"]:
            self.trigger("file_rewrites", path=path, rewrites=self.rewrites[path], limit=self.w["file_rewrites"])
            self.rewrites[path] = 0
            return "step_back"
        return "ok"

    def on_test(self, passed, changed, code_hash):
        """After a test run. changed: code differs from the previous test run.
        Returns one of: 'review', 'restart', 'budget', 'continue'."""
        if changed or self.attempts == 0:
            self.attempts += 1
            self.log.emit("attempt", n=self.attempts, budget=self.caps["attempts_per_tool"], passed=passed)
        if passed:
            self.fail_streak = 0
            if not changed and self.last_green_hash == code_hash:
                self.green_reruns += 1
                if self.green_reruns >= self.w["passing_test_reruns"]:
                    self.trigger("passing_test_rerun", reruns=self.green_reruns, limit=self.w["passing_test_reruns"])
                    return "review"
            else:
                self.green_reruns = 0
            self.last_green_hash = code_hash
            return "review"
        self.green_reruns = 0
        if changed:
            self.fail_streak += 1
        if self.attempts >= self.caps["attempts_per_tool"]:
            self.trigger("attempt_budget", attempts=self.attempts, limit=self.caps["attempts_per_tool"])
            return "budget"
        if self.fail_streak >= self.w["fail_streak_restart"]:
            self.trigger("fail_streak", streak=self.fail_streak, limit=self.w["fail_streak_restart"], action="restart",
                         attempts=self.attempts)
            self.fail_streak = 0
            return "restart"
        return "continue"

    def time_or_turns_exceeded(self):
        el = time.time() - self.task_t0
        if el > self.caps["task_wall_clock_s"] or self.turns >= self.caps["task_turns"]:
            self.trigger("time_turn_limit", elapsed_s=round(el), turns=self.turns,
                         limit_s=self.caps["task_wall_clock_s"], limit_turns=self.caps["task_turns"])
            return True
        return False

    def extend(self):
        """The user chose to continue after a time/turn limit: give the same budget again."""
        self.task_t0 = time.time()
        self.turns = 0
