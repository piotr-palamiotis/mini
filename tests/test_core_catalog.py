"""The core catalog (mini/core): one module per capability, typed calls validated before they run, a generated
prompt section, gap detection and the creator guard that never build what core already does."""
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from mini import core
from mini.agent.core import Agent
from mini.agent.creator import Creator
from mini.harness import checkpoint as ckpt
from mini.harness.approvals import Decision
from mini.harness.registry import Registry
from mini.harness.sandbox import Sandbox
from tests.helpers import call, fake_backend, git, git_folder, step, temp_config

EXPECTED = {"list_files", "read_file", "write_file", "edit_file", "copy_file", "move_file", "delete_file",
            "search_files", "diff_files", "git_status", "git_diff", "git_log", "checkpoint", "undo", "run_code",
            "web_fetch", "ask_user", "read_notes", "write_notes", "search_tools", "inspect_tool", "invoke_tool"}


class CatalogTest(unittest.TestCase):
    def test_every_module_is_a_capability(self):
        cat = core.load()
        self.assertEqual(set(cat.names()), EXPECTED)
        for c in cat.caps.values():
            self.assertTrue(c.description and callable(c.handler), c.name)
            self.assertEqual(c.schema.get("type"), "object", c.name)
            self.assertTrue(set(c.schema.get("required") or []) <= set(c.schema.get("properties") or {}), c.name)
            self.assertEqual(c.tool()["function"]["name"], c.name)
            self.assertIn(f"- {c.signature()}: ", cat.listing())

    def test_arguments_are_validated_against_the_schema(self):
        schema = {"type": "object", "required": ["path"], "properties": {
            "path": {"type": "string"}, "depth": {"type": "integer"}, "on": {"type": "boolean"},
            "mode": {"type": "string", "enum": ["a", "b"]}, "files": {"type": "array", "items": {"type": "string"}}}}
        self.assertEqual(core.validate(schema, {"path": "x", "depth": "2", "on": "true", "files": "f"}),
                         ({"path": "x", "depth": 2, "on": True, "files": ["f"]}, ""))
        _, problem = core.validate(schema, {"depth": "deep", "mode": "c", "files": [1], "extra": 1})
        for part in ("unknown argument extra", "path is required", "depth must be an integer",
                     "mode must be one of a, b", "every item of files must be a string"):
            self.assertIn(part, problem)
        self.assertIn("must be an object", core.validate(schema, ["x"])[1])
        self.assertIn("on must be a boolean", core.validate(schema, {"path": "x", "on": 1})[1])

    def test_a_list_where_a_string_is_expected_is_unwrapped(self):
        schema = {"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}}
        self.assertEqual(core.validate(schema, {"path": ["src"]}), ({"path": "src"}, ""))
        self.assertEqual(core.validate(schema, {"path": []}), ({"path": ""}, ""))
        self.assertIn("path must be a string", core.validate(schema, {"path": ["a", "b"]})[1])

    def test_a_command_merged_into_one_string_is_split_back_into_argv(self):
        from mini.harness.sandbox import repair_argv
        self.assertEqual(repair_argv(['python3", "-c", "print(1)"']), ["python3", "-c", "print(1)"])
        self.assertEqual(repair_argv(["python", "-c", "x"]), ["python", "-c", "x"])
        self.assertEqual(repair_argv(["ls -la"]), ["ls -la"])

    def test_an_invalid_call_is_not_run(self):
        a = Agent(temp_config(), label="valid")
        with mock.patch.object(core.load().get("write_file"), "handler") as h:
            text, _ = a.act("write_file", {"mode": "sideways"})
        h.assert_not_called()
        self.assertIn("write_file was not run: path is required; mode must be one of", text)

    def test_covering_is_strict(self):
        cat = core.load()
        for text, cap in [("list the files in the folder", "list_files"), ("copy a file to another folder", "copy_file"),
                          ("copy_file", "copy_file"), ("fetch_url", "web_fetch"), ("rename_file", "move_file"),
                          ("show the git status", "git_status"), ("delete a file", "delete_file")]:
            self.assertEqual(cat.covering(text), cap, text)
        for text in ("count the lines of a text file", "count_text_lines", "convert a spreadsheet to csv",
                     "send a message to a chat", "resize an image file", "fetch the latest exchange rates"):
            self.assertIsNone(cat.covering(text), text)
        self.assertEqual([c.name for c in cat.search("search the folder for text")][0], "search_files")


class FileCapabilityTest(unittest.TestCase):
    def setUp(self):
        self.root = git_folder({"a.txt": "one\ntwo\nthree\n", "src/m.py": "x = 1\n", "src/n.py": "y = 2  # x\n"})
        self.a = Agent(temp_config(), label="files", workspace=self.root)

    def act(self, _name, **args):
        return self.a.act(_name, args)[0]

    def read(self, rel):
        return open(os.path.join(self.root, rel)).read()

    def test_the_task_inputs_stay_as_given(self):
        self.a.task_inputs = self.a._input_files({"inputs": ["a.txt", "src/", "a description, not a path"],
                                                  "outputs": ["src/n.py"]})
        self.assertEqual(self.a.task_inputs, {"a.txt", "src/m.py"})
        self.assertIn("is an input of this task", self.act("write_file", path="a.txt", content="x"))
        self.assertIn("is an input of this task", self.act("edit_file", path="src/m.py", old="x", new="z"))
        self.assertIn("is an input of this task", self.act("delete_file", path="src"))
        self.assertEqual(self.read("a.txt"), "one\ntwo\nthree\n")
        self.assertIn("updated", self.act("edit_file", path="src/n.py", old="y", new="w"))
        self.assertIn("Wrote", self.act("write_file", path="new.txt", content="x"))

    def test_edit_by_text_and_by_lines(self):
        self.assertIn("a.txt updated", self.act("edit_file", path="a.txt", old="two", new="2"))
        self.assertEqual(self.read("a.txt"), "one\n2\nthree\n")
        self.assertIn("appears 0 times", self.act("edit_file", path="a.txt", old="four", new="4"))
        self.act("edit_file", path="a.txt", start=1, end=2, new="first")
        self.assertEqual(self.read("a.txt"), "first\nthree\n")
        self.assertIn("outside 1..2", self.act("edit_file", path="a.txt", start=2, end=5, new=""))
        self.act("edit_file", path="a.txt", start=2, end=2, new="")
        self.assertEqual(self.read("a.txt"), "first\n")
        self.assertIn("Give old", self.act("edit_file", path="a.txt", new="x"))
        self.assertEqual(self.a.produced_files(), ["a.txt"])

    def test_copy_move_delete_are_checkpointed(self):
        self.assertIn("Copied a.txt", self.act("copy_file", source="a.txt", destination="b/a.txt"))
        self.assertIn("Copied src", self.act("copy_file", source="src", destination="lib"))
        self.assertEqual(self.read("lib/n.py"), "y = 2  # x\n")
        self.assertIn("into itself", self.act("copy_file", source="src", destination="src/deeper"))
        self.assertIn("Moved b/a.txt", self.act("move_file", source="b/a.txt", destination="c.txt"))
        self.assertFalse(os.path.exists(os.path.join(self.root, "b", "a.txt")))
        self.assertIn("Deleted src (2 files)", self.act("delete_file", path="src"))
        self.assertIn("outside the folder", self.act("delete_file", path="../x"))
        self.assertIn("cannot be deleted", self.act("delete_file", path="."))
        self.assertIn("Undid task", self.act("undo"))
        self.assertEqual(self.read("src/m.py"), "x = 1\n")
        for gone in ("c.txt", "lib/m.py", "b/a.txt"):
            self.assertFalse(os.path.exists(os.path.join(self.root, gone)), gone)
        self.assertEqual(self.a.produced_files(), [], "undone changes are no longer the task's output")

    def test_search_and_diff(self):
        self.assertEqual(self.act("search_files", name="*.py").splitlines(), ["src/m.py", "src/n.py"])
        self.assertEqual(self.act("search_files", content="x", path="src").splitlines(),
                         ["src/m.py:1: x = 1", "src/n.py:1: y = 2  # x"])
        self.assertIn("src/m.py:1", self.act("search_files", content=r"^x\s=", regex=True))
        self.assertIn("No matches", self.act("search_files", name="*.md"))
        self.assertIn("Give name", self.act("search_files"))
        out = self.act("diff_files", a="src/m.py", b="src/n.py")
        self.assertIn("-x = 1", out)
        self.assertIn("+y = 2  # x", out)
        self.assertIn("are the same", self.act("diff_files", a="a.txt", b="a.txt"))


class GitCapabilityTest(unittest.TestCase):
    def test_status_diff_log_checkpoint_undo(self):
        root = git_folder({"a.txt": "a\n"})
        a = Agent(temp_config(), label="git", workspace=root)
        act = lambda name, **args: a.act(name, args)[0]
        self.assertIn("start", act("git_log"))
        self.assertIn("This task has changed nothing", act("git_diff", since="checkpoint"))
        self.assertIn(f"This task's checkpoint: {a.log.run_id}", act("checkpoint"))
        a.act("write_file", {"path": "a.txt", "content": "b\n"})
        a.act("write_file", {"path": "new.txt", "content": "n\n"})
        self.assertIn("M a.txt", act("git_status"))
        self.assertIn("+b", act("git_diff"))
        since = act("git_diff", since="checkpoint")
        self.assertIn("new.txt", since)
        self.assertIn("+n", since)
        self.assertIn("new.txt deleted", act("undo", files=["new.txt"]))
        self.assertEqual(open(os.path.join(root, "a.txt")).read(), "b\n", "only the named file")
        self.assertIn("a.txt restored", act("undo"))
        self.assertEqual(open(os.path.join(root, "a.txt")).read(), "a\n")
        self.assertEqual(git(root, "log", "--oneline").count("\n"), 1, "no commit on the branch")

    def test_without_a_repository(self):
        a = Agent(temp_config(), label="nogit", workspace=os.path.realpath(tempfile.mkdtemp()))
        for name in ("git_status", "git_diff", "git_log", "checkpoint", "undo"):
            self.assertIn("not a git repository", a.act(name, {})[0], name)


class RunCodeTest(unittest.TestCase):
    def setUp(self):
        self.root = git_folder({"data.txt": "3\n4\n"})
        self.a = Agent(temp_config(), label="run", workspace=self.root)

    def test_a_silent_script_that_wrote_nothing_is_called_out(self):
        reply = "Go.\n```python\ndef f(): print(1)\n```\n"
        with mock.patch.object(Sandbox, "run", return_value={"exit_code": 0, "output": ""}):
            text, _ = self.a.act("run_code", {}, reply)
        self.assertIn("printed nothing and wrote no file", text)

    def test_a_call_naming_no_files_gets_the_small_project_copied_in(self):
        seen = {}

        def fake_run(argv, box, **kw):
            seen["listing"] = sorted(os.listdir(box))
            return {"exit_code": 0, "output": "ok\n"}
        with mock.patch.object(Sandbox, "run", side_effect=fake_run):
            self.a.act("run_code", {"command": ["wc", "-l", "data.txt"]})
        self.assertIn("data.txt", seen["listing"])

    def test_a_script_runs_against_the_named_files_and_its_output_lands_in_the_folder(self):
        seen = {}

        def fake_run(argv, box, **kw):
            seen["argv"], seen["listing"] = argv, sorted(os.listdir(box))
            seen["script"] = open(os.path.join(box, ".mini", "script.py")).read()
            open(os.path.join(box, "sum.txt"), "w").write("7\n")
            return {"exit_code": 0, "output": "7\n"}
        reply = "Summing.\n```python\nprint(sum(int(x) for x in open('data.txt')))\n```\n"
        with mock.patch.object(Sandbox, "run", side_effect=fake_run):
            text, _ = self.a.act("run_code", {"files": ["data.txt"]}, reply)
        self.assertEqual(seen["argv"], ["python", "/work/.mini/script.py"])
        self.assertEqual(seen["listing"], [".mini", "data.txt"])
        self.assertIn("open('data.txt')", seen["script"])
        self.assertIn("exit 0\n7", text)
        self.assertIn("Files written to the folder: sum.txt", text)
        self.assertIn("need_tool with from_run_code", text)
        self.assertEqual(open(os.path.join(self.root, "sum.txt")).read(), "7\n")
        self.assertEqual(ckpt.task_files(self.root, self.a.log.run_id), ["sum.txt"])
        self.assertFalse(os.path.exists(os.path.join(self.root, ".mini", "script.py")))
        self.assertTrue(self.a.last_script["ok"])

    def test_a_command_is_checked_against_the_allowlist(self):
        with mock.patch.object(Sandbox, "run", return_value={"exit_code": 1, "output": "boom"}) as run:
            self.assertIn("exit 1", self.a.act("run_code", {"command": ["wc", "-l", "data.txt"], "files": ["data.txt"]})[0])
            self.assertIn("not on the sandbox command allowlist", self.a.act("run_code", {"command": ["rm", "-rf", "/"]})[0])
            self.assertIn("not both", self.a.act("run_code", {})[0])
        self.assertEqual(run.call_count, 1)

    def test_promoting_a_script_hands_it_to_the_creator(self):
        self.a.last_script = {"script": "print(1)\n", "ok": True}
        creator = mock.Mock()
        creator.create.side_effect = RuntimeError("stop here")
        with self.assertRaises(RuntimeError):
            self.a._need(creator, {"need": "sum numbers in a file", "from_run_code": True}, "t", "", [])
        self.assertIn("```python\nprint(1)\n```", creator.create.call_args[0][0])
        self.assertIn("core capability copy_file", self.a._need(creator, {"need": "copy a file"}, "t", "", []))


class Response(io.BytesIO):
    def __init__(self, body, ctype):
        super().__init__(body)
        self.headers = {"Content-Type": ctype}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class WebFetchTest(unittest.TestCase):
    PAGE = b"<html><head><style>x{}</style></head><body><h1>Title</h1><p>Some &amp; text</p></body></html>"

    def test_only_approved_hosts(self):
        cfg = temp_config(**{"approvals.auto_approve": [], "web.hosts": ["example.org"]})
        root = git_folder({"r.md": "r"})
        a = Agent(cfg, label="web", workspace=root)
        with mock.patch("urllib.request.urlopen", return_value=Response(self.PAGE, "text/html")) as get:
            self.assertIn("not approved", a.act("web_fetch", {"url": "https://other.example.com/x"})[0])
            get.assert_not_called()
            text = a.act("web_fetch", {"url": "https://example.org/p", "save_as": "page.html"})[0]
        self.assertIn("Title", text)
        self.assertIn("Some & text", text)
        self.assertNotIn("x{}", text)
        self.assertIn("Saved to page.html", text)
        self.assertEqual(open(os.path.join(root, "page.html"), "rb").read(), self.PAGE)
        self.assertIn("http or https", a.act("web_fetch", {"url": "file:///etc/passwd"})[0])

    def test_the_user_approves_a_host_once_per_task(self):
        asked = []
        handler = lambda kind, title, details, options: (asked.append(title), Decision(True, options[0], "", "user"))[1]
        a = Agent(temp_config(**{"approvals.mode": "ask"}), label="web2", approval_handler=handler)
        with mock.patch("urllib.request.urlopen", side_effect=lambda *x, **k: Response(b'{"a": 1}', "application/json")):
            self.assertEqual(a.act("web_fetch", {"url": "http://api.example.net/v1"})[0], '{"a": 1}')
            a.act("web_fetch", {"url": "http://api.example.net/v2"})
        self.assertEqual(asked, ["Fetch from api.example.net?"])


class AskUserTest(unittest.TestCase):
    def agent(self, decision):
        seen = []

        def handler(kind, title, details, options):
            seen.append((kind, title, tuple(options), details))
            return decision
        return Agent(temp_config(**{"approvals.mode": "ask"}), label="ask", approval_handler=handler), seen

    def test_single_multi_and_free_text(self):
        a, seen = self.agent(Decision(True, "green", "", "user"))
        self.assertEqual(a.act("ask_user", {"question": "Colour?", "options": ["red", "green"]})[0], "The user picked: green")
        self.assertEqual(seen[0][:3], ("question", "Colour?", ("red", "green", "cancel")))
        a, seen = self.agent(Decision(True, "answer", "1, blue", "user"))
        out = a.act("ask_user", {"question": "Which?", "options": ["red", "green"], "multi": True})[0]
        self.assertEqual(out, "The user picked: red; blue")
        self.assertIn("1) red\n2) green", seen[0][3]["options"])
        a, _ = self.agent(Decision(True, "answer", "tomorrow", "user"))
        self.assertEqual(a.act("ask_user", {"question": "When?"})[0], "The user answered: tomorrow")

    def test_unattended(self):
        a = Agent(temp_config(), label="ask-auto")
        self.assertIn("Decide yourself", a.act("ask_user", {"question": "When?"})[0])


class NotesAndRegistryTest(unittest.TestCase):
    def test_notes_are_project_memory(self):
        root = git_folder({"a.txt": "a"})
        a = Agent(temp_config(), label="notes", workspace=root)
        self.assertIn("(none)", a.act("read_notes", {})[0])
        a.act("write_notes", {"text": "Tests live in tests/."})
        a.act("write_notes", {"text": "The user prefers short answers."})
        self.assertIn("Tests live in tests/.\nThe user prefers short answers.", a.act("read_notes", {})[0])
        self.assertIn("The user prefers short answers.", Agent(temp_config(), label="n2", workspace=root).context())
        a.act("write_notes", {"text": "Only this.", "mode": "replace"})
        self.assertEqual(open(os.path.join(root, ".mini", "notes.md")).read(), "Only this.\n")
        self.assertIn("No project memory", Agent(temp_config(), label="n3").act("read_notes", {})[0])

    def test_search_and_inspect_tools(self):
        cfg = temp_config()
        d = tempfile.mkdtemp()
        open(os.path.join(d, "tool.py"), "w").write("def run(inputs, io):\n    return {}\n")
        Registry(cfg.abspath("state"), run_id="fixture", auto_accept=True).install({
            "name": "word_counter", "description": "Count the words of a text.", "kind": "code",
            "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
            "output_schema": {"type": "object"}, "settings": [], "permissions": {"network": [], "packages": {}},
            "interaction": ["counts"], "edge_cases": ["empty text"], "files": ["tool.py"],
            "created_by": {"provider": "test"}, "test_results": {"passed": True, "summary": "1 passed"}}, d)
        a = Agent(cfg, label="reg")
        self.assertIn("word_counter (v1", a.act("search_tools", {"query": "count words"})[0])
        out = a.act("inspect_tool", {"name": "word_counter"})[0]
        for part in ("inputs: text?: string", "1. counts", "versions: [1]", "tests: 1 passed (passed)"):
            self.assertIn(part, out)
        self.assertIn("No installed tool", a.act("inspect_tool", {"name": "nope"})[0])
        self.assertIn("read_file is a core capability", a.act("invoke_tool", {"name": "read_file", "inputs": {}})[0])

    def test_invoke_tool_finds_a_missing_name(self):
        cfg = temp_config()
        d = tempfile.mkdtemp()
        open(os.path.join(d, "tool.py"), "w").write("def run(inputs, io):\n    return {}\n")
        Registry(cfg.abspath("state"), run_id="fixture", auto_accept=True).install({
            "name": "word_counter", "description": "Count the words of a text.", "kind": "code",
            "input_schema": {"type": "object", "properties": {"zz_text": {"type": "string"}}},
            "output_schema": {"type": "object"}, "settings": [], "permissions": {"network": [], "packages": {}},
            "interaction": ["counts"], "edge_cases": [], "files": ["tool.py"],
            "created_by": {"provider": "test"}, "test_results": {"passed": True, "summary": "1 passed"}}, d)
        a = Agent(cfg, label="rep")
        repair, c = core.load().caps["invoke_tool"].repair, mock.Mock(agent=a)
        self.assertEqual(repair(c, {"inputs": {"name": "word_counter", "zz_text": "a b"}}),
                         {"name": "word_counter", "inputs": {"zz_text": "a b"}})
        self.assertEqual(repair(c, {"inputs": {"zz_text": "a b"}}), {"name": "word_counter", "inputs": {"zz_text": "a b"}})
        self.assertEqual(repair(c, {"inputs": {"nobody_takes_this": 1}}), {"inputs": {"nobody_takes_this": 1}})
        self.assertEqual(repair(c, {"name": "x", "inputs": {"zz_text": 1}}), {"name": "x", "inputs": {"zz_text": 1}})


class PipelineTest(unittest.TestCase):
    def test_core_covers_the_task_and_no_tool_is_built(self):
        root = git_folder({"a.txt": "a\n"})
        script = [
            step(call("understand", goal="copy a.txt", inputs=["a.txt"], outputs=["b.txt"], plan="1. copy")),
            step(call("decide_coverage", reasoning="core", use=["copy_file"], missing=["copy a file to another path"])),
            step(call("copy_file", source="a.txt", destination="b.txt")),
            step(call("finish", answer="Copied.", files=["b.txt"])),
        ]
        backend = fake_backend(script)
        prompts, offered = [], []
        orig = backend.chat

        def chat(model, messages, tools, *x, **k):
            prompts.append(messages[-1]["content"])
            offered.append([t["function"]["name"] for t in tools])
            return orig(model, messages, tools, *x, **k)
        backend.chat = chat
        a = Agent(temp_config(**{"approvals.confirm_first": False}), backend=backend, label="cover", workspace=root)
        s = a.run_task("copy a.txt to b.txt")
        self.assertEqual(s["status"], "success", s["error"])
        self.assertEqual(s["created"], [])
        self.assertEqual(open(os.path.join(root, "b.txt")).read(), "a\n")
        gap = next(json.loads(l) for l in open(os.path.join(a.run_dir, "log.jsonl")) if '"kind": "gap"' in l)
        self.assertEqual((gap["missing"], gap["core"]), ([], {"copy a file to another path": "copy_file"}))
        self.assertIn("- copy_file(source, destination): ", prompts[1], "the gap prompt lists the catalog")
        self.assertIn("Core capabilities that match this task best: copy_file", prompts[1])
        self.assertTrue(EXPECTED | {"finish", "need_tool", "report_tool_bug"} <= set(offered[2]))

    def test_the_creator_rejects_a_spec_that_core_covers(self):
        c = Creator(Agent(temp_config(), label="guard"))
        spec = {"name": "copy_file", "description": "Copy a file.", "input_schema": {"type": "object"},
                "output_schema": {"type": "object"}, "edge_cases": ["x"]}
        self.assertIn("core capability copy_file", c._validate_spec(spec, new=True))
        self.assertIn("core capability list_files", c._validate_spec(
            {**spec, "name": "folder_lister", "description": "List the files in a folder."}, new=True))
        self.assertIsNone(c._validate_spec({**spec, "name": "count_text_lines",
                                            "description": "Count the lines of a text file."}, new=True))


if __name__ == "__main__":
    unittest.main()
