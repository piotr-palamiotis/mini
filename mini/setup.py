"""First-run setup and health checks.

  mini setup     guided: every check runs; for each problem it explains, proposes the fix and asks first.
                 Steps that need root print the command to run instead of running it.
  mini doctor    the same checks, read-only.

mini runs on one model server it builds and runs itself: llama.cpp's llama-server, from the pinned release, in
router mode over a folder of GGUF files. Nothing else is used, so the setup is self-contained.

Checks, in order: sandbox engine, sandbox image, llama.cpp built in [llamacpp] dir, models in [llamacpp]
models_dir, llama-server running as a user service, the model mini uses (the active preset's) loads, start at boot.
A finished setup is recorded in <state>/setup.json; `mini` / `mini ui` offer the setup when it is missing.
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
from .harness.config import ROOT, llama_settings, resolve_local

LLAMA_REPO = "https://github.com/ggml-org/llama.cpp"
# The llama.cpp release mini is tested with. setup builds exactly this one and checks that the built
# llama-server is it. Tested: gemma4 12B (GGUF from Ollama's store) in router mode, Vulkan on AMD RDNA2
# (RX 6700 XT) and CUDA on an RTX 3070, tool calls through --jinja, thinking through --reasoning-format auto, -fa on with a q8_0 KV cache
# at 64k context. Not every GGUF loads: Ollama's qwen3.5 9B conversion is rejected by this release (rope metadata),
# which is why setup load-tests the model it picks. To move to a newer release, change both values, run
# `mini setup` and the test suite.
LLAMA_TAG = "b11567"
LLAMA_COMMIT = "972b5fc"
UNIT_DIR = os.path.expanduser("~/.config/systemd/user")
UNIT_MARK = "# written by mini setup"
LOCAL_BIN = os.path.expanduser("~/.local/bin")


# ================================================================ small helpers
def sh(cmd, timeout=60, **kw):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(cmd, 127, "", str(e))


def http_json(url, timeout=3):
    """(status, parsed body or None). status 0 = not reachable."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, None
    except (OSError, ValueError):
        return 0, None


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


# ================================================================ status (UI line and doctor)
def ensure_local(cfg, say=print, wait_s=90):
    """Start the llama-server user service when it is installed but not running, and wait until it answers.
    True when the local endpoint is up. Only starts a unit mini wrote; anything else is left to setup/doctor."""
    if cfg["local"].get("backend") == "fake":
        return False
    url = cfg["local"]["url"]
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


def local_status(cfg):
    """(ok, short text) for the model mini uses, with a quick probe."""
    lc = cfg["local"]
    preset = cfg["llamacpp"].get("preset")
    name = f"{lc['model']}" + (f" (preset {preset})" if preset else "")
    if lc.get("backend") == "fake" or has_model(lc):
        return True, name
    code, _ = http_json(lc["url"].rstrip("/") + "/models")
    return False, name + (": llama-server is not running" if code == 0 else ": not in the models folder")


def status_line(cfg):
    ok, text = local_status(cfg)
    return f"model: {'ok' if ok else '!!'} {text}"


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

    out += llama_checks(cfg)
    linger = "Linger=yes" in sh(["loginctl", "show-user", getpass.getuser(), "-p", "Linger"]).stdout
    out.append(Check("linger", "user services start at boot without a login", linger,
                     "" if linger else "systemd stops your services when you log out",
                     fix=None if linger else lambda ui: _run(["loginctl", "enable-linger", getpass.getuser()], ui),
                     fix_text="loginctl enable-linger", manual="" if linger else f"sudo loginctl enable-linger {getpass.getuser()}"))

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


def _wait(url, secs=240, ui=None):
    t0 = time.time()
    while time.time() - t0 < secs:
        code, _ = http_json(url)
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


def llama_checks(cfg):
    entry = cfg["local"]
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
            if shutil.which("glslc") and os.path.exists("/usr/include/vulkan/vulkan.h"):
                flags.append("-DGGML_VULKAN=ON")   # one build for NVIDIA (CUDAn) and other GPUs (Vulkann)
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


INTEGRATED = re.compile(r"(?i)\bintel\b(?!.*\barc\b)|llvmpipe|^amd radeon(\(tm\))? graphics\b")


def gpu_cards(devices):
    """The physical cards behind llama.cpp's devices: one build with CUDA and Vulkan lists an NVIDIA card twice
    (CUDA0 and Vulkan1). [{label, mib, cuda, vulkan, integrated}], discrete cards first, then by VRAM."""
    cards, vulkan = [], [d for d in devices if d[0].startswith("Vulkan")]
    for name, label, mib in devices:
        if name.startswith("CUDA"):
            twin = next((v for v in vulkan if label.lower() in v[1].lower()), None)
            if twin:
                vulkan.remove(twin)
            cards.append({"label": label, "mib": mib, "cuda": name, "vulkan": twin[0] if twin else None})
        elif not name.startswith("Vulkan"):
            cards.append({"label": label, "mib": mib, "cuda": None, "vulkan": None, "other": name})
    cards += [{"label": label, "mib": mib, "cuda": None, "vulkan": name} for name, label, mib in vulkan]
    for c in cards:
        c["integrated"] = bool(INTEGRATED.search(c["label"]))
    return sorted(cards, key=lambda c: (c["integrated"], -c["mib"]))


def card_label(c):
    names = "/".join(x for x in (c.get("cuda"), c.get("vulkan"), c.get("other")) if x)
    return f"{c['label']}, {c['mib'] / 1024:.1f} GB ({names})" + ("  integrated: shares system RAM" if c["integrated"] else "")


def gpu_plan(cards):
    """How llama-server runs on these cards: (backend, device, tensor_split, note). CUDA when every card has it
    (faster on NVIDIA), else Vulkan, which every GPU here speaks. Several cards split the model by layers, in
    proportion to their VRAM when they differ."""
    if not cards:
        return "cpu", "", "", "no GPU: the model runs on the CPU (slow)"
    if all(c.get("cuda") for c in cards):
        backend, devs = "cuda", [c["cuda"] for c in cards]
    elif all(c.get("vulkan") for c in cards):
        backend, devs = "vulkan", [c["vulkan"] for c in cards]
    else:
        raise SetupError("these cards have no backend in common in this llama.cpp build: " +
                         ", ".join(card_label(c) for c in cards))
    note = ""
    if backend == "vulkan" and any(c.get("cuda") for c in cards):
        note = "Vulkan for every card: the others have no CUDA (NVIDIA cards are a little slower on Vulkan than on CUDA)"
    sizes = [c["mib"] for c in cards]
    split = ",".join(str(round(m / min(sizes), 2)).rstrip("0").rstrip(".") for m in sizes) if len(set(sizes)) > 1 else ""
    return backend, ",".join(devs), split, note


def model_size(m):
    return sum(os.path.getsize(p) for p in (m["file"], m.get("mmproj")) if p and os.path.exists(p)) + (
        sum(os.path.getsize(os.path.join(os.path.dirname(m["file"]), f)) for f in os.listdir(os.path.dirname(m["file"]))
            if SPLIT_PART.match(f) and os.path.join(os.path.dirname(m["file"]), f) != m["file"])
        if SPLIT_PART.match(os.path.basename(m["file"])) else 0)


SPLIT_PART = re.compile(r".+-\d{5}-of-\d{5}\.gguf$")


def listing_server(cfg):
    """The llama-server whose devices a preset can use: the CUDA build (it has Vulkan too) when there is one."""
    for gpu in ("cuda", "vulkan", "cpu"):
        server = llama_server(cfg, gpu)
        if server:
            return gpu, server
    raise SetupError("llama.cpp is not built yet; mini setup builds it")


def new_preset(cfg, ui, name=None, model=None, cards=None, ctx=None, use=None):
    """The preset maker: which model (from the models folder) on which GPUs. Asks what is not given, saves the
    preset and offers to use it."""
    models = models_in(os.path.expanduser(cfg["llamacpp"]["models_dir"]))
    if not models:
        raise SetupError("no models in the models folder yet: mini models search WORDS, then mini models download")
    asked = model is None or cards is None or ctx is None or name is None   # interactive: confirm a bad fit
    if model is None:
        labels = {f"{n}  ({model_size(m) / 1073741824:.1f} GB{', vision' if m['mmproj'] else ''})": n for n, m in models.items()}
        model = labels[ui.choice("Which model should the preset load?", list(labels), next(iter(labels)))]
    if model not in models:
        raise SetupError(f"no model {model!r} in the models folder; there are: " + ", ".join(models))
    build, server = listing_server(cfg)
    found = gpu_cards(gpu_devices(server)) if build != "cpu" else []
    if cards is None:
        if found:
            labels = [card_label(c) for c in found]
            nvidia = [c for c in found if c.get("cuda") and not c["integrated"]]
            default = [card_label(c) for c in (nvidia or [c for c in found if not c["integrated"]])]
            picked = ui.checklist("Which GPUs? (several split the model between them)", labels, default)
            cards = [found[labels.index(p)] for p in picked if p in labels]
        else:
            cards = []
    elif all(isinstance(c, int) for c in cards):
        if any(not 0 <= c < len(found) for c in cards):
            raise SetupError(f"GPU numbers are 0 to {len(found) - 1} (mini preset gpus lists them)")
        cards = [found[c] for c in cards]
    backend, device, split, note = gpu_plan(cards)
    vram = sum(c["mib"] for c in cards) * 1048576
    size = model_size(models[model])
    if ctx is None:
        ctx = int(ui.text("Context size (tokens)", str(cfg["llamacpp"]["ctx"])))
    args = cfg["llamacpp"]["extra_args"] + (" --split-mode layer" if len(cards) > 1 else "") + (f" -ts {split}" if split else "")
    if name is None:
        short = re.sub(r"(?i)[-_.](gguf|it|instruct)$", "", model)[:24].strip("-_.")
        name = ui.text("Preset name", f"{short}-{len(cards)}gpu" if cards else f"{short}-cpu")
    values = {"gpu": "cpu" if backend == "cpu" else build, "device": device, "model": model, "ctx": ctx, "extra_args": args}
    ui.say("preset " + name + ": " + model + (" on " + ", ".join(c["label"] for c in cards) if cards else " on the CPU")
           + f" ({backend}" + (f", split {split}" if split else "") + ")" + (f"\n  {note}" if note else ""))
    if cards and size > vram * 0.9:
        ui.say(f"  warning: the model is {size / 1073741824:.1f} GB and these cards have {vram / 1073741824:.1f} GB of VRAM; "
               "it will not fit with its context: pick more cards or a smaller model")
        if asked and not ui.yes("Save it anyway?", False):
            raise SetupError("not saved")
    elif cards and size > vram * 0.75:
        ui.say(f"  the model is {size / 1073741824:.1f} GB of {vram / 1073741824:.1f} GB VRAM: little room for the context; "
               "lower it if loading fails")
    save_preset(cfg, name, values)
    ui.say(f"saved preset {name}")
    if use is None:
        use = ui.yes(f"Use {name} now? (restarts llama-server)", True)
    if use:
        use_preset(cfg, name, ui)
    return name


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
    write_unit("llama-server", f"llama.cpp router over {os.path.expanduser(lc['models_dir'])} (mini's model server)",
               router_args(cfg, server), {}, ui)
    _wait(f"http://127.0.0.1:{lc['port']}/health", ui=ui)


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
    unit and restarts it; the preset's model is the one mini uses from then on."""
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
    resolve_local(cfg)
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
        elif action == "new":
            g = (opts.get("gpus") or "").strip()
            try:
                cards = None if not g else [] if g == "none" else [int(x) for x in g.split(",") if x.strip()]
            except ValueError:
                raise SetupError("--gpus takes card numbers (mini preset gpus), e.g. 0,1, or none") from None
            new_preset(cfg, ui, name, opts.get("model"), cards, opts.get("ctx"), opts.get("use"))
        elif action == "gpus":
            build, server = listing_server(cfg)
            for i, c in enumerate(gpu_cards(gpu_devices(server))):
                ui.say(f"{i}  {card_label(c)}")
        elif action == "show":
            ui.say(json.dumps(llama_settings(cfg) if name in (None, active) else
                              {**cfg["llamacpp"], **cfg.get("presets", {}).get(name, {})}, indent=1))
    except (SetupError, RuntimeError, OSError, subprocess.TimeoutExpired) as e:
        ui.say(f"preset: {e}")
        return 1
    return 0


def choose_model(cfg, ui):
    """Pick the model mini uses, from the models folder; it is load-tested before it is saved (to the active
    preset when there is one, else to [local] model)."""
    lc, url = cfg["llamacpp"], cfg["local"]["url"]
    names = list(models_in(os.path.expanduser(lc["models_dir"])))
    if not names:
        raise SetupError("no models in the models folder")
    current = cfg["local"].get("model")
    while names:
        pick = ui.choice("Which model should mini use locally?", names, current if current in names else names[0])
        ui.say(f"loading {pick} to test it (up to a minute) ...")
        ok, why = load_test(url, pick)
        if ok:
            preset = lc.get("preset")
            if preset:
                config_mod.set_values(cfg.path, {f"presets.{preset}": {"model": pick}})
                cfg["presets"][preset]["model"] = pick
            else:
                config_mod.set_values(cfg.path, {"local": {"model": pick}})
            cfg["local"]["model"] = pick
            ui.say(f"{pick} loads and answers.")
            return
        ui.say(f"{pick} does not load in llama.cpp {LLAMA_TAG}: {why}")
        names.remove(pick)
    raise SetupError("none of the models loads")


# ================================================================ terminal UI and the wizard
class QuietUI:
    """For a preset switch from inside the app: messages go to say(), every question takes its default."""

    def __init__(self, say):
        self.say = say

    def yes(self, prompt, default=True):
        return default

    def text(self, prompt, default=None):
        return default or ""

    def secret(self, prompt):
        return ""

    def checklist(self, prompt, options, checked=()):
        return list(checked)

    def choice(self, prompt, options, default=None):
        return default if default in options else options[0]


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
    ui.say("mini setup: checks what mini needs and helps you set up what is missing. Nothing changes without a yes.\n"
           "mini runs its models on llama.cpp, which this setup builds and runs for you.\n")
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
