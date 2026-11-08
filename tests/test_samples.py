import os
import tempfile
import types
import unittest

from mini.agent import samples


class SamplesTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.d, "in"))
        open(os.path.join(self.d, "in", "orders.csv"), "w").write(
            "id,qty,unit_price\n" + "".join(f"{i},{i},1.5\n" for i in range(5000)))
        open(os.path.join(self.d, "feed.xml"), "w").write("<rss><item>a</item></rss>\n")
        open(os.path.join(self.d, "big.bin"), "wb").write(b"\0" * (samples.MAX_FILE + 1))
        open(os.path.join(self.d, "pic.jpg"), "wb").write(b"\xff\xd8\0\0")
        self.a = types.SimpleNamespace(folder=self.d,
                                       task_inputs={"in/orders.csv", "feed.xml", "big.bin", "pic.jpg"})

    def test_the_spec_step_sees_the_head_of_each_input(self):
        p = samples.preview(self.a)
        self.assertIn("id,qty,unit_price", p)
        self.assertIn("<rss><item>a</item></rss>", p)
        self.assertIn("pic.jpg (4 bytes, binary)", p)
        self.assertLess(p.count("\n0,0,1.5") + p.count("\n11,11"), 3)

    def test_small_samples_are_copied_for_the_tests(self):
        files = os.path.join(self.d, "build")
        got = dict(samples.copy(self.a, files))
        self.assertEqual(set(got), {"in/orders.csv", "feed.xml", "pic.jpg"})      # big binary left out
        csv = open(os.path.join(files, "samples", "in", "orders.csv")).read().splitlines()
        self.assertEqual(len(csv), samples.KEEP_LINES)
        self.assertEqual(csv[0], "id,qty,unit_price")
        self.assertEqual(dict(samples.listing(files)), got)
        note = samples.tests_note(files)
        self.assertIn("samples/feed.xml:\n```\n<rss><item>a</item></rss>", note)
        self.assertNotIn("samples/pic.jpg:", note)                    # binary: named, not shown

    def test_no_inputs_no_samples(self):
        a = types.SimpleNamespace(folder=self.d, task_inputs=set())
        self.assertEqual(samples.preview(a), "")
        self.assertEqual(samples.copy(a, os.path.join(self.d, "b")), [])
        self.assertEqual(samples.tests_note(os.path.join(self.d, "b")), "")


class TestsReadTheSamplesTest(unittest.TestCase):
    def test_tests_that_type_their_own_data_go_back(self):
        from mini.agent.creator import Creator
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "samples"))
        open(os.path.join(d, "samples", "feed.xml"), "w").write("<rss/>")
        open(os.path.join(d, "tool.py"), "w").write("def run(inputs, io):\n    return {}\n")
        cr = Creator(types.SimpleNamespace())
        cr.files_dir, cr.spec = d, {"edge_cases": [], "input_schema": {"type": "object", "properties": {}}}
        open(os.path.join(d, "test_tool.py"), "w").write("def test_a():\n    data = '<rss>'\n")
        self.assertIn("samples/feed.xml", cr._edge_case_gap())
        self.assertEqual(cr._edge_case_gap(), "")                     # said once
        cr.samples_nudged = False
        open(os.path.join(d, "test_tool.py"), "w").write(
            "import os\ndef test_a():\n    p = os.path.join(os.path.dirname(__file__), 'samples', 'feed.xml')\n")
        self.assertEqual(cr._edge_case_gap(), "")
        open(os.path.join(d, "samples", "feed.xml"), "w").write('<?xml version="1.0"?><rss><channel>')
        open(os.path.join(d, "test_tool.py"), "a").write("    data = '<?xml version=\"1.0\"?><rss><title>'\n")
        self.assertIn("types the content of samples/feed.xml", cr._edge_case_gap())      # every time, not once


if __name__ == "__main__":
    unittest.main()
