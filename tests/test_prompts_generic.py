"""Nothing hardcoded: no prompt hints, few-shot examples or code tailored to particular tasks or domains.
The agent and harness code (except the user-editable environment facts in config) must not mention domain words."""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOMAIN_WORDS = ["email", "e-mail", "smtp", "mailpit", "anna", "meeting", "friday", "weather", "prague", "forecast",
              "meteo", "pdf", "ocr", "tesseract", "markdown", "poppler", "scan", "garden", "water"]
CHECKED = ["mini/agent", "mini/core", "mini/harness", "sandbox"]


class GenericPromptsTest(unittest.TestCase):
    def test_no_domain_words_in_agent_or_harness(self):
        hits = []
        for d in CHECKED:
            for base, _, files in os.walk(os.path.join(ROOT, d)):
                for f in files:
                    if not f.endswith((".py", ".md", ".txt", "Containerfile.base")):
                        continue
                    p = os.path.join(base, f)
                    rel = os.path.relpath(p, ROOT)
                    text = open(p).read().lower()
                    for w in DOMAIN_WORDS:
                        for m in re.finditer(r"\b" + re.escape(w) + r"\b", text):
                            line = text[:m.start()].count("\n") + 1
                            hits.append(f"{rel}:{line}: {w}")
        self.assertEqual(hits, [], "domain-specific words found:\n" + "\n".join(hits))

    def test_no_seeded_tools(self):
        state = os.path.join(ROOT, "state")
        import subprocess
        tracked = subprocess.run(["git", "ls-files", "state", "runs"], cwd=ROOT, capture_output=True, text=True).stdout
        self.assertEqual(tracked.strip(), "", "registry state or run output is committed")


if __name__ == "__main__":
    unittest.main()
