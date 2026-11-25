"""Models pick different shapes for the same field; core.coerce turns them into the declared type, the same way for
every model, and Turns applies it to every tool call."""
import unittest
from types import SimpleNamespace

from mini.agent.turns import Turns, fn
from mini.core import coerce, validate

S, ARR = {"type": "string"}, {"type": "array", "items": {"type": "string"}}
UNDERSTAND = {"type": "object", "properties": {"goal": S, "plan": S, "outputs": ARR}}


class CoerceTest(unittest.TestCase):
    def test_plan_as_list_of_steps(self):            # gemma-4 31B
        out = coerce(UNDERSTAND, {"goal": "g", "plan": ["1. read", "2. write"], "outputs": "a.csv"})
        self.assertEqual(out, {"goal": "g", "plan": "1. read\n2. write", "outputs": ["a.csv"]})

    def test_object_as_list_of_one(self):             # Qwen3: a schema object sent inside a list
        spec = {"type": "object", "properties": {"output_schema": {"type": "object"}}}
        self.assertEqual(coerce(spec, {"output_schema": [{"properties": {}}]}), {"output_schema": {"properties": {}}})

    def test_json_sent_as_string(self):
        spec = {"type": "object", "properties": {"rows": {"type": "array", "items": {"type": "integer"}},
                                                 "opt": {"type": "object"}}}
        self.assertEqual(coerce(spec, {"rows": "[1, \"2\"]", "opt": '{"a": 1}'}), {"rows": [1, 2], "opt": {"a": 1}})

    def test_scalars(self):
        spec = {"type": "object", "properties": {"n": {"type": "integer"}, "x": {"type": "number"},
                                                 "b": {"type": "boolean"}, "s": S}}
        self.assertEqual(coerce(spec, {"n": "3", "x": "2.5", "b": "True", "s": 7}), {"n": 3, "x": 2.5, "b": True, "s": "7"})
        self.assertEqual(coerce({"type": "integer"}, "3.5"), "3.5", "not an integer: left for the validator")

    def test_nested_and_unknown_kept(self):
        spec = {"type": "object", "properties": {"steps": {"type": "array", "items": UNDERSTAND}}}
        out = coerce(spec, {"steps": {"plan": ["do a", "do b"]}, "extra": [1]})
        self.assertEqual(out, {"steps": [{"plan": "do a\ndo b"}], "extra": [1]})

    def test_several_names_stay_a_list(self):
        self.assertEqual(coerce(S, ["a.txt", "b.txt"]), ["a.txt", "b.txt"], "two paths are not one path")
        self.assertEqual(coerce({"type": "string", "enum": ["a b", "c d"]}, ["a b", "c d"]), ["a b", "c d"])
        self.assertEqual(coerce(ARR, {"pip": ["x"]}), {"pip": ["x"]}, "left for a repair that reads garbled forms")

    def test_validate_uses_it(self):
        args, problem = validate({"type": "object", "properties": {"plan": S, "files": ARR}, "required": ["plan"]},
                                 {"plan": ["read x", "write y"], "files": "a.txt"})
        self.assertEqual((args, problem), ({"plan": "read x\nwrite y", "files": ["a.txt"]}, ""))


class TurnsCoerceTest(unittest.TestCase):
    def test_every_call_is_coerced_and_logged(self):
        events = []
        agent = SimpleNamespace(model="m", log=SimpleNamespace(emit=lambda kind, **kw: events.append((kind, kw))))
        t = Turns(agent, "parse", [], [fn("understand", "", UNDERSTAND["properties"])])
        calls = [{"name": "understand", "arguments": {"goal": "g", "plan": ["read a", "write b"]}},
                 {"name": "other", "arguments": {"plan": ["a"]}}]
        t._coerce_calls(calls)
        self.assertEqual(calls[0]["arguments"]["plan"], "read a\nwrite b")
        self.assertEqual(calls[1]["arguments"], {"plan": ["a"]}, "a call with no schema is left alone")
        self.assertEqual(events, [("watchdog", {"name": "coerced", "step": "parse", "tool": "understand",
                                                "fields": ["plan"], "model": "m"})])


if __name__ == "__main__":
    unittest.main()
