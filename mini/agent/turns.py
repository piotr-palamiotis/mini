"""One model conversation with the reply-level watchdogs enforced in code.

ask() returns the next reply that carries at least one usable tool call. Runaway output, unparseable calls and
empty replies are answered by the harness (they never count as attempts). Time/turn limits ask the user.
When the local model's time budget for the task is used up, the conversation moves to the free tier.
"""
import json
import re
import time

from ..harness.eventlog import ReplyStream
from ..harness.gates import Stopped

CHARS_PER_TOKEN = 3            # a cautious estimate; code and JSON are denser than prose
CONTEXT_SHARE = 0.75           # shorten old results before the conversation passes this share of the context


def conversation_chars(messages):
    return sum(len(m.get("content") or "") + len(json.dumps(m.get("tool_calls") or "")) for m in messages)


def shrink(messages, target_chars, keep_last=4):
    """Shorten the oldest large tool results until the conversation is under target_chars. The latest messages and
    the structure (who said what, which calls were made) stay. Returns the characters saved."""
    saved = 0
    for m in messages[:-keep_last]:
        if conversation_chars(messages) <= target_chars:
            break
        text = m.get("content") or ""
        if m.get("role") == "tool" and len(text) > 600:
            m["content"] = text[:300].rstrip() + f"\n[... older result shortened to save context: was {len(text)} characters]"
            saved += len(text) - len(m["content"])
    return saved


class AgentStop(Exception):
    """The run stops: cap reached, user said stop, hardware watchdog, unrecoverable model output."""


class RestartRequested(Exception):
    pass


def fn(name, desc, props, req=()):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "required": list(req), "properties": props}}}


class Turns:
    def __init__(self, agent, step, messages, tools, implicit=None, think_tokens=None):
        self.a, self.step, self.messages, self.tools = agent, step, messages, tools
        self.think_tokens = think_tokens   # this conversation's own thinking budget per reply (None: the default)
        self.runaway = False
        self.implicit = implicit       # implicit(reply) -> synthesized tool call or None (e.g. code without a call)

    def _options(self):
        if self.think_tokens and self.a.model_options.get("thinking_controls", True):
            return {**self.a.model_options, "reasoning_budget_tokens": self.think_tokens}
        return self.a.model_options

    def user(self, text):
        self.messages.append({"role": "user", "content": text})

    def result(self, name, text):
        if not text.startswith("BLOCKED"):
            self.a.wd.last_text = text            # what a repeated call keeps getting, shown when it is blocked
        self.messages.append({"role": "tool", "tool_name": name, "content": text})

    def ask_text(self):
        """A text-only turn: no tools offered, the reply is plain text (used for fenced code files)."""
        return self.ask(text_only=True)

    def ask(self, text_only=False):
        a = self.a
        while True:
            a.check_abort()
            if time.time() - a.run_t0 > a.cfg["caps"]["run_hard_wall_clock_s"]:
                a.log.emit("cap", cap="run_hard_wall_clock_s", limit=a.cfg["caps"]["run_hard_wall_clock_s"])
                raise AgentStop(f"hard wall-clock cap of {a.cfg['caps']['run_hard_wall_clock_s']} s per run reached")
            if a.wd.time_or_turns_exceeded():
                self._limit_reached()
            limit = a.context_tokens()
            if limit and conversation_chars(self.messages) > limit * CONTEXT_SHARE * CHARS_PER_TOKEN:
                saved = shrink(self.messages, limit * CONTEXT_SHARE * CHARS_PER_TOKEN)
                if saved:
                    a.log.emit("info", text=f"conversation near the {limit}-token context: older tool results shortened "
                                            f"({saved} characters)")
            stream = ReplyStream(a.log, step=self.step, model=a.model)    # thinking and text blocks, streamed
            try:
                with a.hold_model():
                    reply = a.backend.chat(a.model, self.messages, [] if text_only else self.tools, think=a.cfg["thinking"].get(self.step, True) and not self.runaway,
                                           options=self._options(), on_delta=stream, stop_event=a.stop_event)
            except Stopped:
                stream.close("stopped")
                a.check_abort()
                raise AgentStop("stopped")
            except BaseException:
                stream.close("error")
                raise
            stream.close("stopped" if reply.get("stopped") else "error" if reply.get("raw_error") else "ok")
            used = (reply.get("prompt_tokens") or 0) + (reply.get("output_tokens") or 0)
            a.tokens += used
            a.tier_tokens[a.tier] = a.tier_tokens.get(a.tier, 0) + used
            a.log.emit("model_reply", step=self.step, thinking=reply.get("thinking"), content=reply.get("content"),
                       tool_calls=reply.get("tool_calls"), prompt_tokens=reply.get("prompt_tokens"),
                       output_tokens=reply.get("output_tokens"), secs=reply.get("secs"), done_reason=reply.get("done_reason"),
                       error=reply.get("raw_error"), run_tokens=a.tokens, tier=a.tier, model=a.model)
            if reply.get("think_budget_hit"):
                a.log.emit("watchdog", name="think_budget", step=self.step, limit_tokens=a.cfg["caps"].get("reply_think_budget_tokens"))
            if reply.get("stopped"):
                a.check_abort()
            over = re.search(r"\((\d+) tokens\) exceeds the available context size \((\d+) tokens", reply.get("raw_error") or "")
            if over:                     # the server refused the request: make it smaller, or say plainly that it cannot be
                need, have = int(over[1]), int(over[2])
                chars = conversation_chars(self.messages)
                saved = shrink(self.messages, int(chars * have / need * 0.7))
                a.log.emit("watchdog", name="context_full", request_tokens=need, context_tokens=have, shortened_chars=saved)
                if not saved:
                    raise AgentStop(f"the conversation ({need} tokens) no longer fits the model's context ({have} tokens) "
                                    f"and cannot be shortened further: ask for a smaller step, or raise ctx in the preset")
                continue
            if a.tier_tokens[a.tier] > a.tier_tokens_cap():
                a.log.emit("cap", cap="tokens_per_run", tier=a.tier, used=a.tier_tokens[a.tier], limit=a.tier_tokens_cap())
                raise AgentStop(f"{a.tier} token cap reached: {a.tier_tokens[a.tier]} > {a.tier_tokens_cap()}")
            if text_only:
                if a.wd.on_reply(reply, expect_call=False) == "runaway":
                    self.messages.append({"role": "assistant", "content": "(reply discarded: too long)"})
                    self.user(f"Your reply was cut off after {reply.get('output_tokens')} tokens / {reply.get('secs')} s. "
                              "Make smaller changes: write a shorter file.")
                    continue
                self.messages.append({"role": "assistant", "content": reply.get("content") or ""})
                return reply
            if not reply.get("tool_calls") and self.implicit:
                call = self.implicit(reply)
                if call:
                    a.log.emit("info", text=f"reply had no tool call but carried {call['name']} content; accepted as {call['name']}")
                    reply["tool_calls"] = [call]
            verdict = a.wd.on_reply(reply)
            self.runaway = verdict == "runaway"           # the retry goes without thinking, which is what ran away
            if verdict == "runaway":
                self.messages.append({"role": "assistant", "content": "(reply discarded: too long)"})
                self.user(f"Your reply was cut off after {reply.get('output_tokens')} tokens / {reply.get('secs')} s. "
                          + ("It was mostly comments working through the data by hand. Do not reason in comments: "
                             "write a short script that tests your current guess about the data and prints what it "
                             "finds, then read its output and adjust. Call a tool now."
                             if reply.get("cut") == "comments" else
                             "You ran out of room while reasoning and made no call. Do not work through bytes, "
                             "numbers or long text by hand: write a short Python script and run it with run_code, "
                             "then answer from its output. Call a tool now."
                             if not reply.get("content") and not reply.get("tool_calls") else
                             "Make smaller changes: one file or one small edit per reply."))
                continue
            if verdict in ("parse_error", "parse_limit"):
                if verdict == "parse_limit":
                    raise AgentStop("the model kept producing unparseable tool calls")
                self.messages.append({"role": "assistant", "content": reply.get("content") or ""})
                self.user("Your tool call could not be parsed. Resend it as one small call with valid JSON arguments. "
                          "Put long code in a fenced block in the message text, not in the arguments.")
                continue
            if verdict in ("empty", "empty_limit"):
                self.messages.append({"role": "assistant", "content": reply.get("content") or ""})
                self.user("If the task is done, call finish with your answer; otherwise call one tool for the next step."
                          if verdict == "empty" else
                          "You must call a tool now. Available: " + ", ".join(t["function"]["name"] for t in self.tools))
                continue
            self.messages.append({"role": "assistant", "content": reply.get("content") or "",
                                  "tool_calls": [{"function": {"name": c["name"], "arguments": c["arguments"]}}
                                                 for c in reply["tool_calls"]]})
            return reply

    def _limit_reached(self):
        a = self.a
        options = ["hint", "restart"] + (["use paid"] if a.offer_paid() else []) + ["stop"]
        d = a.gate.request("cap", "Time or turn limit reached",
                           {"elapsed_s": round(a.wd_elapsed()), "turns": a.wd.turns, "step": self.step,
                            "model": f"{a.tier} ({a.model})",
                            "limits": f"{a.cfg['caps']['task_wall_clock_s']} s / {a.cfg['caps']['task_turns']} turns",
                            "use paid": "switch to the paid model for the rest of this task"},
                           options=options)
        a.log.emit("cap", cap="time_or_turns", choice=d.choice)
        if not d.approved or d.choice == "stop":
            raise AgentStop("stopped at the time/turn limit")
        a.wd.extend()
        if d.choice == "hint":
            self.user("Hint from the user: " + (d.note or "Keep it simple and finish the current step."))
        elif d.choice == "restart":
            raise RestartRequested()
        elif d.choice == "use paid":
            a.switch_tier("paid", "the user picked paid at the time/turn limit", "user")
            self.user("A different model continues this task from here. Read the conversation above and carry on "
                      "with the current step." + (f" Note from the user: {d.note}" if d.note else ""))


def args_json(args, limit=300):
    s = json.dumps(args, default=str)
    return s if len(s) <= limit else s[:limit] + "..."
