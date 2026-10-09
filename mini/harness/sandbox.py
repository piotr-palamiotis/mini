"""Sandbox: every piece of generated code runs here, never on the host.

Rootless Podman (or rootless Docker). Per run:
  - read-only root filesystem, /tmp as tmpfs, all capabilities dropped, no-new-privileges
  - memory, CPU and PID limits; wall-clock timeout
  - tool files mounted read-only at /tool, the per-run scratch directory read-write at /work, nothing else
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


def validate_packages(pkgs):
    apt = sorted(set(pkgs.get("apt") or []))
    pip = sorted(set(pkgs.get("pip") or []))
    for p in apt + pip:
        if not PKG_RE.match(str(p)):
            raise PolicyError(f"invalid package name {p!r}")
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
        if argv[0] not in self.cfg["allowed_argv0"]:
            raise PolicyError(f"'{argv[0]}' is not on the sandbox command allowlist: {', '.join(self.cfg['allowed_argv0'])}")
        if argv[0] == "pip" and (len(argv) < 2 or argv[1] not in ("list", "show", "freeze")):
            raise PolicyError("pip may only list packages here; request packages through the spec instead")

    def run(self, argv, work_dir, tool_dir=None, network=(), image=None, timeout=None, purpose=""):
        """Run argv in a fresh container. Returns dict(exit_code, output, secs, timed_out, egress)."""
        self.check_argv(argv)
        targets = validate_targets(network)
        rid = uuid.uuid4().hex[:10]
        name = f"mini-run-{rid}"
        timeout = timeout or self.cfg["timeout_s"]
        args = ["run", "--rm", "--name", name, "--read-only", "--tmpfs", "/tmp:rw,size=512m,exec",
                "--memory", self.cfg["memory"], "--memory-swap", self.cfg["memory"], "--cpus", str(self.cfg["cpus"]),
                "--pids-limit", str(self.cfg["pids"]), "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "-e", "HOME=/tmp", "-e", "PYTHONPATH=/tool",
                "-v", f"{os.path.realpath(work_dir)}:/work:rw", "-v", f"{SANDBOX_DIR}/runner.py:/mini/runner.py:ro",
                "-w", "/work"]
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
               "egress": egress_events}
        self._emit("sandbox", action="exit", purpose=purpose, exit_code=code, secs=res["secs"], timed_out=timed_out,
                   egress=egress_events or None)
        return res

    def invoke_tool(self, manifest, tool_dir, work_dir, inputs, timeout=None):
        """Run a registered code tool on inputs. Returns (output dict, sandbox result)."""
        meta = os.path.join(work_dir, ".mini")
        os.makedirs(meta, exist_ok=True)
        json.dump(inputs, open(os.path.join(meta, "input.json"), "w"))
        outp = os.path.join(meta, "output.json")
        if os.path.exists(outp):
            os.remove(outp)
        perms = manifest.get("permissions") or {}
        image = self.ensure_image(perms.get("packages"))
        res = self.run(["python", "/mini/runner.py"], work_dir, tool_dir=tool_dir, network=perms.get("network") or [],
                       image=image, timeout=timeout, purpose=f"invoke {manifest['name']} v{manifest.get('version', '?')}")
        try:
            out = json.load(open(outp))
        except (OSError, ValueError):
            out = {"error": "NoOutput", "message": "the tool produced no output.json", "sandbox_output": res["output"][-2000:]}
        return out, res
