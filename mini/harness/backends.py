"""Model backends. Each turns a chat request into one normalized reply. Stdlib only.
Reused from the earlier mini (mini/backends.py); changes: caps come from config, Endpoint is local.

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

MAX_OUTPUT_TOKENS = 8192     # hard cap per reply; protects against runaway generation inside a tool call
REPLY_TIMEOUT_S = 300        # no data for this long ends the reply (tool calls are not streamed while being written)


class Endpoint:
    def __init__(self, type, url, options=None, api_key_env="", max_tokens=MAX_OUTPUT_TOKENS, timeout_s=REPLY_TIMEOUT_S):
        self.type, self.url, self.options, self.api_key_env = type, url.rstrip("/"), dict(options or {}), api_key_env
        self.max_tokens, self.timeout_s = max_tokens, timeout_s

    def api_key(self):
        import os
        return os.environ.get(self.api_key_env, "") if self.api_key_env else ""


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


class OllamaBackend(Backend):
    """Ollama native /api/chat with streaming."""

    def load(self, model, options=None):
        body = {"model": model, "keep_alive": "30m", "options": {"num_gpu": 999, **(options or {})}}
        req = urllib.request.Request(self.ep.url + "/api/generate", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as r:
            r.read()

    def list_models(self):
        with urllib.request.urlopen(self.ep.url + "/api/tags", timeout=10) as r:
            return [m["name"] for m in json.loads(r.read()).get("models", [])]

    def chat(self, model, messages, tools, think=False, options=None, on_delta=None, stop_event=None):
        opts = {"num_gpu": 999, "num_predict": self.ep.max_tokens, **self.ep.options, **(options or {})}
        body = {"model": model, "messages": messages, "tools": tools, "stream": True,
                "think": think, "keep_alive": "30m", "options": opts}
        req = urllib.request.Request(self.ep.url + "/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        out = {"content": "", "thinking": "", "tool_calls": [], "done_reason": None,
               "prompt_tokens": 0, "output_tokens": 0, "secs": 0.0, "raw_error": None, "stopped": False}
        t0 = time.time()
        try:
            r = urllib.request.urlopen(req, timeout=self.ep.timeout_s)
        except (socket.timeout, TimeoutError):
            out.update(done_reason="timeout", secs=round(time.time() - t0, 2))
            return out
        try:
            for line in r:
                if stop_event is not None and stop_event.is_set():
                    out["stopped"] = True
                    break
                if not line.strip():
                    continue
                ev = json.loads(line)
                if ev.get("error"):
                    out["raw_error"] = ev["error"]
                    break
                msg = ev.get("message") or {}
                if msg.get("thinking"):
                    out["thinking"] += msg["thinking"]
                    on_delta and on_delta("thinking", msg["thinking"])
                if msg.get("content"):
                    out["content"] += msg["content"]
                    on_delta and on_delta("content", msg["content"])
                for c in msg.get("tool_calls") or []:
                    fn = c.get("function", {})
                    args = fn.get("arguments") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except ValueError:
                            args = {"_unparsed": args}
                    out["tool_calls"].append({"name": fn.get("name", ""), "arguments": args})
                if ev.get("done"):
                    out["done_reason"] = ev.get("done_reason")
                    out["prompt_tokens"] = ev.get("prompt_eval_count") or 0
                    out["output_tokens"] = ev.get("eval_count") or 0
                if time.time() - t0 > self.ep.timeout_s:
                    out["done_reason"] = "timeout"
                    break
        except (socket.timeout, TimeoutError):
            out["done_reason"] = "timeout"
        finally:
            r.close()      # closing the connection makes Ollama stop generating
        out["secs"] = round(time.time() - t0, 2)
        return out


class OpenAIBackend(Backend):
    """OpenAI-compatible /v1/chat/completions with streaming: llama.cpp llama-server, vLLM, LM Studio, hosted APIs.
    Thinking arrives as delta.reasoning_content (llama-server --reasoning-format deepseek/auto).
    endpoint.options may hold: chat_template_kwargs (e.g. {"enable_thinking": true}), extra request fields."""

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
        """Our messages (Ollama style) -> OpenAI style with tool call ids."""
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

    def chat(self, model, messages, tools, think=False, options=None, on_delta=None, stop_event=None):
        opts = {**self.ep.options, **(options or {})}
        body = {"model": model, "messages": self._convert(messages), "tools": tools, "stream": True,
                "max_tokens": opts.pop("max_tokens", self.ep.max_tokens)}
        kw = dict(opts.pop("chat_template_kwargs", {}))
        kw.setdefault("enable_thinking", bool(think))
        body["chat_template_kwargs"] = kw
        if not think:
            body["reasoning_budget"] = 0
        body.update({k: v for k, v in opts.items() if k in ("temperature", "top_p", "top_k", "min_p", "repeat_penalty",
                                                              "presence_penalty", "seed", "reasoning_budget")})
        req = urllib.request.Request(self.ep.url + "/chat/completions", data=json.dumps(body).encode(), headers=self._headers())
        out = {"content": "", "thinking": "", "tool_calls": [], "done_reason": None,
               "prompt_tokens": 0, "output_tokens": 0, "secs": 0.0, "raw_error": None, "stopped": False}
        calls = {}
        t0 = time.time()
        try:
            r = urllib.request.urlopen(req, timeout=self.ep.timeout_s)
        except urllib.error.HTTPError as e:
            out["raw_error"] = f"HTTP {e.code}: {e.read()[:300]!r}"
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
                    if d.get("reasoning_content"):
                        out["thinking"] += d["reasoning_content"]
                        on_delta and on_delta("thinking", d["reasoning_content"])
                    if d.get("content"):
                        out["content"] += d["content"]
                        on_delta and on_delta("content", d["content"])
                    for tc in d.get("tool_calls") or []:
                        c = calls.setdefault(tc.get("index", 0), {"name": "", "args": ""})
                        fn = tc.get("function") or {}
                        c["name"] += fn.get("name") or ""
                        c["args"] += fn.get("arguments") or ""
                    if ch.get("finish_reason"):
                        out["done_reason"] = {"tool_calls": "stop"}.get(ch["finish_reason"], ch["finish_reason"])
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


BACKENDS = {"ollama": OllamaBackend, "openai": OpenAIBackend, "fake": FakeBackend}


def make_backend(endpoint):
    if endpoint.type not in BACKENDS:
        raise ValueError(f"unknown endpoint type {endpoint.type!r}; known: {', '.join(BACKENDS)}")
    return BACKENDS[endpoint.type](endpoint)
