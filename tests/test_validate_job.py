import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from validate_job import txt_files


class ValidateJobPromptTests(unittest.TestCase):
    def test_txt_files_excludes_page_planning_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            prompts = Path(tmp)
            (prompts / "page_plan.prompt.txt").write_text("plan", encoding="utf-8")
            (prompts / "homework_page001.prompt.txt").write_text("page 1", encoding="utf-8")
            (prompts / "homework_page002.prompt.txt").write_text("page 2", encoding="utf-8")

            names = [path.name for path in txt_files(prompts, ignored_names={"page_plan.prompt.txt"})]

            self.assertEqual(["homework_page001.prompt.txt", "homework_page002.prompt.txt"], names)


if __name__ == "__main__":
    unittest.main()
