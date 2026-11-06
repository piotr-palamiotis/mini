"""The conversation must not outgrow the model's context: old tool results are shortened first, and when nothing
can be shortened the run says so instead of reporting unparseable tool calls."""
import unittest

from mini.agent.core import Agent
from mini.agent.turns import AgentStop, Turns, conversation_chars, shrink
from tests.helpers import temp_config

ERR = "HTTP 400: b'{\"error\":{\"code\":400,\"message\":\"request (%d tokens) exceeds the available context size (%d tokens)\"}}'"


def msgs(*sizes):
    out = [{"role": "system", "content": "s"}, {"role": "user", "content": "task"}]
    for i, n in enumerate(sizes):
        out += [{"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "list_files", "arguments": {}}}]},
                {"role": "tool", "tool_name": "list_files", "content": f"R{i}" + "x" * n}]
    return out


class Stub:
    """A backend that fails like llama-server with a full context for the first `fails` calls."""
    def __init__(self, fails):
        self.fails, self.calls = fails, 0

    def chat(self, model, messages, tools, **kw):
        self.calls += 1
        base = {"content": "", "thinking": "", "tool_calls": [], "done_reason": "stop", "prompt_tokens": 0,
                "output_tokens": 0, "secs": 0.0, "raw_error": None, "stopped": False}
        if self.calls <= self.fails:
            return {**base, "raw_error": ERR % (41523, 40960)}
        return {**base, "tool_calls": [{"name": "finish", "arguments": {"answer": "ok"}}], "prompt_tokens": 100}


class ContextTest(unittest.TestCase):
    def test_shrink_cuts_the_oldest_large_results_and_keeps_the_latest(self):
        m = msgs(5000, 5000, 5000, 5000)
        before = conversation_chars(m)
        saved = shrink(m, before - 9000)
        self.assertGreaterEqual(saved, 9000)
        self.assertIn("older result shortened", m[3]["content"])
        self.assertTrue(m[3]["content"].startswith("R0"), "the start of the result stays")
        self.assertEqual(len(m[-1]["content"]), 5002, "the newest results are untouched")
        self.assertEqual(conversation_chars(m), before - saved)
        self.assertEqual(shrink(msgs(100, 100), 10), 0, "small results are not worth shortening")

    def run_turns(self, fails, sizes):
        a = Agent(temp_config(), label="ctx")
        a.backend = Stub(fails)
        t = Turns(a, "execute", msgs(*sizes), [])
        return a, t, t.ask()

    def test_a_full_context_is_shortened_and_retried(self):
        a, t, reply = self.run_turns(1, [9000, 9000, 9000, 9000])
        self.assertEqual(reply["tool_calls"][0]["name"], "finish")
        self.assertEqual(a.backend.calls, 2)
        self.assertTrue(any("older result shortened" in (m.get("content") or "") for m in t.messages))

    def test_nothing_left_to_shorten_is_said_plainly(self):
        a = Agent(temp_config(), label="ctx")
        a.backend = Stub(5)
        t = Turns(a, "execute", msgs(100, 100), [])
        with self.assertRaises(AgentStop) as cm:
            t.ask()
        self.assertIn("no longer fits the model's context (40960 tokens)", str(cm.exception))
        self.assertEqual(a.backend.calls, 1, "no pointless retries")


if __name__ == "__main__":
    unittest.main()
