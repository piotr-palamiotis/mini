"""Sandbox: every piece of generated code runs here, never on the host.

Rootless Podman (or rootless Docker). Per run:
  - read-only root filesystem, /tmp as tmpfs, all capabilities dropped, no-new-privileges
  - memory, CPU and PID limits; wall-clock timeout
  - tool files mounted read-only at /tool, the call's own folder read-write at /work (copies of the files the call
    names, never the user's folder or its .git), nothing else
  - no host environment variables, no credentials, no home directory (HOME=/tmp inside)
  - network: none, unless the manifest declares host:port targets that were approved. Then the container joins
    a fresh --internal network (no route out) and each declared host resolves to an egress forwarder container
    (sandbox/egress.py) that forwards only to those targets.
Commands are argv lists. argv[0] must be on the allowlist; no shell is ever involved.
"""
import hashlib
import json
import os
import re
import subprocess
import threading
import time
import uuid

from .config import ROOT

SANDBOX_DIR = os.path.join(ROOT, "sandbox")
TARGET_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?:\d{1,5}$")
PKG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*([<>=!~]=?[A-Za-z0-9.*+-]+)?$")


class PolicyError(Exception):
    pass


def validate_targets(targets):
    out = []
    for t in targets or []:
        t = str(t).strip().lower()
        for prefix in ("https://", "http://"):
            if t.startswith(prefix):
                t = t[len(prefix):]
        t = t.rstrip("/")
        if not TARGET_RE.match(t):
            raise PolicyError(f"network target must be host:port, got {t!r}")
        out.append(t)
    return sorted(set(out))


PLACEHOLDERS = {"none", "null", "nil", "n/a", "na", "-", "no", "nothing", "stdlib", "builtin", "standard library"}


HOST_RE = re.compile(r"host='([A-Za-z0-9.-]+)',\s*port=(\d+)|Failed to resolve '([A-Za-z0-9.-]+)'|"
                     r"(?:https?://)([A-Za-z0-9.-]+\.[a-z]{2,})(?::(\d+))?/")


def network_diagnosis(output, egress_events, declared):
    """Explain blocked or undeclared connections in plain words, so the model can fix the URL or the spec."""
    declared = list(declared or [])
    notes = []
    for e in egress_events or []:
        if e.get("event") == "deny":
            notes.append(f"blocked {e.get('dest')}:{e.get('port')} ({e.get('reason')})")
    connection_trouble = re.search(r"Connection|resolve|Name or service|SSL|timed out|Max retries|URLError|gaierror",
                                   output or "")
    if connection_trouble:
        for m in HOST_RE.finditer(output or ""):
            host = (m.group(1) or m.group(3) or m.group(4) or "").lower()
            port = m.group(2) or m.group(5)
            if not host or host in ("localhost", "127.0.0.1"):
                continue
            target = f"{host}:{port}" if port else host
            ok = any(d == target or (not port and d.startswith(host + ":")) for d in declared)
            if not ok:
                notes.append(f"{target} is not declared")
    if not notes:
        return ""
    uniq = list(dict.fromkeys(notes))
    return ("Network: " + "; ".join(uniq) + f". Declared targets: {', '.join(declared) or 'none'}. Only declared "
            "host:port pairs are reachable (https uses port 443, http port 80). Fix the URL in the code, or call "
            "update_spec to declare the exact host:port.")


def validate_packages(pkgs):
    import sys
    if not isinstance(pkgs, dict):
        raise PolicyError("packages must be an object: {\"apt\": [...], \"pip\": [...]}")
    apt = sorted(set(pkgs.get("apt") or []))
    pip = sorted(set(pkgs.get("pip") or []))
    for p in apt + pip:
        if str(p).strip().lower() in PLACEHOLDERS:
            raise PolicyError(f"{p!r} is not a package; use an empty list when no packages are needed")
        if not PKG_RE.match(str(p)):
            raise PolicyError(f"invalid package name {p!r}")
    for p in pip:
        if re.split(r"[<>=!~]", p)[0].lower() in sys.stdlib_module_names:
            raise PolicyError(f"{p!r} is part of the Python standard library; do not list it as a package")
    return {"apt": apt, "pip": pip}


class Sandbox:
    def __init__(self, cfg, log=None):
        self.cfg = cfg["sandbox"]
        self.engine = self.cfg["engine"]
        self.log = log

    def _emit(self, kind, **kw):
        if self.log:
            self.log.emit(kind, **kw)

    def _cli(self, args, timeout=60, input=None):
        return subprocess.run([self.engine] + args, capture_output=True, text=True, timeout=timeout, input=input)

    # ------------------------------------------------------------ images
    def image_exists(self, image):
        return self._cli(["image", "exists" if self.engine == "podman" else "inspect", image]).returncode == 0

    def build_base(self):
        r = self._cli(["build", "-t", self.cfg["image"], "-f", os.path.join(SANDBOX_DIR, "Containerfile.base"), SANDBOX_DIR],
                      timeout=900)
        if r.returncode:
            raise RuntimeError("base image build failed:\n" + r.stderr[-3000:])

    @staticmethod
    def image_for(packages):
        pk = validate_packages(packages or {})
        if not pk["apt"] and not pk["pip"]:
            return None
        h = hashlib.sha256(json.dumps(pk, sort_keys=True).encode()).hexdigest()[:12]
        return f"mini-sandbox:pkg-{h}"

    def ensure_image(self, packages):
        """Image for a tool's approved packages. Built from the base image on first use; the build is logged."""
        pk = validate_packages(packages or {})
        image = self.image_for(pk)
        if image is None:
            return self.cfg["image"]
        if self.image_exists(image):
            return image
        lines = [f"FROM {self.cfg['image']}", "USER root"]
        if pk["apt"]:
            lines.append("RUN apt-get update && apt-get install -y --no-install-recommends " + " ".join(pk["apt"]) +
                         " && rm -rf /var/lib/apt/lists/*")
        if pk["pip"]:
            lines.append("RUN pip install --no-cache-dir " + " ".join(pk["pip"]))
        lines.append("USER tool")
        cf = "\n".join(lines) + "\n"
        self._emit("sandbox", action="build_image", image=image, packages=pk, text=cf)
        t0 = time.time()
        r = self._cli(["build", "-t", image, "-f", "-", SANDBOX_DIR], timeout=1200, input=cf)
        self._emit("sandbox", action="build_done", image=image, ok=r.returncode == 0, secs=round(time.time() - t0, 1),
                   text=(r.stdout + r.stderr)[-2500:] if r.returncode else None)
        if r.returncode:
            raise PolicyError(f"package install failed (exit {r.returncode}):\n" + (r.stdout + r.stderr)[-2500:])
        return image

    # ------------------------------------------------------------ network
    def _start_egress(self, targets, rid):
        if self._cli(["network", "exists", self.cfg["service_network"]]).returncode:
            self._cli(["network", "create", self.cfg["service_network"]])
        net = f"mini-sbx-{rid}"
        r = self._cli(["network", "create", "--internal", net])
        if r.returncode:
            raise RuntimeError("cannot create sandbox network: " + r.stderr)
        name = f"mini-egress-{rid}"
        args = ["run", "-d", "--rm", "--name", name, "--network", self.cfg["service_network"], "--network", net,
                "--memory", "128m", "--pids-limit", "128", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--read-only", "--sysctl", "net.ipv4.ip_unprivileged_port_start=0",
                "-v", f"{SANDBOX_DIR}/egress.py:/mini/egress.py:ro", self.cfg["image"],
                "python", "/mini/egress.py", json.dumps(targets)]
        faults = {t: f for t, f in (self.cfg.get("fault_injection") or {}).items() if t in targets}
        if faults:
            args.append(json.dumps(faults))
            self._emit("sandbox", action="fault_injection", faults=faults)
        r = self._cli(args)
        if r.returncode:
            self._cli(["network", "rm", "-f", net])
            raise RuntimeError("cannot start egress forwarder: " + r.stderr)
        ip = None
        for _ in range(50):
            info = self._cli(["inspect", name])
            if info.returncode == 0:
                nets = json.loads(info.stdout)[0]["NetworkSettings"]["Networks"]
                ip = (nets.get(net) or {}).get("IPAddress")
            logs = self._cli(["logs", name]).stdout
            if ip and '"ready"' in logs:
                break
            time.sleep(0.1)
        return net, name, ip

    def _stop_egress(self, net, name):
        logs = self._cli(["logs", name]).stdout if name else ""
        self._cli(["rm", "-f", "-t", "0", name])
        self._cli(["network", "rm", "-f", net])
        events = []
        for line in logs.splitlines():
            try:
                ev = json.loads(line)
                if ev.get("event") != "ready":
                    events.append(ev)
            except ValueError:
                pass
        return events

    # ------------------------------------------------------------ run
    def check_argv(self, argv):
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise PolicyError("command must be a non-empty list of strings (argv), not a shell string")
        if any(c in argv[0] for c in ' "\''):
            raise PolicyError(f"argv[0] is {argv[0][:60]!r}: the whole command is in one string. Put each argument in "
                              f"its own list item, e.g. [\"python\", \"-c\", \"print(1)\"]")
        if argv[0] not in self.cfg["allowed_argv0"]:
            raise PolicyError(f"'{argv[0]}' is not on the sandbox command allowlist: {', '.join(self.cfg['allowed_argv0'])}")
        if argv[0] == "pip" and (len(argv) < 2 or argv[1] not in ("list", "show", "freeze")):
            raise PolicyError("pip may only list packages here; request packages through the spec instead")

    def run(self, argv, work_dir, tool_dir=None, network=(), image=None, timeout=None, purpose="", on_message=None):
        """Run argv in a fresh container. Returns dict(exit_code, output, secs, timed_out, egress).
        on_message(msg) -> reply dict or None: the container's stdout carries JSON-line messages (a tool asking a
        question); each reply goes back on its stdin. Time spent in on_message does not count against the timeout."""
        self.check_argv(argv)
        targets = validate_targets(network)
        rid = uuid.uuid4().hex[:10]
        name = f"mini-run-{rid}"
        timeout = timeout or self.cfg["timeout_s"]
        args = ["run", "--rm", "--name", name, "--read-only", "--tmpfs", "/tmp:rw,size=512m,exec",
                "--memory", self.cfg["memory"], "--memory-swap", self.cfg["memory"], "--cpus", str(self.cfg["cpus"]),
                "--pids-limit", str(self.cfg["pids"]), "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "-e", "HOME=/tmp", "-e", "PYTHONPATH=/tool:/mini",
                "-v", f"{os.path.realpath(work_dir)}:/work:rw", "-v", f"{SANDBOX_DIR}/runner.py:/mini/runner.py:ro",
                "-v", f"{SANDBOX_DIR}/mini_io.py:/mini/mini_io.py:ro", "-w", "/work"]
        if on_message:
            args.append("-i")
        if self.engine == "podman":
            args += ["--userns", "keep-id:uid=1000,gid=1000"]
        if tool_dir:
            args += ["-v", f"{os.path.realpath(tool_dir)}:/tool:ro"]
        egress = None
        if targets:
            net, ename, ip = self._start_egress(targets, rid)
            egress = (net, ename)
            args += ["--network", net, "--dns", "127.0.0.1"]
            for host in sorted({t.rsplit(":", 1)[0] for t in targets}):
                args += ["--add-host", f"{host}:{ip}"]
        else:
            args += ["--network", "none"]
        args += [image or self.cfg["image"]] + argv
        self._emit("sandbox", action="run", purpose=purpose, argv=argv, network=targets or "none",
                   image=image or self.cfg["image"], memory=self.cfg["memory"])
        t0 = time.time()
        timed_out = False
        try:
            if on_message:
                code, out, timed_out = self._run_live(args, timeout, on_message)
                if timed_out:
                    self._cli(["rm", "-f", "-t", "0", name])
                    out += f"\n[sandbox] killed after {timeout} s timeout"
            else:
                r = self._cli(args, timeout=timeout)
                code, out = r.returncode, (r.stdout + ("\n" + r.stderr if r.stderr else ""))
        except subprocess.TimeoutExpired as e:
            self._cli(["rm", "-f", "-t", "0", name])
            timed_out, code = True, -1
            out = ((e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""))
            out += f"\n[sandbox] killed after {timeout} s timeout"
        egress_events = self._stop_egress(*egress) if egress else []
        if code == 137 and not timed_out:
            out += f"\n[sandbox] exit 137: killed, most likely by the {self.cfg['memory']} memory limit"
        res = {"exit_code": code, "output": out, "secs": round(time.time() - t0, 2), "timed_out": timed_out,
               "egress": egress_events, "network_note": network_diagnosis(out, egress_events, targets)}
        self._emit("sandbox", action="exit", purpose=purpose, exit_code=code, secs=res["secs"], timed_out=timed_out,
                   egress=egress_events or None)
        return res

    def _run_live(self, args, timeout, on_message):
        """Run with a JSON-line message channel on stdin/stdout. Returns (exit code, output, timed out)."""
        p = subprocess.Popen([self.engine] + args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, bufsize=1)
        err, plain, waited = [], [], [0.0]
        threading.Thread(target=lambda: err.extend(p.stderr), daemon=True).start()
        t0 = time.time()
        done = threading.Event()

        def watch():                                   # the timeout clock stops while the user answers
            while not done.wait(0.5):
                if time.time() - t0 - waited[0] > timeout:
                    p.kill()
                    return
        threading.Thread(target=watch, daemon=True).start()
        for line in p.stdout:
            try:
                msg = json.loads(line)
                if not isinstance(msg, dict) or "op" not in msg:
                    raise ValueError
            except ValueError:
                plain.append(line)
                continue
            tw = time.time()
            reply = on_message(msg)
            waited[0] += time.time() - tw
            if reply is not None:
                try:
                    p.stdin.write(json.dumps(reply) + "\n")
                    p.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
        code = p.wait()
        done.set()
        timed_out = time.time() - t0 - waited[0] > timeout
        return (-1 if timed_out else code), "".join(plain) + ("\n" + "".join(err) if err else ""), timed_out

    def invoke_tool(self, manifest, tool_dir, work_dir, inputs, timeout=None, on_message=None, settings=None,
                    network=None):
        """Run a registered code tool on inputs. Returns (output dict, sandbox result)."""
        meta = os.path.join(work_dir, ".mini")
        os.makedirs(meta, exist_ok=True)
        with open(os.path.join(meta, "input.json"), "w") as f:
            json.dump(inputs, f)
        outp, setp = os.path.join(meta, "output.json"), os.path.join(meta, "settings.json")
        if os.path.exists(outp):
            os.remove(outp)
        perms = manifest.get("permissions") or {}
        image = self.ensure_image(perms.get("packages"))
        if network is None:
            network = [t for t in perms.get("network") or [] if not t.startswith("setting:")]
        if settings:                                   # only the settings the manifest declares; removed after the run
            fd = os.open(setp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(settings, f)
        try:
            res = self.run(["python", "/mini/runner.py"], work_dir, tool_dir=tool_dir, network=network, image=image,
                           timeout=timeout, purpose=f"invoke {manifest['name']} v{manifest.get('version', '?')}",
                           on_message=on_message or (lambda msg: {"error": "no one to ask: pass this value in the inputs"}
                                                     if msg.get("op") == "ask" else None))
        finally:
            if os.path.exists(setp):
                os.remove(setp)
        try:
            with open(outp) as f:
                out = json.load(f)
        except (OSError, ValueError):
            out = {"error": "NoOutput", "message": "the tool produced no output.json", "sandbox_output": res["output"][-2000:]}
        return out, res
