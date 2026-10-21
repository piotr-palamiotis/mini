"""Checkpoints in hidden refs and undo, on throwaway git repositories."""
import os
import sys
import tempfile
import unittest
from unittest import mock

from mini.harness import checkpoint as ckpt
from tests.helpers import git, git_folder


def read(folder, rel):
    with open(os.path.join(folder, rel)) as f:
        return f.read()


def write(folder, rel, text):
    os.makedirs(os.path.dirname(os.path.join(folder, rel)) or folder, exist_ok=True)
    with open(os.path.join(folder, rel), "w") as f:
        f.write(text)


def task(folder, run, changes):
    """What a task does: checkpoint, write, record."""
    cp = ckpt.Checkpoint(folder, run, "a task")
    cp.before_change(list(changes))
    for rel, text in changes.items():
        write(folder, rel, text)
    cp.after_change(list(changes))
    return cp


class CheckpointTest(unittest.TestCase):
    def setUp(self):
        self.d = git_folder({"a.txt": "a1\n", "src/b.py": "b1\n", ".gitignore": "*.log\n"})

    def test_a_dirty_tree_is_snapshotted_without_touching_index_or_history(self):
        write(self.d, "a.txt", "a2 uncommitted\n")
        write(self.d, "new.txt", "untracked\n")
        write(self.d, "src/b.py", "b2 staged\n")
        git(self.d, "add", "src/b.py")
        status, cached, log = git(self.d, "status", "--porcelain"), git(self.d, "diff", "--cached"), git(self.d, "log", "--oneline")
        head = git(self.d, "rev-parse", "HEAD")
        task(self.d, "r1", {"a.txt": "by the task\n"})
        self.assertEqual(git(self.d, "rev-parse", "HEAD"), head, "no commit on the branch")
        self.assertEqual(git(self.d, "diff", "--cached"), cached, "the user's index is untouched")
        self.assertEqual(git(self.d, "status", "--porcelain"), status, "same status: only a.txt's content changed")
        self.assertEqual(git(self.d, "log", "--oneline"), log, "branch history unchanged, nothing new reachable from it")
        self.assertIn("refs/mini/r1", git(self.d, "for-each-ref", "refs/mini"))
        before = git(self.d, "rev-parse", "refs/mini/r1^1").strip()
        self.assertEqual(git(self.d, "show", f"{before}:a.txt"), "a2 uncommitted\n")
        self.assertEqual(git(self.d, "show", f"{before}:new.txt"), "untracked\n")
        self.assertEqual(git(self.d, "show", f"{before}:src/b.py"), "b2 staged\n")
        self.assertEqual(ckpt.task_files(self.d, "r1"), ["a.txt"])

    def test_undo_a_whole_task(self):
        write(self.d, "a.txt", "dirty\n")
        task(self.d, "r1", {"a.txt": "x\n", "src/b.py": "y\n", "made/new.txt": "n\n"})
        write(self.d, "other.txt", "the user's\n")
        run, done = ckpt.undo(self.d)
        self.assertEqual(run, "r1")
        self.assertEqual(sorted(done), [("a.txt", "restored"), ("made/new.txt", "deleted"), ("src/b.py", "restored")])
        self.assertEqual(read(self.d, "a.txt"), "dirty\n", "back to the uncommitted state, not to HEAD")
        self.assertEqual(read(self.d, "src/b.py"), "b1\n")
        self.assertFalse(os.path.exists(os.path.join(self.d, "made", "new.txt")))
        self.assertEqual(read(self.d, "other.txt"), "the user's\n", "files the task did not touch stay")

    def test_undo_single_files_and_undo_the_undo(self):
        task(self.d, "r1", {"a.txt": "x\n", "src/b.py": "y\n"})
        run, done = ckpt.undo(self.d, "r1", ["src/b.py"])
        self.assertEqual(done, [("src/b.py", "restored")])
        self.assertEqual(read(self.d, "a.txt"), "x\n")
        self.assertEqual(read(self.d, "src/b.py"), "b1\n")
        undo_run = next(r for r, _, _ in ckpt.checkpoints(self.d) if r.startswith("undo-r1-"))
        ckpt.undo(self.d, undo_run)
        self.assertEqual(read(self.d, "src/b.py"), "y\n", "an undo is a task of its own")
        self.assertEqual(ckpt.undo(self.d)[0], "r1", "the default skips undo checkpoints")

    def test_an_ignored_file_the_task_overwrites_is_kept(self):
        write(self.d, "run.log", "old log\n")
        cp = task(self.d, "r1", {"a.txt": "x\n"})
        cp.before_change(["run.log"])
        write(self.d, "run.log", "new log\n")
        cp.after_change(["run.log"])
        ckpt.undo(self.d, "r1")
        self.assertEqual(read(self.d, "run.log"), "old log\n")
        self.assertEqual(read(self.d, "a.txt"), "a1\n")

    def test_a_subfolder_of_a_repository(self):
        sub = os.path.join(self.d, "src")
        task(sub, "r1", {"b.py": "changed\n", "c.py": "new\n"})
        self.assertEqual(sorted(ckpt.task_files(sub, "r1")), ["b.py", "c.py"])
        ckpt.undo(sub, None, ["b.py"])
        self.assertEqual(read(sub, "b.py"), "b1\n")
        self.assertTrue(os.path.exists(os.path.join(sub, "c.py")))
        with self.assertRaises(ckpt.GitError):
            ckpt.undo(sub, None, ["../a.txt"])

    def test_a_repository_without_commits(self):
        d = git_folder()
        write(d, "x.txt", "first\n")
        task(d, "r1", {"x.txt": "second\n", "y.txt": "y\n"})
        self.assertEqual(sorted(ckpt.undo(d)[1]), [("x.txt", "restored"), ("y.txt", "deleted")])
        self.assertEqual(read(d, "x.txt"), "first\n")

    def test_undo_never_writes_through_a_link_out(self):
        outside = tempfile.mkdtemp(prefix="mini-outside-")
        task(self.d, "r1", {"src/b.py": "y\n"})
        os.rename(os.path.join(self.d, "src"), os.path.join(self.d, "src-moved"))
        os.symlink(outside, os.path.join(self.d, "src"))
        self.assertEqual(ckpt.undo(self.d)[1], [("src/b.py", "refused")])
        self.assertEqual(os.listdir(outside), [])

    def test_undo_args(self):
        task(self.d, "r1", {"a.txt": "x\n"})
        self.assertEqual(ckpt.undo_args(self.d, ["r1", "a.txt"]), ("r1", ["a.txt"]))
        self.assertEqual(ckpt.undo_args(self.d, ["a.txt"]), (None, ["a.txt"]))
        self.assertEqual(ckpt.undo_args(self.d, []), (None, []))


class GuardTest(unittest.TestCase):
    def test_nothing_happens_until_the_user_agrees(self):
        d = os.path.realpath(tempfile.mkdtemp(prefix="mini-plain-"))
        asked = []
        self.assertFalse(ckpt.offer_init(d, lambda q: asked.append(q) or False))
        self.assertIn("not a git repository", asked[0])
        self.assertFalse(os.path.exists(os.path.join(d, ".git")))
        self.assertTrue(ckpt.offer_init(d, lambda q: True))
        self.assertTrue(ckpt.is_repo(d))
        self.assertTrue(ckpt.offer_init(d, lambda q: self.fail("a repository is not asked about")))

    def test_mini_run_refuses_outside_a_repository(self):
        from mini.__main__ import main
        d = os.path.realpath(tempfile.mkdtemp(prefix="mini-plain-"))
        cfg = os.path.join(tempfile.mkdtemp(), "none.toml")
        with mock.patch("os.getcwd", return_value=d), mock.patch("mini.setup.needs_setup", return_value=False), \
                mock.patch.object(sys.stdin, "isatty", return_value=False), mock.patch("sys.stderr"):
            self.assertEqual(main(["--config", cfg, "run", "hello", "--quiet"]), 2)
        self.assertFalse(os.path.exists(os.path.join(d, ".git")))
        self.assertEqual(os.listdir(d), [], "nothing written")

    def test_mini_undo_command(self):
        from mini.__main__ import main
        d = git_folder({"a.txt": "a1\n"})
        task(d, "r1", {"a.txt": "x\n"})
        cfg = os.path.join(tempfile.mkdtemp(), "none.toml")
        with mock.patch("os.getcwd", return_value=d), mock.patch("builtins.print") as p:
            self.assertEqual(main(["--config", cfg, "undo", "--list"]), 0)
            self.assertIn("r1", str(p.call_args_list))
            self.assertEqual(main(["--config", cfg, "undo"]), 0)
        self.assertEqual(read(d, "a.txt"), "a1\n")


if __name__ == "__main__":
    unittest.main()
