"""Cloud fallback providers: text in, text out. Nothing else.

The provider never gets tools: no file edits, no shell, no web, no MCP. Its reply is plain text; code in it goes
through mini's own extraction, sandbox, tests and approval gate like local code. Credentials stay on the host:
the CLI uses its own existing login; nothing is passed into any sandbox.

Add a provider by subclassing Provider and registering it in PROVIDERS. Possible next ones: Codex CLI with a
ChatGPT login, Gemini CLI, any API-key endpoint (OpenAIBackend already speaks that protocol).
"""
import json
import os
import subprocess
import tempfile
import time


class FallbackCapError(Exception):
    pass


class Provider:
    name = "base"
    leaves_machine = True

    def complete(self, system, prompt, max_budget_usd, timeout_s):
        """Returns dict(text, input_tokens, output_tokens, cost_usd, error, secs)."""
        raise NotImplementedError


class ClaudeCLI(Provider):
    """Claude subscription through `claude -p` and the existing Claude Code login. No API key in the repo."""
    name = "claude_cli"
    DENY = "Bash Edit Write Read MultiEdit NotebookEdit WebFetch WebSearch Glob Grep Task TodoWrite Agent"

    def __init__(self, model, max_output_tokens=8000):
        self.model, self.max_output_tokens = model, max_output_tokens

    def argv(self, system, max_budget_usd):
        return ["claude", "-p", "--model", self.model,
                "--tools", "",                                   # no built-in tools at all
                "--disallowed-tools", self.DENY,                 # and deny them by name as a second layer
                "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--setting-sources", "", "--disable-slash-commands", "--no-session-persistence",
                "--output-format", "json", "--max-budget-usd", f"{max_budget_usd:.4f}",
                "--system-prompt", system]

    def complete(self, system, prompt, max_budget_usd, timeout_s):
        t0 = time.time()
        env = {k: v for k, v in os.environ.items() if not k.startswith("MINI_")}
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(self.max_output_tokens)   # bounds the worst-case cost of one call
        with tempfile.TemporaryDirectory(prefix="mini-fallback-") as empty:   # no project files, no CLAUDE.md
            try:
                r = subprocess.run(self.argv(system, max_budget_usd), input=prompt, capture_output=True, text=True,
                                   timeout=timeout_s, cwd=empty, env=env)
            except subprocess.TimeoutExpired:
                return {"text": "", "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
                        "error": f"timeout after {timeout_s} s", "secs": round(time.time() - t0, 1)}
        try:
            d = json.loads(r.stdout)
        except ValueError:
            return {"text": "", "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
                    "error": f"exit {r.returncode}: {(r.stderr or r.stdout)[-500:]}", "secs": round(time.time() - t0, 1)}
        u = d.get("usage") or {}
        tin = (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
        return {"text": d.get("result") or "", "input_tokens": tin, "output_tokens": u.get("output_tokens") or 0,
                "cost_usd": float(d.get("total_cost_usd") or 0.0),
                "error": (d.get("subtype") if d.get("is_error") else None), "secs": round(time.time() - t0, 1),
                "num_turns": d.get("num_turns")}


class FakeProvider(Provider):
    """For harness tests: returns scripted replies, reports a fixed cost."""
    name = "fake"

    def __init__(self, replies, cost=0.1, tokens=1000):
        self.replies, self.cost, self.tokens = list(replies), cost, tokens
        self.calls = []

    def complete(self, system, prompt, max_budget_usd, timeout_s):
        self.calls.append({"system": system, "prompt": prompt, "max_budget_usd": max_budget_usd})
        text = self.replies.pop(0) if self.replies else ""
        return {"text": text, "input_tokens": self.tokens, "output_tokens": self.tokens // 4, "cost_usd": self.cost,
                "error": None, "secs": 0.01}


PROVIDERS = {"claude_cli": lambda fcfg: ClaudeCLI(fcfg["model"], fcfg["max_output_tokens"])}


class Fallback:
    """Enforces the fallback caps from config [fallback] across one run."""

    def __init__(self, cfg, log, provider=None):
        self.f = cfg["fallback"]
        self.log = log
        self.provider = provider or (PROVIDERS[self.f["provider"]](self.f) if self.f["enabled"] else None)
        self.tokens = 0
        self.spend = 0.0
        self.calls = 0

    @property
    def enabled(self):
        return bool(self.f["enabled"] and self.provider)

    def remaining(self):
        return {"tokens": self.f["tokens_per_run"] - self.tokens, "usd": round(self.f["spend_usd_per_run"] - self.spend, 4)}

    def complete(self, system, prompt):
        rem = self.remaining()
        est_in = (len(system) + len(prompt)) // 3
        worst_tokens = est_in + self.f["max_output_tokens"]
        worst_usd = (est_in * self.f["price_in_per_mtok"] + self.f["max_output_tokens"] * self.f["price_out_per_mtok"]) / 1e6
        if rem["tokens"] < worst_tokens or rem["usd"] < worst_usd:
            self.log.emit("cap", cap="fallback", tokens_used=self.tokens, spend_usd=round(self.spend, 4),
                          limit_tokens=self.f["tokens_per_run"], limit_usd=self.f["spend_usd_per_run"],
                          next_call_worst_case_usd=round(worst_usd, 4), next_call_worst_case_tokens=worst_tokens)
            raise FallbackCapError(f"fallback cap: the next call could cost up to ${worst_usd:.3f} / {worst_tokens} tokens; "
                                   f"used {self.tokens} tokens, ${self.spend:.4f} "
                                   f"(caps {self.f['tokens_per_run']} tokens, ${self.f['spend_usd_per_run']})")
        self.calls += 1
        self.log.emit("fallback", phase="request", provider=self.provider.name, model=self.f.get("model"),
                      prompt_chars=len(prompt), budget_usd=rem["usd"], text=prompt)
        r = self.provider.complete(system, prompt, rem["usd"], self.f["timeout_s"])
        self.tokens += r["input_tokens"] + r["output_tokens"]
        self.spend += r["cost_usd"]
        self.log.emit("fallback", phase="reply", provider=self.provider.name, input_tokens=r["input_tokens"],
                      output_tokens=r["output_tokens"], cost_usd=r["cost_usd"], total_spend_usd=round(self.spend, 4),
                      total_tokens=self.tokens, error=r["error"], secs=r["secs"], text=r["text"])
        return r
