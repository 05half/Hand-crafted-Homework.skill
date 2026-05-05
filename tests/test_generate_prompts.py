import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from generate_prompts import page_plan_paths, prepare_job, split_question_text
from package_outputs import DEFAULT_CONFIG


class GeneratePromptSplittingTests(unittest.TestCase):
    def test_split_question_text_uses_page_markers(self):
        text = "--- page 1 ---\n第一题\n\n--- page 2 ---\n第二题\n\n--- page 3 ---\n第三题"
        self.assertEqual(["第一题", "第二题\n\n第三题"], split_question_text(text, 2))

    def base_config(self):
        return {
            **DEFAULT_CONFIG,
            "handwriting_prompt": "style",
            "prompt_template": {"exact_prompt": "{handwriting_prompt}"},
            "prompt_cache": {
                "cache_dir": "_image2_cache",
                "base_prompt": "base_prompt.txt",
                "sample_manifest": "sample_image.json",
                "image2_manifest": "image2_manifest.json",
            },
            "generation": {"default_tool": "imagegen", "imagegen_status": "ready_for_imagegen"},
            "page_planning": {
                "prompt_txt": "page_plan.prompt.txt",
                "output_json": "page_plan.json",
                "prompt": "先完整做一遍题，再按答案文字量规划分页，输出严格 JSON。",
            },
        }

    def test_prepare_job_stops_after_writing_ai_page_plan_prompt_when_plan_is_missing(self):
        try:
            from pypdf import PdfWriter
        except ImportError:
            self.skipTest("pypdf is required to create the sample PDF")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "needs_plan.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=595, height=842)
            with source.open("wb") as handle:
                writer.write(handle)

            job_dir = root / "needs_plan"
            extracted = job_dir / "extracted"
            extracted.mkdir(parents=True)
            (extracted / "source_text.txt").write_text("题目甲\n题目乙", encoding="utf-8")
            (extracted / "source_metadata.json").write_text(
                json.dumps({"kind": "pdf", "pages": 1, "chars": 6}),
                encoding="utf-8",
            )

            with self.assertRaises(SystemExit) as caught:
                prepare_job(source, job_dir, None, "", [], self.base_config(), "imagegen")

            prompt_path, plan_path = page_plan_paths(job_dir, self.base_config())
            self.assertTrue(prompt_path.exists())
            self.assertFalse(plan_path.exists())
            prompt = prompt_path.read_text(encoding="utf-8")
            self.assertIn("先完整做一遍题", prompt)
            self.assertIn(str(plan_path), prompt)
            self.assertIn("题目甲", prompt)
            self.assertIn("page_plan.json", str(caught.exception))

    def test_prepare_job_splits_pdf_attachment_per_prompt(self):
        try:
            from pypdf import PdfWriter
        except ImportError:
            self.skipTest("pypdf is required to create the sample PDF")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "many_questions.pdf"
            writer = PdfWriter()
            for _ in range(4):
                writer.add_blank_page(width=595, height=842)
            with source.open("wb") as handle:
                writer.write(handle)

            job_dir = root / "many_questions"
            extracted = job_dir / "extracted"
            extracted.mkdir(parents=True)
            (extracted / "source_text.txt").write_text("", encoding="utf-8")
            (extracted / "source_metadata.json").write_text(
                json.dumps({"kind": "pdf", "pages": 4, "chars": 0}),
                encoding="utf-8",
            )

            config = self.base_config()
            _prompt_path, plan_path = page_plan_paths(job_dir, config)
            plan_path.write_text(
                json.dumps(
                    {
                        "answer_page_count": 2,
                        "pages": [
                            {"page": 1, "question_scope": "1-2", "answer_outline": "前半", "estimated_handwritten_chars": 500},
                            {"page": 2, "question_scope": "3-4", "answer_outline": "后半", "estimated_handwritten_chars": 500},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            manifest = prepare_job(source, job_dir, None, "", [], config, "imagegen")

            self.assertEqual(2, len(manifest["prompts"]))
            self.assertEqual(2, manifest["answer_page_estimate"])
            self.assertEqual("page_plan.json", Path(manifest["page_plan"]["path"]).name)
            attached = [Path(item["attach_source_file"]) for item in manifest["prompts"]]
            self.assertTrue(all(path.name.startswith("source_part") for path in attached))
            self.assertTrue(all(path.exists() for path in attached))
            self.assertTrue(all(item["question_text_embedded_in_prompt"] for item in manifest["prompts"]))

    def test_prepare_job_crops_single_page_pdf_when_text_is_dense(self):
        try:
            from pypdf import PdfReader, PdfWriter
        except ImportError:
            self.skipTest("pypdf is required to create the sample PDF")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "dense_one_page.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=595, height=842)
            with source.open("wb") as handle:
                writer.write(handle)

            job_dir = root / "dense_one_page"
            extracted = job_dir / "extracted"
            extracted.mkdir(parents=True)
            (extracted / "source_text.txt").write_text("x" * 2600, encoding="utf-8")
            (extracted / "source_metadata.json").write_text(
                json.dumps({"kind": "pdf", "pages": 1, "chars": 2600}),
                encoding="utf-8",
            )

            config = self.base_config()
            _prompt_path, plan_path = page_plan_paths(job_dir, config)
            plan_path.write_text(
                json.dumps(
                    {
                        "answer_page_count": 2,
                        "pages": [
                            {"page": 1, "question_scope": "dense first half", "answer_outline": "part 1", "estimated_handwritten_chars": 800},
                            {"page": 2, "question_scope": "dense second half", "answer_outline": "part 2", "estimated_handwritten_chars": 800},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            manifest = prepare_job(source, job_dir, None, "", [], config, "imagegen")

            self.assertEqual(2, len(manifest["prompts"]))
            for item in manifest["prompts"]:
                reader = PdfReader(item["attach_source_file"])
                self.assertEqual(1, len(reader.pages))
                prompt = Path(item["prompt_path"]).read_text(encoding="utf-8")
                self.assertIn("题目文本", prompt)
                self.assertIn("请根据以上题目文本", prompt)


if __name__ == "__main__":
    unittest.main()
