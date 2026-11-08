"""The attempt log that replaces the fix conversation: what an attempt changed, and how its tests failed."""
import unittest

from mini.agent.creator import _diff_summary, _failures


class AttemptLogTest(unittest.TestCase):
    def test_diff_summary_names_lines_and_counts(self):
        self.assertEqual(_diff_summary("tool.py", "a\nb\nc\n", "a\nB\nc\nd = 1\n"), "tool.py lines 2, 4 (+2 -1): B")
        self.assertEqual(_diff_summary("tool.py", "x\n", "x\n"), "")

    def test_diff_summary_of_a_removal(self):
        self.assertIn("removed def test_extra", _diff_summary("test_tool.py", "a\ndef test_extra():\n", "a\n"))

    def test_failures_from_pytest_summary(self):
        out = ("FAILED ../tool/test_tool.py::test_normal - TypeError: bad operand\n"
               "ERROR ../tool/test_tool.py - SyntaxError: invalid syntax\n")
        self.assertEqual(_failures(out), ["test_normal: TypeError: bad operand",
                                          "collecting test_tool.py: SyntaxError: invalid syntax"])

    def test_failures_without_summary_take_the_first_error_line(self):
        self.assertEqual(_failures("x\nE   ValueError: nope\n"), ["E   ValueError: nope"])


if __name__ == "__main__":
    unittest.main()
