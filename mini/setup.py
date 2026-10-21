"""First-run setup and health checks.

  mini setup     guided: every check runs; for each problem it explains, proposes the fix and asks first.
                 Steps that need root print the command to run instead of running it.
  mini doctor    the same checks, read-only.

Checks, in order: sandbox engine, sandbox image, the local backends ([local] use: llama.cpp built in [llamacpp]
dir, serving every GGUF in [llamacpp] models_dir in router mode; or another OpenAI-compatible server), start at
boot, cloud gateway (OmniRoute) installed and running, its API key, the free/paid model names. A finished setup is
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
from .harness.config import ENV_FILE, ROOT, llama_settings, local_candidates, secret

LLAMA_REPO = "https://github.com/ggml-org/llama.cpp"
# The llama.cpp release mini is tested with. setup builds exactly this one and checks that the built
# llama-server is it. Tested: gemma4 12B (GGUF from Ollama's store) in router mode, Vulkan on AMD RDNA2
# (RX 6700 XT) and CUDA on an RTX 3070, tool calls through --jinja, thinking through --reasoning-format auto, -fa on with a q8_0 KV cache
# at 64k context. Not every GGUF loads: Ollama's qwen3.5 9B conversion is rejected by this release (rope metadata),
# which is why setup load-tests the model it picks. To move to a newer release, change both values, run
# `mini setup` and the test suite.
LLAMA_TAG = "b11567"
LLAMA_COMMIT = "972b5fc"
BACKENDS = {                                # the local backends setup can set up; order = default preference
    "llamacpp": "llama.cpp (llama-server on the GPU: Vulkan or CUDA, every model in the models folder)",
    "custom": "another OpenAI-compatible server (LM Studio, vLLM, ...)",
}
DEFAULT_URLS = {"llamacpp": "http://127.0.0.1:8080/v1", "custom": "http://127.0.0.1:1234/v1"}
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
def ensure_local(cfg, say=print, wait_s=90):
    """Start the llama-server user service when it is installed but not running, and wait until it answers.
    True when the local endpoint is up. Only starts a unit mini wrote; anything else is left to setup/doctor."""
    cands = local_candidates(cfg)
    if not cands or cands[0][0] != "llamacpp" or cands[0][1].get("backend") == "fake":
        return False
    url = cands[0][1]["url"]
    if http_json(url.rstrip("/") + "/models")[0] == 200:
        return True
    exists, active, _, ours = unit_state("llama-server")
    if not (exists and ours):
        return False
    say("local llama-server is not running; starting it ...")
    r = sh(["systemctl", "--user", "reset-failed", "llama-server"])
    r = sh(["systemctl", "--user", "start", "llama-server"])
    if r.returncode:
        say("could not start llama-server: " + (r.stderr.strip() or "systemctl failed") + "; `mini doctor` has details")
        return False
    end = time.time() + wait_s
    while time.time() < end:
        if http_json(url.rstrip("/") + "/models")[0] == 200:
            say("llama-server is up")
            return True
        time.sleep(1)
    say("llama-server was started but does not answer yet; " + load_error())
    return False


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


# ================================================================ fixes
def _run(cmd, ui, timeout=1800, cwd=None):
    ui.say("$ " + " ".join(cmd))
    r = subprocess.run(cmd, cwd=cwd, timeout=timeout)
    if r.returncode:
        raise SetupError(f"{cmd[0]} exited with {r.returncode}")


class SetupError(Exception):
    pass


def _build_image(cfg, ui):
    from .harness.eventlog import EventBus, LineSink
    from .harness.sandbox import Sandbox
    ui.say("building the sandbox image ...")
    bus = EventBus()
    bus.subscribe(LineSink(ui.say))                   # the build's output, line by line as it arrives
    Sandbox(cfg, bus).build_base()


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


# ---------------------------------------------------------------- llama.cpp (router mode over a models folder)
def llama_dir(cfg):
    return os.path.expanduser(cfg["llamacpp"]["dir"])


GPU_BACKENDS = ("vulkan", "cuda", "cpu")


def llama_gpu(cfg):
    """The llama.cpp backend the active preset (or [llamacpp]) asks for: vulkan | cuda | cpu."""
    return llama_settings(cfg).get("gpu") or "vulkan"


def llama_build_dir(cfg, gpu=None):
    gpu = gpu or llama_gpu(cfg)
    return os.path.join(llama_dir(cfg), "build" if gpu == "vulkan" else f"build-{gpu}")   # "build": the first, Vulkan one


def llama_server(cfg, gpu=None):
    """The llama-server built in [llamacpp] dir for the active backend, or None."""
    p = os.path.join(llama_build_dir(cfg, gpu), "bin", "llama-server")
    return p if os.access(p, os.X_OK) else None


def llama_commit(server):
    r = sh([server, "--version"], timeout=30)
    m = re.search(r"commit ([0-9a-f]{7,})", r.stdout + r.stderr)
    return m.group(1)[:7] if m else None


def llama_supports(server, arch):
    """Does this build know the architecture name? (Read from the built library. A known name does not prove the
    file loads: a converter can write metadata this release rejects, so setup also load-tests the model.)"""
    import glob
    libs = glob.glob(os.path.join(os.path.dirname(os.path.realpath(server)), "libllama.so*"))
    for lib in libs:
        with open(lib, "rb") as f:
            if b"\x00" + arch.encode() + b"\x00" in f.read():
                return True
    return False if libs else None


def models_in(models_dir):
    """{name: {"file", "mmproj"}} the way llama-server's router sees a models folder: NAME.gguf, or a folder
    NAME/ holding the model and optionally its mmproj-*.gguf (vision)."""
    out = {}
    if not os.path.isdir(models_dir):
        return out
    for e in sorted(os.listdir(models_dir)):
        p = os.path.join(models_dir, e)
        if os.path.isfile(p) and e.endswith(".gguf") and not e.startswith("mmproj"):
            out[e[:-5]] = {"file": p, "mmproj": None}
        elif os.path.isdir(p):
            files = sorted(f for f in os.listdir(p) if f.endswith(".gguf"))
            main = [f for f in files if not f.startswith("mmproj")]
            if main:
                proj = [f for f in files if f.startswith("mmproj")]
                out[e] = {"file": os.path.join(p, main[0]), "mmproj": os.path.join(p, proj[0]) if proj else None}
    return out


def served_models(url):
    """{name: status} from the router's /models (status: loaded, unloaded, loading, failed ...)."""
    code, body = http_json(url.rstrip("/") + "/models")
    out = {}
    for m in (body or {}).get("data") or []:
        st = m.get("status")
        out[m.get("id")] = (st.get("value") if isinstance(st, dict) else st) or "available"
    return out


def load_error(unit="llama-server"):
    """llama.cpp's own reason for the last failed model load, from the service log."""
    log = sh(["journalctl", "--user", "-u", unit, "-n", "200", "--no-pager", "-o", "cat"]).stdout
    errs = [ln.split(" E ", 1)[-1].strip() for ln in log.splitlines() if "error loading model" in ln]
    return errs[-1] if errs else "see journalctl --user -u llama-server"


def load_test(url, model, timeout=600):
    """Ask the model for one token. (True, "") or (False, reason)."""
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": "ok"}], "max_tokens": 1}).encode()
    req = urllib.request.Request(url.rstrip("/") + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
        return True, ""
    except urllib.error.HTTPError:
        return False, load_error()
    except OSError as e:
        return False, str(e)


def router_args(cfg, server):
    lc = llama_settings(cfg)
    dev = f" --device {lc['device']} -ngl 999" if lc.get("device") and lc.get("gpu") != "cpu" else ""
    return (f"{server} --models-dir {os.path.expanduser(lc['models_dir'])} --models-max {lc['models_max']} "
            f"--host 127.0.0.1 --port {lc['port']}{dev} -c {lc['ctx']} {lc['extra_args']} --no-webui")


def backend_checks(cfg, name, entry):
    if name != "llamacpp":
        ok = entry.get("backend") == "fake" or has_model(entry)
        return [Check(name, f"{BACKENDS.get(name, name)}: {entry['model']} at {entry['url']}", ok,
                      "" if ok else "not answering with that model",
                      fix=None if ok else lambda ui: setup_custom(cfg, ui), fix_text="set its URL and model name")]
    out, lc = [], llama_settings(cfg)
    server = llama_server(cfg)
    gpu = llama_gpu(cfg)
    if not server:
        return [Check("llama_install", f"llama.cpp {LLAMA_TAG} ({gpu}) in {llama_dir(cfg)}", False,
                      f"no {gpu} build there yet",
                      fix=lambda ui: build_llama(cfg, ui), fix_text=f"build llama.cpp {LLAMA_TAG} with {gpu} (asks where)")]
    have = llama_commit(server)
    right = have == LLAMA_COMMIT
    out.append(Check("llama_version", f"llama.cpp {LLAMA_TAG} ({LLAMA_COMMIT}) in {llama_dir(cfg)}", right,
                     "" if right else f"built: commit {have or 'unknown'}; mini is tested with {LLAMA_TAG}",
                     fix=None if right else lambda ui: build_llama(cfg, ui), fix_text=f"build {LLAMA_TAG}"))
    mdir = os.path.expanduser(lc["models_dir"])
    models = models_in(mdir)
    out.append(Check("models", f"models in {mdir}", bool(models),
                     ", ".join(models) if models else "no GGUF files there yet",
                     fix=None if models else lambda ui: choose_models_dir(cfg, ui),
                     fix_text="choose the folder that holds your GGUF files",
                     manual="" if models else f"put GGUF files in {mdir} (NAME.gguf, or NAME/ with its mmproj-*.gguf); "
                                              f"models from Ollama: scripts/ollama-export {mdir}"))
    if not models:
        return out
    from .harness.gguf import architecture
    unknown = []
    for n, m in models.items():
        try:
            arch = architecture(m["file"])
        except (OSError, ValueError, KeyError):
            arch = "unreadable"
        if arch == "unreadable" or llama_supports(server, arch) is False:
            unknown.append(f"{n} ({arch})")
    out.append(Check("model_arch", "every model's architecture is known to this build", not unknown,
                     "" if not unknown else "cannot load: " + ", ".join(unknown) + "; remove them or get other GGUFs"))
    exists, active, enabled, ours = unit_state("llama-server")
    current = open(unit_path("llama-server")).read() if exists else ""
    unit_ok = active and router_args(cfg, server) in current
    out.append(Check("router", f"llama-server serving {mdir} on port {lc['port']}", unit_ok,
                     "" if unit_ok else ("running with other settings" if active else "not running"),
                     fix=None if unit_ok else lambda ui: setup_router(cfg, ui),
                     fix_text="pick the GPU and run llama-server over the models folder as a user service"))
    if unit_ok:
        out.append(Check("llamacpp_boot", "llama-server starts at boot", enabled, "" if enabled else "running, but not enabled",
                         fix=None if enabled else lambda ui: _enable("llama-server", ui),
                         fix_text="systemctl --user enable llama-server"))
        served = served_models(entry["url"])
        st, why = served.get(entry["model"]), "not in the models folder"
        ok = st == "loaded"
        if st is not None and not ok:                  # the router does not remember failures: load it to know
            ok, why = load_test(entry["url"], entry["model"])
            why = "does not load: " + why
        out.append(Check("local_model", f"local model {entry['model']} loads", ok, "" if ok else why,
                         fix=None if ok else lambda ui: choose_model(cfg, ui), fix_text="pick the model mini uses (load-tested)"))
    return out


def cuda_host_compiler():
    """CUDA's nvcc refuses a host g++ newer than it knows (nvcc 12.0 - 12.3: g++ 12). The g++ to hand it, or None
    when the default one is fine."""
    v = sh(["nvcc", "--version"]).stdout
    m = re.search(r"release (\d+)\.(\d+)", v)
    g = re.search(r"(\d+)\.\d+\.\d+", sh(["g++", "-dumpfullversion"]).stdout)
    if m and g and (int(m[1]), int(m[2])) < (12, 4) and int(g[1]) > 12:
        return "g++-12"
    return None


def build_llama(cfg, ui):
    """Check out the pinned llama.cpp release in [llamacpp] dir and build llama-server for the active GPU backend."""
    gpu = llama_gpu(cfg)
    missing = [b for b in ("git", "cmake", "g++") if not shutil.which(b)]
    flags = []
    if gpu == "vulkan":
        missing += [b for b in ("glslc",) if not shutil.which(b)]
        if not os.path.exists("/usr/include/vulkan/vulkan.h"):
            missing.append("libvulkan-dev")
        flags = ["-DGGML_VULKAN=ON"]
    elif gpu == "cuda":
        if not shutil.which("nvcc"):
            missing.append("nvidia-cuda-toolkit")
        else:
            host = cuda_host_compiler()
            if host and not shutil.which(host):
                missing.append(host)
            flags = ["-DGGML_CUDA=ON", "-DCMAKE_CUDA_ARCHITECTURES=native"] + ([f"-DCMAKE_CUDA_HOST_COMPILER={host}"] if host else [])
    if missing:
        pk = {"g++": "build-essential"}
        raise SetupError("missing build tools; run: sudo apt install " +
                         " ".join(sorted({pk.get(m, m) for m in missing})) + " and then mini setup again")
    src = os.path.abspath(os.path.expanduser(ui.text("Where should llama.cpp live?", llama_dir(cfg))))
    if src != llama_dir(cfg):
        config_mod.set_values(cfg.path, {"llamacpp": {"dir": src}})
        cfg["llamacpp"]["dir"] = src
    if not os.path.isdir(os.path.join(src, ".git")):
        if os.path.exists(src) and os.listdir(src):
            raise SetupError(f"{src} exists and is not a llama.cpp checkout; pick an empty or new folder")
        _run(["git", "clone", "-q", "--depth", "1", "--branch", LLAMA_TAG, LLAMA_REPO, src], ui)
    elif sh(["git", "-C", src, "rev-parse", "--short=7", "HEAD"]).stdout.strip() != LLAMA_COMMIT:
        _run(["git", "-C", src, "fetch", "-q", "--depth", "1", "origin", "tag", LLAMA_TAG], ui)
        _run(["git", "-C", src, "checkout", "-q", "-f", LLAMA_TAG], ui)
    head = sh(["git", "-C", src, "rev-parse", "--short=7", "HEAD"]).stdout.strip()
    if head != LLAMA_COMMIT:
        raise SetupError(f"{src} is at {head}, expected {LLAMA_TAG} ({LLAMA_COMMIT})")
    bdir = os.path.basename(llama_build_dir(cfg))
    _run(["cmake", "-B", bdir, *flags, "-DLLAMA_CURL=OFF", "-DLLAMA_BUILD_TESTS=OFF",
          "-DLLAMA_BUILD_EXAMPLES=OFF", "-DCMAKE_BUILD_TYPE=Release"], ui, cwd=src)
    ui.say(f"building llama-server for {gpu} (10-20 minutes on 4 cores) ...")
    _run(["cmake", "--build", bdir, "--target", "llama-server", f"-j{max(1, (os.cpu_count() or 2) - 1)}"], ui,
         timeout=7200, cwd=src)
    os.makedirs(LOCAL_BIN, exist_ok=True)
    link = os.path.join(LOCAL_BIN, "llama-server")
    if os.path.lexists(link) and os.path.islink(link):
        os.remove(link)
    if not os.path.exists(link):
        os.symlink(os.path.join(src, bdir, "bin", "llama-server"), link)
    if sh(["systemctl", "--user", "is-active", "llama-server"]).stdout.strip() == "active":
        _run(["systemctl", "--user", "restart", "llama-server"], ui)         # serve with the new build


def choose_models_dir(cfg, ui):
    d = os.path.abspath(os.path.expanduser(ui.text("Folder with your GGUF models", cfg["llamacpp"]["models_dir"])))
    found = models_in(d)
    if not found:
        raise SetupError(f"no models in {d} (NAME.gguf, or NAME/ with the model and its mmproj-*.gguf)")
    ui.say("found: " + ", ".join(found))
    config_mod.set_values(cfg.path, {"llamacpp": {"models_dir": d}})
    cfg["llamacpp"]["models_dir"] = d


def gpu_devices(server):
    """[(name, label, MiB)] from llama-server --list-devices."""
    out = []
    for ln in sh([server, "--list-devices"], timeout=60).stdout.splitlines():
        m = re.match(r"\s*(\w+\d+):\s*(.+?)\s*\((\d+) MiB", ln)
        if m:
            out.append((m.group(1), m.group(2), int(m.group(3))))
    return out


def setup_router(cfg, ui):
    """Write and start the llama-server unit. With an active preset it asks nothing: the preset says what to run."""
    server = llama_server(cfg)
    lc = llama_settings(cfg)
    if not lc.get("preset"):
        devs = gpu_devices(server)
        if devs:
            best = max(devs, key=lambda d: d[2])
            cur = next((d for d in devs if d[0] == lc.get("device")), best)
            dev = ui.choice("Which GPU?", [f"{d[0]}: {d[1]} ({d[2]} MiB)" for d in devs],
                            f"{cur[0]}: {cur[1]} ({cur[2]} MiB)").split(":")[0]
        else:
            dev = ""
            ui.say("llama.cpp finds no GPU: models run on the CPU (slow).")
        ctx = int(ui.text("Context size per model (tokens)", str(lc["ctx"])))
        config_mod.set_values(cfg.path, {"llamacpp": {"device": dev, "ctx": ctx}})
        cfg["llamacpp"].update(device=dev, ctx=ctx)
    write_unit("llama-server", f"llama.cpp router over {os.path.expanduser(lc['models_dir'])} (mini local tier)",
               router_args(cfg, server), {}, ui)
    _wait(f"http://127.0.0.1:{lc['port']}/health", ui=ui)
    url = f"http://127.0.0.1:{lc['port']}/v1"
    have = cfg.get("backends", {}).get("llamacpp", {})
    model = lc.get("model") or have.get("model") or cfg["local"]["model"]
    if have.get("url") != url or have.get("model") != model:
        save_backend(cfg, "llamacpp", {"backend": "openai", "url": url, "model": model})


# ---------------------------------------------------------------- presets: named ways to run llama-server
PRESET_KEYS = ("gpu", "device", "model", "ctx", "models_max", "extra_args")


def preset_summary(name, p, active=False):
    return (f"{'*' if active else ' '} {name:<16} {p.get('gpu', 'vulkan'):<7} {p.get('device') or '-':<9} "
            f"{p.get('model') or '(model unchanged)'}  ctx {p.get('ctx', '(default)')}")


def save_preset(cfg, name, values):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name or ""):
        raise SetupError("a preset name is letters, digits, - and _")
    if values.get("gpu", "vulkan") not in GPU_BACKENDS:
        raise SetupError("gpu is one of: " + ", ".join(GPU_BACKENDS))
    values = {k: v for k, v in values.items() if k in PRESET_KEYS and v not in (None, "")}
    config_mod.set_values(cfg.path, {f"presets.{name}": values})
    cfg.setdefault("presets", {}).setdefault(name, {}).update(values)


def use_preset(cfg, name, ui):
    """Make a preset the active one: builds llama.cpp for its backend if that is missing, rewrites the llama-server
    unit and restarts it, and points the local tier at the preset's model."""
    if name not in cfg.get("presets", {}):
        raise SetupError(f"no preset {name!r}; known: " + (", ".join(cfg.get("presets", {})) or "none"))
    old = cfg["llamacpp"].get("preset", "")
    config_mod.set_values(cfg.path, {"llamacpp": {"preset": name}})
    cfg["llamacpp"]["preset"] = name
    try:
        if not llama_server(cfg):
            build_llama(cfg, ui)
        setup_router(cfg, ui)
    except Exception:
        config_mod.set_values(cfg.path, {"llamacpp": {"preset": old}})
        cfg["llamacpp"]["preset"] = old
        raise
    ui.say(f"preset {name} is active: " + preset_summary(name, llama_settings(cfg)).strip())


def preset_cmd(cfg, action, name=None, opts=None, ui=None):
    ui = ui or TerminalUI()
    opts = opts or {}
    active = cfg["llamacpp"].get("preset")
    try:
        if action == "list":
            for n, p in cfg.get("presets", {}).items():
                ui.say(preset_summary(n, p, n == active))
            if not cfg.get("presets"):
                ui.say("(no presets; mini preset add NAME --gpu cuda --device CUDA0 --model NAME --ctx N)")
        elif action == "add":
            if not name:
                raise SetupError("usage: mini preset add NAME [--gpu G] [--device D] [--model M] [--ctx N] [--args ARGS]")
            save_preset(cfg, name, {"gpu": opts.get("gpu") or "vulkan", "device": opts.get("device"),
                                    "model": opts.get("model"), "ctx": opts.get("ctx"), "extra_args": opts.get("args")})
            ui.say("saved: " + preset_summary(name, cfg["presets"][name]).strip() + "   (mini preset use " + name + ")")
        elif action == "use":
            use_preset(cfg, name or "", ui)
        elif action == "show":
            ui.say(json.dumps(llama_settings(cfg) if name in (None, active) else
                              {**cfg["llamacpp"], **cfg.get("presets", {}).get(name, {})}, indent=1))
    except (SetupError, RuntimeError, OSError, subprocess.TimeoutExpired) as e:
        ui.say(f"preset: {e}")
        return 1
    return 0


def choose_model(cfg, ui):
    """Pick the model mini's local tier uses, from the models folder; it is load-tested before it is saved."""
    lc, url = cfg["llamacpp"], cfg["backends"]["llamacpp"]["url"]
    names = list(models_in(os.path.expanduser(lc["models_dir"])))
    if not names:
        raise SetupError("no models in the models folder")
    current = cfg["backends"]["llamacpp"].get("model")
    while names:
        pick = ui.choice("Which model should mini use locally?", names, current if current in names else names[0])
        ui.say(f"loading {pick} to test it (up to a minute) ...")
        ok, why = load_test(url, pick)
        if ok:
            save_backend(cfg, "llamacpp", {"backend": "openai", "url": url, "model": pick})
            ui.say(f"{pick} loads and answers.")
            return
        ui.say(f"{pick} does not load in llama.cpp {LLAMA_TAG}: {why}")
        names.remove(pick)
    raise SetupError("none of the models loads")


def save_backend(cfg, name, entry):
    """Write [backends.<name>] and make sure it is in [local] use."""
    config_mod.set_values(cfg.path, {f"backends.{name}": entry})
    cfg.setdefault("backends", {})[name] = {**cfg.get("backends", {}).get(name, {}), **entry}
    use = list(cfg["local"].get("use") or [])
    if name not in use:
        use.append(name)
        config_mod.set_values(cfg.path, {"local": {"use": use}})
        cfg["local"]["use"] = use


def setup_custom(cfg, ui):
    entry = cfg["backends"].get("custom") or {}
    url = ui.text("Base URL of its OpenAI-compatible API", entry.get("url") or DEFAULT_URLS["custom"])
    code, body = http_json(url.rstrip("/") + "/models")
    ids = model_ids(body)
    model = ui.choice("Which model?", ids, entry.get("model")) if ids else ui.text("Model name", entry.get("model"))
    save_backend(cfg, "custom", {"backend": "openai", "url": url, "model": model})


def choose_backends(cfg, ui):
    """Checklist of local backends, then their order of preference. Written to [local] use and [backends.*]."""
    current = [n for n in cfg["local"].get("use") or [] if n in BACKENDS] or ["llamacpp"]
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
            base = {"backend": "openai", "url": DEFAULT_URLS[n], "model": cfg["local"]["model"]}
            if n == "llamacpp":
                base["url"] = f"http://127.0.0.1:{cfg['llamacpp']['port']}/v1"
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
