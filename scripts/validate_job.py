#!/usr/bin/env python3
"""校验单个作业 job 输出目录是否完整。

这个脚本检查 source、extracted、prompts、manifest、PNG 页面和最终 PDF，
并尽量发现不同 job 的文件被混在同一个目录中的问题。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from package_outputs import DEFAULT_CONFIG, load_config, safe_job_id


def expected_page_name(pattern: str, job_id: str, page: int) -> str:
    """根据命名模板计算某一页应该使用的 PNG 文件名。"""
    return pattern.format(job_id=job_id, page=page)


def txt_files(folder: Path, ignored_names: set[str] | None = None) -> list[Path]:
    """返回目录下所有提示词 TXT 文件，按文件名排序。"""
    ignored_names = ignored_names or set()
    return sorted(path for path in folder.glob("*.txt") if path.is_file() and path.name not in ignored_names)


def main() -> int:
    """命令行入口：读取 job 目录并逐项校验输出完整性。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-dir", required=True, type=Path, help="One job output folder.")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "homework_config.yaml")
    parser.add_argument("--job-id", help="Expected job id. Defaults to job folder name.")
    args = parser.parse_args()

    job_dir = args.job_dir.resolve()
    job_id = safe_job_id(args.job_id or job_dir.name)
    # 读取配置后才能知道期望的页面、PDF、DOCX 命名规则。
    config = load_config(args.config)
    naming = config.get("naming", DEFAULT_CONFIG["naming"])
    page_planning = config.get("page_planning", {})
    page_plan_prompt_name = "page_plan.prompt.txt"
    if isinstance(page_planning, dict):
        page_plan_prompt_name = str(page_planning.get("prompt_txt", page_plan_prompt_name))
    page_pattern = naming.get("page_png", DEFAULT_CONFIG["naming"]["page_png"])
    pdf_name = naming.get("pdf", DEFAULT_CONFIG["naming"]["pdf"]).format(job_id=job_id)
    docx_name = naming.get("docx", DEFAULT_CONFIG["naming"]["docx"]).format(job_id=job_id)

    pages_dir = job_dir / "pages"
    extracted_dir = job_dir / "extracted"
    prompts_dir = job_dir / "prompts"
    manifest_path = job_dir / "image2_manifest.json"
    # 收集页面和提示词文件，后面会和 manifest 交叉比对数量。
    pages = sorted(pages_dir.glob("*.png")) if pages_dir.exists() else []
    ignored_prompt_names = {page_plan_prompt_name}
    prompt_txts = txt_files(prompts_dir, ignored_prompt_names) if prompts_dir.exists() else []
    errors = []

    if not any(path.is_file() for path in job_dir.glob("source.*")):
        # 每个 job 必须保存源文件副本，避免和原始输入路径强绑定。
        errors.append(f"Missing source.<ext> in {job_dir}")
    if not (extracted_dir / "source_text.txt").is_file():
        errors.append(f"Missing extraction text: {extracted_dir / 'source_text.txt'}")
    if not (extracted_dir / "source_metadata.json").is_file():
        errors.append(f"Missing extraction metadata: {extracted_dir / 'source_metadata.json'}")
    if not prompt_txts:
        errors.append(f"No prompt TXT found in {prompts_dir}")
    manifest_prompts = []
    if not manifest_path.exists():
        errors.append(f"Missing image2 manifest: {manifest_path.name}")
    else:
        try:
            # manifest 记录每页 prompt 与目标 PNG，是生成链路的核心索引。
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"Invalid image2 manifest: {exc}")
        else:
            if not isinstance(manifest, dict):
                errors.append("image2_manifest.json must be an object")
            else:
                manifest_prompts = manifest.get("prompts", [])
                if not isinstance(manifest_prompts, list):
                    errors.append("image2_manifest.json prompts must be a list")
                    manifest_prompts = []
                elif len(manifest_prompts) != len(prompt_txts):
                    errors.append(
                        f"Manifest prompts count ({len(manifest_prompts)}) does not match prompt TXT count ({len(prompt_txts)})"
                    )

    if not pages:
        errors.append(f"No PNG pages found in {pages_dir}")
    else:
        # PNG 页面数量必须和 manifest 里的 prompt 数量一致。
        if len(manifest_prompts) != len(pages):
            errors.append(f"Manifest prompts count ({len(manifest_prompts)}) does not match PNG page count ({len(pages)})")
        for index, page in enumerate(pages, start=1):
            expected = expected_page_name(page_pattern, job_id, index)
            if page.name != expected:
                errors.append(f"Page {index} should be {expected}, found {page.name}")
            if job_id not in page.stem:
                errors.append(f"Page filename does not include job id {job_id}: {page.name}")

    all_outputs = list(job_dir.glob("*")) + list(pages_dir.glob("*") if pages_dir.exists() else [])
    # 查找不像当前 job 的输出文件，避免不同作业混到同一个目录。
    other_job_names = [
        path.name
        for path in all_outputs
        if path.is_file()
        and path.suffix.lower() in {".png", ".pdf", ".docx", ".txt"}
        and path.name not in ignored_prompt_names
        and not path.name.startswith(job_id)
        and not re.match(r"source\.", path.name, flags=re.IGNORECASE)
    ]
    if other_job_names:
        errors.append("Possible cross-job files: " + ", ".join(sorted(other_job_names)))

    if "pdf" in set(config.get("output_formats", DEFAULT_CONFIG["output_formats"])) and not (job_dir / pdf_name).exists():
        # 当前配置要求 PDF，所以最终 PDF 缺失就是失败。
        errors.append(f"Missing PDF: {pdf_name}")
    formats = set(config.get("output_formats", DEFAULT_CONFIG["output_formats"]))
    if (job_dir / docx_name).exists():
        # 这个 skill 的最终交付只允许 PDF；DOCX 若存在则提示清理。
        errors.append(f"Unexpected DOCX final output: {docx_name}")

    if errors:
        # 所有错误一次性输出，便于用户集中修复。
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print(f"OK: {job_id} prompts={len(prompt_txts)} pages={len(pages)}; expected packaged outputs found.")
    return 0


if __name__ == "__main__":
    # 作为脚本执行时返回 main() 的退出码。
    raise SystemExit(main())
