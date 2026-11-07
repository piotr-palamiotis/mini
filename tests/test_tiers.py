"""Model tiers: local -> free moves by itself (attempts or time used up) and the conversation carries over;
paid is only used when the user picks it; with no tier left the user is asked, then the run stops.
Uses scripted backends per tier plus the real sandbox."""
import io
import json
import os
import unittest
from unittest import mock

from mini.agent.core import Agent
from mini.harness.approvals import Decision
from mini.harness.backends import Endpoint, OpenAIBackend
from tests.helpers import call, fake_backend, fence, step, temp_config
from tests.test_pipeline import SPEC, TESTS, TOOL_BAD, TOOL_FIX_NEW, TOOL_FIX_OLD

GOOD = TOOL_BAD.replace(TOOL_FIX_OLD, TOOL_FIX_NEW)
LESSON = step(call("record_lesson", problem="p", cause="c", avoid="a", try_instead="t"))


def local_failing():
    """Local model: spec, a bad tool, one edit that does not fix it (2 attempts), then the failure lesson."""
    return [step(call("understand", goal="count lines", inputs=["in/a.txt"], outputs=["n"], plan="1")),
            step(call("decide_coverage", reasoning="empty", use=[], missing=["count the lines of a text file"])),
            step(call("write_spec", **SPEC)),
            step(content=fence("tool.py", TOOL_BAD)),
            step(content=fence("test_tool.py", TESTS)),
            step(call("replace", path="tool.py", old="import os", new="import os  # try"), call("run_tests")),
            LESSON]


def fixes_and_finishes():
    return [step(call("replace", path="tool.py", old=TOOL_FIX_OLD, new=TOOL_FIX_NEW), call("run_tests")),
            step(call("invoke_tool", name="count_text_lines", inputs={"path": "in/a.txt"})),
            step(call("finish", answer="2 lines"))]


def recording(backend):
    """Keep the messages each chat call saw."""
    backend.seen = []
    orig = backend.chat
    def chat(model, messages, *a, **k):
        backend.seen.append([dict(m) for m in messages])
        return orig(model, messages, *a, **k)
    backend.chat = chat
    return backend


def run(cfg, local, others=None, handler=None, start_tier=None):
    agent = Agent(cfg, backend=local, tier_backends=others or {}, approval_handler=handler, start_tier=start_tier,
                  label="tiers")
    src = os.path.join(agent.run_dir, "a.txt")
    open(src, "w").write("a\nb\n")
    return agent, agent.run_task("count lines", [src])


def events(agent, kind=None):
    ev = [json.loads(l) for l in open(os.path.join(agent.run_dir, "log.jsonl"))]
    return [e for e in ev if kind is None or e["kind"] == kind]


class TiersTest(unittest.TestCase):
    def cfg(self, **over):
        return temp_config(**{"caps.attempts_per_tool": 2, **over})

    def test_local_failure_never_moves_to_another_model(self):
        free = fake_backend(fixes_and_finishes())
        agent, s = run(self.cfg(**{"free.enabled": True, "paid.enabled": True}), fake_backend(local_failing() + [LESSON]),
                       {"free": free, "paid": fake_backend([])})
        self.assertEqual(s["status"], "failed")                      # auto mode refuses the consult dialog: stop
        self.assertEqual(s["tiers"]["used"], ["local"])
        self.assertEqual(events(agent, "tier"), [])
        self.assertTrue(free.script, "the free model was never asked")

    def test_no_tier_left_asks_then_stops(self):
        agent, s = run(self.cfg(), fake_backend(local_failing() + [LESSON]))
        self.assertEqual(s["status"], "failed")
        self.assertIn("tests still fail after 2 attempts", s["error"])
        self.assertEqual(s["tiers"]["used"], ["local"])
        consult = [e for e in events(agent, "approval") if e["req"] == "consult" and e["phase"] == "requested"]
        self.assertEqual(consult[0]["options"], ["send advice", "stop"], "paid is off: not offered")

    def test_paid_only_when_the_user_picks_it(self):
        offered = []
        def handler(kind, title, details, options):
            if kind == "consult":
                offered.append(options)
                return Decision(True, "use paid", "", "user")
            return Decision(True, options[0], "", "user")
        paid = fake_backend(fixes_and_finishes())
        cfg = self.cfg(**{"paid.enabled": True, "approvals.mode": "ask", "consult.enabled": False})
        agent, s = run(cfg, fake_backend(local_failing()), {"paid": paid}, handler=handler)
        self.assertEqual(s["status"], "success", s["error"])
        self.assertEqual(offered, [["use paid", "stop"]])
        self.assertEqual([(e["tier"], e["decided_by"]) for e in events(agent, "tier")], [("paid", "user")])
        self.assertEqual(agent.registry.get("count_text_lines")["created_by"]["provider"], "paid")

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
        self.assertEqual(asked[0]["attempts used"], '{"local": 2}')
        self.assertTrue(any(e.get("budget") == 5 and e.get("passed") for e in events(agent, "attempt")))
        fixed = [l for l in agent.lessons.all() if l["outcome"] == "fixed"]
        self.assertEqual(fixed[0]["user_advice"], "count with str.splitlines() instead of split")

    def test_start_tier(self):
        paid = fake_backend([step(call("understand", goal="g", inputs=[], outputs=["n"], plan="1")),
                             step(call("decide_coverage", reasoning="none needed", use=[], missing=[])),
                             step(call("finish", answer="done"))])
        agent, s = run(self.cfg(**{"paid.enabled": True}), fake_backend([]), {"paid": paid}, start_tier="paid")
        self.assertEqual(s["status"], "success", s["error"])
        self.assertEqual(s["tiers"], {"start": "paid", "used": ["paid"], "tokens": {"paid": 360}})


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

    def test_gateway_gets_no_llama_fields(self):
        sent, _ = self.body(thinking_controls=False)
        self.assertNotIn("chat_template_kwargs", sent)
        self.assertNotIn("reasoning_budget_tokens", sent)
        self.assertNotIn("thinking_controls", sent)


if __name__ == "__main__":
    unittest.main()
