"""One model conversation with the reply-level watchdogs enforced in code.

ask() returns the next reply that carries at least one usable tool call. Runaway output, unparseable calls and
empty replies are answered by the harness (they never count as attempts). Time/turn limits ask the user.
When the local model's time budget for the task is used up, the conversation moves to the free tier.
"""
import json
import time


class AgentStop(Exception):
    """The run stops: cap reached, user said stop, hardware watchdog, unrecoverable model output."""


class RestartRequested(Exception):
    pass


def fn(name, desc, props, req=()):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "required": list(req), "properties": props}}}


class Turns:
    def __init__(self, agent, step, messages, tools, implicit=None):
        self.a, self.step, self.messages, self.tools = agent, step, messages, tools
        self.implicit = implicit       # implicit(reply) -> synthesized tool call or None (e.g. code without a call)

    def user(self, text):
        self.messages.append({"role": "user", "content": text})

    def result(self, name, text):
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
            if a.local_time_up() and not a.local_budget_noted:
                budget = a.cfg["routing"]["local_budget_s"]
                if a.auto_escalate(f"the local model used its {budget} s for this task"):
                    self.user("A different model continues this task from here. Read the conversation above and "
                              "carry on with the current step.")
                else:
                    a.local_budget_noted = True
                    a.log.emit("info", text=f"local time budget ({budget} s) used up; free tier not available, "
                                            "staying on local")
            if a.wd.time_or_turns_exceeded():
                self._limit_reached()
            reply = a.backend.chat(a.model, self.messages, [] if text_only else self.tools, think=a.cfg["thinking"].get(self.step, True),
                                   options=a.model_options, on_delta=a.on_delta, stop_event=a.stop_event)
            used = (reply.get("prompt_tokens") or 0) + (reply.get("output_tokens") or 0)
            a.tokens += used
            a.tier_tokens[a.tier] = a.tier_tokens.get(a.tier, 0) + used
            a.log.emit("model_reply", step=self.step, thinking=reply.get("thinking"), content=reply.get("content"),
                       tool_calls=reply.get("tool_calls"), prompt_tokens=reply.get("prompt_tokens"),
                       output_tokens=reply.get("output_tokens"), secs=reply.get("secs"), done_reason=reply.get("done_reason"),
                       error=reply.get("raw_error"), run_tokens=a.tokens, tier=a.tier, model=a.model)
            if reply.get("stopped"):
                a.check_abort()
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
            if verdict == "runaway":
                self.messages.append({"role": "assistant", "content": "(reply discarded: too long)"})
                self.user(f"Your reply was cut off after {reply.get('output_tokens')} tokens / {reply.get('secs')} s. "
                          "Make smaller changes: one file or one small edit per reply.")
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
                self.user("Do the next step now: call one of the tools." if verdict == "empty" else
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
