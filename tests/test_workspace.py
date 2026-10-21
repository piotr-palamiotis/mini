"""The folder mini is launched in is the workspace: the agent works on it in place, tools get copies of the files a
call names and their writes are exported back, confined to the folder, checkpointed in git first."""
import os
import tempfile
import unittest
from unittest import mock

from mini.agent.core import Agent
from mini.harness import checkpoint as ckpt
from mini.harness import workspace as ws
from mini.harness.registry import Registry
from mini.harness.sandbox import PolicyError, Sandbox
from tests.helpers import git, git_folder, temp_config


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.root = git_folder({"src/a.py": "print(1)\n", "notes.txt": "hello\n"})
        self.cfg = temp_config()

    def agent(self):
        return Agent(self.cfg, label="ws", workspace=self.root)

    def test_resolve_refuses_broad_folders(self):
        self.assertEqual(ws.resolve(self.cfg, self.root), (self.root, ""))
        d, why = ws.resolve(self.cfg, os.path.expanduser("~"))
        self.assertIsNone(d)
        self.assertIn("too broad", why)
        self.assertIsNone(ws.resolve(self.cfg, "/")[0])

    def test_paths_cannot_leave_the_folder(self):
        os.symlink("/etc", os.path.join(self.root, "link"))
        self.assertEqual(ws.confine(self.root, "src/a.py"), (os.path.join(self.root, "src", "a.py"), "src/a.py"))
        self.assertEqual(ws.confine(self.root, os.path.join(self.root, "notes.txt"))[1], "notes.txt")
        for bad in ("../etc/passwd", "link/passwd", "/etc/passwd", ".git/config", "src/../.git/HEAD"):
            with self.assertRaises(PolicyError, msg=bad):
                ws.confine(self.root, bad)

    def test_agent_lists_and_reads_the_folder(self):
        a = self.agent()
        top = a._list({"path": "."})
        self.assertIn(".: 3 folders, 1 files", top, "src/, .git/ and .mini/")
        self.assertIn("src/ (1 items)", top)
        self.assertIn("notes.txt (6 B)", top)
        self.assertNotIn("a.py", top, "depth 1 does not open subfolders")
        deep = a._list({"path": ".", "depth": 2})
        self.assertIn("  a.py (9 B)", deep)
        self.assertIn("a.py", a._list({"path": ".", "recursive": True}), "recursive still means a little deeper")
        self.assertEqual(a._read({"path": "notes.txt"}), "hello\n")
        self.assertIn("use list_files", a._read({"path": "src"}))
        self.assertIn("Cannot read", a._read({"path": "../etc/passwd"}))
        self.assertIn("Cannot read", a._read({"path": ".git/config"}))
        self.assertIn(f"You work in the user's folder {self.root}", a.context())
        self.assertNotIn("user's folder", Agent(self.cfg, label="none").context(), "no folder, no mention")

    def test_a_big_folder_listing_stays_small(self):
        big = os.path.join(self.root, "big")
        for i in range(400):
            os.makedirs(os.path.join(big, f"dir{i:03d}", "deeper"))
            open(os.path.join(big, f"file{i:03d}.txt"), "w").write("x" * i)
        a = self.agent()
        for depth in (1, 2, 3):
            out = a._list({"path": "big", "depth": depth})
            self.assertLessEqual(len(out), ws.MAX_CHARS + 300, depth)
            self.assertLessEqual(len(out.splitlines()), ws.MAX_LINES + 3, depth)
        out = a._list({"path": "big"})
        self.assertIn("400 folders, 400 files", out)
        self.assertIn("more lines hidden. List a subfolder", out)
        self.assertIn("dir000/ (1 items)", out)
        os.makedirs(os.path.join(self.root, "pkg", ".git"))
        self.assertIn(".git/ (0 items, not expanded)", a._list({"path": "pkg", "depth": 2}))

    def test_long_files_are_read_in_parts(self):
        open(os.path.join(self.root, "long.txt"), "w").write("a" * 8000 + "b" * 100)
        a = self.agent()
        first = a._read({"path": "long.txt"})
        self.assertTrue(first.startswith("a" * 8000))
        self.assertIn("read_file with offset=8000", first)
        rest = a._read({"path": "long.txt", "offset": 8000})
        self.assertTrue(rest.startswith("b" * 100))
        self.assertIn("the end", rest)

    def test_writes_happen_in_place_after_a_checkpoint(self):
        open(os.path.join(self.root, "notes.txt"), "w").write("dirty, not committed\n")
        a = self.agent()
        self.assertIn("Wrote src/a.py", a._write({"path": "src/a.py", "content": "print(2)\n"}, ""))
        a._write({"path": "new/b.txt", "content": "b\n"}, "")
        self.assertEqual(open(os.path.join(self.root, "src", "a.py")).read(), "print(2)\n")
        self.assertEqual(open(os.path.join(self.root, "new", "b.txt")).read(), "b\n")
        self.assertEqual(a.produced_files(), ["new/b.txt", "src/a.py"])
        self.assertEqual(a.checkpoint.ref, f"refs/mini/{a.log.run_id}")
        self.assertEqual(sorted(ckpt.task_files(self.root, a.log.run_id)), ["new/b.txt", "src/a.py"])
        self.assertIn("inside .git", a._write({"path": ".git/hooks/x", "content": "x"}, ""))
        self.assertIn("outside the folder", a._write({"path": "../x.txt", "content": "x"}, ""))
        ckpt.undo(self.root, a.log.run_id)
        self.assertEqual(open(os.path.join(self.root, "src", "a.py")).read(), "print(1)\n")
        self.assertFalse(os.path.exists(os.path.join(self.root, "new", "b.txt")))
        self.assertEqual(open(os.path.join(self.root, "notes.txt")).read(), "dirty, not committed\n")

    def test_no_repository_no_change(self):
        plain = os.path.realpath(tempfile.mkdtemp(prefix="mini-plain-"))
        a = Agent(self.cfg, label="plain", workspace=plain)
        out = a._write({"path": "a.txt", "content": "x"}, "")
        self.assertIn("not a git repository", out)
        self.assertEqual(os.listdir(plain), [])
        self.assertFalse(os.path.exists(os.path.join(plain, ".git")))


class CopyInExportTest(unittest.TestCase):
    def setUp(self):
        self.root = git_folder({"src/a.py": "print(1)\n", "notes.txt": "hello\n", "data/x.csv": "1,2\n"})
        self.box = tempfile.mkdtemp(prefix="mini-box-")

    def test_named_files_are_copied_at_their_relative_path(self):
        inputs = {"file": "src/a.py", "files": ["notes.txt", os.path.join(self.root, "data")], "out": "report.md",
                  "abs_out": os.path.join(self.root, "r.md"), "n": 3, "text": "just words"}
        got = ws.copy_in(self.root, self.box, inputs)
        self.assertEqual(got, {"file": "src/a.py", "files": ["notes.txt", "data"], "out": "report.md",
                               "abs_out": "r.md", "n": 3, "text": "just words"})
        self.assertEqual(open(os.path.join(self.box, "src", "a.py")).read(), "print(1)\n")
        self.assertTrue(os.path.isfile(os.path.join(self.box, "data", "x.csv")))
        self.assertFalse(os.path.exists(os.path.join(self.box, "r.md")))

    def test_the_sandbox_never_sees_git(self):
        self.assertEqual(ws.copy_in(self.root, self.box, {"all": "."}), {"all": "."})
        self.assertTrue(os.path.isfile(os.path.join(self.box, "notes.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.box, ".git")))
        with self.assertRaises(PolicyError):
            ws.copy_in(self.root, self.box, {"f": ".git/config"})

    def test_files_outside_the_folder_only_when_attached(self):
        src = os.path.join(tempfile.mkdtemp(), "att.txt")
        open(src, "w").write("a")
        with self.assertRaises(PolicyError):
            ws.copy_in(self.root, self.box, {"f": src})
        self.assertEqual(ws.copy_in(self.root, self.box, {"f": src}, outside=[os.path.realpath(src)]), {"f": ".mini/in/att.txt"})
        os.symlink("/etc/hostname", os.path.join(self.root, "leak"))
        with self.assertRaises(PolicyError, msg="a link in the folder that leads out"):
            ws.copy_in(self.root, self.box, {"f": "leak"})

    def test_only_what_the_tool_wrote_goes_back(self):
        ws.copy_in(self.root, self.box, {"f": "src/a.py", "g": "notes.txt"})
        open(os.path.join(self.box, "src", "a.py"), "w").write("print(2)\n")         # an edit in place
        os.makedirs(os.path.join(self.box, "out"))
        open(os.path.join(self.box, "out", "new.txt"), "w").write("new\n")           # a new file
        os.makedirs(os.path.join(self.box, ".mini"))
        open(os.path.join(self.box, ".mini", "output.json"), "w").write("{}")      # the runner's
        os.symlink("/etc/passwd", os.path.join(self.box, "sneaky"))                 # never followed
        items, refused = ws.changes(self.root, self.box)
        self.assertEqual(sorted(rel for rel, _ in items), ["out/new.txt", "src/a.py"], "notes.txt is unchanged")
        self.assertEqual([rel for rel, _ in refused], ["sneaky"])
        self.assertEqual(sorted(ws.export(self.root, items)), ["out/new.txt", "src/a.py"])
        self.assertEqual(open(os.path.join(self.root, "src", "a.py")).read(), "print(2)\n")
        self.assertFalse(os.path.exists(os.path.join(self.root, "sneaky")))
        self.assertFalse(os.path.exists(os.path.join(self.root, ".mini")))

    def test_export_refuses_to_follow_a_symlink_out(self):
        outside = tempfile.mkdtemp(prefix="mini-outside-")
        os.symlink(outside, os.path.join(self.root, "escape"))
        os.makedirs(os.path.join(self.box, "escape"))
        open(os.path.join(self.box, "escape", "x.txt"), "w").write("x")
        items, refused = ws.changes(self.root, self.box)
        self.assertEqual((items, [r for r, _ in refused]), ([], ["escape/x.txt"]))
        with self.assertRaises(PolicyError):
            ws.export(self.root, [("escape/x.txt", os.path.join(self.box, "escape", "x.txt"))])
        self.assertEqual(os.listdir(outside), [])

    def test_a_tool_call_end_to_end(self):
        """The agent copies in, the (simulated) tool edits and adds a file, the agent exports and checkpoints."""
        cfg = temp_config()
        d = tempfile.mkdtemp()
        open(os.path.join(d, "tool.py"), "w").write("def run(inputs, io):\n    return {}\n")
        Registry(cfg.abspath("state"), run_id="fixture", auto_accept=True).install({
            "name": "upper", "description": "Upper-case a file.", "kind": "code",
            "input_schema": {"type": "object", "required": ["file"], "properties": {"file": {"type": "string"}}},
            "output_schema": {"type": "object"}, "settings": [], "permissions": {"network": [], "packages": {}},
            "edge_cases": [], "files": ["tool.py"], "created_by": {"provider": "test", "model": "none"},
            "test_results": {"passed": True, "summary": "fixture"}}, d)
        seen = {}

        def fake_tool(m, tool_dir, box, inputs, **kw):
            seen["box"], seen["inputs"], seen["listing"] = box, inputs, sorted(os.listdir(box))
            p = os.path.join(box, inputs["file"])
            text = open(p).read()
            open(p, "w").write(text.upper())
            open(os.path.join(box, "summary.txt"), "w").write("done\n")
            return {"file": inputs["file"]}, {"exit_code": 0, "output": ""}
        a = Agent(cfg, label="call", workspace=self.root)
        with mock.patch.object(Sandbox, "invoke_tool", side_effect=fake_tool):
            text = a._invoke({"name": "upper", "inputs": {"file": "src/a.py"}}, {})
        self.assertEqual(seen["inputs"], {"file": "src/a.py"})
        self.assertEqual(seen["listing"], ["src"], "only the named file is in the sandbox, never .git")
        self.assertFalse(os.path.exists(seen["box"]), "the call's folder is removed afterwards")
        self.assertIn("Files written to the folder: src/a.py, summary.txt", text)
        self.assertEqual(open(os.path.join(self.root, "src", "a.py")).read(), "PRINT(1)\n")
        self.assertEqual(a.produced_files(), ["src/a.py", "summary.txt"])
        self.assertEqual(git(self.root, "log", "--oneline").count("\n"), 1, "no commit on the branch")
        ckpt.undo(self.root)
        self.assertEqual(open(os.path.join(self.root, "src", "a.py")).read(), "print(1)\n")
        self.assertFalse(os.path.exists(os.path.join(self.root, "summary.txt")))

    def test_sandbox_mounts_no_user_folder(self):
        sb = Sandbox(temp_config())
        with mock.patch.object(Sandbox, "_cli") as cli:
            cli.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            sb.run(["python", "-c", "1"], self.box)
        args = " ".join(cli.call_args[0][0])
        self.assertNotIn(self.root, args)
        self.assertIn(f"{os.path.realpath(self.box)}:/work:rw", args)


if __name__ == "__main__":
    unittest.main()
