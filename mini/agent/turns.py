"""One model conversation with the reply-level watchdogs enforced in code.

ask() returns the next reply that carries at least one usable tool call. Runaway output, unparseable calls and
empty replies are answered by the harness (they never count as attempts). Time/turn limits ask the user.
"""
import json
import time


class AgentStop(Exception):
    """The run stops: cap reached, user said stop, hardware watchdog, unrecoverable model output."""


class RestartRequested(Exception):
    pass


class EscalateRequested(Exception):
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

    def ask(self):
        a = self.a
        while True:
            a.check_abort()
            if time.time() - a.run_t0 > a.cfg["caps"]["run_hard_wall_clock_s"]:
                a.log.emit("cap", cap="run_hard_wall_clock_s", limit=a.cfg["caps"]["run_hard_wall_clock_s"])
                raise AgentStop(f"hard wall-clock cap of {a.cfg['caps']['run_hard_wall_clock_s']} s per run reached")
            if a.wd.time_or_turns_exceeded():
                self._limit_reached()
            reply = a.backend.chat(a.model, self.messages, self.tools, think=a.cfg["thinking"].get(self.step, True),
                                   options=a.model_options, on_delta=a.on_delta, stop_event=a.stop_event)
            a.tokens += (reply.get("prompt_tokens") or 0) + (reply.get("output_tokens") or 0)
            a.log.emit("model_reply", step=self.step, thinking=reply.get("thinking"), content=reply.get("content"),
                       tool_calls=reply.get("tool_calls"), prompt_tokens=reply.get("prompt_tokens"),
                       output_tokens=reply.get("output_tokens"), secs=reply.get("secs"), done_reason=reply.get("done_reason"),
                       error=reply.get("raw_error"), run_tokens=a.tokens)
            if reply.get("stopped"):
                a.check_abort()
            if a.tokens > a.cfg["caps"]["tokens_per_run"]:
                a.log.emit("cap", cap="tokens_per_run", used=a.tokens, limit=a.cfg["caps"]["tokens_per_run"])
                raise AgentStop(f"token cap reached: {a.tokens} > {a.cfg['caps']['tokens_per_run']}")
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
        options = ["hint", "restart", "fallback", "stop"] if a.fallback.enabled else ["hint", "restart", "stop"]
        d = a.gate.request("cap", "Time or turn limit reached",
                           {"elapsed_s": round(a.wd_elapsed()), "turns": a.wd.turns, "step": self.step,
                            "limits": f"{a.cfg['caps']['task_wall_clock_s']} s / {a.cfg['caps']['task_turns']} turns"},
                           options=options)
        a.log.emit("cap", cap="time_or_turns", choice=d.choice)
        if not d.approved or d.choice == "stop":
            raise AgentStop("stopped at the time/turn limit")
        a.wd.extend()
        if d.choice == "hint":
            self.user("Hint from the user: " + (d.note or "Keep it simple and finish the current step."))
        elif d.choice == "restart":
            raise RestartRequested()
        elif d.choice == "fallback":
            raise EscalateRequested()


def args_json(args, limit=300):
    s = json.dumps(args, default=str)
    return s if len(s) <= limit else s[:limit] + "..."
