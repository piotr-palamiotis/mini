"""Model downloads from Hugging Face into the models folder. Stdlib only.

A download is a job: <state>/downloads/<id>.json says what to fetch (repo, files with size and sha256, where they
go) and how far it got. The job runs in its own process, outside mini: a systemd user unit (systemd-run --user)
where there is one, else a detached process. So closing mini, or a crash, does not stop it. Files are written to
NAME.part next to their place and resumed from where they stopped (HTTP Range), checked against the sha256 the Hub
lists, and only then moved into place, so llama-server's router never sees half a model.

Layout in the models folder (what llama-server's router reads): one file -> NAME.gguf; a model split in parts, or
one with a vision projector (mmproj-*.gguf), -> a folder NAME/ holding them.

  python -m mini.models worker JOB.json        the job's process (started by start(); not for people)
"""
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HUB = "https://huggingface.co"
UNIT_PREFIX = "mini-download-"
CHUNK = 1 << 20
SAVE_EVERY_S = 2.0
RETRIES = 30                  # network errors in a row before the job gives up (resume picks it up again)
SPLIT = re.compile(r"^(?P<stem>.+)-(?P<n>\d{5})-of-(?P<total>\d{5})\.gguf$")
QUANT = re.compile(r"(?i)(?:^|[-_.])((?:UD-)?(?:I?Q\d[A-Z0-9_]*|BF16|F16|F32|MXFP4[A-Z0-9_]*))(?=[-_.]|$)")
USER_AGENT = "mini-model-downloader"
HELPER = re.compile(r"(?i)^mtp-|draft|^imatrix")   # speculative-decoding heads and the like: not a model on their own


class DownloadError(Exception):
    pass


# ================================================================ the Hub
def _get_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise DownloadError("no such public repository on Hugging Face (it does not exist, or it needs a login: "
                                "mini downloads public models only)") from e
        if e.code == 404:
            raise DownloadError("no such repository on Hugging Face") from e
        raise DownloadError(f"Hugging Face answered {e.code}") from e
    except (OSError, ValueError) as e:
        raise DownloadError(f"Hugging Face is not reachable: {e}") from e


def search(query, limit=15):
    """[(repo, downloads)]: GGUF repositories matching query, most downloaded first."""
    q = urllib.parse.urlencode({"search": query, "filter": "gguf", "sort": "downloads", "limit": limit})
    return [(m["id"], m.get("downloads") or 0) for m in _get_json(f"{HUB}/api/models?{q}")]


def tree(repo, revision="main"):
    """Every file of a repo: [{path, size, sha256}]."""
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo or ""):
        raise DownloadError("a repository is OWNER/NAME, e.g. unsloth/gemma-4-31B-it-GGUF")
    rows = _get_json(f"{HUB}/api/models/{repo}/tree/{urllib.parse.quote(revision)}?recursive=true")
    return [{"path": r["path"], "size": r.get("size") or 0, "sha256": (r.get("lfs") or {}).get("oid") or ""}
            for r in rows if r.get("type") == "file"]


def choices(files):
    """The models a repo offers: [{name, quant, files: [...], size}], one per quantization (split parts together),
    plus "mmproj": [the vision projector files]."""
    ggufs = [f for f in files if f["path"].endswith(".gguf") and not HELPER.search(os.path.basename(f["path"]))]
    proj = [f for f in ggufs if os.path.basename(f["path"]).lower().startswith("mmproj")]
    groups = {}
    for f in ggufs:
        if f in proj:
            continue
        base = os.path.basename(f["path"])
        m = SPLIT.match(base)
        key = (os.path.dirname(f["path"]), m["stem"] if m else base[:-5])
        groups.setdefault(key, []).append(f)
    out = []
    for (_, stem), fs in sorted(groups.items(), key=lambda kv: sum(f["size"] for f in kv[1])):
        q = QUANT.findall(stem)
        out.append({"name": stem, "quant": q[-1].upper() if q else "", "files": sorted(fs, key=lambda f: f["path"]),
                    "size": sum(f["size"] for f in fs)})
    return out, proj


def pick(options, wanted):
    """The option a user means by wanted: its name, its quantization (Q4_K_M), or a file path in it."""
    w = wanted.strip().lower().removesuffix(".gguf")
    for key in (lambda o: o["name"].lower(), lambda o: o["quant"].lower(),
                lambda o: next((f["path"].lower().removesuffix(".gguf") for f in o["files"] if f["path"].lower().removesuffix(".gguf") == w), None)):
        hits = [o for o in options if key(o) == w]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise DownloadError(f"{wanted} matches several: " + ", ".join(o["name"] for o in hits))
    raise DownloadError(f"no {wanted} in this repository; it has: " + ", ".join(o["quant"] or o["name"] for o in options))


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


# ================================================================ jobs
def jobs_dir(cfg):
    return os.path.join(cfg.abspath("state"), "downloads")


def plan(cfg, repo, option, name=None, mmproj=None, revision="main"):
    """A job for option (from choices) in the models folder. Not started; refuses what would not fit or exists."""
    models_dir = os.path.expanduser(cfg["llamacpp"]["models_dir"])
    name = name or option["name"]
    if not re.fullmatch(r"[\w.+-]+", name):
        raise DownloadError("a model name is letters, digits and . _ + -")
    folder = len(option["files"]) > 1 or mmproj is not None
    files = []
    for f in option["files"]:
        base = os.path.basename(f["path"])
        dest = os.path.join(models_dir, name, base) if folder else os.path.join(models_dir, name + ".gguf")
        files.append({**f, "dest": dest})
    if mmproj:
        files.append({**mmproj, "dest": os.path.join(models_dir, name, "mmproj-" + name + ".gguf")})
    if all(os.path.exists(f["dest"]) for f in files):
        raise DownloadError(f"{name} is already in the models folder")
    for j in all_jobs(cfg):
        if j["name"] == name and j["status"] in ("queued", "running"):
            raise DownloadError(f"{name} is already being downloaded (job {j['id']})")
    need = sum(f["size"] - _have(f) for f in files)
    os.makedirs(models_dir, exist_ok=True)
    free = shutil.disk_usage(models_dir).free
    if need > free - (1 << 30):
        raise DownloadError(f"not enough disk space in {models_dir}: needs {human(need)}, {human(free)} free")
    jid = time.strftime("%Y%m%d-%H%M%S") + "-" + re.sub(r"[^\w]+", "-", name)[:40].strip("-")
    return {"id": jid, "repo": repo, "revision": revision, "name": name, "models_dir": models_dir, "files": files,
            "size": sum(f["size"] for f in files), "done": sum(_have(f) for f in files), "status": "queued",
            "error": "", "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "updated_at": time.time(), "speed": 0}


def _have(f):
    for p in (f["dest"], f["dest"] + ".part"):
        if os.path.exists(p):
            return os.path.getsize(p)
    return 0


def job_path(cfg, jid):
    return os.path.join(jobs_dir(cfg), jid + ".json")


def save_job(path, job):
    job["updated_at"] = time.time()
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(job, f, indent=1)
    os.replace(tmp, path)


def load_job(path):
    with open(path) as f:
        return json.load(f)


def all_jobs(cfg):
    """Every job, newest first. A job marked running whose process is gone is shown as stopped."""
    d = jobs_dir(cfg)
    out = []
    for e in sorted(os.listdir(d), reverse=True) if os.path.isdir(d) else []:
        if e.endswith(".json"):
            try:
                j = load_job(os.path.join(d, e))
            except (OSError, ValueError):
                continue
            if j["status"] in ("queued", "running") and not _alive(j):
                j["status"], j["error"] = "stopped", j.get("error") or "its process ended (mini models resume continues it)"
            out.append(j)
    return out


def find_job(cfg, ref):
    js = all_jobs(cfg)
    hits = [j for j in js if j["id"] == ref] or [j for j in js if ref and (j["id"].startswith(ref) or j["name"] == ref)]
    if not hits:
        raise DownloadError(f"no download {ref!r}")
    return hits[0]


# ================================================================ the job's process
def _alive(job):
    unit = job.get("unit")
    if unit:
        r = subprocess.run(["systemctl", "--user", "is-active", "--quiet", unit], capture_output=True)
        if r.returncode == 0:
            return True
    pid = job.get("pid")
    if pid:
        try:
            os.kill(pid, 0)
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                return b"mini.models" in f.read()
        except OSError:
            return False
    # just started: the process has not written its pid yet
    return job["status"] == "queued" and time.time() - job.get("updated_at", 0) < 30


def start(cfg, job, runner=None):
    """Write the job and start its process outside mini. Returns the job (with unit or pid)."""
    os.makedirs(jobs_dir(cfg), exist_ok=True)
    path = job_path(cfg, job["id"])
    job.update(status="queued", error="", done=sum(_have(f) for f in job["files"]))
    save_job(path, job)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    argv = [sys.executable, "-m", "mini.models", "worker", path]
    if runner:                                     # tests
        runner(argv, job)
    elif shutil.which("systemd-run"):
        unit = UNIT_PREFIX + job["id"]
        r = subprocess.run(["systemd-run", "--user", "--quiet", "--collect", f"--unit={unit}",
                            f"--working-directory={root}", f"--setenv=PYTHONPATH={root}", "-p", "Nice=10", *argv],
                           capture_output=True, text=True)
        if r.returncode:
            raise DownloadError("could not start the download: " + (r.stderr.strip() or "systemd-run failed"))
        job["unit"] = unit
    else:
        log = open(path[:-5] + ".log", "ab")
        p = subprocess.Popen(argv, cwd=root, env={**os.environ, "PYTHONPATH": root}, stdin=subprocess.DEVNULL,
                             stdout=log, stderr=log, start_new_session=True)
        job["pid"] = p.pid
    cur = load_job(path)                            # the process may have written already
    cur.update({k: job[k] for k in ("unit", "pid") if k in job})
    save_job(path, cur)
    return cur


def cancel(cfg, ref, discard=False):
    """Stop a download. Its .part files stay for resume() unless discard."""
    job = find_job(cfg, ref)
    if job.get("unit"):
        subprocess.run(["systemctl", "--user", "stop", job["unit"]], capture_output=True)
    if job.get("pid"):
        try:
            os.killpg(job["pid"], signal.SIGTERM)
        except OSError:
            pass
    path = job_path(cfg, job["id"])
    for _ in range(20):                             # let the worker record where it stopped
        if load_job(path)["status"] not in ("queued", "running"):
            break
        time.sleep(0.1)
    job = load_job(path)
    if job["status"] in ("queued", "running"):
        job["status"] = "stopped"
    if discard:
        for f in job["files"]:
            if os.path.exists(f["dest"] + ".part"):
                os.remove(f["dest"] + ".part")
        job["status"], job["done"] = "cancelled", 0
    save_job(path, job)
    return job


def resume(cfg, ref, runner=None):
    job = find_job(cfg, ref)
    if job["status"] == "done":
        raise DownloadError(f"{job['name']} is already downloaded")
    if job["status"] in ("queued", "running"):
        raise DownloadError(f"{job['name']} is still downloading")
    job.pop("unit", None)
    job.pop("pid", None)
    return start(cfg, job, runner)


def worker(path, opener=None):
    """Download every file of the job at path, resuming .part files; record progress in the job file."""
    opener = opener or urllib.request.urlopen
    job = load_job(path)
    job.update(status="running", error="", pid=os.getpid())
    save_job(path, job)
    stopping = []
    signal.signal(signal.SIGTERM, lambda *a: stopping.append(1))
    try:
        for f in job["files"]:
            if os.path.exists(f["dest"]) and os.path.getsize(f["dest"]) == f["size"]:
                continue
            _fetch(job, f, path, opener, stopping)
        job.update(status="done", done=job["size"], speed=0)
    except _Stopped:
        job.update(status="stopped", error="stopped by the user", speed=0)
    except Exception as e:   # noqa: BLE001 - every failure ends in the job file, for the user to see
        job.update(status="failed", error=str(e) or type(e).__name__, speed=0)
    job["done"] = sum(_have(f) for f in job["files"])
    save_job(path, job)
    return job


class _Stopped(Exception):
    pass


def _fetch(job, f, path, opener, stopping):
    os.makedirs(os.path.dirname(f["dest"]), exist_ok=True)
    part = f["dest"] + ".part"
    url = f"{HUB}/{job['repo']}/resolve/{urllib.parse.quote(job.get('revision') or 'main')}/{urllib.parse.quote(f['path'])}"
    errors = 0
    while True:
        h = hashlib.sha256()
        have = os.path.getsize(part) if os.path.exists(part) else 0
        if have > f["size"]:
            os.remove(part)
            have = 0
        with open(part, "rb") if have else open(os.devnull, "rb") as old:   # the hash continues over what is there
            for block in iter(lambda: old.read(CHUNK), b""):
                h.update(block)
        try:
            if have < f["size"]:
                h = _stream(job, f, path, url, part, have, h, opener, stopping)
            break
        except _Stopped:
            raise
        except (OSError, urllib.error.URLError) as e:   # network trouble: wait and continue from where it stopped
            if isinstance(e, urllib.error.HTTPError) and e.code in (401, 403, 404):
                raise DownloadError(f"{f['path']}: Hugging Face answered {e.code}") from e
            errors += 1
            if errors > RETRIES:
                raise DownloadError(f"{f['path']}: {e} ({RETRIES} retries)") from e
            job.update(error=f"network: {e}; retrying", speed=0)
            save_job(path, job)
            for _ in range(min(60, 2 ** min(errors, 6))):
                if stopping:
                    raise _Stopped()
                time.sleep(1)
    if f.get("sha256") and h.hexdigest() != f["sha256"]:
        os.remove(part)
        raise DownloadError(f"{f['path']}: the download is damaged (sha256 differs); it was removed, resume fetches it again")
    os.replace(part, f["dest"])


def _stream(job, f, path, url, part, have, h, opener, stopping):
    headers = {"User-Agent": USER_AGENT}
    if have:
        headers["Range"] = f"bytes={have}-"
    with opener(urllib.request.Request(url, headers=headers), timeout=60) as r:
        if have and getattr(r, "status", 200) != 206:     # the server ignored the range: start the file again
            have, h = 0, hashlib.sha256()
        with open(part, "ab" if have else "wb") as out:
            last, last_bytes = time.time(), have
            while True:
                if stopping:
                    raise _Stopped()
                block = r.read(CHUNK)
                if not block:
                    break
                out.write(block)
                h.update(block)
                have += len(block)
                now = time.time()
                if now - last >= SAVE_EVERY_S:
                    out.flush()
                    job.update(done=sum(_have(x) for x in job["files"]), error="",
                               speed=int((have - last_bytes) / (now - last)))
                    save_job(path, job)
                    last, last_bytes = now, have
    if have < f["size"]:
        raise OSError(f"the connection closed at {human(have)} of {human(f['size'])}")
    return h


def progress_text(job):
    pct = 100 * job["done"] / job["size"] if job["size"] else 100
    s = f"{job['name']} {pct:.0f}% of {human(job['size'])}"
    if job["status"] == "running" and job.get("speed"):
        left = (job["size"] - job["done"]) / job["speed"]
        s += f", {human(job['speed'])}/s, {int(left // 60)} min left"
    return s


# ================================================================ command line (mini models ...)
def cli(cfg, action, ref=None, wanted=None, name=None, mmproj=False, discard=False, ui=None):
    from .setup import TerminalUI, models_in
    ui = ui or TerminalUI()
    try:
        if action == "search":
            if not ref:
                raise DownloadError("usage: mini models search WORDS")
            for repo, n in search(ref):
                ui.say(f"{repo:<60} {n:>10,} downloads")
            ui.say("then: mini models files OWNER/NAME")
        elif action == "files":
            options, proj = choices(tree(ref or ""))
            for o in options:
                ui.say(f"{o['quant'] or '-':<12} {human(o['size']):>10}  {o['name']}"
                       + (f"  ({len(o['files'])} parts)" if len(o["files"]) > 1 else ""))
            for f in proj:
                ui.say(f"{'vision':<12} {human(f['size']):>10}  {f['path']}  (--mmproj)")
            if not options:
                ui.say("(no GGUF models in this repository)")
            else:
                ui.say(f"then: mini models download {ref} QUANT")
        elif action == "download":
            job = download(cfg, ref or "", wanted, name, mmproj, ui)
            ui.say(f"downloading in the background ({'unit ' + job['unit'] if job.get('unit') else 'pid ' + str(job.get('pid'))}): "
                   f"{progress_text(job)}\nit keeps going when mini closes; mini models list shows how far it is")
        elif action == "list":
            have = models_in(os.path.expanduser(cfg["llamacpp"]["models_dir"]))
            for n, m in have.items():
                size = sum(os.path.getsize(os.path.join(os.path.dirname(m["file"]), x)) for x in os.listdir(os.path.dirname(m["file"]))
                           if x.endswith(".gguf")) if os.path.dirname(m["file"]) != os.path.expanduser(cfg["llamacpp"]["models_dir"]) else os.path.getsize(m["file"])
                ui.say(f"  {n:<40} {human(size):>10}" + ("  + vision" if m["mmproj"] else ""))
            if not have:
                ui.say("(no models yet: mini models search WORDS)")
            for j in all_jobs(cfg):
                if j["status"] != "done":
                    ui.say(f"{j['status']:>9}  {j['id']}  {progress_text(j)}" + (f"  ({j['error']})" if j.get("error") else ""))
        elif action == "cancel":
            j = cancel(cfg, ref or "", discard)
            ui.say(f"{j['name']}: {j['status']}" + ("" if discard else " (mini models resume continues it)"))
        elif action == "resume":
            j = resume(cfg, ref or "")
            ui.say(f"resumed: {progress_text(j)}")
    except DownloadError as e:
        ui.say(f"models: {e}")
        return 1
    return 0


def download(cfg, repo, wanted=None, name=None, mmproj=False, ui=None, runner=None):
    """Plan and start a download of one quantization of repo (asks which when wanted is empty and ui is given)."""
    options, proj = choices(tree(repo))
    if not options:
        raise DownloadError("no GGUF models in this repository")
    if wanted:
        option = pick(options, wanted)
    elif ui is not None:
        labels = [f"{o['quant'] or o['name']}  {human(o['size'])}" for o in options]
        option = options[labels.index(ui.choice("Which one?", labels, labels[0]))]
    else:
        raise DownloadError("say which one: " + ", ".join(o["quant"] or o["name"] for o in options))
    projector = None
    if mmproj:
        if not proj:
            raise DownloadError("this repository has no vision projector (mmproj)")
        projector = min(proj, key=lambda f: (0 if "f16" in f["path"].lower() else 1, f["size"]))
    return start(cfg, plan(cfg, repo, option, name, projector), runner)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "worker":
        sys.exit(0 if worker(sys.argv[2])["status"] == "done" else 1)
    print(__doc__)
    sys.exit(2)
