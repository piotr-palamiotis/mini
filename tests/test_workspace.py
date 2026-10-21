"""The shared folder: read-only /host, changes wait in out/ until they are applied."""
import json
import os
import tempfile
import unittest
from unittest import mock

from mini.agent.core import Agent
from mini.harness import workspace as ws
from mini.harness.sandbox import PolicyError, Sandbox
from tests.helpers import temp_config


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="mini-ws-"))
        os.makedirs(os.path.join(self.root, "src"))
        open(os.path.join(self.root, "src", "a.py"), "w").write("print(1)\n")
        open(os.path.join(self.root, "notes.txt"), "w").write("hello\n")
        self.cfg = temp_config()

    def agent(self):
        return Agent(self.cfg, label="ws", workspace=self.root)

    def test_resolve_refuses_broad_folders(self):
        self.assertEqual(ws.resolve(self.cfg, self.root), (self.root, ""))
        d, why = ws.resolve(self.cfg, os.path.expanduser("~"))
        self.assertIsNone(d)
        self.assertIn("too broad", why)
        self.assertIsNone(ws.resolve(self.cfg, "/")[0])
        self.cfg["workspace"]["enabled"] = False
        self.assertEqual(ws.resolve(self.cfg, self.root), (None, ""))

    def test_paths_cannot_leave_the_shared_folder(self):
        os.symlink("/etc", os.path.join(self.root, "link"))
        self.assertEqual(ws.host_path(self.root, "/host/src/a.py"), os.path.join(self.root, "src", "a.py"))
        for bad in ("/host/../etc/passwd", "/host/link/passwd", "/etc/passwd", "src/a.py"):
            with self.assertRaises(PolicyError, msg=bad):
                ws.host_path(self.root, bad)
        with self.assertRaises(PolicyError):
            ws.host_path(None, "/host/src")

    def test_agent_lists_and_reads_the_shared_folder(self):
        a = self.agent()
        top = a._list({"path": "/host"})
        self.assertIn("/host: 1 folders, 1 files", top)
        self.assertIn("src/ (1 items)", top)
        self.assertIn("notes.txt (6 B)", top)
        self.assertNotIn("a.py", top, "depth 1 does not open subfolders")
        deep = a._list({"path": "/host", "depth": 2})
        self.assertIn("  a.py (9 B)", deep)
        self.assertIn("a.py", a._list({"path": "/host", "recursive": True}), "recursive still means a little deeper")
        self.assertEqual(a._read({"path": "/host/notes.txt"}), "hello\n")
        self.assertIn("use list_files", a._read({"path": "/host/src"}))
        self.assertIn("Cannot read", a._read({"path": "/host/../etc/passwd"}))
        self.assertIn("/host/src", a.context())
        self.assertNotIn("/host", Agent(self.cfg, label="none").context(), "no shared folder, no mention")

    def test_a_big_folder_listing_stays_small(self):
        big = os.path.join(self.root, "big")
        for i in range(400):
            os.makedirs(os.path.join(big, f"dir{i:03d}", "deeper"))
            open(os.path.join(big, f"file{i:03d}.txt"), "w").write("x" * i)
        a = self.agent()
        for depth in (1, 2, 3):
            out = a._list({"path": "/host/big", "depth": depth})
            self.assertLessEqual(len(out), ws.MAX_CHARS + 300, depth)
            self.assertLessEqual(len(out.splitlines()), ws.MAX_LINES + 3, depth)
        out = a._list({"path": "/host/big"})
        self.assertIn("400 folders, 400 files", out)
        self.assertIn("more lines hidden. List a subfolder", out)
        self.assertIn("dir000/ (1 items)", out)
        os.makedirs(os.path.join(self.root, "pkg", ".git"))
        self.assertIn(".git/ (0 items, not expanded)", a._list({"path": "/host/pkg", "depth": 2}))

    def test_long_files_are_read_in_parts(self):
        open(os.path.join(self.root, "long.txt"), "w").write("a" * 8000 + "b" * 100)
        a = self.agent()
        first = a._read({"path": "/host/long.txt"})
        self.assertTrue(first.startswith("a" * 8000))
        self.assertIn("read_file with offset=8000", first)
        rest = a._read({"path": "/host/long.txt", "offset": 8000})
        self.assertTrue(rest.startswith("b" * 100))
        self.assertIn("the end", rest)

    def test_writes_stay_in_the_scratch_folder_and_apply_on_request(self):
        a = self.agent()
        a.log.emit("run_start", workspace=self.root)
        self.assertIn("Wrote out/src/a.py", a._write({"path": "out/src/a.py", "content": "print(2)\n"}, ""))
        a._write({"path": "out/new/b.txt", "content": "b\n"}, "")
        a._write({"path": "out/notes.txt", "content": "hello\n"}, "")                # identical: nothing to apply
        self.assertEqual(open(os.path.join(self.root, "src", "a.py")).read(), "print(1)\n", "not touched by the write")
        items = {rel: (st, diff) for rel, st, diff in ws.pending(a.run_dir)}
        self.assertEqual(sorted(items), ["new/b.txt", "src/a.py"])
        self.assertEqual(items["src/a.py"][0], "changed")
        self.assertIn("-print(1)", items["src/a.py"][1])
        self.assertIn("+print(2)", items["src/a.py"][1])
        self.assertEqual(items["new/b.txt"][0], "new")
        self.assertEqual(sorted(ws.apply(a.run_dir)), ["new/b.txt", "src/a.py"])
        self.assertEqual(open(os.path.join(self.root, "src", "a.py")).read(), "print(2)\n")
        self.assertEqual(open(os.path.join(self.root, "new", "b.txt")).read(), "b\n")
        self.assertEqual(ws.pending(a.run_dir), [], "applied: nothing left")

    def test_apply_refuses_to_follow_a_symlink_out(self):
        a = self.agent()
        a.log.emit("run_start", workspace=self.root)
        outside = tempfile.mkdtemp(prefix="mini-outside-")
        os.symlink(outside, os.path.join(self.root, "escape"))
        a._write({"path": "out/escape/x.txt", "content": "x"}, "")
        self.assertEqual([(r, s) for r, s, _ in ws.pending(a.run_dir)], [("escape/x.txt", "refused")])
        self.assertEqual(ws.apply(a.run_dir), [])
        self.assertEqual(os.listdir(outside), [])

    def test_sandbox_mounts_the_folder_read_only(self):
        sb = Sandbox(self.cfg)
        for folder, want in ((self.root, True), (None, False)):
            with mock.patch.object(Sandbox, "_cli") as cli:
                cli.return_value = mock.Mock(returncode=0, stdout="", stderr="")
                sb.run(["python", "-c", "1"], tempfile.mkdtemp(), workspace=folder)
            args = cli.call_args[0][0]
            self.assertEqual(f"{self.root}:/host:ro" in args, want)


if __name__ == "__main__":
    unittest.main()
