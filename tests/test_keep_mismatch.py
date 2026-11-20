"""keep_tool keeps the last run_code script: a description of what an earlier script made is refused once."""
import unittest

from mini.agent.core import Agent
from tests.helpers import git_folder, temp_config

ZIP = "import zipfile\nz = zipfile.ZipFile('bundle.zip', 'w')\nz.write('meta.json')\nz.close()\n"
THUMBS = ("Processes images in photos/ to create thumbnails in thumbs/ and a meta.json file with image metadata "
          "(width, height, format, and EXIF date).")


class KeepMismatchTest(unittest.TestCase):
    def agent(self):
        a = Agent(temp_config(), label="keep_mismatch", workspace=git_folder({"photos/a.jpg": "x"}))
        a.changed = ["thumbs/a.jpg", "meta.json", "contact.jpg", "bundle.zip"]
        a.last_script = {"script": ZIP, "ok": True, "record": "/nowhere", "written": ["bundle.zip"], "output": ""}
        return a

    def test_a_description_of_an_earlier_script_is_refused_once(self):
        a = self.agent()
        text = a._keep(None, THUMBS, "task", "", [])
        self.assertIn("Not kept", text)
        self.assertIn("wrote bundle.zip", text)
        self.assertIn("thumbs/, meta.json", text)
        self.assertNotIn("photos/", text)            # an input, not something the task made
        self.assertEqual(a._kept_elsewhere(THUMBS, a.last_script), ["thumbs/", "meta.json"])
        self.assertTrue(a.last_script["mismatch_told"])

    def test_a_description_that_names_what_the_script_wrote_fits(self):
        a = self.agent()
        self.assertEqual(a._kept_elsewhere("Zips thumbs/ and meta.json into one archive.", a.last_script), [])
        self.assertEqual(a._kept_elsewhere("Bundles thumbs/ and meta.json.", a.last_script), [])
        self.assertEqual(a._kept_elsewhere("Packs the results for download.", a.last_script), [])   # names no file
        a.last_script = {"written": ["thumbs/a.jpg", "meta.json"]}
        self.assertEqual(a._kept_elsewhere(THUMBS, a.last_script), [])


if __name__ == "__main__":
    unittest.main()
