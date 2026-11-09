"""Gap detection judges the registry's best matches one tool at a time, then picks when several fit."""
import json
import os
import unittest

from mini.agent.core import Agent
from tests.helpers import call, fake_backend, git_folder, step, temp_config


def manifest(name, desc):
    return {"name": name, "description": desc, "version": 1, "created_by": {"provider": "local"},
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
            "output_schema": {"type": "object", "properties": {}}}


class GapTest(unittest.TestCase):
    def run_gap(self, script, tools):
        cfg = temp_config(**{"gap.judge": True})
        a = Agent(cfg, backend=fake_backend(script), label="gap", workspace=git_folder({"a.txt": "x\n"}))
        a.registry.scored = lambda q, limit=10: [(3, m) for m in tools][:limit]
        a.registry.names = lambda: [m["name"] for m in tools]
        a.task_text = "Convert config.yaml to TOML"
        return a, a.gap({"goal": "convert yaml to toml", "plan": "1. convert"})

    def test_each_tool_is_judged_alone_and_only_the_fitting_one_is_used(self):
        tools = [manifest("yaml_to_toml", "Convert a YAML file to TOML."), manifest("pdf_read", "Read a PDF.")]
        script = [step(call("judge_tool", fits=True, step="convert", why="it converts")),
                  step(call("judge_tool", fits=False, why="pdf")),
                  step(call("decide_coverage", reasoning="the tool does it", use=[], missing=[]))]
        a, got = self.run_gap(script, tools)
        self.assertEqual(got, {"use": ["yaml_to_toml"], "missing": []})
        ev = [json.loads(l) for l in open(os.path.join(a.run_dir, "log.jsonl"))]
        self.assertEqual([(e["tool"], e["fits"]) for e in ev if e["kind"] == "gap_judge"],
                         [("yaml_to_toml", True), ("pdf_read", False)])

    def test_two_tools_for_the_same_step_one_is_picked(self):
        tools = [manifest("yaml_to_toml", "Convert a YAML file to TOML."), manifest("convert_yaml", "YAML to TOML.")]
        script = [step(call("judge_tool", fits=True, step="convert")), step(call("judge_tool", fits="true", step="convert")),
                  step(call("choose_tools", use=["convert_yaml"], why="newer")),
                  step(call("decide_coverage", reasoning="ok", use=[], missing=[]))]
        a, got = self.run_gap(script, tools)
        self.assertEqual(got["use"], ["convert_yaml"])


if __name__ == "__main__":
    unittest.main()
