"""Run bench tasks on a preset: each run of a task gets a fresh folder with its fixture, mini works on it
unattended (`mini run --auto --no-confirm`), and the task's check decides passed or failed. Every run adds a row to
<state>/bench/results.jsonl (bench/results.py).

Everything a bench writes is under <state>/bench: its own mini state (tools, lessons: emptied before every run, so no
run inherits what an earlier one built), the run logs, the trial folders and the transcript of each run.

The settings that change what a model can do are pinned for the bench (BENCH_CONFIG: sampling, thinking, limits), so a
preset's results stay comparable whatever the user's own settings are. The bench switches llama-server to the preset
it runs on and back to the one that was active when it ends.

  python -m mini.bench.run worker JOB.json     the process of a background bench (mini bench run --background)
"""
import copy
import json
import os
import shutil
import subprocess
import sys
import time

from .. import bench
from ..harness import config as config_mod
from . import results

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # the mini source folder
MIN_FREE_GB = 3
UNIT = "mini-bench"

BENCH_CONFIG = {
    "local": {"temperature": 0.6, "top_k": 40, "top_p": 0.95, "min_p": 0.05},
    "thinking": {"parse": True, "gap": True, "spec": True, "code": True, "fix": True, "execute": True, "write": True},
    "caps": {"attempts_per_tool": 10, "task_wall_clock_s": 1200, "task_turns": 30,
             "run_hard_wall_clock_s": 99999999},          # the task's own timeout ends a run
    "approvals": {"confirm_first": True, "mode": "ask", "auto_approve": []},
    "lessons": {"enabled": True},
    "tools": {"auto_accept": False},
}


class BenchError(Exception):
    pass


def bench_dir(cfg):
    return os.path.join(cfg.abspath("state"), "bench")


def mini_commit():
    r = subprocess.run(["git", "-C", ROOT, "rev-parse", "--short", "HEAD"], capture_output=True, text=True)
    dirty = subprocess.run(["git", "-C", ROOT, "status", "--porcelain", "--untracked-files=no"],
                           capture_output=True, text=True).stdout.strip()
    return (r.stdout.strip() or "?") + ("+" if dirty else "")


def bench_config(cfg, preset, seed, root):
    """The config file the bench's mini runs read: the user's settings, the preset active, BENCH_CONFIG laid over
    them, state and runs under root, and this run's sampling seed."""
    data = config_mod._merge(copy.deepcopy(dict(cfg)), BENCH_CONFIG)
    data["llamacpp"]["preset"] = preset
    data["paths"] = dict(data["paths"], state=os.path.join(root, "state"), runs=os.path.join(root, "runs"))
    from ..harness.sandbox import package_cache
    data["sandbox"] = dict(data["sandbox"], package_cache=package_cache(cfg))   # shared with mini's own runs
    if seed is not None:
        data["local"]["seed"] = seed
    data["local"].pop("model", None)              # taken from the preset
    path = os.path.join(root, "mini.toml")
    os.makedirs(root, exist_ok=True)
    with open(path, "w") as f:
        f.write("# written by mini bench for its runs; changes are overwritten\n")
        f.write(config_mod.dumps(_clean(data)))
    return path


def _clean(d):
    """Drop None values (TOML has none) at any depth."""
    return {k: _clean(v) if isinstance(v, dict) else v for k, v in d.items() if v is not None}


def reset_state(root):
    """Each run starts without the tools and lessons earlier runs left (bundled tools re-seed themselves)."""
    st = os.path.join(root, "state")
    for d in ("tools", "registry"):
        shutil.rmtree(os.path.join(st, d), ignore_errors=True)
    for f in ("lessons.jsonl",):
        if os.path.exists(os.path.join(st, f)):
            os.remove(os.path.join(st, f))


def free_space(root, say, keep_gb=12):
    """A run needs MIN_FREE_GB free where trials and sandbox images are kept. Package images are kept between runs
    (with the package cache, mini/harness/sandbox.py, they are what makes a run need no network); they go only when
    less than keep_gb is free, and are built again from the cached packages."""
    images = os.path.expanduser("~/.local/share/containers")
    if shutil.which("podman") and os.path.isdir(images) and shutil.disk_usage(images).free / 2**30 < keep_gb:
        out = subprocess.run(["podman", "images", "--format", "{{.Repository}}:{{.Tag}}"], capture_output=True,
                             text=True).stdout.split()
        old = [n for n in out if ":pkg-" in n]
        if old:
            subprocess.run(["podman", "rmi"] + old, capture_output=True)
            subprocess.run(["podman", "image", "prune", "-f"], capture_output=True)
            say(f"removed {len(old)} package images (low disk space)")
    for d in (root, images):
        if os.path.isdir(d):
            free = shutil.disk_usage(d).free / 2**30
            if free < MIN_FREE_GB:
                raise BenchError(f"only {free:.1f} GB free under {d} (a run needs {MIN_FREE_GB} GB)")


def _mini(args, cwd, config, timeout, out=None):
    """mini from this source folder, on the bench config. Returns the exit code, or "timeout"."""
    argv = [sys.executable, "-m", "mini", "--config", config] + args
    env = dict(os.environ, PYTHONPATH=ROOT, MINI_CONFIG=config)
    try:
        if out:
            with open(out, "w") as f:
                return subprocess.run(argv, cwd=cwd, env=env, stdout=f, stderr=subprocess.STDOUT,
                                      stdin=subprocess.DEVNULL, timeout=timeout).returncode
        return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return "timeout"


def write_fixture(task, w):
    for rel, body in task.files.items():
        p = os.path.join(w, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(body if isinstance(body, bytes) else body.encode())
    g = ["git", "-c", "user.name=bench", "-c", "user.email=bench@localhost"]
    subprocess.run(["git", "init", "-q"], cwd=w, check=True)
    subprocess.run(g + ["add", "-A"], cwd=w, check=True)
    subprocess.run(g + ["commit", "-qm", "fixture", "--allow-empty"], cwd=w, check=True)


def run_task(task, config, root, run_id):
    """One run of one task. Returns (passed, secs, note)."""
    reset_state(root)
    trials = os.path.join(root, "trials")
    w = os.path.join(trials, run_id)
    os.makedirs(w)
    write_fixture(task, w)
    if task.setup:                                        # e.g. start a service, save a setting
        p = os.path.join(trials, run_id + ".setup.py")
        with open(p, "w") as f:
            f.write(task.setup)
        subprocess.run([sys.executable, "-I", p, w, os.path.join(root, "state")], check=True, timeout=120)
    out = os.path.join(trials, run_id + ".out")
    t0 = time.time()
    rc = _mini(["run", "--auto", "--no-confirm", "--label", run_id, task.prompt], w, config, task.timeout, out)
    if task.prompt2:                                      # the follow-up, in the same folder with the tools kept
        staged = _mini(["tools", "staged"], w, config, 120)
        names = sorted({ln.split()[0] for ln in getattr(staged, "stdout", "").splitlines()
                        if ln.strip() and not ln.startswith("(")})
        for n in names:                                   # accepted as the user would after a review
            _mini(["tools", "accept", n, "--force"], w, config, 120)
        left = max(60, task.timeout - round(time.time() - t0))
        rc2 = _mini(["run", "--auto", "--no-confirm", "--label", run_id + "-b", task.prompt2], w, config, left,
                    out + "2")
        rc = rc or rc2
    secs = round(time.time() - t0)
    check = os.path.join(trials, run_id + ".check.py")
    with open(check, "w") as f:
        f.write(task.check)
    r = subprocess.run([sys.executable, "-I", check, w, out], capture_output=True, text=True, timeout=300)
    note = (r.stdout + r.stderr).strip()[-300:]
    if rc == "timeout":
        note = f"timed out after {task.timeout} s; " + note
    return r.returncode == 0, secs, note


def run_bench(cfg, preset, task_list, reps=1, seed=None, say=print, status=None):
    """Run task_list reps times on preset (default: the active one). Returns the rows written."""
    from .. import setup
    preset = preset or cfg["llamacpp"].get("preset") or ""
    if preset not in (cfg.get("presets") or {}):
        raise BenchError(f"no preset {preset!r}; a bench runs on a preset (mini preset list)")
    model = config_mod.llama_settings({**cfg, "llamacpp": {**cfg["llamacpp"], "preset": preset}}).get("model") or "?"
    root = bench_dir(cfg)
    os.makedirs(os.path.join(root, "trials"), exist_ok=True)
    was = cfg["llamacpp"].get("preset") or ""
    batch = time.strftime("%Y%m%d-%H%M%S")
    commit = mini_commit()
    rows, total = [], reps * len(task_list)
    if was != preset:
        say(f"switching llama-server to preset {preset} ...")
        setup.use_preset(cfg, preset, setup.QuietUI(say))
    try:
        for rep in range(reps):
            for task in task_list:
                free_space(root, say)
                s = None if seed is None else seed + rep
                config = bench_config(cfg, preset, s, root)
                run_id = f"{preset}-{task.name}-{rep}-{time.strftime('%H%M%S')}"
                n = len(rows) + 1
                if status:
                    status(dict(state="running", preset=preset, model=model, task=task.name, rep=rep, n=n, total=total,
                                started=time.time(), batch=batch))
                say(f"[{n}/{total}] {task.name} (rep {rep}) on {preset} ...")
                passed, secs, note = run_task(task, config, root, run_id)
                row = results.add(cfg, dict(preset=preset, model=model, suite=task.suite, task=task.name, rep=rep,
                                            seed=s, passed=passed, secs=secs, run=run_id, batch=batch, mini=commit,
                                            note=note))
                rows.append(row)
                say(f"[{n}/{total}] {task.name}: {'passed' if passed else 'FAILED'} in {secs} s"
                    + ("" if passed else f" ({note[-160:]})"))
    finally:
        if was and was != preset:
            say(f"switching llama-server back to preset {was} ...")
            try:
                setup.use_preset(config_mod.reload(cfg), was, setup.QuietUI(say))
            except Exception as e:      # noqa: BLE001 - the results stand; the user switches back by hand
                say(f"could not switch back to {was}: {e}")
    return rows


# ---------------------------------------------------------------- background runs (mini bench run --background, UI)
def job_path(cfg):
    return os.path.join(bench_dir(cfg), "job.json")


def load_job(cfg):
    try:
        with open(job_path(cfg)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_job(cfg, job):
    os.makedirs(bench_dir(cfg), exist_ok=True)
    p = job_path(cfg)
    with open(p + ".tmp", "w") as f:
        json.dump(job, f, indent=1)
    os.replace(p + ".tmp", p)


def running(cfg):
    """The background bench job while its process is alive, else None."""
    job = load_job(cfg)
    pid = job.get("pid")
    if job.get("state") not in ("queued", "running") or not pid:
        return job if job.get("state") == "queued" and time.time() - job.get("updated", 0) < 30 else None
    try:
        os.kill(pid, 0)
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return job if b"mini.bench.run" in f.read() else None
    except OSError:
        return None


def start(cfg, presets, names, reps, seed):
    """Start a bench in its own process outside mini (a systemd user unit when there is one): the tasks on each preset
    in turn. Returns the job."""
    if running(cfg):
        raise BenchError("a bench is already running (mini bench stop ends it)")
    bench.tasks(names)                                    # unknown names fail here, not in the background
    presets = [presets] if isinstance(presets, str) else list(presets)
    job = dict(state="queued", preset=presets[0], presets=presets, names=names, reps=reps, seed=seed, config=cfg.named,
               updated=time.time(), log=os.path.join(bench_dir(cfg), "bench.log"))
    save_job(cfg, job)
    argv = [sys.executable, "-m", "mini.bench.run", "worker", job_path(cfg)]
    env = {**os.environ, "PYTHONPATH": ROOT}
    if cfg.named:
        env["MINI_CONFIG"] = cfg.named
    if shutil.which("systemd-run"):
        subprocess.run(["systemctl", "--user", "reset-failed", UNIT], capture_output=True)
        r = subprocess.run(["systemd-run", "--user", "--quiet", "--collect", f"--unit={UNIT}",
                            f"--working-directory={ROOT}", f"--setenv=PYTHONPATH={ROOT}"]
                           + ([f"--setenv=MINI_CONFIG={cfg.named}"] if cfg.named else []) + argv,
                           capture_output=True, text=True)
        if r.returncode:
            raise BenchError("could not start the bench: " + (r.stderr.strip() or "systemd-run failed"))
    else:
        log = open(job["log"], "ab")
        subprocess.Popen(argv, cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True)
    return job


def stop(cfg):
    """End the background bench: the run in progress is lost (no row), finished runs stay."""
    job = running(cfg)
    if not job:
        return False
    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "--user", "stop", UNIT], capture_output=True)
    if job.get("pid"):
        try:
            os.killpg(job["pid"], 15)
        except OSError:
            pass
    job.update(state="stopped", updated=time.time())
    save_job(cfg, job)
    return True


def worker(path):
    cfg = config_mod.load(os.environ.get("MINI_CONFIG") or None)
    with open(path) as f:
        job = json.load(f)
    job.update(pid=os.getpid(), state="running", updated=time.time())
    save_job(cfg, job)
    log = open(job["log"], "a")

    def say(m):
        log.write(time.strftime("%H:%M:%S ") + m + "\n")
        log.flush()

    def status(s):
        cur = load_job(cfg)
        if cur.get("state") == "stopped":
            raise SystemExit(0)
        cur.update(s, pid=os.getpid(), updated=time.time())
        save_job(cfg, cur)

    try:
        for p in job.get("presets") or [job["preset"]]:   # one preset after another
            run_bench(config_mod.reload(cfg), p, bench.tasks(job["names"]), job["reps"], job["seed"], say, status)
        state, error = "done", ""
    except Exception as e:      # noqa: BLE001 - the job records why it stopped
        state, error = "failed", f"{type(e).__name__}: {e}"
        say("bench stopped: " + error)
    cur = load_job(cfg)
    cur.update(state=state, error=error, updated=time.time())
    save_job(cfg, cur)


if __name__ == "__main__":
    if sys.argv[1:2] == ["worker"] and len(sys.argv) == 3:
        worker(sys.argv[2])
    else:
        sys.exit("usage: python -m mini.bench.run worker JOB.json")
