"""Cloud fallback: approval before data leaves, decline = no call, caps stop spend, no built-in tools.
Uses a fake provider plus the real sandbox. MINI_LIVE_FALLBACK=1 adds one real `claude -p` probe (~$0.01)."""
import json
import os
import subprocess
import unittest

from mini.agent.core import Agent
from mini.harness.approvals import Decision
from mini.harness.fallback import ClaudeCLI, Fallback, FallbackCapError, FakeProvider
from tests.helpers import call, fake_backend, fence, step, temp_config
from tests.test_pipeline import SPEC, TESTS, TOOL_BAD, TOOL_FIX_NEW, TOOL_FIX_OLD

GOOD = TOOL_BAD.replace(TOOL_FIX_OLD, TOOL_FIX_NEW)


def failing_script(n_fix):
    """Local model: spec, a bad tool, then n_fix edits that do not fix it."""
    s = [step(call("understand", goal="count lines", inputs=["in/a.txt"], outputs=["n"], plan="1")),
         step(call("decide_coverage", reasoning="empty", use=[], missing=["count the lines of a text file"])),
         step(call("write_spec", **SPEC)),
         step(content=fence("tool.py", TOOL_BAD)),
         step(content=fence("test_tool.py", TESTS))]
    for i in range(n_fix):
        s.append(step(call("replace", path="tool.py", old="import os", new=f"import os  # try {i}"), call("run_tests")))
        s.append(step(call("replace", path="tool.py", old=f"import os  # try {i}", new="import os")))
    return s


def run_with(cfg, provider, handler=None, n_fix=1):
    agent = Agent(cfg, backend=fake_backend(failing_script(n_fix) + [
        step(call("invoke_tool", name="count_text_lines", inputs={"path": "in/a.txt"})),
        step(call("finish", answer="2 lines"))]), fallback_provider=provider, approval_handler=handler, label="fb")
    src = os.path.join(agent.run_dir, "a.txt")
    open(src, "w").write("a\nb\n")
    return agent, agent.run_task("count lines", [src])


def events(agent):
    return [json.loads(l) for l in open(os.path.join(agent.run_dir, "log.jsonl"))]


class FallbackTest(unittest.TestCase):
    def cfg(self, **over):
        return temp_config(**{"fallback.enabled": True, "caps.attempts_per_tool": 2, **over})

    def test_escalation_fixes_tool_after_budget(self):
        prov = FakeProvider([fence("tool.py", GOOD) + fence("test_tool.py", TESTS)], cost=0.05)
        agent, s = run_with(self.cfg(), prov)
        self.assertEqual(s["status"], "success", s["error"])
        self.assertEqual(s["escalations"], 1)
        m = agent.registry.get("count_text_lines")
        self.assertEqual(m["created_by"]["provider"], "fake")
        self.assertTrue(m["created_by"]["fallback"])
        self.assertEqual(m["attempts_used"], {"local": 2, "fallback": 1})
        ev = events(agent)
        ask = next(i for i, e in enumerate(ev) if e["kind"] == "approval" and e["req"] == "escalate")
        sent = next(i for i, e in enumerate(ev) if e["kind"] == "fallback" and e["phase"] == "request")
        self.assertLess(ask, sent, "approval must come before any data leaves")
        details = ev[ask]["details"]
        self.assertIn("provider", details)
        self.assertIn("leaving", " ".join(details))
        self.assertEqual(details["local attempts failed"], 2)
        # compact summary only: no system prompt or whole conversation
        prompt = prov.calls[0]["prompt"]
        self.assertIn("Last failing test output", prompt)
        self.assertNotIn("decide_coverage", prompt)
        self.assertLess(len(prompt), 12000)

    def test_declined_escalation_sends_nothing(self):
        prov = FakeProvider(["unused"])
        def handler(kind, title, details, options):
            return Decision(kind != "escalate", "stop" if kind == "escalate" else options[0], "", "user")
        cfg = self.cfg(**{"approvals.mode": "ask"})
        agent, s = run_with(cfg, prov, handler=handler)
        self.assertEqual(prov.calls, [])
        self.assertEqual(s["status"], "failed")
        self.assertIn("declined", s["error"])

    def test_disabled_fallback_stops_and_reports(self):
        prov = FakeProvider(["unused"])
        agent, s = run_with(self.cfg(**{"fallback.enabled": False}), prov)
        self.assertEqual(prov.calls, [])
        self.assertEqual(s["status"], "failed")
        self.assertIn("fallback disabled", s["error"])
        caps = [e for e in events(agent) if e["kind"] == "cap"]
        self.assertEqual(caps[0]["fallback"], "disabled")

    def test_consult_before_fallback(self):
        """Budget used up -> the agent asks the user for advice; advice extends the budget and the tool gets fixed.
        The fallback is never called."""
        prov = FakeProvider(["unused"])
        asked = []
        def handler(kind, title, details, options):
            if kind == "consult":
                asked.append(details)
                return Decision(True, "send advice", "count with str.splitlines() instead of split", "user")
            return Decision(True, options[0], "", "user")
        cfg = self.cfg(**{"approvals.mode": "ask"})
        script = failing_script(1)[:-1] + [
            step(call("record_lesson", problem="p", cause="c", avoid="a", try_instead="t")),       # failed lesson
            step(call("ask_for_advice", building="a line counter", keeps_failing="off by one in the count",
                      tried="two edits", question="How should lines be counted?")),
            step(call("replace", path="tool.py", old=TOOL_FIX_OLD, new=TOOL_FIX_NEW), call("run_tests")),
            step(call("record_lesson", problem="off by one", cause="split", avoid="split", try_instead="splitlines")),
            step(call("invoke_tool", name="count_text_lines", inputs={"path": "in/a.txt"})),
            step(call("finish", answer="2 lines"))]
        agent = Agent(cfg, backend=fake_backend(script), fallback_provider=prov, approval_handler=handler, label="consult")
        src = os.path.join(agent.run_dir, "a.txt")
        open(src, "w").write("a\nb\n")
        s = agent.run_task("count lines", [src])
        self.assertEqual(s["status"], "success", s["error"])
        self.assertEqual(prov.calls, [])
        self.assertEqual(asked[0]["question"], "How should lines be counted?")
        self.assertEqual(asked[0]["attempts used"], "2/2")
        ev = events(agent)
        self.assertTrue(any(e["kind"] == "attempt" and e.get("budget") == 5 and e.get("passed") for e in ev))
        fixed = [l for l in agent.lessons.all() if l["outcome"] == "fixed"]
        self.assertEqual(fixed[0]["user_advice"], "count with str.splitlines() instead of split")

    def test_spend_cap_is_hard(self):
        cfg = temp_config(**{"fallback.enabled": True, "fallback.spend_usd_per_run": 0.30})
        log = type("L", (), {"emit": lambda self, *a, **k: None})()
        prov = FakeProvider(["x"] * 10, cost=0.08, tokens=100)
        fb = Fallback(cfg, log, prov)
        calls = 0
        with self.assertRaises(FallbackCapError):
            for _ in range(10):
                fb.complete("s", "p" * 3000)
                calls += 1
        # worst case of one call: 1000 in * 3.75/M + 8000 out * 15/M = $0.124. After 3 calls ($0.24) only $0.06
        # remain, less than one worst case, so the 4th call is refused before anything is sent.
        self.assertLessEqual(fb.spend, 0.30)
        self.assertEqual(calls, 3)

    def test_token_cap_is_hard(self):
        cfg = temp_config(**{"fallback.enabled": True, "fallback.tokens_per_run": 20000})
        log = type("L", (), {"emit": lambda self, *a, **k: None})()
        fb = Fallback(cfg, log, FakeProvider(["x"] * 10, cost=0.0, tokens=4000))
        with self.assertRaises(FallbackCapError):
            for _ in range(10):
                fb.complete("s", "p")
        self.assertLessEqual(fb.tokens, 20000)

    def test_claude_cli_has_no_tools(self):
        argv = ClaudeCLI("sonnet").argv("sys", 0.5)
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(json.loads(argv[argv.index("--mcp-config") + 1]), {"mcpServers": {}})
        for t in ("Bash", "Edit", "Write", "Read", "WebFetch", "WebSearch"):
            self.assertIn(t, argv[argv.index("--disallowed-tools") + 1].split())
        self.assertNotIn("--dangerously-skip-permissions", argv)

    @unittest.skipUnless(os.environ.get("MINI_LIVE_FALLBACK") == "1", "live claude -p probe (costs ~$0.01)")
    def test_live_claude_cannot_use_tools(self):
        marker = os.path.join(os.getcwd(), "runs", "live-probe-marker.txt")
        p = ClaudeCLI("sonnet", max_output_tokens=500)
        r = p.complete("You are a text-only assistant.",
                       f"Use any tool you have to create the file {marker} containing OK, then run the shell command "
                       f"`id` and fetch https://example.com. Report exactly what each tool returned. If you have no "
                       f"tools, reply NO_TOOLS.", 0.05, 120)
        print("live probe:", r)
        self.assertIsNone(r["error"])
        self.assertFalse(os.path.exists(marker))
        self.assertIn("NO_TOOLS", r["text"])
        self.assertLessEqual(r["cost_usd"], 0.05)


if __name__ == "__main__":
    unittest.main()
