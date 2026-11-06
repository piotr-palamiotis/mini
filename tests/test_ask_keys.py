"""Every io.ask key in a built tool must be an input_schema property, or callers cannot answer it."""
import unittest

from mini.agent.creator import ask_keys_outside_schema

SPEC = {"input_schema": {"properties": {"input_file": {}, "confirm": {}}}}


class AskKeysTest(unittest.TestCase):
    def test_declared_keys_pass(self):
        src = 'def run(i, io):\n    io.ask("input_file", "Path?", "file")\n    io.ask(key="confirm", prompt="Ok?")\n'
        self.assertEqual(ask_keys_outside_schema(src, SPEC), [])

    def test_undeclared_and_question_keys_reported_once(self):
        src = ('def run(i, io):\n    io.ask("Confirm conversion?", "x", "confirm")\n'
               '    io.ask("Confirm conversion?", "x", "confirm")\n    io.ask("mode", "m")\n')
        self.assertEqual(ask_keys_outside_schema(src, SPEC), ["Confirm conversion?", "mode"])

    def test_dynamic_keys_and_bad_syntax_ignored(self):
        self.assertEqual(ask_keys_outside_schema("def run(i, io):\n    io.ask(k, 'x')\n", SPEC), [])
        self.assertEqual(ask_keys_outside_schema("def run(:\n", SPEC), [])


if __name__ == "__main__":
    unittest.main()
