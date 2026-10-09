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


DEMO_DISPLAY, SIZE, APP_ID = ":2", "1440x960", "org.mini.DemoTerm"   # wider than the 1280 monitor; x11grab still gets it all
TERM_GEOMETRY = "97x53+0+0"           # mini on the left ~800 px
BROWSER_BOX = (800, 0, 640, 960)      # Mailpit on the right: x, y, width, height


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


FF_DIR = os.path.expanduser("~/snap/firefox/common/mini-show")   # snap Firefox can only use its own home area


def open_browser(env, url="http://127.0.0.1:8025/"):
    """Open Firefox on the recorded screen at a fixed place beside mini. There is no window manager on the
    nested screen, so the window geometry is pre-written into the profile's xulstore.json."""
    shutil.rmtree(FF_DIR, ignore_errors=True)
    os.makedirs(FF_DIR)
    x, y, w, h = BROWSER_BOX
    json.dump({"chrome://browser/content/browser.xhtml": {"main-window": {
        "screenX": str(x), "screenY": str(y), "width": str(w), "height": str(h), "sizemode": "normal"}}},
        open(os.path.join(FF_DIR, "xulstore.json"), "w"))
    open(os.path.join(FF_DIR, "user.js"), "w").write(
        'user_pref("browser.shell.checkDefaultBrowser", false);\n'
        'user_pref("browser.aboutwelcome.enabled", false);\n'
        'user_pref("datareporting.policy.dataSubmissionPolicyBypassNotification", true);\n'
        'user_pref("browser.startup.homepage_override.mstone", "ignore");\n'
        'user_pref("trailhead.firstrun.didSeeAboutWelcome", true);\n')
    ff = subprocess.Popen(["firefox", "--new-instance", "--profile", FF_DIR, url], env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(8)
    return ff


def show_message(env, secs=10):
    """At the end: open the newest message in the same Firefox window and hold it on screen."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:8025/api/v1/messages", timeout=10) as r:
            msgs = json.load(r)["messages"]
    except OSError:
        msgs = []
    time.sleep(4)                       # the live inbox with the new mail
    if msgs:
        subprocess.Popen(["firefox", "--profile", FF_DIR, f"http://127.0.0.1:8025/view/{msgs[0]['ID']}"], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(secs)


def close_browser(ff):
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
    browser = open_browser(env) if task in ("email", "compose") else None   # visible from the start
    ff = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "x11grab", "-framerate", "15", "-draw_mouse", "0",
                           "-video_size", SIZE, "-i", DEMO_DISPLAY + ".0",
                           "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24", "-pix_fmt", "yuv420p", video],
                          stdin=subprocess.PIPE)
    t0 = time.time()
    done = os.path.join(OUT, f".done-{task}-{n}")
    if os.path.exists(done):
        os.remove(done)
    py = os.path.join(ROOT, ".venv", "bin", "python")
    cmd = (f"{py} -m mini --config {os.path.join(ROOT, 'config', 'demo.toml')} ui --task {json.dumps(TASKS[task])} "
           f"--exit-when-done --exit-delay 22; touch {done}")
    subprocess.run(["gnome-terminal", "--app-id", APP_ID, f"--geometry={TERM_GEOMETRY}", f"--working-directory={ROOT}",
                    "--", "bash", "-c", cmd], env=env, capture_output=True)
    # the terminal client returns at once; the take ends when mini itself exits (it writes the marker).
    # As soon as the run has finished (summary written), open the message while mini's final screen is visible.
    shown = False
    while not os.path.exists(done) and time.time() - t0 < 3 * 3600:
        new_runs = sorted(set(glob.glob(os.path.join(ROOT, "runs", "*"))) - before)
        if browser and not shown and new_runs and os.path.exists(os.path.join(new_runs[-1], "summary.json")):
            show_message(env)
            shown = True
        time.sleep(1)
    ff.communicate(b"q", timeout=60)
    if browser:
        close_browser(browser)
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
