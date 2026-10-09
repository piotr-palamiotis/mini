"""Record demo takes: mini's real UI, full screen in a terminal, captured with ffmpeg x11grab.

Each take runs one task from a prepared registry with config/demo.toml (3-attempt budget, dialogs shown and
answered by the demo policy). After the take the result is graded against reality (Mailpit inbox, live
forecast). A take that fails is kept on disk and a fresh take starts, up to --max-takes.

usage: python demo/record_takes.py email|weather|compose [--max-takes 5] [--registry-from DIR]
Output: demo/out/takes/<task>-<n>.mkv, <task>-<n>.json, mailpit-<task>-<n>.png
"""
import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from campaign import grade  # noqa: E402

TASKS = {
    "email": "Send an email to anna@example.local saying the meeting moved to Friday.",
    "weather": "What will the weather be in Prague tomorrow?",
    "compose": "Email anna@example.local tomorrow's weather forecast for Prague.",
}
OUT = os.path.join(ROOT, "demo", "out", "takes")
REG = os.path.join(ROOT, "state", "registry")


DEMO_DISPLAY, SIZE, APP_ID = ":2", "1280x800", "org.mini.DemoTerm"


def demo_screen():
    """A private nested X screen (Xephyr) for the recording: other windows cannot cover it and the user's
    desktop never appears in the video. A separate gnome-terminal server instance is bound to it."""
    env = {**os.environ, "DISPLAY": DEMO_DISPLAY}
    if subprocess.run(["xdpyinfo", "-display", DEMO_DISPLAY], capture_output=True).returncode != 0:
        subprocess.Popen(["Xephyr", DEMO_DISPLAY, "-screen", SIZE, "-br", "-ac", "-noreset", "-title", "mini demo screen"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        for _ in range(40):
            if subprocess.run(["xdpyinfo", "-display", DEMO_DISPLAY], capture_output=True).returncode == 0:
                break
            time.sleep(0.25)
    running = subprocess.run(["pgrep", "-f", "gnome-terminal-server --app-id " + APP_ID], capture_output=True).stdout
    if not running.strip():
        subprocess.Popen(["/usr/libexec/gnome-terminal-server", "--app-id", APP_ID], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        time.sleep(2)
    return env


def mailpit_shot(path):
    """Screenshot of the real Mailpit web inbox. Firefox is a snap here, so its profile and output must live in
    the snap's own home area; the PNG is copied out afterwards."""
    d = os.path.expanduser("~/snap/firefox/common/mini-shot")
    os.makedirs(os.path.join(d, "profile"), exist_ok=True)
    tmp = os.path.join(d, "shot.png")
    if os.path.exists(tmp):
        os.remove(tmp)
    subprocess.run(["firefox", "--headless", "--profile", os.path.join(d, "profile"), "--window-size=1280,800",
                    "--screenshot", tmp, "http://127.0.0.1:8025/"], capture_output=True, timeout=90)
    if os.path.exists(tmp):
        shutil.copy(tmp, path)
    return os.path.exists(path)


def show_inbox(env, secs=12):
    """Open the real Mailpit inbox in Firefox on the recorded screen, open the newest message, hold it."""
    d = os.path.expanduser("~/snap/firefox/common/mini-show")
    os.makedirs(d, exist_ok=True)
    try:
        with urllib.request.urlopen("http://127.0.0.1:8025/api/v1/messages", timeout=10) as r:
            msgs = json.load(r)["messages"]
        url = f"http://127.0.0.1:8025/view/{msgs[0]['ID']}" if msgs else "http://127.0.0.1:8025/"
    except OSError:
        url = "http://127.0.0.1:8025/"
    ff = subprocess.Popen(["firefox", "--new-instance", "--profile", d, "--kiosk", "http://127.0.0.1:8025/"],
                          env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(secs / 2 + 4)          # the inbox list with the new mail
    subprocess.Popen(["firefox", "--profile", d, url], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(secs / 2 + 2)          # the message itself
    ff.terminate()
    try:
        ff.wait(10)
    except subprocess.TimeoutExpired:
        ff.kill()


def take(task, n, registry_from):
    shutil.rmtree(REG, ignore_errors=True)
    if registry_from:
        shutil.copytree(registry_from, REG)
    else:
        os.makedirs(REG)
    subprocess.run([os.path.join(ROOT, "demo", "services.sh"), "up"], capture_output=True)
    time.sleep(1)
    grade.mailpit_clear()
    video = os.path.join(OUT, f"{task}-{n}.mkv")
    before = set(glob.glob(os.path.join(ROOT, "runs", "*")))
    env = demo_screen()
    ff = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "x11grab", "-framerate", "15",
                           "-video_size", SIZE, "-i", DEMO_DISPLAY + ".0",
                           "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24", "-pix_fmt", "yuv420p", video],
                          stdin=subprocess.PIPE)
    t0 = time.time()
    done = os.path.join(OUT, f".done-{task}-{n}")
    if os.path.exists(done):
        os.remove(done)
    py = os.path.join(ROOT, ".venv", "bin", "python")
    cmd = (f"{py} -m mini --config {os.path.join(ROOT, 'config', 'demo.toml')} ui --task {json.dumps(TASKS[task])} "
           f"--exit-when-done; touch {done}")
    subprocess.run(["gnome-terminal", "--app-id", APP_ID, "--geometry=156x46+0+0", f"--working-directory={ROOT}",
                    "--", "bash", "-c", cmd], env=env, capture_output=True)
    # the terminal client returns at once; the take ends when mini itself exits (it writes the marker)
    while not os.path.exists(done) and time.time() - t0 < 3 * 3600:
        time.sleep(2)
    if task in ("email", "compose"):
        show_inbox(env)            # the inbox appears in the recording itself, after mini has finished
    ff.communicate(b"q", timeout=60)
    new = sorted(set(glob.glob(os.path.join(ROOT, "runs", "*"))) - before)
    run_dir = new[-1] if new else None
    summ = os.path.join(run_dir, "summary.json") if run_dir else None
    if not summ or not os.path.exists(summ):
        rec = {"task": task, "take": n, "passed": False, "reason": "no run summary (UI closed early?)",
               "video": video, "secs": round(time.time() - t0)}
    else:
        s = json.load(open(summ))
        passed, details = grade.GRADERS[task](s, run_dir)
        rec = {"task": task, "take": n, "passed": passed, "agent_status": s["status"], "grade": details,
               "run_id": s["run_id"], "created": s["created"], "error": s["error"], "answer": s["answer"],
               "video": video, "secs": round(time.time() - t0)}
        if task in ("email", "compose"):
            shot = os.path.join(OUT, f"mailpit-{task}-{n}.png")
            rec["mailpit_screenshot"] = shot if mailpit_shot(shot) else None
        if passed:
            rec["registry_after"] = os.path.join(OUT, f"registry-after-{task}-{n}")
            shutil.copytree(REG, rec["registry_after"])
    json.dump(rec, open(os.path.join(OUT, f"{task}-{n}.json"), "w"), indent=1)
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("task", choices=list(TASKS))
    ap.add_argument("--max-takes", type=int, default=5)
    ap.add_argument("--registry-from")
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    start = len(glob.glob(os.path.join(OUT, f"{a.task}-*.json"))) + 1
    for n in range(start, start + a.max_takes):
        rec = take(a.task, n, a.registry_from)
        print(json.dumps({k: rec.get(k) for k in ("task", "take", "passed", "agent_status", "secs", "error")}), flush=True)
        if rec["passed"]:
            break


if __name__ == "__main__":
    main()
