"""Replay suite: recorded tasks (tests/replays/*.json) run against a scripted model in a throwaway git folder, and
the behaviors that must hold whatever the model does are asserted on the outcome.

A replay is {"task", "files": {path: text}, "script": [{"content"?, "calls": [{"name", "args"}]}], "expect": {...}}.
Optional setup: "dirty" (uncommitted edits), "outside" (files in a folder beside the workspace) and "symlinks"
({link: target}, "<outside>" standing for that folder). expect keys: status, no_tool_created, files, absent, changed,
rejected (texts the model must have been told), no_checkpoint, outside_unchanged, no_files_beside_folder, journal,
checkpoint, undone, undone_absent."""
import glob
import json
import os
import tempfile
import unittest

from mini.agent.core import Agent
from mini.harness import checkpoint as ckpt, journal
from tests.helpers import call, fake_backend, git, git_folder, step, temp_config

DIR = os.path.join(os.path.dirname(__file__), "replays")


def load(name):
    with open(os.path.join(DIR, name + ".json")) as f:
        return json.load(f)


def write(folder, files):
    for rel, text in files.items():
        os.makedirs(os.path.dirname(os.path.join(folder, rel)) or folder, exist_ok=True)
        with open(os.path.join(folder, rel), "w") as f:
            f.write(text)


def snapshot(folder):
    out = {}
    for root, dirs, names in os.walk(folder):
        dirs[:] = [d for d in dirs if d != ".git"]
        for n in names:
            p = os.path.join(root, n)
            if not os.path.islink(p):
                with open(p) as f:
                    out[os.path.relpath(p, folder)] = f.read()
    return out


class Replay:
    """Sets the recorded task up, runs it, and keeps what the asserts need."""

    def __init__(self, name):
        self.rec = load(name)
        self.parent = os.path.realpath(tempfile.mkdtemp(prefix="mini-replay-"))
        self.outside = os.path.join(self.parent, "outside")
        os.makedirs(self.outside)
        write(self.outside, self.rec.get("outside", {}))
        self.folder = git_folder(self.rec.get("files"))      # its own temp dir; never the project
        for link, target in self.rec.get("symlinks", {}).items():
            os.symlink(target.replace("<outside>", self.outside), os.path.join(self.folder, link))
        write(self.folder, self.rec.get("dirty", {}))
        self.before_outside = snapshot(self.outside)
        script = [step(*[call(c["name"], **c.get("args", {})) for c in s["calls"]], content=s.get("content", ""))
                  for s in self.rec["script"]]
        backend = fake_backend(script + [step(call("finish", answer="end of script"))] * 3)
        self.seen = []                                        # everything the model was told
        chat = backend.chat

        def spy(model, messages, tools, *a, **k):
            self.seen += [str(m.get("content")) for m in messages]
            return chat(model, messages, tools, *a, **k)
        backend.chat = spy
        self.agent = Agent(temp_config(**{"approvals.confirm_first": False}), backend=backend, label="replay",
                           workspace=self.folder)
        self.summary = self.agent.run_task(self.rec["task"])

    def text(self, rel):
        with open(os.path.join(self.folder, rel)) as f:
            return f.read()


class ReplayTest(unittest.TestCase):
    def test_every_replay_is_well_formed(self):
        names = sorted(os.path.basename(p)[:-5] for p in glob.glob(os.path.join(DIR, "*.json")))
        self.assertGreaterEqual(len(names), 4)
        for n in names:
            rec = load(n)
            self.assertTrue(rec["task"] and rec["script"] and rec["expect"], n)
            for s in rec["script"]:
                self.assertTrue(all(c.get("name") for c in s["calls"]), n)

    def test_core_covers_the_step_so_no_tool_is_created(self):
        r = Replay("core_covers_copy")
        s, e = r.summary, r.rec["expect"]
        self.assertEqual(s["status"], e["status"], s["error"])
        self.assertEqual(s["created"], [])
        with open(os.path.join(r.agent.run_dir, "log.jsonl")) as f:
            events = [json.loads(l) for l in f]
        self.assertFalse([x for x in events if x["kind"] in ("spec", "tool_installed", "tool_created", "creator")])
        self.assertEqual(r.agent.registry.snapshot(), s["registry_after"])
        for rel, text in e["files"].items():
            self.assertEqual(r.text(rel), text)
        self.assertEqual(s["outputs"], e["changed"])

    def test_nothing_changes_outside_the_folder_symlinks_included(self):
        r = Replay("symlink_escape")
        self.assertEqual(r.summary["status"], "success", r.summary["error"])
        self.assertEqual(snapshot(r.outside), r.before_outside, "files behind links and '..' stay as they were")
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(r.folder), "escaped.txt")), "no '..' escape")
        self.assertFalse(os.path.exists(os.path.join(r.outside, "new.txt")))
        self.assertEqual(r.text("a.txt"), "a\n")
        self.assertEqual(git(r.folder, "status", "--porcelain", "--untracked-files=all"), "?? leak.txt\n?? out\n", "only the planted links")
        self.assertEqual(r.summary["outputs"], [], "the refused writes are not the task's output")

    def test_an_invalid_call_is_rejected_before_it_runs(self):
        r = Replay("invalid_call")
        e = r.rec["expect"]
        told = "\n".join(r.seen)
        for part in e["rejected"]:
            self.assertIn(part, told)
        self.assertEqual(r.text("a.txt"), "a\n")
        for rel in e["absent"]:
            self.assertFalse(os.path.exists(os.path.join(r.folder, rel)), rel)
        self.assertIsNone(r.summary["checkpoint"], "a rejected call changes nothing, so nothing is snapshotted")
        self.assertEqual(ckpt.checkpoints(r.folder), [])

    def test_a_task_leaves_a_journal_entry_and_a_checkpoint_that_undo_restores(self):
        r = Replay("journal_undo")
        e, s = r.rec["expect"], r.summary
        self.assertEqual(s["status"], e["status"], s["error"])
        for rel, text in e["files"].items():
            self.assertEqual(r.text(rel), text)
        es = journal.entries(r.folder)
        self.assertEqual([(x["run"], x["task"], x["status"]) for x in es], [(s["run_id"], r.rec["task"], "success")])
        self.assertEqual(sorted(es[0]["files"]), ["a.txt", "note.txt"])
        self.assertEqual(es[0]["checkpoint"], f"refs/mini/{s['run_id']}")
        self.assertIn(f"refs/mini/{s['run_id']}", git(r.folder, "for-each-ref", "refs/mini"))
        self.assertEqual(git(r.folder, "log", "--oneline").count("\n"), 1, "the branch history is untouched")
        run, done = ckpt.undo(r.folder)
        self.assertEqual(run, s["run_id"])
        for rel, text in e["undone"].items():
            self.assertEqual(r.text(rel), text, "the user's uncommitted edit comes back too")
        for rel in e["undone_absent"]:
            self.assertFalse(os.path.exists(os.path.join(r.folder, rel)), rel)


if __name__ == "__main__":
    unittest.main()
