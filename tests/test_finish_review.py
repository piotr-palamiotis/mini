"""Before a task ends, the model checks each file it wrote against the task's words: finish carries one check per
file, and a file that does not match, or has no check, keeps the task going (at most 3 rounds)."""
import json
import os
import unittest

from mini.agent.core import Agent
from tests.helpers import call, fake_backend, git_folder, step, temp_config

ASKS = "a list of strings"
OK = [{"file": "names.json", "sample": '"x"', "task_asks": ASKS, "file_has": "a JSON array of texts", "matches": True}]
BAD = [{"file": "names.json", "sample": '{"name": "x"}', "task_asks": ASKS, "file_has": "a list of objects",
        "matches": False}]
COPIED = [dict(BAD[0], file_has=ASKS, matches=True)]
MADE_UP = [dict(OK[0], sample='"y"')]


def finish(checks=None):
    return step(call("finish", answer="Done.", files=["names.json"], **({"checks": checks} if checks else {})))


def steps(*after_review):
    return [
        step(call("understand", goal="list names", inputs=["a.txt"], outputs=["names.json"], plan="1. write")),
        step(call("decide_coverage", reasoning="write_file does it", use=[], missing=[])),
        step(call("write_file", path="names.json", content='[{"name": "x"}]\n')),
        finish(),
        *after_review,
    ]


class FinishReviewTest(unittest.TestCase):
    def run_task(self, script, task="Write the names in a.txt to names.json as " + ASKS + ".", **over):
        folder = git_folder({"a.txt": "x\n"})
        agent = Agent(temp_config(**{"finish.review_outputs": True, **over}), backend=fake_backend(script), label="review",
                      workspace=folder)
        s = agent.run_task(task)
        events = [json.loads(l) for l in open(os.path.join(agent.run_dir, "log.jsonl"))]
        reviews = [e for e in events if e["kind"] == "watchdog" and e.get("name") == "finish_review"]
        results = [e["text"] for e in reviews]
        return s, folder, results, reviews

    def test_a_file_that_does_not_match_is_fixed_before_the_task_ends(self):
        fix = [finish(BAD), step(call("write_file", path="names.json", content='["x"]\n')), finish(OK)]
        s, folder, results, reviews = self.run_task(steps(*fix))
        self.assertEqual(s["status"], "success", s["error"])
        self.assertEqual(open(os.path.join(folder, "names.json")).read(), '["x"]\n')
        self.assertEqual(len(reviews), 2)
        self.assertIn('[{"name": "x"}]', results[0])                    # the head of the file, next to the task
        self.assertIn(ASKS, results[0])
        self.assertIn("a list of objects", results[1])

    def test_a_check_must_quote_the_file_and_describe_it_in_its_own_words(self):
        fix = [finish(MADE_UP), finish(COPIED), step(call("write_file", path="names.json", content='["x"]\n')),
               finish(OK)]
        s, folder, results, reviews = self.run_task(steps(*fix))
        self.assertEqual(s["status"], "success", s["error"])
        self.assertIn("sample is not in the file", results[1])
        self.assertIn("repeats the task's words", results[2])
        self.assertEqual(open(os.path.join(folder, "names.json")).read(), '["x"]\n')

    def test_a_fresh_judge_sends_back_a_file_whose_form_differs(self):
        judge = lambda ok, why: step(call("judge_form", matches=ok, why=why))
        fix = [finish([dict(OK[0], sample='{"name": "x"}', file_has="objects with a name")]),
               judge(False, "the task asks for strings; the file has objects."),
               step(call("write_file", path="names.json", content='["x"]\n')),
               finish(OK), judge(True, "a list of strings.")]
        s, folder, results, reviews = self.run_task(steps(*fix), **{"finish.judge": True})
        self.assertEqual(s["status"], "success", s["error"])
        self.assertIn("the task asks for strings; the file has objects.", results[1])
        self.assertEqual(open(os.path.join(folder, "names.json")).read(), '["x"]\n')
        self.assertEqual(len(reviews), 2)

    def test_one_check_covers_a_set_of_files_in_a_folder(self):
        parts = [step(call("write_file", path="parts/1.txt", content="p1\n")),
                 step(call("write_file", path="parts/2.txt", content="p2\n"))]
        part = {"file": "parts/*.txt", "sample": "p1", "task_asks": "one text per part", "file_has": "a line of text",
                "matches": True}
        script = steps()[:3] + parts + [finish(), finish(OK + [part])]
        s, _, results, reviews = self.run_task(script)
        self.assertEqual(s["status"], "success", s["error"])
        self.assertIn("parts/*.txt: 2 files like this one", results[0])
        self.assertEqual(len(reviews), 1)

    def test_the_judge_looks_at_a_checked_file_while_another_file_still_lacks_a_check(self):
        judge = lambda ok, why: step(call("judge_form", matches=ok, why=why))
        objects = [dict(OK[0], sample='{"name": "x"}', file_has="entries with a name")]
        script = steps()[:3] + [step(call("write_file", path="other.txt", content="o\n")), finish(), finish(objects),
                                judge(False, "objects, not strings."),
                                step(call("delete_file", path="other.txt")),
                                step(call("write_file", path="names.json", content='["x"]\n')),
                                finish(OK), judge(True, "a list of strings.")]
        s, folder, results, reviews = self.run_task(script, **{"finish.judge": True})
        self.assertEqual(s["status"], "success", s["error"])
        self.assertIn("no check for other.txt", results[1])
        self.assertIn("objects, not strings.", results[1])
        self.assertEqual(open(os.path.join(folder, "names.json")).read(), '["x"]\n')

    def test_lines_that_differ_from_the_tasks_form_only_in_spacing_are_named(self):
        tight = {"file": "names.json", "sample": "a.txt:1:x", "task_asks": "path:line: text", "file_has": "file, line, text",
                 "matches": True}
        script = steps()[:2] + [step(call("write_file", path="names.json", content="a.txt:1:x\n")), finish(),
                                finish([tight]), step(call("write_file", path="names.json", content="a.txt:1: x\n")),
                                finish([dict(tight, sample="a.txt:1: x")])]
        s, folder, results, reviews = self.run_task(script, task="List a.txt's lines in names.json as `path:line: text`.")
        self.assertEqual(s["status"], "success", s["error"])
        self.assertIn("differ from it in spacing, e.g. 'a.txt:1:x'", results[1])
        self.assertEqual(len(reviews), 2)

    def test_a_file_with_no_check_is_named(self):
        stray = [step(call("write_file", path="test.txt", content="t\n")), finish(OK)]
        s, _, results, reviews = self.run_task(steps(*stray, step(call("delete_file", path="test.txt")), finish(OK)))
        self.assertEqual(s["status"], "success", s["error"])
        self.assertTrue(any("no check for test.txt" in r for r in results), results)

    def test_deleting_a_stray_file_at_the_end_names_the_files_that_may_have_read_it(self):
        folder = git_folder({"a.txt": "x\n"})
        script = steps(step(call("write_file", path="test.txt", content="t\n")), finish(OK),
                       step(call("delete_file", path="test.txt")), finish(OK))
        agent = Agent(temp_config(**{"finish.review_outputs": True}), backend=fake_backend(script), label="review",
                      workspace=folder)
        self.assertEqual(agent.run_task("Write names.json as " + ASKS + ".")["status"], "success")
        events = [json.loads(l) for l in open(os.path.join(agent.run_dir, "log.jsonl"))]
        deleted = [e["text"] for e in events if e["kind"] == "tool_result" and e.get("name") == "delete_file"]
        self.assertIn("names.json", deleted[0])
        self.assertIn("run that step again", deleted[0])

    def test_after_three_rounds_the_task_may_end(self):
        s, _, _, reviews = self.run_task(steps(finish(), finish(), finish()))
        self.assertEqual(s["status"], "success", s["error"])
        self.assertEqual(len(reviews), 3)

    def test_out_of_rounds_a_file_the_judge_refused_and_nothing_changed_is_named_as_unchanged(self):
        judge = lambda ok, why: step(call("judge_form", matches=ok, why=why))
        again = lambda n: step(call("finish", answer=f"Done ({n}).", files=["names.json"], checks=OK))
        fix = [finish(OK), judge(False, "the task asks for strings; the file has objects."), again(2), again(3),
               again(4), step(call("write_file", path="names.json", content='["x"]\n')), again(5),
               judge(True, "a list of strings.")]
        s, folder, results, reviews = self.run_task(steps(*fix), **{"finish.judge": True})
        self.assertEqual(s["status"], "success", s["error"])
        self.assertIn("Fix it, then finish.", results[3])
        self.assertIn("It is unchanged since that was found", results[4])
        self.assertEqual(len(reviews), 5)

    def test_off_in_config_means_no_review(self):
        folder = git_folder({"a.txt": "x\n"})
        agent = Agent(temp_config(), backend=fake_backend(steps()), label="review", workspace=folder)
        self.assertEqual(agent.run_task("Write names.json.")["status"], "success")


class FormGapTest(unittest.TestCase):
    def test_only_a_form_of_words_between_punctuation_that_the_lines_nearly_follow(self):
        from mini.agent.core import form_gap
        t = "Write TODO.md in the form `path:line: text` (the text after `TODO:`); run `python3 -m unittest`."
        self.assertIn("2 of 2 lines", form_gap(t, "a.py:3:fix it\nb.c:2:  more\n"))
        self.assertEqual(form_gap(t, "- `a.py:3: fix it`\n* b.c:2: more: later\n"), "")   # list marks, backticks
        self.assertEqual(form_gap(t, "# TODOs\nnothing like the form\nat all\n"), "")


class DescribeTest(unittest.TestCase):
    def test_a_binary_file_says_what_it_is(self):
        import struct
        import tempfile
        import zipfile
        from mini.agent import samples
        d = tempfile.mkdtemp()
        png = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 290, 120) + b"\x08\x02\0\0\0"
        open(os.path.join(d, "a.png"), "wb").write(png + b"\0" * 8)
        date = b"2026:01:10 12:30:00\0"                  # IFD0: DateTime + a pointer to the Exif IFD with DateTimeOriginal
        ifd0 = struct.pack("<H", 2) + struct.pack("<HHII", 0x0132, 2, 20, 38) + struct.pack("<HHII", 0x8769, 4, 1, 58)
        tiff = b"II*\0" + struct.pack("<I", 8) + ifd0 + struct.pack("<I", 0) + date
        tiff += struct.pack("<H", 1) + struct.pack("<HHII", 0x9003, 2, 20, 76) + struct.pack("<I", 0) + date
        app1 = b"Exif\0\0" + tiff
        sof = b"\x08" + struct.pack(">HH", 170, 256) + b"\x03" + b"\0" * 9
        jpg = (b"\xff\xd8\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1 +
               b"\xff\xc0" + struct.pack(">H", len(sof) + 2) + sof + b"\xff\xd9")
        open(os.path.join(d, "t.jpg"), "wb").write(jpg)
        with zipfile.ZipFile(os.path.join(d, "b.zip"), "w") as z:
            z.writestr("meta.json", "[]")
            z.writestr("thumbs/a.jpg", "x")
        with zipfile.ZipFile(os.path.join(d, "r.xlsx"), "w") as z:
            z.writestr("xl/workbook.xml", '<workbook><sheets><sheet name="Totals" sheetId="1"/><sheet name="east"/>'
                                          '</sheets></workbook>')
        self.assertIn("binary: PNG image, 290x120", samples.head(d, "a.png"))
        self.assertIn("JPEG image, 256x170, EXIF DateTimeOriginal 2026:01:10 12:30:00; EXIF DateTime 2026:01:10",
                      samples.head(d, "t.jpg"))
        self.assertIn("zip archive holding: meta.json, thumbs/a.jpg", samples.head(d, "b.zip"))
        self.assertIn("Excel workbook, sheets: Totals; east", samples.head(d, "r.xlsx"))
        open(os.path.join(d, "x.bin"), "wb").write(b"\0\1\2")
        self.assertEqual(samples.head(d, "x.bin"), "x.bin (3 bytes, binary)")


if __name__ == "__main__":
    unittest.main()
