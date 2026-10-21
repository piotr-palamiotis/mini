"""Configuration. Every cap, budget and watchdog threshold lives here. Stdlib only.

Lookup order: --config PATH, $MINI_CONFIG, <project>/config/mini.toml.
Values missing from the file fall back to DEFAULTS, so an old file keeps working when new keys appear.
"""
import copy
import os
import re
import tomllib

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULTS = {
    "local": {
        "backend": "openai",                 # openai = any OpenAI-compatible server (llama.cpp llama-server, ...)
        "url": "http://127.0.0.1:8080/v1",
        "model": "gemma4-12b",
        "temperature": 0.6,
        "use": [],                           # local backends in order of preference, from [backends.<name>];
                                             # a task runs on the first that answers and has its model.
                                             # Empty: the backend/url/model above.
    },
    "backends": {},                          # name -> {backend, url, model}; written by mini setup
    "llamacpp": {                            # the llama.cpp install mini setup manages
        "dir": "/mnt/disk_a/llama.cpp",      # source checkout and build of the pinned release
        "models_dir": "/mnt/disk_a/models",  # GGUF files: NAME.gguf, or NAME/ with the model and its mmproj-*.gguf
        "port": 8080,
        "preset": "",                        # the active entry of [presets.*]; empty: the values below as they are
        "gpu": "vulkan",                     # which llama.cpp build serves: vulkan | cuda | cpu
        "device": "",                        # e.g. Vulkan0, CUDA0; empty: setup asks (llama-server --list-devices)
        "ctx": 65536,                        # context per model
        "models_max": 1,                     # models in memory at once; others load on demand
        "extra_args": "-fa on -ctk q8_0 -ctv q8_0 --jinja --reasoning-format auto --parallel 1 --cache-ram 1024",
    },
    # Named ways to run llama-server, e.g. [presets.rtx3070]: gpu, device, model, ctx, models_max, extra_args.
    # The preset named in [llamacpp] preset is laid over [llamacpp]; `mini preset use NAME` switches.
    "presets": {},
    "workspace": {                           # the folder mini is launched in is where it works (a git repository)
        "refuse": ["~", "/"],                # too broad to work in: mini does not start a task there
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
        "swap_watch": True,                  # false: never abort a run for swap growth (it is still recorded)
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
        "auto_approve": [],                  # kinds approved in auto mode: confirm, plan, permissions, install, consult, cap, question
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
        "local_budget_s": 900,               # local model time per task; then the task moves to free (0: no limit)
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


def local_candidates(cfg):
    """[(name, local config)] in order of preference: [local] use, each merged over [local]."""
    names = [n for n in cfg["local"].get("use") or [] if n in cfg.get("backends", {})]
    if not names:
        return [("local", dict(cfg["local"]))]
    return [(n, {**cfg["local"], **cfg["backends"][n]}) for n in names]


def llama_settings(cfg):
    """[llamacpp] with the active preset laid over it (gpu, device, model, ctx, models_max, extra_args)."""
    lc = dict(cfg["llamacpp"])
    lc.update((cfg.get("presets") or {}).get(lc.get("preset") or "") or {})
    return lc


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
    cfg["local"] = local_candidates(cfg)[0][1]       # [local] shows the preferred backend's url and model
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


def set_values(path, updates):
    """Change keys in a TOML file in place, keeping its comments and layout. updates: {section: {key: value}}.
    Missing keys are added at the end of their section, missing sections at the end of the file."""
    lines = open(path).read().splitlines() if os.path.exists(path) else []
    for sec, kv in updates.items():
        start = next((i for i, ln in enumerate(lines) if ln.strip() == f"[{sec}]"), None)
        if start is None:
            lines += ["", f"[{sec}]"] + [f"{k} = {_fmt(v)}" for k, v in kv.items()]
            continue
        end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
        for k, v in kv.items():
            pat = re.compile(rf"^(\s*){re.escape(k)}\s*=\s*(.*?)(\s+#.*)?$")
            for i in range(start + 1, end):
                m = pat.match(lines[i])
                if m:
                    lines[i] = f"{m.group(1)}{k} = {_fmt(v)}{m.group(3) or ''}"
                    break
            else:
                last = max((i for i in range(start, end) if lines[i].strip()), default=start)
                lines.insert(last + 1, f"{k} = {_fmt(v)}")
                end += 1
    text = "\n".join(lines) + "\n"
    tomllib.loads(text)                                   # never write a file that does not parse
    with open(path + ".tmp", "w") as f:
        f.write(text)
    os.replace(path + ".tmp", path)
