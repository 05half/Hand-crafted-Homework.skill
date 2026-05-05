#!/usr/bin/env python3
"""把作业源文件提取到同目录的独立 job 文件夹中。

这个脚本只做本地提取：复制源文件、读取 DOCX/PDF 可选中文本、记录图片元数据，
并把结果写入每个 job 的 extracted/ 目录，供后续提示词生成脚本使用。
"""

from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from generate_prompts import DEFAULT_EXTENSIONS, collect_sources, copy_source, output_folder_for
from package_outputs import load_config


def write_json(path: Path, data: dict) -> None:
    """用 UTF-8 写出格式化 JSON，避免中文被 ASCII 转义。"""
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def clean_text(text: str) -> str:
    """Drop invalid surrogate code points that PDF extractors may emit."""
    return text.encode("utf-8", errors="ignore").decode("utf-8")


def docx_text(path: Path) -> str:
    """从 DOCX 的 word/document.xml 中提取正文文本。"""
    # DOCX 本质上是 zip 包；正文 XML 存在 word/document.xml。
    with zipfile.ZipFile(path) as docx:
        xml = docx.read("word/document.xml")
    root = ElementTree.fromstring(xml)
    parts: list[str] = []
    # 遍历 XML 节点，把文本、换行、制表符还原成近似纯文本。
    for elem in root.iter():
        tag = elem.tag.rsplit("}", 1)[-1]
        if tag == "t" and elem.text:
            parts.append(elem.text)
        elif tag in {"tab", "br", "cr"}:
            parts.append("\n" if tag != "tab" else "\t")
        elif tag == "p":
            parts.append("\n")
    return "".join(parts).strip()


def pdf_text(path: Path) -> tuple[str, dict]:
    """用 pypdf 提取 PDF 可选中文本，并返回页数等元数据。"""
    try:
        from pypdf import PdfReader
    except ImportError:
        # pypdf 是可选依赖；缺失时不阻塞整个流程，只记录原因。
        return "", {"pdf_text_extractor": "missing pypdf"}

    reader = PdfReader(str(path))
    pages = []
    # 逐页提取文本，保留每页字符数，方便判断是否需要人工视觉检查。
    for index, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        pages.append({"page": index, "chars": len(text), "text": text})
    combined = "\n\n".join(f"--- page {page['page']} ---\n{page['text']}" for page in pages if page["text"].strip())
    return clean_text(combined.strip()), {"pages": len(reader.pages), "page_text_chars": [page["chars"] for page in pages]}


def image_metadata(path: Path) -> tuple[str, dict]:
    """读取图片尺寸、模式和格式；图片题目内容仍需视觉检查。"""
    try:
        from PIL import Image
    except ImportError:
        # Pillow 是可选依赖；缺失时仍可继续创建 job 目录。
        return "", {"image_reader": "missing pillow"}

    with Image.open(path) as image:
        info = {"width": image.width, "height": image.height, "mode": image.mode, "format": image.format}
    text = "Image source. Use visual inspection for homework content."
    return text, info


def extract_source(source: Path) -> tuple[str, dict]:
    """根据文件扩展名选择对应提取方式。"""
    suffix = source.suffix.lower()
    # DOCX/PDF 尽量提取文本，图片只记录元数据。
    if suffix == ".docx":
        text = docx_text(source)
        return text, {"kind": "docx", "chars": len(text)}
    if suffix == ".pdf":
        text, meta = pdf_text(source)
        meta.update({"kind": "pdf", "chars": len(text)})
        if not text:
            meta["note"] = "No selectable text found; inspect the PDF visually."
        return text, meta
    if suffix in {".png", ".jpg", ".jpeg"}:
        text, meta = image_metadata(source)
        meta.update({"kind": "image", "chars": len(text)})
        return text, meta
    return "", {"kind": "unsupported", "note": f"Unsupported extension: {suffix}"}


def prepare_extraction(source: Path, config: dict, output_dir: Path | None) -> dict:
    """为单个源文件创建 job 目录、复制源文件并写出提取结果。"""
    job_dir = output_folder_for(source, config, output_dir)
    job_dir.mkdir(parents=True, exist_ok=True)
    # 保留一份 source.<ext>，保证每个 job 都能独立复现。
    copied = copy_source(source, job_dir)
    extracted_dir = job_dir / "extracted"
    extracted_dir.mkdir(parents=True, exist_ok=True)

    text, metadata = extract_source(source)
    text = clean_text(text)
    text_path = extracted_dir / "source_text.txt"
    metadata_path = extracted_dir / "source_metadata.json"
    # 后续 generate_prompts.py 会优先读取 source_text.txt 作为题目文本。
    text_path.write_text(text + ("\n" if text else ""), encoding="utf-8")
    metadata.update({"source_input": str(source), "source_copy": str(copied), "text_path": str(text_path)})
    write_json(metadata_path, metadata)
    return {"job_dir": str(job_dir), "source": str(source), "text_path": str(text_path), "metadata": metadata}


def main() -> int:
    """命令行入口：解析输入、加载配置并批量执行提取。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-files", nargs="*", type=Path, default=[], help="Homework files to extract.")
    parser.add_argument("--input-dir", type=Path, help="Process all supported files in this folder.")
    parser.add_argument("--output-dir", type=Path, help="Optional batch output root.")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "homework_config.yaml")
    args = parser.parse_args()

    if not args.input_files and not args.input_dir:
        # 至少需要显式文件列表或输入文件夹，否则不知道处理什么。
        raise SystemExit("Provide --input-files <file...> or --input-dir <folder>.")

    config = load_config(args.config)
    sources = collect_sources(args.input_files, args.input_dir, config.get("input_extensions", DEFAULT_EXTENSIONS), None)
    if not sources:
        raise SystemExit("No supported homework files found.")

    results = [prepare_extraction(source, config, args.output_dir) for source in sources]
    # 打印简短摘要，便于 IDE/终端里确认每个 job 的输出位置。
    print(f"Jobs: {len(results)}")
    for result in results:
        meta = result["metadata"]
        print(f"{Path(result['source']).name}: {result['job_dir']}")
        print(f"  extracted: {result['text_path']} ({meta.get('chars', 0)} chars)")
    return 0


if __name__ == "__main__":
    # 作为脚本执行时返回 main() 的退出码。
    raise SystemExit(main())
