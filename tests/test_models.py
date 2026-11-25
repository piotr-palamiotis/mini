"""Model downloads (mini.models) against a fake Hugging Face (a local HTTP server that honours Range): what a repo
offers, where files go, the background job, resume from a .part file, the sha256 check, cancel and resume."""
import hashlib
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from mini import models
from tests.helpers import temp_config

BLOB = {"m-Q4_K_M.gguf": os.urandom(300_000), "m-Q8_0-00001-of-00002.gguf": os.urandom(200_000),
        "m-Q8_0-00002-of-00002.gguf": os.urandom(100_000), "mmproj-F16.gguf": os.urandom(50_000),
        "mtp-m-Q4_0.gguf": os.urandom(1000)}


class Hub(BaseHTTPRequestHandler):
    seen = []                                # (path, Range header) of every file request
    honour_range = True

    def do_GET(self):
        if self.path.startswith("/api/models/o/m/tree/main"):
            rows = [{"type": "file", "path": n, "size": len(b), "lfs": {"oid": hashlib.sha256(b).hexdigest()}}
                    for n, b in BLOB.items()] + [{"type": "file", "path": "README.md", "size": 10}]
            return self._send(200, json.dumps(rows).encode())
        if self.path.startswith("/api/models/"):
            return self._send(401, b"{}")
        name = self.path.rsplit("/", 1)[-1]
        if name not in BLOB:
            return self._send(404, b"")
        rng = self.headers.get("Range")
        Hub.seen.append((name, rng))
        body = BLOB[name]
        if rng and Hub.honour_range:
            start = int(rng.split("=")[1].rstrip("-"))
            return self._send(206, body[start:])
        self._send(200, body)

    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class ModelsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Hub)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.hub = mock.patch.object(models, "HUB", f"http://127.0.0.1:{cls.srv.server_port}")
        cls.hub.start()

    @classmethod
    def tearDownClass(cls):
        cls.hub.stop()
        cls.srv.shutdown()

    def setUp(self):
        Hub.seen, Hub.honour_range = [], True
        self.cfg = temp_config()
        self.cfg["llamacpp"]["models_dir"] = tempfile.mkdtemp(prefix="mini-models-")
        self.options, self.proj = models.choices(models.tree("o/m"))

    def run_job(self, option, name=None, mmproj=None):
        """Plan and run a job in this process (the real start() runs the same worker in a unit of its own)."""
        job = models.start(self.cfg, models.plan(self.cfg, "o/m", option, name, mmproj), runner=lambda argv, j: None)
        return models.worker(models.job_path(self.cfg, job["id"]))

    def test_what_a_repo_offers(self):
        self.assertEqual([(o["quant"], len(o["files"]), o["size"]) for o in self.options],
                         [("Q4_K_M", 1, 300_000), ("Q8_0", 2, 300_000)])
        self.assertEqual([f["path"] for f in self.proj], ["mmproj-F16.gguf"])
        self.assertEqual(models.pick(self.options, "q8_0")["name"], "m-Q8_0", "by quantization, any case")
        with self.assertRaises(models.DownloadError):
            models.pick(self.options, "Q2_K")
        with self.assertRaises(models.DownloadError) as e:
            models.tree("o/private")
        self.assertIn("login", str(e.exception))

    def test_one_file_goes_in_as_name_gguf(self):
        job = self.run_job(self.options[0])
        self.assertEqual(job["status"], "done", job["error"])
        dest = os.path.join(self.cfg["llamacpp"]["models_dir"], "m-Q4_K_M.gguf")
        self.assertEqual(open(dest, "rb").read(), BLOB["m-Q4_K_M.gguf"])
        self.assertFalse([f for f in os.listdir(self.cfg["llamacpp"]["models_dir"]) if f.endswith(".part")])

    def test_split_parts_and_vision_go_in_a_folder(self):
        job = self.run_job(self.options[1], name="big", mmproj=self.proj[0])
        self.assertEqual(job["status"], "done", job["error"])
        folder = os.path.join(self.cfg["llamacpp"]["models_dir"], "big")
        self.assertEqual(sorted(os.listdir(folder)), ["m-Q8_0-00001-of-00002.gguf", "m-Q8_0-00002-of-00002.gguf",
                                                      "mmproj-big.gguf"])
        from mini.setup import models_in
        m = models_in(self.cfg["llamacpp"]["models_dir"])["big"]
        self.assertTrue(m["file"].endswith("00001-of-00002.gguf") and m["mmproj"].endswith("mmproj-big.gguf"))

    def test_a_part_file_is_resumed_with_a_range(self):
        job = models.plan(self.cfg, "o/m", self.options[0])
        part = job["files"][0]["dest"] + ".part"
        open(part, "wb").write(BLOB["m-Q4_K_M.gguf"][:120_000])
        job = models.start(self.cfg, job, runner=lambda argv, j: None)
        self.assertEqual(job["done"], 120_000, "what is already there counts")
        job = models.worker(models.job_path(self.cfg, job["id"]))
        self.assertEqual(job["status"], "done", job["error"])
        self.assertEqual(Hub.seen, [("m-Q4_K_M.gguf", "bytes=120000-")])
        self.assertEqual(open(job["files"][0]["dest"], "rb").read(), BLOB["m-Q4_K_M.gguf"])

    def test_a_server_that_ignores_the_range_gives_the_whole_file(self):
        Hub.honour_range = False
        job = models.plan(self.cfg, "o/m", self.options[0])
        open(job["files"][0]["dest"] + ".part", "wb").write(b"x" * 1000)
        job = models.worker(models.job_path(self.cfg, models.start(self.cfg, job, runner=lambda a, j: None)["id"]))
        self.assertEqual(job["status"], "done", job["error"])
        self.assertEqual(open(job["files"][0]["dest"], "rb").read(), BLOB["m-Q4_K_M.gguf"])

    def test_a_damaged_download_is_removed_not_installed(self):
        job = models.plan(self.cfg, "o/m", self.options[0])
        job["files"][0]["sha256"] = "0" * 64
        job = models.worker(models.job_path(self.cfg, models.start(self.cfg, job, runner=lambda a, j: None)["id"]))
        self.assertEqual(job["status"], "failed")
        self.assertIn("damaged", job["error"])
        self.assertEqual(os.listdir(self.cfg["llamacpp"]["models_dir"]), [])

    def test_network_errors_are_retried(self):
        real, calls = models.urllib.request.urlopen, []

        def flaky(req, timeout=None):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("connection reset")
            return real(req, timeout=timeout)
        job = models.plan(self.cfg, "o/m", self.options[0])
        path = models.job_path(self.cfg, models.start(self.cfg, job, runner=lambda a, j: None)["id"])
        with mock.patch.object(models.time, "sleep"):
            job = models.worker(path, opener=flaky)
        self.assertEqual((job["status"], len(calls)), ("done", 2), job["error"])

    def test_refused_up_front(self):
        self.run_job(self.options[0])
        with self.assertRaises(models.DownloadError) as e:
            models.plan(self.cfg, "o/m", self.options[0])
        self.assertIn("already in the models folder", str(e.exception))
        with mock.patch.object(models.shutil, "disk_usage", return_value=mock.Mock(free=1000)), \
                self.assertRaises(models.DownloadError) as e:
            models.plan(self.cfg, "o/m", self.options[1])
        self.assertIn("not enough disk space", str(e.exception))
        with self.assertRaises(models.DownloadError):
            models.plan(self.cfg, "o/m", self.options[1], name="../evil")

    def test_a_job_whose_process_is_gone_shows_stopped_and_resumes(self):
        job = models.start(self.cfg, models.plan(self.cfg, "o/m", self.options[0]), runner=lambda a, j: None)
        j = models.load_job(models.job_path(self.cfg, job["id"]))
        j.update(status="running", pid=999_999_999)
        models.save_job(models.job_path(self.cfg, job["id"]), j)
        self.assertEqual(models.find_job(self.cfg, job["id"])["status"], "stopped")
        started = []
        models.resume(self.cfg, job["name"], runner=lambda argv, j: started.append(argv))
        self.assertEqual(started[0][-3:], ["mini.models", "worker", models.job_path(self.cfg, job["id"])])

    def test_cancel_with_discard_deletes_the_part(self):
        job = models.start(self.cfg, models.plan(self.cfg, "o/m", self.options[0]), runner=lambda a, j: None)
        part = job["files"][0]["dest"] + ".part"
        open(part, "wb").write(b"x")
        j = models.cancel(self.cfg, job["id"], discard=True)
        self.assertEqual(j["status"], "cancelled")
        self.assertFalse(os.path.exists(part))

    def test_start_runs_the_worker_as_a_user_unit(self):
        calls = []
        job = models.plan(self.cfg, "o/m", self.options[0])
        with mock.patch.object(models.shutil, "which", return_value="/usr/bin/systemd-run"), \
                mock.patch.object(models.subprocess, "run",
                                  side_effect=lambda cmd, **k: calls.append(cmd) or mock.Mock(returncode=0, stderr="")):
            job = models.start(self.cfg, job)
        self.assertEqual(calls[0][:3], ["systemd-run", "--user", "--quiet"])
        self.assertIn("--collect", calls[0])
        self.assertEqual(job["unit"], models.UNIT_PREFIX + job["id"])


if __name__ == "__main__":
    unittest.main()
