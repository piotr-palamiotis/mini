"""Configuration. Every cap, budget and watchdog threshold lives here. Stdlib only.

Lookup order: --config PATH, $MINI_CONFIG, <project>/config/mini.toml.
Values missing from the file fall back to DEFAULTS, so an old file keeps working when new keys appear.
"""
import copy
import os
import tomllib

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULTS = {
    "local": {
        "backend": "ollama",                 # ollama | openai (llama.cpp llama-server, vLLM, LM Studio ...)
        "url": "http://127.0.0.1:11434",     # for openai: http://127.0.0.1:8080/v1
        "model": "gemma4:12b",
        "num_ctx": 65536,                    # changing this needs the owner's approval (VRAM budget)
        "gpu_env": "",                       # e.g. "GGML_VK_VISIBLE_DEVICES=0"; applied to launch_cmd
        "launch_cmd": [],                    # optional argv to start a local server (llama-server ...)
        "temperature": 0.6,
    },
    "thinking": {                            # thinking per step
        "parse": False, "gap": False, "spec": True, "code": True, "fix": True, "execute": True,
    },
    "caps": {
        "attempts_per_tool": 10,             # one attempt = generate/fix code + run tests
        "task_wall_clock_s": 1200,           # per tool creation and per execution phase; then the user decides
        "task_turns": 30,                    # model turns per tool creation and per execution phase
        "run_hard_wall_clock_s": 3600,       # hard stop for a whole run, no question asked
        "reply_max_tokens": 8192,
        "reply_max_s": 300,
        "tokens_per_run": 1500000,           # local tokens per run, prompt + output summed over every model call
        "max_repairs_per_task": 2,           # new versions after a failed real run
    },
    "watchdogs": {
        "identical_call_repeat": 3,
        "passing_test_reruns": 2,
        "file_rewrites": 3,
        "fail_streak_restart": 3,
        "parse_error_retries": 5,
        "empty_reply_nudges": 1,
        "swap_growth_mib": 250,
    },
    "sandbox": {
        "engine": "podman",                  # podman | docker (rootless)
        "image": "mini-sandbox:base",
        "memory": "1g",
        "cpus": "2",
        "pids": 256,
        "timeout_s": 180,
        "service_network": "mini-net",       # network of local services (Mailpit) and the egress forwarder
        "allowed_argv0": ["python", "python3", "pytest", "ls", "cat", "head", "tail", "wc", "file", "pip"],
    },
    "approvals": {
        "confirm_first": True,
        "mode": "ask",                       # ask | auto (headless runs; every auto decision is logged)
        "auto_approve": [],                  # kinds approved in auto mode: confirm, permissions, install, escalate, cap
        "auto_choice": {"confirm": "proceed", "cap": "hint"},
    },
    "fallback": {
        "enabled": True,
        "provider": "claude_cli",
        "model": "sonnet",
        "attempts_per_tool": 3,
        "tokens_per_run": 60000,
        "spend_usd_per_run": 0.50,
        "max_output_tokens": 8000,           # per call; with the prices below this bounds the worst case of one call
        "price_in_per_mtok": 3.75,           # estimate (Sonnet input incl. cache writes), USD per million tokens
        "price_out_per_mtok": 15.0,
        "timeout_s": 300,
    },
    "environment": {
        # Facts about the user's environment, shown to the agent as given. Set by the user, not the agent.
        "facts": [],
    },
    "paths": {
        "state": "state",                    # registry and image cache, relative to the project root
        "runs": "runs",
    },
}


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Config(dict):
    path = None

    def abspath(self, key):
        p = self["paths"][key]
        return p if os.path.isabs(p) else os.path.join(ROOT, p)


def load(path=None):
    path = path or os.environ.get("MINI_CONFIG") or os.path.join(ROOT, "config", "mini.toml")
    data = {}
    if os.path.exists(path):
        with open(path, "rb") as f:
            data = tomllib.load(f)
    cfg = Config(_merge(DEFAULTS, data))
    cfg.path = path
    return cfg


def _fmt(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(v, list):
        return "[" + ", ".join(_fmt(x) for x in v) + "]"
    raise TypeError(f"cannot write {type(v).__name__} to TOML")


def dumps(cfg, prefix=""):
    lines, subs = [], []
    for section, values in cfg.items():
        name = prefix + section
        lines.append(f"[{name}]")
        for k, v in values.items():
            if isinstance(v, dict):
                subs.append((name + "." + k, v))
            else:
                lines.append(f"{k} = {_fmt(v)}")
        lines.append("")
        for sub, vals in subs:
            lines.append(dumps({sub: vals}))
        subs = []
    return "\n".join(lines)


def save(cfg, path=None):
    path = path or cfg.path
    with open(path, "w") as f:
        f.write(dumps({k: v for k, v in cfg.items()}))
