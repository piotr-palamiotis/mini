"""A plain-text answer finishes the task; the understanding tolerates strings for its list fields; /exit quits."""
import unittest

from mini.agent.core import Agent, as_list, normalize_understanding
from mini.ui.commands import Commands


class FinishPaths(unittest.TestCase):
    def test_long_text_reply_is_the_answer(self):
        call = Agent._implicit_finish({"content": "The translation is: " + "word " * 60})
        self.assertEqual(call["name"], "finish")
        self.assertTrue(call["arguments"]["answer"].startswith("The translation"))

    def test_short_or_announcing_text_is_not(self):
        self.assertIsNone(Agent._implicit_finish({"content": "Let me check."}))
        self.assertIsNone(Agent._implicit_finish({"content": "x " * 200 + "Next I will run:"}))
        self.assertIsNone(Agent._implicit_finish({"content": ""}))

    def test_understanding_list_fields(self):
        for v in (None, "", "-", "None", []):
            self.assertEqual(as_list(v), [])
        self.assertEqual(as_list("write a.txt"), ["write a.txt"])
        u = normalize_understanding({"goal": "g", "side_effects": "-", "outputs": ["x"]})
        self.assertEqual((u["side_effects"], u["outputs"], u["inputs"]), ([], ["x"], []))
        # gemma-4 31B sends the plan as a list of steps; gap() joins these fields as text
        u = normalize_understanding({"goal": "g", "plan": ["1. read", "2. write"], "outputs": ["a", ["b"]]})
        self.assertEqual((u["plan"], u["outputs"]), ("1. read\n2. write", ["a", '["b"]']))

    def test_exit_command(self):
        c, args, problem = Commands().parse("/exit")
        self.assertEqual(c.name, "exit")
        self.assertFalse(problem)


if __name__ == "__main__":
    unittest.main()
