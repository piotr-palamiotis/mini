"""The attempt budget: when it is used up the user is asked for advice, then the run stops; a task never moves to
another model. Plus the stuck-loop watchdog and the request llama-server gets. Scripted models plus the real sandbox."""
import io
import json
import os
import unittest
from unittest import mock

from mini.agent.core import Agent
from mini.harness.approvals import Decision
from mini.harness.backends import Endpoint, OpenAIBackend
from tests.helpers import spec_steps, call, fake_backend, fence, step, temp_config
from tests.test_pipeline import SPEC, TESTS, TOOL_BAD, TOOL_FIX_NEW, TOOL_FIX_OLD

GOOD = TOOL_BAD.replace(TOOL_FIX_OLD, TOOL_FIX_NEW)
LESSON = step(call("record_lesson", problem="p", cause="c", avoid="a", try_instead="t"))


def local_failing():
    """Local model: spec, a bad tool, one edit that does not fix it (2 attempts), then the failure lesson."""
    return [step(call("understand", goal="count lines", inputs=["in/a.txt"], outputs=["n"], plan="1")),
            step(call("decide_coverage", reasoning="empty", use=[], missing=["count the lines of a text file"])),
            *spec_steps(SPEC),
            step(content=fence("tool.py", TOOL_BAD)),
            step(content=fence("test_tool.py", TESTS)),
            step(call("replace", path="tool.py", old="import os", new="import os  # try"), call("run_tests")),
            LESSON]


def recording(backend):
    """Keep the messages each chat call saw."""
    backend.seen = []
    orig = backend.chat
    def chat(model, messages, *a, **k):
        backend.seen.append([dict(m) for m in messages])
        return orig(model, messages, *a, **k)
    backend.chat = chat
    return backend


def run(cfg, local, handler=None):
    agent = Agent(cfg, backend=local, approval_handler=handler, label="budget")
    src = os.path.join(agent.run_dir, "a.txt")
    open(src, "w").write("a\nb\n")
    return agent, agent.run_task("count lines", [src])


def events(agent, kind=None):
    ev = [json.loads(l) for l in open(os.path.join(agent.run_dir, "log.jsonl"))]
    return [e for e in ev if kind is None or e["kind"] == kind]


class BudgetTest(unittest.TestCase):
    def cfg(self, **over):
        return temp_config(**{"caps.attempts_per_tool": 2, **over})

    def test_budget_used_up_asks_then_stops(self):
        agent, s = run(self.cfg(), fake_backend(local_failing() + [LESSON]))
        self.assertEqual(s["status"], "failed")
        self.assertIn("tests still fail after 2 attempts", s["error"])
        consult = [e for e in events(agent, "approval") if e["req"] == "consult" and e["phase"] == "requested"]
        self.assertEqual(consult[0]["options"], ["send advice", "stop"])

    def test_consult_advice_extends_the_budget(self):
        asked = []
        def handler(kind, title, details, options):
            if kind == "consult":
                asked.append(details)
                return Decision(True, "send advice", "count with str.splitlines() instead of split", "user")
            return Decision(True, options[0], "", "user")
        script = local_failing() + [
            step(call("ask_for_advice", building="a line counter", keeps_failing="off by one in the count",
                      tried="two edits", question="How should lines be counted?")),
            step(call("replace", path="tool.py", old=TOOL_FIX_OLD, new=TOOL_FIX_NEW), call("run_tests")),
            step(call("record_lesson", problem="off by one", cause="split", avoid="split", try_instead="splitlines")),
            step(call("invoke_tool", name="count_text_lines", inputs={"path": "in/a.txt"})),
            step(call("finish", answer="2 lines"))]
        agent, s = run(self.cfg(**{"approvals.mode": "ask"}), fake_backend(script), handler=handler)
        self.assertEqual(s["status"], "success", s["error"])
        self.assertEqual(asked[0]["question"], "How should lines be counted?")
        self.assertEqual(asked[0]["attempts used"], "2")
        self.assertTrue(any(e.get("budget") == 5 and e.get("passed") for e in events(agent, "attempt")))
        fixed = [l for l in agent.lessons.all() if l["outcome"] == "fixed"]
        self.assertEqual(fixed[0]["user_advice"], "count with str.splitlines() instead of split")


class StuckLoopTest(unittest.TestCase):
    def test_the_same_failing_call_is_blocked_with_its_error_then_the_run_stops(self):
        bad = call("list_files", path="x", bogus=1)
        script = [step(call("understand", goal="g", inputs=[], outputs=["n"], plan="1")),
                  step(call("decide_coverage", reasoning="none needed", use=[], missing=[]))] + [step(bad)] * 12
        backend = recording(fake_backend(script))
        agent, s = run(temp_config(), backend)
        self.assertNotEqual(s["status"], "success")
        self.assertIn("stuck repeating list_files", s["error"])
        blocked = [m["content"] for chat in backend.seen for m in chat if m.get("role") == "tool" and m["content"].startswith("BLOCKED")]
        self.assertTrue(blocked)
        self.assertIn("unknown argument bogus", blocked[0])


class OpenAIBodyTest(unittest.TestCase):
    def body(self, **options):
        sent = {}
        def fake_urlopen(req, timeout):
            sent.update(json.loads(req.data))
            return io.BytesIO(b'data: {"choices":[{"delta":{"reasoning":"r","content":"c"},"finish_reason":"stop"}]}\n'
                              b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2}}\ndata: [DONE]\n')
        b = OpenAIBackend(Endpoint("openai", "http://x/v1"))
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            out = b.chat("m", [{"role": "user", "content": "hi"}], [], think=False, options=options)
        return sent, out

    def test_a_restarting_server_is_waited_for(self):
        import urllib.error
        from mini.harness import backends
        tries = []
        def fake_urlopen(req, timeout):             # slip6: llama-server OOM-killed, systemd brought it back
            tries.append(1)
            if len(tries) < 3:
                raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
            return io.BytesIO(b'data: {"choices":[{"delta":{"content":"c"},"finish_reason":"stop"}]}\ndata: [DONE]\n')
        b = OpenAIBackend(Endpoint("openai", "http://x/v1"))
        with mock.patch("urllib.request.urlopen", fake_urlopen), mock.patch.object(backends, "RESTART_POLL_S", 0):
            out = b.chat("m", [{"role": "user", "content": "hi"}], [])
        self.assertEqual((len(tries), out["raw_error"], out["content"]), (3, None, "c"))
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError(ConnectionRefusedError())), \
                mock.patch.object(backends, "RESTART_POLL_S", 0), mock.patch.object(backends, "RESTART_WAIT_S", 0):
            self.assertIn("unreachable", b.chat("m", [{"role": "user", "content": "hi"}], [])["raw_error"])

    def test_a_reply_cut_off_by_the_server_dying_is_sent_again(self):
        import http.client
        import urllib.error
        from mini.harness import backends

        class Cut(io.BytesIO):                          # the model process was OOM-killed while it streamed
            def __iter__(self):
                yield b'data: {"choices":[{"delta":{"content":"half"}}]}\n'
                raise http.client.IncompleteRead(b"")

        class Unreadable(io.BytesIO):
            def read(self, *a):
                raise http.client.IncompleteRead(b"")
        tries, seen = [], []

        def fake_urlopen(req, timeout):
            tries.append(1)
            if len(tries) == 1:
                return Cut()
            if len(tries) == 2:                         # the router, while its model restarts; the body is cut too
                raise urllib.error.HTTPError("http://x", 502, "bad gateway", {}, Unreadable())
            return io.BytesIO(b'data: {"choices":[{"delta":{"content":"whole"},"finish_reason":"stop"}]}\ndata: [DONE]\n')
        b = OpenAIBackend(Endpoint("openai", "http://x/v1"))
        with mock.patch("urllib.request.urlopen", fake_urlopen), mock.patch.object(backends, "RESTART_POLL_S", 0):
            out = b.chat("m", [{"role": "user", "content": "hi"}], [], on_delta=lambda k, t: seen.append(k))
        self.assertEqual((len(tries), out["raw_error"], out["content"], out["resent"]), (3, None, "whole", 1))
        self.assertEqual(seen, ["content", "reset", "content"], "the cut text is closed before the new reply")

        def refused(req, timeout):                    # an error whose body cannot be read is an error, not a crash
            raise urllib.error.HTTPError("http://x", 400, "bad", {}, Unreadable())
        with mock.patch("urllib.request.urlopen", refused):
            self.assertEqual(b.chat("m", [{"role": "user", "content": "hi"}], [])["raw_error"], "HTTP 400: b''")
        with mock.patch("urllib.request.urlopen", lambda req, timeout: Cut()), \
                mock.patch.object(backends, "RESTART_POLL_S", 0):
            out = b.chat("m", [{"role": "user", "content": "hi"}], [])
        self.assertIn("dropped the reply 3 times", out["raw_error"])

    def test_llama_server_gets_thinking_controls(self):
        sent, out = self.body()
        self.assertEqual(sent["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(sent["reasoning_budget_tokens"], 0)
        self.assertNotIn("tools", sent, "an empty tool list is not sent")
        self.assertEqual((out["thinking"], out["content"], out["prompt_tokens"]), ("r", "c", 3))

    def test_thinking_loop_is_cut_before_any_answer(self):
        chunks = b"".join(b'data: {"choices":[{"delta":{"reasoning":"xxxxxxxxxx"}}]}\n' for _ in range(50))
        b = OpenAIBackend(Endpoint("openai", "http://x/v1", think_max_chars=100))
        with mock.patch("urllib.request.urlopen", lambda req, timeout: io.BytesIO(chunks + b"data: [DONE]\n")):
            out = b.chat("m", [{"role": "user", "content": "hi"}], [], think=True)
        self.assertEqual(out["done_reason"], "length")
        self.assertLess(len(out["thinking"]), 200)

    def test_thinking_budget_is_sent_and_its_use_reported(self):
        from mini.harness.backends import THINK_BUDGET_MESSAGE
        sent = {}
        chunk = json.dumps({"choices": [{"delta": {"reasoning_content": "x" + THINK_BUDGET_MESSAGE}}]}).encode()

        def fake_urlopen(req, timeout):
            sent.update(json.loads(req.data))
            return io.BytesIO(b"data: " + chunk + b"\ndata: [DONE]\n")
        b = OpenAIBackend(Endpoint("openai", "http://x/v1", think_budget_tokens=50))
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            out = b.chat("m", [{"role": "user", "content": "hi"}], [], think=True)
        self.assertEqual((sent["reasoning_budget_tokens"], sent["reasoning_budget_message"]), (50, THINK_BUDGET_MESSAGE))
        self.assertTrue(out["think_budget_hit"])
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            b.ep.think_budget_tokens = 0
            sent.clear()
            b.chat("m", [{"role": "user", "content": "hi"}], [], think=True)
        self.assertNotIn("reasoning_budget_tokens", sent)

    def test_tool_call_of_mostly_comments_is_cut_while_streaming(self):
        script = "import struct\\n" + "# 0x1c: 6f 6e 79 78 (a name?)\\n" * 400
        deltas = [script[i:i + 50] for i in range(0, len(script), 50)]
        chunks = b"".join(b"data: " + json.dumps({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"name": "run_code" if i == 0 else "", "arguments": ('{"script":"' if i == 0 else "") + d}}]}}]}).encode()
            + b"\n" for i, d in enumerate(deltas))
        sent = {}

        def fake_urlopen(req, timeout):
            sent.update(json.loads(req.data))
            return io.BytesIO(chunks + b"data: [DONE]\n")
        b = OpenAIBackend(Endpoint("openai", "http://x/v1", max_tokens=8192, max_tokens_tools=6144, comment_cut_chars=4000))
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            out = b.chat("m", [{"role": "user", "content": "hi"}], [{"type": "function"}], think=True)
        self.assertEqual((out["done_reason"], out["cut"]), ("length", "comments"))
        self.assertEqual(sent["max_tokens"], 6144, "a reply that may call tools gets the lower cap")
        code = "x = 1\\n" * 2000                      # long but real code: not cut
        chunks = b"data: " + json.dumps({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"name": "run_code", "arguments": '{"script":"' + code + '"}'}}]}}]}).encode() + b"\n"
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            out = b.chat("m", [{"role": "user", "content": "hi"}], [{"type": "function"}], think=True)
        self.assertNotIn("cut", out)
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            b.chat("m", [{"role": "user", "content": "hi"}], [], think=True)
        self.assertEqual(sent["max_tokens"], 8192, "text-only turns keep the full cap")


if __name__ == "__main__":
    unittest.main()
