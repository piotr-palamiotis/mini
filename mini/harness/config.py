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
        "parse": False, "gap": False, "spec": True, "code": True, "fix": True, "execute": True, "write": False,
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
        "service_network": "mini-net",       # network of local services the user runs (e.g. a mail server) and the egress forwarder
        "fault_injection": {},               # tests only: {"host:port": "reset"} makes egress reset those connections
        "allowed_argv0": ["python", "python3", "pytest", "ls", "cat", "head", "tail", "wc", "file", "pip"],
    },
    "approvals": {
        "confirm_first": True,
        "mode": "ask",                       # ask | auto (headless runs; every auto decision is logged)
        "auto_approve": [],                  # kinds approved in auto mode: confirm, permissions, install, consult, cap
        "auto_choice": {"confirm": "proceed", "cap": "hint"},
    },
    # Cloud tiers through an OpenAI-compatible gateway (OmniRoute). The model names are gateway combos, so the
    # actual models are chosen in the gateway's dashboard, not here. The key comes from api_key_env or ~/.config/mini/env.
    "free": {
        "enabled": True,
        "backend": "openai",
        "url": "http://127.0.0.1:20128/v1",
        "model": "mini-free",
        "api_key_env": "OMNIROUTE_API_KEY",
        "temperature": 0.4,
        "thinking_controls": False,          # do not send llama.cpp-only thinking fields to the gateway
        "tokens_per_run": 3000000,
    },
    "paid": {
        "enabled": True,
        "backend": "openai",
        "url": "http://127.0.0.1:20128/v1",
        "model": "mini-paid",
        "api_key_env": "OMNIROUTE_API_KEY",
        "temperature": 0.4,
        "thinking_controls": False,
        "tokens_per_run": 2000000,
    },
    "routing": {
        "start": "local",                    # tier a task starts on: local | free | paid
        "local_budget_s": 900,               # local model time per task; then the task moves to free
        "auto_escalate": True,               # local -> free without asking; paid is only ever the user's choice
    },
    "consult": {
        "enabled": True,                     # when the attempt budget runs out, ask the user for advice first
        "extra_attempts": 3,                 # attempts granted after the user gives advice
        "max_rounds": 1,                     # advice rounds per tool
    },
    "tools": {
        "auto_accept": False,                # true: a tool a task builds goes straight to main (no review step)
        "bin_dir": "~/.local/bin",           # accepted tools get a command here
    },
    "lessons": {
        "enabled": True,                     # agent-written lessons from failures, kept in <state>/lessons.jsonl
        "max_in_prompt": 3,
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


TIERS = ("local", "free", "paid")
ENV_FILE = os.path.expanduser("~/.config/mini/env")


def secret(name):
    """A credential from the environment, else from ~/.config/mini/env (KEY=VALUE lines). Never from the repo."""
    if not name:
        return ""
    if os.environ.get(name):
        return os.environ[name]
    try:
        with open(ENV_FILE) as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                if k.strip() == name and not line.lstrip().startswith("#"):
                    return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


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
