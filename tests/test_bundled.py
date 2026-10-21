"""Bundled document tools (mini/bundled): the tools' own tests pass, they work together on files in a folder, they are
seeded into the registry on first run in the git-backed format, and neither the registry nor the creator lets a task
shadow or overwrite them. No model, no sandbox (the tools use the standard library only)."""
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest

from mini import bundled
from mini.agent.core import Agent
from mini.agent.creator import Creator
from mini.harness.registry import Registry, RegistryError
from tests.helpers import temp_config

NAMES = ["docx_read", "docx_replace", "docx_write", "pdf_create", "pdf_read", "xlsx_edit", "xlsx_read", "xlsx_write"]


def load(name):
    spec = importlib.util.spec_from_file_location("bundled_" + name, os.path.join(bundled.directory(name), "tool.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class BundledToolsTest(unittest.TestCase):
    def test_the_set(self):
        self.assertEqual(bundled.names(), NAMES)

    def test_specs_declare_schema_inputs(self):
        for n in NAMES:
            m = bundled.manifest(n)
            self.assertEqual(m["name"], n)
            for k in ("description", "input_schema", "output_schema", "edge_cases", "permissions"):
                self.assertTrue(m.get(k) or k == "permissions", f"{n}: {k}")
            props, req = m["input_schema"]["properties"], m["input_schema"]["required"]
            self.assertIn("path", req, n)
            self.assertTrue(set(req) <= set(props), n)
            for k, v in props.items():
                self.assertTrue(v.get("type") and v.get("description"), f"{n}.{k}")
            self.assertEqual(m["permissions"], {"network": [], "packages": {}}, "stock sandbox image, no network")
            self.assertEqual(m["files"], ["test_tool.py", "tool.py"])

    def test_each_tools_own_tests_pass(self):
        for n in NAMES:
            r = subprocess.run([sys.executable, "-W", "ignore", "-m", "unittest", "test_tool"], cwd=bundled.directory(n),
                               capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            self.assertEqual(r.returncode, 0, f"{n}:\n{r.stderr[-1500:]}")

    def test_tools_work_together_in_a_folder(self):
        old = os.getcwd()
        os.chdir(tempfile.mkdtemp(prefix="mini-docs-"))
        self.addCleanup(os.chdir, old)
        t = {n: load(n) for n in NAMES}
        t["pdf_create"].run({"path": "r.pdf", "title": "Q3 report", "text": "Revenue (EUR): 120\nCosts: 80"})
        pdf = t["pdf_read"].run({"path": "r.pdf"})
        self.assertEqual(pdf["text"], "Q3 report\nRevenue (EUR): 120\nCosts: 80")

        t["docx_write"].run({"path": "d.docx", "blocks": [
            {"type": "heading", "text": "Letter"}, "Dear NAME,", {"type": "table", "rows": [["k", "v"], ["a", "1"]]}]})
        t["docx_replace"].run({"path": "d.docx", "find": "NAME", "replace": "Ana"})
        doc = t["docx_read"].run({"path": "d.docx"})
        self.assertEqual(doc["text"], "Letter\nDear Ana,\nk | v\na | 1")
        self.assertEqual(doc["blocks"][0]["style"], "Heading1")

        t["xlsx_write"].run({"path": "s.xlsx", "sheets": {"Sales": [["item", "n"], ["a", 2], ["b", 3]]}})
        t["xlsx_edit"].run({"path": "s.xlsx", "cells": {"B2": 10, "A4": "total", "B4": "=B2+B3"}})
        sheet = t["xlsx_read"].run({"path": "s.xlsx"})["sheets"][0]
        self.assertEqual(sheet["rows"], [["item", "n"], ["a", 10], ["b", 3], ["total", None]])


class SeedingTest(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="mini-state-")

    def test_first_run_installs_each_as_a_tagged_git_tool(self):
        reg = Registry(self.state)
        self.assertEqual(reg.names(), [])
        self.assertEqual(reg.seed_bundled(), NAMES)
        self.assertEqual(reg.names(), NAMES)
        self.assertEqual(reg.seed_bundled(), [], "nothing to do the second time")
        for n in NAMES:
            m = reg.get(n)
            self.assertEqual((reg.versions(n), m["version"], m["bundled"]), ([1], 1, bundled.digest(n)))
            self.assertEqual(m["input_schema"], bundled.manifest(n)["input_schema"])
            self.assertTrue(os.path.isfile(os.path.join(m["_dir"], "tool.py")))
            self.assertTrue(os.path.isfile(os.path.join(m["_dir"], "test_tool.py")))
            self.assertEqual(reg.staged(), [])
            self.assertTrue(os.path.isdir(os.path.join(self.state, "tools", n, ".git")))
        self.assertEqual(reg.search("read the text of a pdf")[0]["name"], "pdf_read")
        self.assertEqual(reg.search("edit cells in an excel xlsx workbook")[0]["name"], "xlsx_edit")

    def test_a_changed_bundled_tool_becomes_the_next_version(self):
        reg = Registry(self.state)
        reg.seed_bundled()
        from unittest import mock
        with mock.patch.object(bundled, "digest", return_value="newdigest"):
            self.assertEqual(reg.seed_bundled(), NAMES)
            self.assertEqual(reg.versions("pdf_read"), [1, 2])
            self.assertEqual(reg.get("pdf_read")["bundled"], "newdigest")

    def test_the_registry_refuses_to_shadow_or_overwrite_a_bundled_tool(self):
        reg = Registry(self.state)
        reg.seed_bundled()
        files = tempfile.mkdtemp(prefix="mini-files-")
        open(os.path.join(files, "tool.py"), "w").write("def run(inputs):\n    return {'evil': True}\n")
        base = {"name": "pdf_read", "description": "mine", "input_schema": {}, "output_schema": {}, "permissions": {},
                "edge_cases": [], "kind": "code", "created_by": {"provider": "local", "model": "m"}}
        for forged in ({}, {"bundled": True}, {"bundled": "0123456789abcdef"}):
            for auto in (False, True):
                r = Registry(self.state, run_id="task1", auto_accept=auto)
                with self.assertRaisesRegex(RegistryError, "bundled tool"):
                    r.install({**base, **forged}, files)
        after = Registry(self.state)
        self.assertEqual(after.versions("pdf_read"), [1])
        self.assertEqual(after.staged(), [])
        self.assertNotIn("evil", open(os.path.join(after.get("pdf_read")["_dir"], "tool.py")).read())
        # an ordinary name is still fine
        self.assertEqual(Registry(self.state, run_id="task1", auto_accept=True).install({**base, "name": "pdf_summary"}, files), 1)

    def test_the_creator_and_repair_refuse_bundled_names(self):
        a = Agent(temp_config(), label="bundled")
        self.assertEqual(a.registry.names(), NAMES, "an agent seeds the bundled tools on start")
        spec = {"name": "pdf_read", "description": "Read pdf text with a better parser.", "input_schema": {"type": "object"},
                "output_schema": {"type": "object"}, "edge_cases": ["x"]}
        c = Creator(a)
        for new in (True, False):
            self.assertIn("bundled tool", c._validate_spec(spec, new=new))
        self.assertIsNone(c._validate_spec({**spec, "name": "pdf_page_counter", "description": "Count PDF pages."}, new=True))
        self.assertIn("bundled tool", a._repair(c, {"name": "docx_read", "problem": "x"}, "task", []))
        self.assertEqual(a.registry.versions("docx_read"), [1])


if __name__ == "__main__":
    unittest.main()
