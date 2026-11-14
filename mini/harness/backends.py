"""Model backends. Each turns a chat request into one normalized reply. Stdlib only.
Every real backend speaks the OpenAI-compatible API: llama.cpp's llama-server locally, the gateway for the cloud.

Normalized reply:
  {"content": str, "thinking": str, "tool_calls": [{"name": str, "arguments": dict}],
   "done_reason": str, "prompt_tokens": int, "output_tokens": int, "secs": float, "raw_error": str|None}

Streaming: chat() takes on_delta(kind, text) with kind "thinking" or "content", so a UI can show progress.
Add a backend by subclassing Backend and registering it in BACKENDS.
"""
import json
import socket
import time
import urllib.error
import urllib.request

RESTART_WAIT_S = 180         # how long a refused connection or a 503 (model loading) is retried
RESTART_POLL_S = 5

MAX_OUTPUT_TOKENS = 8192     # hard cap per reply; protects against runaway generation inside a tool call
REPLY_TIMEOUT_S = 300        # no data for this long ends the reply (tool calls are not streamed while being written)
COMMENT_SHARE = 0.5           # a reply this much comment lines (past comment_cut_chars) is reasoning written as code


def comment_heavy(text, min_chars):
    """True when text (a streamed tool-call argument or reply) is long and mostly comment lines: the model works
    through data by hand in comments instead of running code. JSON-escaped newlines count as line breaks."""
    if not min_chars or len(text) < min_chars:
        return False
    lines = [l.strip() for l in text.replace("\\n", "\n").split("\n")]
    lines = [l for l in lines if l]
    return bool(lines) and sum(l.startswith("#") for l in lines) / len(lines) > COMMENT_SHARE


THINK_BUDGET_MESSAGE = " Thinking budget reached. Stop reasoning and act now on what you have: make the tool call or give the answer."


class Endpoint:
    def __init__(self, type, url, options=None, api_key_env="", max_tokens=MAX_OUTPUT_TOKENS, timeout_s=REPLY_TIMEOUT_S, think_max_chars=0,
                 think_budget_tokens=0, max_tokens_tools=0, comment_cut_chars=0):
        self.type, self.url, self.options, self.api_key_env = type, url.rstrip("/"), dict(options or {}), api_key_env
        self.max_tokens, self.timeout_s = max_tokens, timeout_s
        self.think_max_chars = think_max_chars        # 0 = no cap; thinking past this with no answer or call ends the reply
        self.think_budget_tokens = think_budget_tokens  # 0 = none; llama-server closes thinking here and the model answers
        self.max_tokens_tools = max_tokens_tools        # 0 = max_tokens; cap for replies that may call tools (thinking included)
        self.comment_cut_chars = comment_cut_chars      # 0 = off; a call or reply past this, mostly comments, is cut

    def api_key(self):
        from .config import secret
        return secret(self.api_key_env)


class Backend:
    def __init__(self, endpoint):
        self.ep = endpoint

    def chat(self, model, messages, tools, think=False, options=None, on_delta=None, stop_event=None):
        raise NotImplementedError

    def list_models(self):
        return []

    def load(self, model, options=None):
        """Load the model before a run, so loading is not counted against the run's watchdogs."""
        return None


class OpenAIBackend(Backend):
    """OpenAI-compatible /v1/chat/completions with streaming: llama.cpp llama-server, vLLM, LM Studio, hosted APIs.
    Thinking arrives as delta.reasoning_content (llama-server --reasoning-format deepseek/auto).
    endpoint.options may hold: chat_template_kwargs (e.g. {"enable_thinking": true}), extra request fields."""

    def load(self, model, options=None):
        """A one-token request, so a server that loads models on demand (llama-server router mode) has the model
        in memory before the run's clocks start. Errors show up again on the first real request."""
        body = {"model": model, "messages": [{"role": "user", "content": "ok"}], "max_tokens": 1, "stream": False}
        req = urllib.request.Request(self.ep.url + "/chat/completions", data=json.dumps(body).encode(),
                                     headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                r.read()
        except urllib.error.HTTPError:
            pass

    def list_models(self):
        req = urllib.request.Request(self.ep.url + "/models", headers=self._headers())
        with urllib.request.urlopen(req, timeout=10) as r:
            return [m["id"] for m in json.loads(r.read()).get("data", [])]

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.ep.api_key():
            h["Authorization"] = "Bearer " + self.ep.api_key()
        return h

    @staticmethod
    def _convert(messages):
        """Our messages (tool calls without ids) -> OpenAI style with tool call ids."""
        out, pending, n = [], [], 0
        for m in messages:
            if m["role"] == "assistant" and m.get("tool_calls"):
                calls = []
                for c in m["tool_calls"]:
                    n += 1
                    cid = f"call_{n}"
                    pending.append(cid)
                    calls.append({"id": cid, "type": "function", "function": {
                        "name": c["function"]["name"], "arguments": json.dumps(c["function"].get("arguments") or {})}})
                out.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": calls})
            elif m["role"] == "tool":
                cid = pending.pop(0) if pending else f"call_{n}"
                out.append({"role": "tool", "tool_call_id": cid, "content": m.get("content", "")})
            else:
                out.append({"role": m["role"], "content": m.get("content", "")})
        return out

    @staticmethod
    def _pause(stop_event):
        if stop_event is not None:
            stop_event.wait(RESTART_POLL_S)
        else:
            time.sleep(RESTART_POLL_S)

    def chat(self, model, messages, tools, think=False, options=None, on_delta=None, stop_event=None):
        opts = {**self.ep.options, **(options or {})}
        body = {"model": model, "messages": self._convert(messages), "stream": True,
                "stream_options": {"include_usage": True}, "max_tokens": opts.pop("max_tokens", self.ep.max_tokens)}
        if tools and self.ep.max_tokens_tools:
            body["max_tokens"] = min(body["max_tokens"], self.ep.max_tokens_tools)
        if tools:
            body["tools"] = tools
        if opts.pop("thinking_controls", True):          # llama-server fields; a gateway may reject them
            kw = dict(opts.pop("chat_template_kwargs", {}))
            kw.setdefault("enable_thinking", bool(think))
            body["chat_template_kwargs"] = kw
            if not think:
                body["reasoning_budget_tokens"] = 0
            elif self.ep.think_budget_tokens:      # the server ends thinking at the budget, so the reply still has an answer
                body["reasoning_budget_tokens"] = self.ep.think_budget_tokens
                body["reasoning_budget_message"] = THINK_BUDGET_MESSAGE
        body.update({k: v for k, v in opts.items() if k in ("temperature", "top_p", "top_k", "min_p", "repeat_penalty",
                                                              "presence_penalty", "seed", "reasoning_budget_tokens")})
        req = urllib.request.Request(self.ep.url + "/chat/completions", data=json.dumps(body).encode(), headers=self._headers())
        out = {"content": "", "thinking": "", "tool_calls": [], "done_reason": None,
               "prompt_tokens": 0, "output_tokens": 0, "secs": 0.0, "raw_error": None, "stopped": False}
        calls = {}
        t0 = time.time()
        while True:          # a server that restarts (OOM kill, model load) is waited for, not read as a bad reply
            try:
                r = urllib.request.urlopen(req, timeout=self.ep.timeout_s)
                break
            except urllib.error.HTTPError as e:
                if e.code == 503 and time.time() - t0 < RESTART_WAIT_S:
                    self._pause(stop_event)
                    continue
                out["raw_error"] = f"HTTP {e.code}: {e.read()[:300]!r}"
                return out
            except urllib.error.URLError as e:
                if isinstance(e.reason, (ConnectionRefusedError, ConnectionResetError)) and \
                        time.time() - t0 < RESTART_WAIT_S and not (stop_event is not None and stop_event.is_set()):
                    self._pause(stop_event)
                    continue
                out["raw_error"] = f"unreachable: {e.reason}"
                return out
            except (socket.timeout, TimeoutError):
                out.update(done_reason="timeout", secs=round(time.time() - t0, 2))
                return out
        try:
            for raw in r:
                if stop_event is not None and stop_event.is_set():
                    out["stopped"] = True
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                ev = json.loads(data)
                if ev.get("error"):
                    out["raw_error"] = str(ev["error"])[:300]
                    break
                if ev.get("usage"):
                    out["prompt_tokens"] = ev["usage"].get("prompt_tokens") or 0
                    out["output_tokens"] = ev["usage"].get("completion_tokens") or 0
                for ch in ev.get("choices") or []:
                    d = ch.get("delta") or {}
                    rc = d.get("reasoning_content") or d.get("reasoning")
                    if isinstance(rc, str) and rc:
                        out["thinking"] += rc
                        on_delta and on_delta("thinking", rc)
                        if (self.ep.think_max_chars and len(out["thinking"]) > self.ep.think_max_chars
                                and not out["content"] and not calls):
                            out["done_reason"] = "length"          # a reasoning loop: cut it, the watchdog retries without thinking
                            break
                    if d.get("content"):
                        out["content"] += d["content"]
                        on_delta and on_delta("content", d["content"])
                    for tc in d.get("tool_calls") or []:
                        c = calls.setdefault(tc.get("index", 0), {"name": "", "args": ""})
                        fn = tc.get("function") or {}
                        c["name"] += fn.get("name") or ""
                        c["args"] += fn.get("arguments") or ""
                    if tools and (comment_heavy(out["content"], self.ep.comment_cut_chars)
                                  or any(comment_heavy(c["args"], self.ep.comment_cut_chars) for c in calls.values())):
                        out["done_reason"], out["cut"] = "length", "comments"   # reasoning in comments: stop paying for it
                        break
                    if ch.get("finish_reason"):
                        out["done_reason"] = {"tool_calls": "stop"}.get(ch["finish_reason"], ch["finish_reason"])
                if out["done_reason"] == "length":
                    break
                if time.time() - t0 > self.ep.timeout_s:
                    out["done_reason"] = "timeout"
                    break
        except (socket.timeout, TimeoutError):
            out["done_reason"] = "timeout"
        finally:
            r.close()
        for i in sorted(calls):
            try:
                args = json.loads(calls[i]["args"]) if calls[i]["args"] else {}
            except ValueError:
                args = {"_unparsed": calls[i]["args"]}
            out["tool_calls"].append({"name": calls[i]["name"], "arguments": args})
        if not out["output_tokens"]:
            out["output_tokens"] = (len(out["thinking"]) + len(out["content"])) // 4
        out["think_budget_hit"] = THINK_BUDGET_MESSAGE.strip() in out["thinking"]
        out["secs"] = round(time.time() - t0, 2)
        return out


class FakeBackend(Backend):
    """Scripted backend for UI tests without a GPU. Replies come from endpoint.options["script"]."""

    def __init__(self, endpoint):
        super().__init__(endpoint)
        self.script = list(endpoint.options.get("script", []))

    def list_models(self):
        return ["fake"]

    def chat(self, model, messages, tools, think=False, options=None, on_delta=None, stop_event=None):
        step = self.script.pop(0) if self.script else {"content": "Nothing more to do.", "tool_calls": [{"name": "finish", "arguments": {"summary": "done"}}]}
        for word in (step.get("thinking", "") + " ").split(" "):
            on_delta and word and on_delta("thinking", word + " ")
            time.sleep(0.01)
        for word in step.get("content", "").split(" "):
            on_delta and on_delta("content", word + " ")
            time.sleep(0.01)
        return {"content": step.get("content", ""), "thinking": step.get("thinking", ""),
                "tool_calls": step.get("tool_calls", []), "done_reason": "stop",
                "prompt_tokens": 100, "output_tokens": 20, "secs": 0.1, "raw_error": None, "stopped": False}


BACKENDS = {"openai": OpenAIBackend, "fake": FakeBackend}


def has_model(entry, timeout=3):
    """Does the server in entry ({backend, url, model, api_key_env}) answer and list entry's model?"""
    import urllib.request
    from .config import secret
    try:
        key = secret(entry.get("api_key_env", ""))
        req = urllib.request.Request(entry["url"].rstrip("/") + "/models",
                                     headers={"Authorization": "Bearer " + key} if key else {})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read())
        names = [m.get("id") or m.get("name") for m in body.get("data") or body.get("models") or []]
        return entry["model"] in names
    except (OSError, ValueError):
        return False


def make_backend(endpoint):
    if endpoint.type not in BACKENDS:
        raise ValueError(f"unknown endpoint type {endpoint.type!r}; known: {', '.join(BACKENDS)}")
    return BACKENDS[endpoint.type](endpoint)
