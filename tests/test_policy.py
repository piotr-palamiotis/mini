"""Sandbox policy: argv allowlist, undeclared network and paths are unreachable, no host secrets.
Needs podman and mini-sandbox:base. The test starts its own service container (svc, ports 1025 and 8025) on the
service network, standing in for a local service the user runs."""
import os
import subprocess
import tempfile
import unittest

from mini.harness.sandbox import PolicyError, Sandbox, validate_targets
from tests.helpers import temp_config

PROBE = r'''
import os, socket, sys
def tcp(h, p):
    try:
        socket.create_connection((h, p), timeout=4).close(); return "OPEN"
    except Exception as e:
        return "BLOCKED"
res = {}
res["svc:1025"] = tcp("svc", 1025)
res["svc:8025"] = tcp("svc", 8025)
res["example.com:443"] = tcp("example.com", 443)
res["1.1.1.1:53"] = tcp("1.1.1.1", 53)
res["10.0.2.2:11434"] = tcp("10.0.2.2", 11434)
res["host.containers.internal:11434"] = tcp("host.containers.internal", 11434)
res["env"] = sorted(k for k in os.environ if "SECRET" in k or "ANTHROPIC" in k or "CLAUDE" in k)
res["home_entries"] = sorted(os.listdir("/home"))
res["claude_dir"] = os.path.exists("/home/xxx/.claude") or os.path.exists(os.path.expanduser("~/.claude"))
for p in ["/mnt/disk_a", "/home/xxx", "/etc/shadow"]:
    try:
        os.listdir(p) if os.path.isdir(p) else open(p).read(1); res[p] = "READABLE"
    except Exception:
        res[p] = "DENIED"
try:
    open("/usr/x", "w"); res["rootfs"] = "WRITABLE"
except Exception:
    res["rootfs"] = "READ-ONLY"
import json; print("RESULT" + json.dumps(res))
'''


def run_probe(sb, network):
    w = tempfile.mkdtemp(prefix="mini-probe-")
    open(os.path.join(w, "probe.py"), "w").write(PROBE)
    os.environ["SECRET_TOKEN_FOR_TEST"] = "x"
    r = sb.run(["python", "/work/probe.py"], w, network=network, purpose="policy test")
    import json
    line = [l for l in r["output"].splitlines() if l.startswith("RESULT")][0]
    return json.loads(line[6:]), r


class PolicyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cfg = temp_config()
        cls.sb = Sandbox(cfg)
        net = cfg["sandbox"]["service_network"]
        if subprocess.run(["podman", "network", "exists", net]).returncode:
            subprocess.run(["podman", "network", "create", net], check=True, capture_output=True)
        subprocess.run(["podman", "rm", "-f", "svc"], capture_output=True)
        subprocess.run(["podman", "run", "-d", "--rm", "--name", "svc", "--network", net, cfg["sandbox"]["image"], "python", "-c",
                        "import socket,threading,time\n"
                        "def srv(p):\n"
                        "    s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind(('',p)); s.listen()\n"
                        "    while True: s.accept()[0].close()\n"
                        "for p in (1025,8025): threading.Thread(target=srv,args=(p,),daemon=True).start()\n"
                        "time.sleep(3600)"], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["podman", "rm", "-f", "-t", "0", "svc"], capture_output=True)

    def test_argv_allowlist(self):
        for bad in ("ls -R", ["bash", "-c", "id"], ["curl", "x"], ["sh"], [], ["pip", "install", "x"]):
            with self.assertRaises(PolicyError):
                self.sb.check_argv(bad)
        self.sb.check_argv(["python", "-c", "print('a; b && c')"])     # quotes are data, never split

    def test_target_validation(self):
        self.assertEqual(validate_targets(["https://Api.Example.org:443/"]), ["api.example.org:443"])
        for bad in ("example.org", "a b:1", "x:99999999", "*:443"):
            with self.assertRaises(PolicyError):
                validate_targets([bad])

    def test_no_network_by_default(self):
        res, r = run_probe(self.sb, [])
        for k in ("svc:1025", "svc:8025", "example.com:443", "1.1.1.1:53", "10.0.2.2:11434",
                  "host.containers.internal:11434"):
            self.assertEqual(res[k], "BLOCKED", k)
        self.assertEqual(r["egress"], [])
        self._no_host_access(res)

    def test_only_declared_targets(self):
        res, r = run_probe(self.sb, ["svc:1025"])
        self.assertEqual(res["svc:1025"], "OPEN")
        for k in ("svc:8025", "example.com:443", "1.1.1.1:53", "10.0.2.2:11434", "host.containers.internal:11434"):
            self.assertEqual(res[k], "BLOCKED", k)
        self.assertTrue(any(e["event"] == "allow" and e["dest"] == "svc" for e in r["egress"]))
        self._no_host_access(res)

    def _no_host_access(self, res):
        self.assertEqual(res["env"], [])
        self.assertEqual(res["home_entries"], ["tool"])
        self.assertFalse(res["claude_dir"])
        self.assertEqual(res["/mnt/disk_a"], "DENIED")
        self.assertEqual(res["/home/xxx"], "DENIED")
        self.assertEqual(res["/etc/shadow"], "DENIED")
        self.assertEqual(res["rootfs"], "READ-ONLY")

    def test_sni_routing_rejects_undeclared_name_on_shared_port(self):
        # two declared hosts on 443: the forwarder routes by SNI; a third name mapped by hand must be refused
        w = tempfile.mkdtemp(prefix="mini-probe-")
        open(os.path.join(w, "p.py"), "w").write(
            "import ssl,socket,urllib.request\n"
            "print('A', urllib.request.urlopen('https://api.open-meteo.com/v1/forecast?latitude=1&longitude=1', timeout=10).status)\n"
            "ip = socket.gethostbyname('api.open-meteo.com')\n"
            "ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE\n"
            "try:\n"
            "    s = ctx.wrap_socket(socket.create_connection((ip, 443), timeout=5), server_hostname='example.com')\n"
            "    print('B OPEN')\n"
            "except Exception as e:\n"
            "    print('B BLOCKED', type(e).__name__)\n")
        r = self.sb.run(["python", "/work/p.py"], w, network=["api.open-meteo.com:443", "geocoding-api.open-meteo.com:443"])
        self.assertIn("A 200", r["output"])
        self.assertIn("B BLOCKED", r["output"])
        self.assertTrue(any(e["event"] == "deny" and e["dest"] == "example.com" for e in r["egress"]))


if __name__ == "__main__":
    unittest.main()
