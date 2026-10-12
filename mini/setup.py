"""First-run setup and health checks.

  mini setup     guided: every check runs; for each problem it explains, proposes the fix and asks first.
                 Steps that need root print the command to run instead of running it.
  mini doctor    the same checks, read-only.

Checks, in order: sandbox engine, sandbox image, local model server (llama.cpp or Ollama), start at boot,
cloud gateway (OmniRoute) installed and running, its API key, the free/paid model names. A finished setup is
recorded in <state>/setup.json; `mini` / `mini ui` offer the setup when it is missing.
"""
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

from .harness import config as config_mod
from .harness.backends import has_model
from .harness.config import ENV_FILE, ROOT, local_candidates, secret

LLAMA_REPO = "https://github.com/ggml-org/llama.cpp"
# The llama.cpp release mini is tested with. setup builds exactly this one and checks that the installed
# llama-server is it. Tested: gemma4 (12B) and qwen35 (9B) GGUFs, Vulkan on AMD RDNA2 (RX 6700 XT), tool calls
# through --jinja, thinking through --reasoning-format auto, -fa on with a q8_0 KV cache at 64k context.
# To move to a newer release, change both values, run `mini setup` and the test suite.
LLAMA_TAG = "b11567"
LLAMA_COMMIT = "972b5fc"
BACKENDS = {                                # the local backends setup can set up; order = default preference
    "llamacpp": "llama.cpp (llama-server, Vulkan GPU)",
    "ollama": "Ollama",
    "custom": "another OpenAI-compatible server (LM Studio, vLLM, ...)",
}
DEFAULT_URLS = {"llamacpp": "http://127.0.0.1:8080/v1", "ollama": "http://127.0.0.1:11434",
                "custom": "http://127.0.0.1:1234/v1"}
UNIT_DIR = os.path.expanduser("~/.config/systemd/user")
UNIT_MARK = "# written by mini setup"
LOCAL_BIN = os.path.expanduser("~/.local/bin")


# ================================================================ small helpers
def sh(cmd, timeout=60, **kw):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(cmd, 127, "", str(e))


def http_json(url, key="", timeout=3):
    """(status, parsed body or None). status 0 = not reachable."""
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + key} if key else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, None
    except (OSError, ValueError):
        return 0, None


def model_ids(body):
    if not isinstance(body, dict):
        return []
    return [m.get("id") or m.get("name") for m in body.get("data") or body.get("models") or [] if isinstance(m, dict)]


def find_bin(name):
    """On PATH, else in ~/.local/bin or an nvm Node install (services do not see the login shell's PATH)."""
    import glob
    p = shutil.which(name)
    if p:
        return p
    for pattern in (os.path.join(LOCAL_BIN, name), os.path.expanduser(f"~/.nvm/versions/node/*/bin/{name}")):
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[-1]
    return None


def unit_path(name):
    return os.path.join(UNIT_DIR, name + ".service")


def unit_state(name):
    """(exists, active, enabled, ours)."""
    p = unit_path(name)
    exists = os.path.exists(p)
    ours = exists and UNIT_MARK in open(p).read()
    active = sh(["systemctl", "--user", "is-active", name]).stdout.strip() == "active"
    enabled = sh(["systemctl", "--user", "is-enabled", name]).stdout.strip() == "enabled"
    return exists, active, enabled, ours


def gateway_url(cfg):
    for t in ("free", "paid"):
        if cfg[t].get("enabled", True):
            return cfg[t]["url"]
    return None


def gateway_root(url):
    return re.sub(r"/v1/?$", "", url.rstrip("/"))


# ================================================================ tier status (UI line and doctor)
def tier_status(cfg):
    """{tier: (ok, short text)} with quick probes."""
    out = {}
    cands = local_candidates(cfg)
    up = [(n, e) for n, e in cands if e["backend"] == "fake" or has_model(e)]
    if up:
        n, e = up[0]
        rest = [x for x, _ in cands if x != n]
        out["local"] = (True, f"{e['model']} via {n}" + (f" (also: {', '.join(rest)})" if rest else "")
                        + ("" if n == cands[0][0] else f"; {cands[0][0]} is down"))
    else:
        out["local"] = (False, "no local backend answers with its model: " + ", ".join(n for n, _ in cands))
    for t in ("free", "paid"):
        tc = cfg[t]
        if not tc.get("enabled", True):
            out[t] = (None, "off")
            continue
        key = secret(tc.get("api_key_env", ""))
        code, body = http_json(tc["url"].rstrip("/") + "/models", key)
        if code == 0:
            out[t] = (False, "gateway not running")
        elif code in (401, 403):
            out[t] = (False, "API key missing or wrong")
        elif tc["model"] not in model_ids(body):
            out[t] = (False, f"no model or combo named {tc['model']}")
        else:
            out[t] = (True, tc["model"])
    return out


def status_line(cfg):
    mark = {True: "ok", False: "!!", None: "--"}
    return "   ".join(f"{t}: {mark[ok]} {text}" for t, (ok, text) in tier_status(cfg).items())


# ================================================================ checks
class Check:
    def __init__(self, key, title, ok, detail="", fix=None, fix_text="", manual=""):
        self.key, self.title, self.ok, self.detail = key, title, ok, detail
        self.fix, self.fix_text, self.manual = fix, fix_text, manual


def checks(cfg, ui):
    """The checks in order. A fix is a callable(ui) that changes the system; manual is a command for the user."""
    out = []
    engine = cfg["sandbox"]["engine"]
    has_engine = shutil.which(engine) is not None
    out.append(Check("engine", f"sandbox engine ({engine})", has_engine,
                     "" if has_engine else f"{engine} is not installed; tools run in its containers.",
                     manual="" if has_engine else "sudo apt install podman uidmap"))
    if has_engine:
        from .harness.sandbox import Sandbox
        sb = Sandbox(cfg)
        ok = sb.image_exists(cfg["sandbox"]["image"])
        out.append(Check("image", f"sandbox image {cfg['sandbox']['image']}", ok,
                         "" if ok else "not built yet (python:3.12-slim + pytest, a few minutes, about 150 MB)",
                         fix=None if ok else lambda ui: _build_image(cfg, ui), fix_text="build it now"))

    st = tier_status(cfg)
    for name, entry in local_candidates(cfg):
        out += backend_checks(cfg, name, entry)
    linger = "Linger=yes" in sh(["loginctl", "show-user", getpass.getuser(), "-p", "Linger"]).stdout
    out.append(Check("linger", "user services start at boot without a login", linger,
                     "" if linger else "systemd stops your services when you log out",
                     fix=None if linger else lambda ui: _run(["loginctl", "enable-linger", getpass.getuser()], ui),
                     fix_text="loginctl enable-linger", manual="" if linger else f"sudo loginctl enable-linger {getpass.getuser()}"))

    url = gateway_url(cfg)
    if url:
        root = gateway_root(url)
        code, _ = http_json(url.rstrip("/") + "/models")
        running = code != 0
        installed = find_bin("omniroute") is not None
        if not running:
            if not installed:
                npm = find_bin("npm")
                out.append(Check("gateway", "cloud gateway (OmniRoute)", False,
                                 "not installed. It routes the free and paid tiers to cloud models.",
                                 fix=(lambda ui: _install_omniroute(npm, ui)) if npm else None,
                                 fix_text="npm install -g omniroute",
                                 manual="" if npm else "install Node.js 20+ (https://nodejs.org or nvm), then run mini setup again"))
            else:
                out.append(Check("gateway", "cloud gateway (OmniRoute)", False, f"installed but not running at {root}",
                                 fix=lambda ui: setup_omniroute(cfg, ui), fix_text="run it as a user service (127.0.0.1 only, API key required)"))
        else:
            exists, active, enabled, ours = unit_state("omniroute")
            if exists:
                out.append(Check("gateway_boot", "OmniRoute starts at boot", enabled, "" if enabled else "running, but not enabled",
                                 fix=None if enabled else lambda ui: _enable("omniroute", ui),
                                 fix_text="systemctl --user enable omniroute"))
            tiers = [t for t in ("free", "paid") if cfg[t].get("enabled", True)]
            if any("key" in st[t][1] for t in tiers):
                out.append(Check("gateway_key", "OmniRoute API key", False, "missing or not accepted by the gateway",
                                 fix=lambda ui: set_gateway_key(cfg, ui), fix_text="save the OmniRoute API key"))
            else:
                for t in tiers:
                    ok, text = st[t]
                    out.append(Check(t, f"{t} tier ({cfg[t]['model']})", ok, "" if ok else text,
                                     fix=None if ok else (lambda ui, t=t: setup_combo(cfg, t, ui)),
                                     fix_text=f"create {cfg[t]['model']} in OmniRoute, or turn the {t} tier off"))
    return out


def llama_commit(server):
    r = sh([server, "--version"], timeout=30)
    m = re.search(r"commit ([0-9a-f]{7,})", r.stdout + r.stderr)
    return m.group(1)[:7] if m else None


def llama_server_bin():
    """The standalone llama-server (Ollama ships a private copy that cannot run on its own)."""
    p = find_bin("llama-server")
    return p if p and "ollama" not in os.path.realpath(p) else None


def llama_supports(server, arch):
    """Does this llama.cpp build know the model architecture? Read from the built library, not guessed."""
    import glob
    libs = glob.glob(os.path.join(os.path.dirname(os.path.realpath(server)), "libllama.so*"))
    for lib in libs:
        with open(lib, "rb") as f:
            if b"\x00" + arch.encode() + b"\x00" in f.read():
                return True
    return False if libs else None


def model_file(entry):
    """The GGUF a llamacpp backend serves: [backends.llamacpp] gguf = "ollama:NAME" or a path."""
    g = entry.get("gguf") or ""
    if g.startswith("ollama:"):
        from .harness.ollama_store import gguf
        return gguf(g[len("ollama:"):])
    return g or None


def backend_checks(cfg, name, entry):
    out = []
    ok = entry.get("backend") == "fake" or has_model(entry)
    label = BACKENDS.get(name, name)
    if name == "llamacpp" or (name == "local" and entry["backend"] == "openai" and "8080" in entry["url"]):
        server = llama_server_bin()
        if not server:
            return [Check("llama_install", "llama.cpp installed", False, f"not installed; mini uses release {LLAMA_TAG}",
                          fix=lambda ui: build_llama(ui), fix_text=f"build llama.cpp {LLAMA_TAG} with Vulkan")]
        have = llama_commit(server)
        right = have == LLAMA_COMMIT
        out.append(Check("llama_version", f"llama.cpp {LLAMA_TAG} ({LLAMA_COMMIT})", right,
                         "" if right else f"installed: commit {have or 'unknown'}; mini is tested with {LLAMA_TAG}",
                         fix=None if right else lambda ui: build_llama(ui), fix_text=f"build {LLAMA_TAG}"))
        try:
            from .harness.gguf import architecture
            path = model_file(entry)
            arch = architecture(path) if path else None
        except (OSError, ValueError, KeyError):
            arch = None
        if arch:
            sup = llama_supports(server, arch)
            out.append(Check("llama_arch", f"llama.cpp can load {entry['model']} (architecture {arch})", sup is not False,
                             "" if sup is not False else f"this build does not know {arch}; build {LLAMA_TAG} or pick another model",
                             fix=None if sup is not False else lambda ui: build_llama(ui), fix_text=f"build {LLAMA_TAG}"))
        exists, active, enabled, _ = unit_state("llama-server")
        out.append(Check("llamacpp", f"llama-server serving {entry['model']}", ok,
                         "" if ok else f"not answering at {entry['url']}",
                         fix=None if ok else lambda ui: setup_llama(cfg, ui), fix_text="pick a model and GPU, start llama-server"))
        if ok and exists:
            out.append(Check("llamacpp_boot", "llama-server starts at boot", enabled, "" if enabled else "running, but not enabled",
                             fix=None if enabled else lambda ui: _enable("llama-server", ui),
                             fix_text="systemctl --user enable llama-server"))
    elif entry["backend"] == "ollama":
        if not shutil.which("ollama"):
            return [Check("ollama_install", "Ollama installed", False, "not installed",
                          manual="curl -fsSL https://ollama.com/install.sh | sh")]
        code, body = http_json(entry["url"].rstrip("/") + "/api/tags")
        if code != 200:
            return [Check("ollama_run", "Ollama running", False, f"not answering at {entry['url']}",
                          manual="sudo systemctl enable --now ollama")]
        out.append(Check("ollama", f"Ollama has {entry['model']}", ok, "" if ok else "model not pulled",
                         fix=None if ok else lambda ui: setup_ollama(cfg, ui), fix_text="pick or pull a model"))
    else:
        out.append(Check(name, f"{label}: {entry['model']} at {entry['url']}", ok, "" if ok else "not answering with that model",
                         fix=None if ok else lambda ui: setup_custom(cfg, ui), fix_text="set its URL and model name"))
    return out


# ================================================================ fixes
def _run(cmd, ui, timeout=1800, cwd=None):
    ui.say("$ " + " ".join(cmd))
    r = subprocess.run(cmd, cwd=cwd, timeout=timeout)
    if r.returncode:
        raise SetupError(f"{cmd[0]} exited with {r.returncode}")


class SetupError(Exception):
    pass


def _build_image(cfg, ui):
    from .harness.sandbox import Sandbox
    ui.say("building the sandbox image ...")
    Sandbox(cfg).build_base()


def _ollama_pull(model, ui):
    _run(["ollama", "pull", model], ui)


def _enable(name, ui):
    _run(["systemctl", "--user", "enable", name], ui)


def write_unit(name, description, exec_start, env, ui):
    """Write ~/.config/systemd/user/<name>.service. A unit mini did not write is replaced only after asking."""
    p = unit_path(name)
    if os.path.exists(p) and UNIT_MARK not in open(p).read():
        ui.say(f"{p} exists and was not written by mini:\n" + open(p).read())
        if not ui.yes(f"Replace it? (a copy is kept as {p}.bak)", False):
            raise SetupError(f"kept your {name} unit; adjust it by hand or move it away")
        shutil.copy(p, p + ".bak")
    os.makedirs(UNIT_DIR, exist_ok=True)
    lines = ["[Unit]", f"Description={description}", UNIT_MARK, "", "[Service]"]
    lines += [f"Environment={k}={v}" for k, v in env.items()]
    lines += [f"ExecStart={exec_start}", "Restart=on-failure", "RestartSec=5", "", "[Install]", "WantedBy=default.target", ""]
    with open(p, "w") as f:
        f.write("\n".join(lines))
    _run(["systemctl", "--user", "daemon-reload"], ui)
    _run(["systemctl", "--user", "enable", "--now", name], ui)
    _run(["systemctl", "--user", "restart", name], ui)


def _wait(url, key="", secs=240, ui=None):
    t0 = time.time()
    while time.time() - t0 < secs:
        code, _ = http_json(url, key)
        if code and code < 500:
            return code
        time.sleep(2)
    raise SetupError(f"{url} did not answer within {secs} s (journalctl --user -u <service> shows why)")


# ---------------------------------------------------------------- llama.cpp
def build_llama(ui):
    """Clone the pinned llama.cpp release and build llama-server with Vulkan. Returns the binary path."""
    missing = [b for b in ("git", "cmake", "g++", "glslc") if not shutil.which(b)]
    if not os.path.exists("/usr/include/vulkan/vulkan.h"):
        missing.append("libvulkan-dev")
    if missing:
        pk = {"g++": "build-essential", "glslc": "glslc"}
        raise SetupError("missing build tools; run: sudo apt install " +
                         " ".join(sorted({pk.get(m, m) for m in missing})) + " and then mini setup again")
    server = llama_server_bin()
    default = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(server)))) if server else \
        os.path.join(os.path.dirname(ROOT), "llama.cpp")
    src = os.path.expanduser(ui.text("Where should llama.cpp be built?", default))
    if not os.path.isdir(os.path.join(src, ".git")):
        _run(["git", "clone", "-q", "--depth", "1", "--branch", LLAMA_TAG, LLAMA_REPO, src], ui)
    elif sh(["git", "-C", src, "rev-parse", "--short=7", "HEAD"]).stdout.strip() != LLAMA_COMMIT:
        _run(["git", "-C", src, "fetch", "-q", "--depth", "1", "origin", "tag", LLAMA_TAG], ui)
        _run(["git", "-C", src, "checkout", "-q", "-f", LLAMA_TAG], ui)
    head = sh(["git", "-C", src, "rev-parse", "--short=7", "HEAD"]).stdout.strip()
    if head != LLAMA_COMMIT:
        raise SetupError(f"{src} is at {head}, expected {LLAMA_TAG} ({LLAMA_COMMIT})")
    _run(["cmake", "-B", "build", "-DGGML_VULKAN=ON", "-DLLAMA_CURL=OFF", "-DLLAMA_BUILD_TESTS=OFF",
          "-DLLAMA_BUILD_EXAMPLES=OFF", "-DCMAKE_BUILD_TYPE=Release"], ui, cwd=src)
    ui.say("building llama-server (10-20 minutes on 4 cores) ...")
    _run(["cmake", "--build", "build", "--target", "llama-server", f"-j{max(1, (os.cpu_count() or 2) - 1)}"], ui,
         timeout=7200, cwd=src)
    os.makedirs(LOCAL_BIN, exist_ok=True)
    link = os.path.join(LOCAL_BIN, "llama-server")
    if os.path.lexists(link):
        os.remove(link)
    os.symlink(os.path.join(src, "build", "bin", "llama-server"), link)
    if sh(["systemctl", "--user", "is-active", "llama-server"]).stdout.strip() == "active":
        _run(["systemctl", "--user", "restart", "llama-server"], ui)         # serve with the new build
    return link


def gpu_devices(server):
    """[(name, label, MiB)] from llama-server --list-devices."""
    out = []
    for ln in sh([server, "--list-devices"], timeout=60).stdout.splitlines():
        m = re.match(r"\s*(\w+\d+):\s*(.+?)\s*\((\d+) MiB", ln)
        if m:
            out.append((m.group(1), m.group(2), int(m.group(3))))
    return out


def setup_llama(cfg, ui):
    server = llama_server_bin()
    if not server:
        if not ui.yes(f"llama.cpp (llama-server) is not installed. Build {LLAMA_TAG} now with Vulkan GPU support?", True):
            raise SetupError("skipped")
        server = build_llama(ui)
    from .harness.gguf import architecture
    from .harness.ollama_store import gguf, list_models
    models = list_models()
    def label(n, size):
        try:
            arch = architecture(gguf(n))
            sup = llama_supports(server, arch)
        except (OSError, ValueError, KeyError):
            arch, sup = "?", None
        return f"{n}  ({size / 1e9:.1f} GB, {arch}" + (", not supported by this build" if sup is False else "") + ")"
    choices = [label(n, size) for n, size in models] + ["a GGUF file on disk"]
    current = cfg.get("backends", {}).get("llamacpp", {}).get("model") or cfg["local"]["model"]
    pick = ui.choice("Which model should llama.cpp run?", choices,
                     next((c for c in choices if c.startswith(current + " ") and "not supported" not in c), choices[0]))
    if pick == "a GGUF file on disk":
        path = os.path.abspath(os.path.expanduser(ui.text("Path to the .gguf file")))
        if not os.path.isfile(path):
            raise SetupError(f"no such file: {path}")
        alias = ui.text("Name for it in mini", os.path.splitext(os.path.basename(path))[0])
        model_arg, source = f'"{path}"', path
    else:
        alias = pick.split()[0]
        path, source = gguf(alias), "ollama:" + alias
        model_arg = f'"$({os.path.join(ROOT, "scripts", "ollama-gguf")} {alias})"'
    arch = architecture(path)
    if llama_supports(server, arch) is False:
        raise SetupError(f"{alias} is a {arch} model; this llama.cpp build cannot load it (build {LLAMA_TAG})")
    devs = gpu_devices(server)
    dev_args = ""
    if devs:
        best = max(devs, key=lambda d: d[2])
        dev = ui.choice("Which GPU?", [f"{d[0]}: {d[1]} ({d[2]} MiB)" for d in devs],
                        f"{best[0]}: {best[1]} ({best[2]} MiB)").split(":")[0]
        dev_args = f" --device {dev} -ngl 999"
    else:
        ui.say("no GPU found by llama.cpp: the model runs on the CPU (slow).")
    ctx = int(ui.text("Context size (tokens)", str(cfg["local"].get("num_ctx") or 65536)))
    port = 8080
    cmd = (f"exec {server} -m {model_arg} --alias {alias} --host 127.0.0.1 --port {port}{dev_args} -c {ctx} "
           "-fa on -ctk q8_0 -ctv q8_0 --jinja --reasoning-format auto --parallel 1 --no-webui")
    write_unit("llama-server", f"llama.cpp server (mini local tier): {alias}", f"/bin/sh -c '{cmd}'", {}, ui)
    ui.say("waiting for the model to load ...")
    _wait(f"http://127.0.0.1:{port}/health", ui=ui)
    save_backend(cfg, "llamacpp", {"backend": "openai", "url": f"http://127.0.0.1:{port}/v1", "model": alias,
                                   "num_ctx": ctx, "gguf": source})


def save_backend(cfg, name, entry):
    """Write [backends.<name>] and make sure it is in [local] use."""
    config_mod.set_values(cfg.path, {f"backends.{name}": entry})
    cfg.setdefault("backends", {})[name] = {**cfg.get("backends", {}).get(name, {}), **entry}
    use = list(cfg["local"].get("use") or [])
    if name not in use:
        use.append(name)
        config_mod.set_values(cfg.path, {"local": {"use": use}})
        cfg["local"]["use"] = use


def setup_ollama(cfg, ui):
    entry = cfg["backends"]["ollama"]
    code, body = http_json(entry["url"].rstrip("/") + "/api/tags")
    have = [m.get("name") for m in (body or {}).get("models") or []]
    pick = ui.choice("Which Ollama model?", have + ["pull another model"], entry["model"] if entry["model"] in have else None)
    if pick == "pull another model":
        pick = ui.text("Model to pull (name:tag)", entry["model"])
        _ollama_pull(pick, ui)
    save_backend(cfg, "ollama", {"backend": "ollama", "url": entry["url"], "model": pick})


def setup_custom(cfg, ui):
    entry = cfg["backends"].get("custom") or {}
    url = ui.text("Base URL of its OpenAI-compatible API", entry.get("url") or DEFAULT_URLS["custom"])
    code, body = http_json(url.rstrip("/") + "/models")
    ids = model_ids(body)
    model = ui.choice("Which model?", ids, entry.get("model")) if ids else ui.text("Model name", entry.get("model"))
    save_backend(cfg, "custom", {"backend": "openai", "url": url, "model": model})


def choose_backends(cfg, ui):
    """Checklist of local backends, then their order of preference. Written to [local] use and [backends.*]."""
    current = [n for n in cfg["local"].get("use") or [] if n in BACKENDS]
    if not current:                                    # an older config: derive from [local]
        current = ["ollama"] if cfg["local"]["backend"] == "ollama" else ["llamacpp"]
    picked = ui.checklist("Which local model backends should mini use? (several = a fallback when one is down)",
                          list(BACKENDS.values()), [BACKENDS[n] for n in current])
    names = [n for n, lbl in BACKENDS.items() if lbl in picked]
    if not names:
        ui.say("keeping: " + ", ".join(current))
        names = current
    if len(names) > 1:
        first = ui.choice("Which one first?", [BACKENDS[n] for n in names],
                          BACKENDS[current[0]] if current[0] in names else BACKENDS[names[0]])
        names.sort(key=lambda n: BACKENDS[n] != first)
    for n in names:
        if n not in cfg.get("backends", {}):
            base = {"backend": "ollama" if n == "ollama" else "openai", "url": DEFAULT_URLS[n],
                    "model": cfg["local"]["model"]}
            if cfg["local"]["backend"] == base["backend"] and n != "custom":
                base.update(url=cfg["local"]["url"])
            if n == "llamacpp":
                base["gguf"] = "ollama:" + base["model"]
            config_mod.set_values(cfg.path, {f"backends.{n}": base})
            cfg.setdefault("backends", {})[n] = base
    config_mod.set_values(cfg.path, {"local": {"use": names}})
    cfg["local"]["use"] = names


# ---------------------------------------------------------------- OmniRoute
def _install_omniroute(npm, ui):
    _run([npm, "install", "-g", "omniroute"], ui, timeout=1800)


def setup_omniroute(cfg, ui):
    bin_ = find_bin("omniroute")
    if not bin_:
        raise SetupError("omniroute is not installed")
    node_dir = os.path.dirname(os.path.realpath(find_bin("node") or bin_))
    port = re.search(r":(\d+)", gateway_url(cfg) or ":20128").group(1)
    write_unit("omniroute", "OmniRoute AI gateway (mini free/paid tiers)",
               f"{bin_} serve --port {port} --no-open --no-tray",
               {"PATH": f"{node_dir}:{os.path.dirname(bin_)}:/usr/local/bin:/usr/bin:/bin",
                "OMNIROUTE_SERVER_HOST": "127.0.0.1", "REQUIRE_API_KEY": "true"}, ui)
    ui.say("starting OmniRoute ...")
    _wait(gateway_url(cfg).rstrip("/") + "/models", ui=ui)


def set_gateway_key(cfg, ui):
    url = gateway_url(cfg)
    ui.say(f"Open the OmniRoute dashboard: {gateway_root(url)}\n"
           "  1. Providers: connect the services you want (free ones for the free tier, your subscription for paid).\n"
           "  2. API Keys: create a key for mini and copy it.")
    key = ui.secret("Paste the OmniRoute API key")
    if not key:
        raise SetupError("no key given")
    code, _ = http_json(url.rstrip("/") + "/models", key)
    if code != 200:
        raise SetupError(f"the gateway answered {code} with this key")
    name = cfg["free"].get("api_key_env") or cfg["paid"].get("api_key_env") or "OMNIROUTE_API_KEY"
    save_env(name, key)
    os.environ[name] = key
    ui.say(f"saved to {ENV_FILE} (readable only by you)")


def save_env(name, value):
    os.makedirs(os.path.dirname(ENV_FILE), mode=0o700, exist_ok=True)
    lines = []
    if os.path.exists(ENV_FILE):
        lines = [ln for ln in open(ENV_FILE).read().splitlines() if not ln.split("=", 1)[0].strip() == name]
    lines.append(f"{name}={value}")
    fd = os.open(ENV_FILE + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(ENV_FILE + ".tmp", ENV_FILE)


def setup_combo(cfg, tier, ui):
    tc = cfg[tier]
    want = {"free": "free models (several, so one can take over when another hits its limit)",
            "paid": "your paid subscription models"}[tier]
    ui.say(f"mini asks the gateway for the model {tc['model']!r} on the {tier} tier.\n"
           f"In the OmniRoute dashboard ({gateway_root(tc['url'])}) open Combos, create one named {tc['model']} "
           f"with {want}, strategy priority.")
    choice = ui.choice(f"Then:", ["check again", f"use another model or combo name for {tier}", f"turn the {tier} tier off"],
                       "check again")
    if choice.startswith("use another"):
        name = ui.text("Model or combo name")
        config_mod.set_values(cfg.path, {tier: {"model": name}})
        tc["model"] = name
    elif choice.startswith("turn"):
        config_mod.set_values(cfg.path, {tier: {"enabled": False}})
        tc["enabled"] = False


# ================================================================ terminal UI and the wizard
class TerminalUI:
    def say(self, text):
        print(text, flush=True)

    def yes(self, prompt, default=True):
        a = input(f"{prompt} [{'Y/n' if default else 'y/N'}] ").strip().lower()
        return default if not a else a.startswith("y")

    def text(self, prompt, default=None):
        a = input(f"{prompt}" + (f" [{default}]" if default else "") + ": ").strip()
        return a or (default or "")

    def secret(self, prompt):
        return getpass.getpass(prompt + ": ").strip()

    def checklist(self, prompt, options, checked=()):
        sel = [o in checked for o in options]
        while True:
            print(prompt)
            for i, o in enumerate(options, 1):
                print(f"  {i}) [{'x' if sel[i - 1] else ' '}] {o}")
            a = input("numbers to toggle (e.g. 1 3), Enter when done: ").strip()
            if not a:
                return [o for o, on in zip(options, sel) if on]
            for t in a.replace(",", " ").split():
                if t.isdigit() and 1 <= int(t) <= len(options):
                    sel[int(t) - 1] = not sel[int(t) - 1]

    def choice(self, prompt, options, default=None):
        print(prompt)
        for i, o in enumerate(options, 1):
            print(f"  {i}) {o}" + ("   (default)" if o == default else ""))
        while True:
            a = input("> ").strip()
            if not a and default:
                return default
            if a.isdigit() and 1 <= int(a) <= len(options):
                return options[int(a) - 1]


def marker(cfg):
    return os.path.join(cfg.abspath("state"), "setup.json")


def needs_setup(cfg):
    return not os.path.exists(marker(cfg))


def doctor(cfg, ui=None):
    ui = ui or TerminalUI()
    bad = 0
    for c in checks(cfg, ui):
        ui.say(f"[{'ok' if c.ok else '!!'}] {c.title}" + (f": {c.detail}" if c.detail else ""))
        if not c.ok:
            bad += 1
            if c.manual:
                ui.say(f"     run: {c.manual}")
            elif c.fix:
                ui.say(f"     mini setup can: {c.fix_text}")
    return 1 if bad else 0


def wizard(cfg, ui=None):
    ui = ui or TerminalUI()
    ui.say("mini setup: checks what mini needs and helps you set up what is missing. Nothing changes without a yes.\n")
    if any(cfg[t].get("enabled", True) for t in ("free", "paid")) and needs_setup(cfg):
        if not ui.yes("Use cloud models (through OmniRoute) when the local model gets stuck? "
                      "Free ones are used automatically, paid ones only when you pick them.", True):
            config_mod.set_values(cfg.path, {"free": {"enabled": False}, "paid": {"enabled": False}})
            cfg["free"]["enabled"] = cfg["paid"]["enabled"] = False
    choose_backends(cfg, ui)
    skipped, done = set(), False
    while not done:
        done = True
        for c in checks(cfg, ui):
            if c.ok or c.key in skipped:
                continue
            ui.say(f"\n[!!] {c.title}" + (f": {c.detail}" if c.detail else ""))
            if c.manual and not c.fix:
                ui.say(f"     This needs you: {c.manual}")
                skipped.add(c.key)
                continue
            if not ui.yes(f"     Fix: {c.fix_text}. Do it now?", True):
                skipped.add(c.key)
                continue
            try:
                c.fix(ui)
            except (SetupError, RuntimeError, OSError, subprocess.TimeoutExpired, ValueError) as e:
                ui.say(f"     not fixed: {e}")
                if c.manual:
                    ui.say(f"     or run: {c.manual}")
                skipped.add(c.key)
            done = False                      # something changed: check everything again
            break
    ui.say("\n" + "-" * 60)
    rc = doctor(cfg, ui)
    os.makedirs(os.path.dirname(marker(cfg)), exist_ok=True)
    with open(marker(cfg), "w") as f:
        json.dump({"done_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "open": sorted(skipped)}, f)
    ui.say("\nSetup finished." + (" Some items are still open; `mini doctor` lists them, `mini setup` continues."
                                  if rc else " Everything is ready."))
    return rc


def first_run_offer(cfg):
    """`mini` / `mini ui` on a machine that never finished setup: offer it (interactive terminals only)."""
    if not needs_setup(cfg) or not sys.stdin.isatty():
        return
    print("mini has not been set up on this machine yet.")
    if TerminalUI().yes("Run the setup now?", True):
        wizard(cfg)
    else:
        os.makedirs(os.path.dirname(marker(cfg)), exist_ok=True)
        with open(marker(cfg), "w") as f:
            json.dump({"done_at": None, "skipped": True}, f)
        print("Skipped. `mini setup` runs it any time; `mini doctor` shows what is missing.")
