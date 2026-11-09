"""The attempt log that replaces the fix conversation: what an attempt changed, and how its tests failed."""
import unittest

from mini.agent.creator import _diff_summary, _failures, quoted


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

    def test_failures_read_the_traceback_not_the_cut_summary(self):
        out = ("___________________ test_empty_csv ___________________\n"
               "    run(inputs, io)\n"
               "E   mini_io.MissingInput: no answer for 'output_dir'\n"
               "=========================== short test summary info ============================\n"
               "FAILED ../tool/test_tool.py::test_empty_csv - mini_io.M...\n")
        self.assertEqual(_failures(out), ["test_empty_csv: mini_io.MissingInput: no answer for 'output_dir'"])

    def test_failures_without_summary_take_the_first_error_line(self):
        self.assertEqual(_failures("x\nE   ValueError: nope\n"), ["E   ValueError: nope"])

    def test_an_edge_case_quote_must_be_the_tasks_own_words(self):
        task = "Some values are missing or not numbers; leave those rows out."
        self.assertTrue(quoted("values are missing or not numbers", task))
        self.assertTrue(quoted("Leave those rows OUT", task))
        self.assertFalse(quoted("rows out", task))                       # too short to say anything
        self.assertFalse(quoted("malformed rows raise an error", task))


if __name__ == "__main__":
    unittest.main()
