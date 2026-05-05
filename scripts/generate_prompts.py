#!/usr/bin/env python3
"""创建同目录 job 文件夹，并生成直接给内置 imagegen 使用的提示词文件。

这个脚本负责提示词主链路：读取配置、复制源文件、生成只包含手写风格提示词的
base_prompt.txt 和 prompts/*.prompt.txt，并把题目/笔迹图片附件写入 image2_manifest.json。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
from copy import copy
from pathlib import Path

from package_outputs import DEFAULT_CONFIG, load_config, safe_job_id


# 默认支持作为作业源文件输入的扩展名。
DEFAULT_EXTENSIONS = [".png", ".jpg", ".jpeg", ".pdf", ".docx"]
# 默认支持作为参考笔记输入的扩展名。
DEFAULT_REFERENCE_EXTENSIONS = [".txt", ".md", ".docx", ".pdf"]


def sha256(path: Path) -> str:
    """计算文件 SHA-256，用于判断源文件和样例图是否变化。"""
    h = hashlib.sha256()
    # 分块读取，避免大文件一次性读入内存。
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def text_sha256(text: str) -> str:
    """计算文本 SHA-256，用于记录基础提示词内容是否变化。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def collect_sources(input_files: list[Path], input_dir: Path | None, extensions: list[str], sample_image: Path | None) -> list[Path]:
    """收集待处理作业源文件，并去掉重复路径和样例图本身。"""
    allowed = {ext.lower() for ext in extensions}
    sample = sample_image.resolve() if sample_image else None
    sources: list[Path] = []

    # 先处理用户显式传入的文件路径。
    for path in input_files:
        source = path.resolve()
        if not source.is_file():
            raise SystemExit(f"Input file does not exist: {source}")
        if source.suffix.lower() not in allowed:
            raise SystemExit(f"Unsupported input extension: {source}")
        if not sample or source != sample:
            sources.append(source)

    # 再处理输入目录中的所有支持类型文件。
    if input_dir:
        folder = input_dir.resolve()
        if not folder.is_dir():
            raise SystemExit(f"Input folder does not exist: {folder}")
        for source in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
            if source.is_file() and source.suffix.lower() in allowed and (not sample or source.resolve() != sample):
                sources.append(source.resolve())

    unique = []
    seen = set()
    # Windows 路径通常大小写不敏感，这里用小写字符串去重。
    for source in sources:
        key = str(source).lower()
        if key not in seen:
            unique.append(source)
            seen.add(key)
    return unique


def default_sample(config: dict, explicit_sample: Path | None) -> Path | None:
    """解析笔迹样例图片路径：命令行参数优先，其次使用配置默认值。"""
    if explicit_sample:
        return explicit_sample.resolve()
    sample_config = config.get("handwriting_sample", {})
    configured = sample_config.get("default_path") if isinstance(sample_config, dict) else None
    if not configured:
        return None
    candidate = Path(configured)
    if not candidate.is_absolute():
        # 配置里的相对路径默认相对于 skill 根目录。
        candidate = Path(__file__).resolve().parents[1] / candidate
    return candidate.resolve() if candidate.is_file() else None


def output_folder_for(source: Path, config: dict, output_dir: Path | None) -> Path:
    """根据源文件、配置和可选输出根目录计算 job 文件夹路径。"""
    job_id = safe_job_id(source.stem)
    if output_dir:
        return output_dir.resolve() / job_id
    output_config = config.get("output", {})
    pattern = "{job_id}"
    if isinstance(output_config, dict):
        pattern = output_config.get("default_folder", pattern)
    # 默认放在源文件旁边，例如 math01.png -> math01/。
    return source.parent / pattern.format(job_id=job_id)


def copy_source(source: Path, job_dir: Path) -> Path:
    """把源文件复制到 job 目录中的 source.<ext>，内容不同时才覆盖。"""
    target = job_dir / f"source{source.suffix.lower()}"
    if not target.exists() or sha256(source) != sha256(target):
        shutil.copy2(source, target)
    return target


def read_docx_text(path: Path) -> str:
    """从 DOCX 文件中读取正文纯文本。"""
    import zipfile
    from xml.etree import ElementTree

    with zipfile.ZipFile(path) as docx:
        xml = docx.read("word/document.xml")
    root = ElementTree.fromstring(xml)
    parts: list[str] = []
    # 只提取正文文字和简单换行/制表符，不解析复杂版式。
    for elem in root.iter():
        tag = elem.tag.rsplit("}", 1)[-1]
        if tag == "t" and elem.text:
            parts.append(elem.text)
        elif tag in {"tab", "br", "cr"}:
            parts.append("\n" if tag != "tab" else "\t")
        elif tag == "p":
            parts.append("\n")
    return "".join(parts).strip()


def read_pdf_text(path: Path) -> str:
    """从 PDF 中读取可选中文本；无法导入 pypdf 时返回空字符串。"""
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""

    reader = PdfReader(str(path))
    pages = []
    # 给每页加页码标签，方便生成器理解题目来源。
    for index, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        if text.strip():
            pages.append(f"--- page {index} ---\n{text}")
    return "\n\n".join(pages).strip()


def read_text_file(path: Path) -> str:
    """用常见编码读取文本文件，兼容 UTF-8 和 GB18030。"""
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return path.read_text(encoding=encoding).strip()
        except UnicodeDecodeError:
            continue
    return path.read_text(errors="replace").strip()


def readable_text(path: Path) -> str:
    """根据参考文件扩展名提取可读文本。"""
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md"}:
        return read_text_file(path)
    if suffix == ".docx":
        return read_docx_text(path)
    if suffix == ".pdf":
        return read_pdf_text(path)
    return ""


def collect_reference_notes(note_files: list[Path], note_texts: list[str]) -> tuple[str, list[dict]]:
    """汇总参考笔记元信息；当前不把笔记文本拼入图像 prompt。"""
    parts: list[str] = []
    records: list[dict] = []

    # 命令行 --reference-text 传入的内联笔记。
    for index, raw_text in enumerate(note_texts, start=1):
        text = raw_text.strip()
        if text:
            title = f"inline_note_{index}"
            parts.append(f"## {title}\n{text}")
            records.append({"kind": "inline", "title": title, "chars": len(text)})

    for path in note_files:
        note = path.resolve()
        if not note.is_file():
            raise SystemExit(f"Reference note does not exist: {note}")
        if note.suffix.lower() not in DEFAULT_REFERENCE_EXTENSIONS:
            raise SystemExit(f"Unsupported reference note extension: {note}")
        # 文件笔记会尽量转成纯文本；PDF/DOCX 若无法提取则留下提示。
        text = readable_text(note)
        parts.append(f"## {note.name}\n{text or '[No selectable text extracted; inspect attached/reference file if available.]'}")
        records.append({"kind": "file", "path": str(note), "sha256": sha256(note), "chars": len(text)})

    return "\n\n".join(parts).strip(), records


def format_prompt_template(value: str, config: dict) -> str:
    """替换提示词模板里的占位符。"""
    return (
        value.replace("{handwriting_prompt}", str(config.get("handwriting_prompt", "")))
        .replace("{direct_answer_style}", str(config.get("direct_answer_style", config.get("answer_style", ""))))
    )


def write_base_prompt(job_dir: Path, config: dict, sample_image: Path | None) -> tuple[Path, str]:
    # 提示词输入点 3：从 homework_config.yaml 的 prompt_template 读取基础提示词模板。
    """写出稳定的基础提示词 base_prompt.txt，并返回文件路径和哈希。"""
    cache = config.get("prompt_cache", {})
    prompt_template = config.get("prompt_template", {})
    cache_dir = job_dir / cache.get("cache_dir", "_image2_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    base_path = cache_dir / cache.get("base_prompt", "base_prompt.txt")
    exact_prompt = prompt_template.get("exact_prompt", "")
    if exact_prompt:
        # 提示词输入点 4：把 {handwriting_prompt} 等占位符替换成最终基础提示词，并写入 base_prompt.txt。
        # homework_config.yaml 当前 handwriting_prompt 原文：
        # A4白纸上的手写版答案，简略回答，只包含最主要的公式、图片、表格和结果，
        # 极端潦草，连笔严重，字迹模糊不清，过程需要包含勾抹、涂改。
        # 图像、表格扭曲变形。极端潦草，连笔严重，字迹模糊不清，
        # 过程需要包含勾抹、涂改。图像、表格扭曲变形。极端潦草，
        # 连笔严重，字迹模糊不清，过程需要包含勾抹、涂改。
        # 图像、表格扭曲变形。不同位置出现的相同字母和汉字连笔严重程度、
        # 大小、扭曲程度不同。不要加入对号、横杠、方框、题型说明、
        # 重点加粗等。笔迹参考另一张图片。
        base = format_prompt_template(str(exact_prompt), config)
        base_path.write_text(base + "\n", encoding="utf-8")
        return base_path, text_sha256(base)
    sample_part = prompt_template.get("reference_without_sample", "")
    if sample_image:
        # 如果有样例图，就使用带样例图说明的模板片段。
        sample_part = prompt_template.get("reference_with_sample", "")
    base_parts = [
        prompt_template.get("task", ""),
        prompt_template.get("style", ""),
        sample_part,
        prompt_template.get("paper", ""),
        prompt_template.get("isolation", ""),
        prompt_template.get("negative", ""),
    ]
    base = "\n".join(format_prompt_template(part, config) for part in base_parts if part)
    base_path.write_text(base + "\n", encoding="utf-8")
    return base_path, text_sha256(base)


def write_sample_manifest(job_dir: Path, config: dict, sample_image: Path | None) -> dict | None:
    """记录样例图片路径和哈希，供生成时确认样式参考。"""
    cache = config.get("prompt_cache", {})
    cache_dir = job_dir / cache.get("cache_dir", "_image2_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = cache_dir / cache.get("sample_manifest", "sample_image.json")
    if not sample_image:
        # 没有样例图也写 manifest，便于后续流程判断。
        manifest_path.write_text(json.dumps({"sample_image": None}, ensure_ascii=False, indent=2), encoding="utf-8")
        return None
    data = {
        "path": str(sample_image.resolve()),
        "sha256": sha256(sample_image),
        "usage": config.get("prompt_template", {}).get("sample_manifest_usage", ""),
    }
    manifest_path.write_text(json.dumps({"sample_image": data}, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def extracted_question_text(job_dir: Path) -> str:
    """读取题目文本长度供 manifest 记录；当前不拼入图像 prompt。"""
    text_path = job_dir / "extracted" / "source_text.txt"
    if text_path.is_file():
        return read_text_file(text_path)
    return ""


def source_metadata(job_dir: Path) -> dict:
    """读取本地提取出的源文件元数据，用于估算答案页数和裁切附件。"""
    metadata_path = job_dir / "extracted" / "source_metadata.json"
    if not metadata_path.is_file():
        return {}
    try:
        data = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def page_plan_paths(job_dir: Path, config: dict) -> tuple[Path, Path]:
    """Return the AI page-planning prompt path and the expected JSON output path."""
    prompts_dir = job_dir / "prompts"
    planning = config.get("page_planning", {})
    prompt_name = "page_plan.prompt.txt"
    output_name = "page_plan.json"
    if isinstance(planning, dict):
        prompt_name = str(planning.get("prompt_txt", prompt_name))
        output_name = str(planning.get("output_json", output_name))
    return prompts_dir / prompt_name, job_dir / output_name


def page_planning_prompt(config: dict) -> str:
    """Read the configurable prompt used before imagegen page generation."""
    planning = config.get("page_planning", {})
    if isinstance(planning, dict) and planning.get("prompt"):
        return format_prompt_template(str(planning["prompt"]), config)
    return (
        "请先完整做一遍题目，估算手写答案需要分配到几张 A4 纸上。"
        "按题量、公式密度、必要过程和可读性分页，不要按固定字数或源 PDF 页数机械分页。"
        "输出 page_plan.json：answer_page_count 为正整数，pages 为数组；"
        "每项包含 page、question_scope、answer_outline、estimated_handwritten_chars。"
    )


def write_page_plan_prompt(
    job_dir: Path,
    source: Path,
    copied_source: Path,
    question_text: str,
    metadata: dict,
    reference_notes: str,
    config: dict,
) -> Path:
    """Write the prompt that asks the AI agent to solve once and plan answer pages."""
    prompt_path, plan_path = page_plan_paths(job_dir, config)
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    source_description = (
        f"源文件: {source}\n"
        f"job 内源文件副本: {copied_source}\n"
        f"元数据: {json.dumps(metadata, ensure_ascii=False)}\n"
        f"题目文本字符数: {len(question_text)}\n"
    )
    if question_text.strip():
        source_description += f"\n已提取题目文本:\n{question_text.strip()}\n"
    else:
        source_description += "\n未提取到可用题目文本；请视觉检查源文件或附件后再规划分页。\n"
    if reference_notes.strip():
        source_description += f"\n参考笔记:\n{reference_notes.strip()}\n"
    prompt = (
        page_planning_prompt(config).rstrip()
        + "\n\n"
        + source_description
        + "\n请把结果保存为严格 JSON 到以下路径：\n"
        + str(plan_path)
        + "\n"
    )
    prompt_path.write_text(prompt, encoding="utf-8")
    return prompt_path


def load_page_plan(job_dir: Path, config: dict) -> dict | None:
    """Load the AI-authored page_plan.json. Return None when it has not been created yet."""
    _prompt_path, plan_path = page_plan_paths(job_dir, config)
    if not plan_path.is_file():
        return None
    data = json.loads(plan_path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise SystemExit(f"Invalid page plan, expected JSON object: {plan_path}")
    count = data.get("answer_page_count")
    pages = data.get("pages")
    if not isinstance(count, int) or count < 1:
        raise SystemExit(f"Invalid page plan answer_page_count: {plan_path}")
    if not isinstance(pages, list) or len(pages) != count:
        raise SystemExit(f"Invalid page plan pages length: {plan_path}")
    for index, item in enumerate(pages, start=1):
        if not isinstance(item, dict):
            raise SystemExit(f"Invalid page plan item #{index}: {plan_path}")
        item.setdefault("page", index)
    return data


def estimate_answer_pages_from_rule_removed(question_text: str, metadata: dict) -> int:
    """按题目量粗估需要几张手写答案图。"""
    raise SystemExit("Hard-rule answer page estimation has been removed; create page_plan.json from the AI planning prompt.")


def split_ranges(total: int, parts: int) -> list[tuple[int, int]]:
    """把 total 个 0-based 页面均匀分成 parts 段，返回闭开区间。"""
    parts = max(1, min(parts, total))
    return [(i * total // parts, (i + 1) * total // parts) for i in range(parts)]


def split_question_text(question_text: str, parts: int) -> list[str]:
    """把已提取题目文本拆成与答案页一一对应的片段。"""
    parts = max(1, parts)
    text = question_text.strip()
    if not text:
        return [""] * parts

    page_marker = re.compile(r"(?m)^--- page\s+\d+\s*---\s*$")
    markers = list(page_marker.finditer(text))
    if markers:
        page_chunks: list[str] = []
        for index, marker in enumerate(markers):
            start = marker.end()
            end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
            chunk = text[start:end].strip()
            if chunk:
                page_chunks.append(chunk)
        if page_chunks:
            ranges = split_ranges(len(page_chunks), parts)
            return ["\n\n".join(page_chunks[start:end]).strip() for start, end in ranges]

    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    if len(paragraphs) >= parts:
        chunks = ["" for _ in range(parts)]
        lengths = [0 for _ in range(parts)]
        for paragraph in paragraphs:
            index = min(range(parts), key=lambda item: lengths[item])
            chunks[index] = (chunks[index] + "\n\n" + paragraph).strip()
            lengths[index] += len(paragraph)
        return chunks

    size = math.ceil(len(text) / parts)
    chunks = [text[index * size : (index + 1) * size].strip() for index in range(parts)]
    return chunks + [""] * (parts - len(chunks))


def split_pdf_source(source: Path, attachments_dir: Path, parts: int) -> list[Path]:
    """把 PDF 按页均分为若干分片附件。"""
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        return [source]

    reader = PdfReader(str(source))
    if len(reader.pages) <= 1:
        return crop_pdf_pages(reader, attachments_dir, parts, PdfWriter)
    if parts > len(reader.pages):
        return crop_pdf_pages(reader, attachments_dir, parts, PdfWriter)
    targets: list[Path] = []
    for index, (start, end) in enumerate(split_ranges(len(reader.pages), parts), start=1):
        writer = PdfWriter()
        for page in reader.pages[start:end]:
            writer.add_page(page)
        target = attachments_dir / f"source_part{index:03}.pdf"
        with target.open("wb") as handle:
            writer.write(handle)
        targets.append(target)
    return targets


def crop_pdf_pages(reader, attachments_dir: Path, parts: int, pdf_writer) -> list[Path]:
    """按纵向区域裁切 PDF 页面，适合单页密集题目。"""
    targets: list[Path] = []
    total_pages = len(reader.pages)
    part_index = 1
    for page_index, page in enumerate(reader.pages):
        page_parts = parts // total_pages + (1 if page_index < parts % total_pages else 0)
        for top_start, top_end in split_ranges(10000, page_parts):
            cropped = copy(page)
            box = page.mediabox
            height = float(box.top) - float(box.bottom)
            top = float(box.top) - height * top_start / 10000
            bottom = float(box.top) - height * top_end / 10000
            cropped.mediabox.lower_left = (float(box.left), bottom)
            cropped.mediabox.upper_right = (float(box.right), top)
            cropped.cropbox.lower_left = (float(box.left), bottom)
            cropped.cropbox.upper_right = (float(box.right), top)
            writer = pdf_writer()
            writer.add_page(cropped)
            target = attachments_dir / f"source_part{part_index:03}.pdf"
            with target.open("wb") as handle:
                writer.write(handle)
            targets.append(target)
            part_index += 1
    return targets


def split_image_source(source: Path, attachments_dir: Path, parts: int) -> list[Path]:
    """把图片按高度均分为若干分片附件；缺少 Pillow 时退回原图。"""
    if parts <= 1:
        return [source]
    try:
        from PIL import Image
    except ImportError:
        return [source]

    targets: list[Path] = []
    with Image.open(source) as image:
        for index, (top, bottom) in enumerate(split_ranges(image.height, parts), start=1):
            target = attachments_dir / f"source_part{index:03}{source.suffix.lower()}"
            image.crop((0, top, image.width, bottom)).save(target)
            targets.append(target)
    return targets


def source_parts(copied_source: Path, job_dir: Path, answer_pages: int) -> list[Path]:
    """根据估算页数生成传给 imagegen 的源文件分片附件。"""
    attachments_dir = job_dir / "attachments"
    attachments_dir.mkdir(parents=True, exist_ok=True)
    suffix = copied_source.suffix.lower()
    if suffix == ".pdf":
        parts = split_pdf_source(copied_source, attachments_dir, answer_pages)
    elif suffix in {".png", ".jpg", ".jpeg"}:
        parts = split_image_source(copied_source, attachments_dir, answer_pages)
    else:
        parts = [copied_source]
    return parts or [copied_source]


def direct_page_prompt(
    base_prompt: str,
    job_id: str,
    page_index: int,
    source: Path,
    copied_source: Path,
    question_chunk: str,
    page_plan_item: dict | None,
    reference_notes: str,
    config: dict,
) -> str:
    # 提示词输入点 5：最终 prompt 包含基础手写风格和当前分片题目文本。
    """返回单页最终提示词。

    当前流程把拆分后的题目文本直接拼入 prompt，以便 imagegen 在没有源文件
    附件能力时仍能按题目内容生成答案页。
    """
    prompt = base_prompt.rstrip()
    if question_chunk.strip():
        prompt += (
            f"\n\n题目文本（{job_id}，第 {page_index} 部分）：\n"
            f"{question_chunk.strip()}\n\n"
            "请根据以上题目文本生成这一部分的简略手写答案页。"
        )
    else:
        prompt += (
            f"\n\n题目文本（{job_id}，第 {page_index} 部分）：\n"
            "[未提取到可用题目文字；请只生成空白的手写答案占位页，不要编造题目内容。]"
        )
    if page_plan_item:
        prompt += (
            "\n\nAI 分页计划（本页）：\n"
            + json.dumps(page_plan_item, ensure_ascii=False, indent=2)
            + "\n请只生成本页计划对应范围的简略手写答案。"
        )
    return prompt.rstrip() + "\n"


def generation_tool(config: dict, explicit_tool: str | None) -> str:
    """决定使用哪个生成器：命令行参数优先，其次读配置。"""
    if explicit_tool:
        return explicit_tool
    generation = config.get("generation", {})
    if isinstance(generation, dict) and generation.get("default_tool"):
        return str(generation["default_tool"])
    return "imagegen"


def mark_ready_for_imagegen(manifest: dict, config: dict) -> None:
    """把 manifest 标记为等待 Codex 内置 imagegen 处理。"""
    generation = config.get("generation", {})
    status = "ready_for_imagegen"
    if isinstance(generation, dict) and generation.get("imagegen_status"):
        status = str(generation["imagegen_status"])
    manifest["generation"]["status"] = status


def write_manifest(job_dir: Path, config: dict, manifest: dict) -> Path:
    """把生成任务 manifest 写到 job 目录。"""
    cache = config.get("prompt_cache", {})
    manifest_path = job_dir / cache.get("image2_manifest", "image2_manifest.json")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


def prepare_job(
    source: Path,
    job_dir: Path,
    sample_image: Path | None,
    reference_notes: str,
    reference_note_records: list[dict],
    config: dict,
    tool: str,
) -> dict:
    """为单个源文件准备 prompt、manifest，并按配置触发或标记生成器。"""
    job_id = safe_job_id(source.stem)
    naming = config.get("naming", DEFAULT_CONFIG["naming"])
    cache = config.get("prompt_cache", {})
    prompts_dir = job_dir / "prompts"
    pages_dir = job_dir / "pages"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    pages_dir.mkdir(parents=True, exist_ok=True)

    # 每个 job 都保留源文件副本、基础提示词和样例图记录。
    copied_source = copy_source(source, job_dir)
    base_path, base_hash = write_base_prompt(job_dir, config, sample_image)
    sample_data = write_sample_manifest(job_dir, config, sample_image)
    base_prompt = base_path.read_text(encoding="utf-8")
    question_text = extracted_question_text(job_dir)
    metadata = source_metadata(job_dir)
    page_plan_prompt_path = write_page_plan_prompt(job_dir, source, copied_source, question_text, metadata, reference_notes, config)
    page_plan = load_page_plan(job_dir, config)
    if page_plan is None:
        _prompt_path, plan_path = page_plan_paths(job_dir, config)
        raise SystemExit(
            "AI page planning required before imagegen prompts. "
            f"Use {page_plan_prompt_path} to create {plan_path}, then rerun generate_prompts.py."
        )
    answer_pages = int(page_plan["answer_page_count"])
    attachments = source_parts(copied_source, job_dir, answer_pages)
    question_chunks = split_question_text(question_text, len(attachments))

    manifest = {
        # job 基本信息，便于外部工具定位源文件和输出目录。
        "job_id": job_id,
        "source_input": str(source),
        "source_copy": str(copied_source),
        "job_dir": str(job_dir),
        "base_prompt_path": str(base_path),
        "base_prompt_sha256": base_hash,
        "sample_image": sample_data,
        "reference_notes": {
            # 参考笔记只记录元信息和字符数，不重复展开过长文本。
            "records": reference_note_records,
            "chars": len(reference_notes),
        },
        "answer_page_estimate": answer_pages,
        "page_plan": {
            "path": str(page_plan_paths(job_dir, config)[1]),
            "prompt_path": str(page_plan_prompt_path),
            "data": page_plan,
        },
        "source_parts": [str(path) for path in attachments],
        "generation": {
            # 通用生成状态；本脚本只准备内置 imagegen 所需的 manifest，不生成 PNG。
            "tool": tool,
            "status": "pending",
        },
        "imagegen": {
            # imagegen 专用状态。
            "status": "pending" if tool == "imagegen" else "not_used",
            # 提示词输入点 7B：使用内置 imagegen 时，人工/代理读取 manifest.prompts[].prompt_path 后调用 image_gen。
            "note": "Use Codex built-in imagegen/image_gen for each prompt. The split question text is embedded in each prompt file; save each returned PNG directly to its target_png path, then package PNGs from the pages folder into PDF.",
        },
        "prompts": [],
    }

    for stale_prompt in prompts_dir.glob("*.prompt.txt"):
        if stale_prompt != page_plan_prompt_path:
            stale_prompt.unlink()

    for index, attachment in enumerate(attachments, start=1):
        question_chunk = question_chunks[index - 1] if index - 1 < len(question_chunks) else ""
        page_plan_item = page_plan["pages"][index - 1] if index - 1 < len(page_plan["pages"]) else None
        prompt_name = naming.get("prompt_txt", "{job_id}_page{page:03}.prompt.txt").format(job_id=job_id, page=index)
        prompt_path = prompts_dir / prompt_name
        # 提示词输入点 6：最终单页 prompt 文件在这里落盘到 prompts/*.prompt.txt。
        prompt_path.write_text(
            direct_page_prompt(base_prompt, job_id, index, source, attachment, question_chunk, page_plan_item, reference_notes, config),
            encoding="utf-8",
        )
        target_png = pages_dir / naming.get("page_png", DEFAULT_CONFIG["naming"]["page_png"]).format(job_id=job_id, page=index)
        manifest["prompts"].append(
            {
                # 单页 prompt 记录：生成器读取 prompt_path，输出到 target_png。
                "page": index,
                "prompt_path": str(prompt_path),
                "target_png": str(target_png),
                "attach_sample_image": str(sample_image) if sample_image else None,
                "attach_source_file": str(attachment),
                "source_part": index,
                "source_parts_total": len(attachments),
                "question_text_chars": len(question_text),
                "question_text_chunk_chars": len(question_chunk),
                "question_text_embedded_in_prompt": True,
                "reference_notes_chars": len(reference_notes),
            }
        )

    write_manifest(job_dir, config, manifest)
    if tool == "imagegen":
        # imagegen 模式只准备 prompt 和 manifest，等待 Codex 内置 image_gen 调用。
        mark_ready_for_imagegen(manifest, config)
        manifest["imagegen"]["status"] = manifest["generation"]["status"]
    else:
        raise SystemExit(f"Unsupported generator: {tool}")
    write_manifest(job_dir, config, manifest)
    return manifest


def main() -> int:
    """命令行入口：解析参数、加载配置、批量准备生成任务。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-files", nargs="*", type=Path, default=[], help="Full paths to homework files pasted from an IDE chat.")
    parser.add_argument("--input-dir", type=Path, help="Optional compatibility mode: process all supported files in this folder.")
    parser.add_argument("--output-dir", type=Path, help="Optional batch output root. Defaults to one folder beside each source file.")
    parser.add_argument("--sample-image", type=Path, help="Optional handwriting sample image path from the IDE chat.")
    parser.add_argument("--reference-notes", nargs="*", type=Path, default=[], help="Optional reference-note files used by imagegen while solving.")
    parser.add_argument("--reference-text", action="append", default=[], help="Optional inline reference-note text. Can be passed multiple times.")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "homework_config.yaml")
    parser.add_argument("--generator", choices=["imagegen"], help="Generation tool. Defaults to generation.default_tool in config.")
    args = parser.parse_args()

    if not args.input_files and not args.input_dir:
        # 必须提供文件或目录作为输入。
        raise SystemExit("Provide --input-files <file...> or --input-dir <folder>.")

    config = load_config(args.config)
    # 命令行参数优先级高于配置文件。
    tool = generation_tool(config, args.generator)
    if tool != "imagegen":
        raise SystemExit("Only built-in imagegen is supported. Scripted image generation is forbidden.")
    sample_image = default_sample(config, args.sample_image)
    if args.sample_image and not sample_image:
        # 显式传入的样例图不存在时直接失败，避免静默退回默认图。
        raise SystemExit(f"Sample image does not exist: {args.sample_image}")
    reference_notes, reference_note_records = collect_reference_notes(args.reference_notes, args.reference_text)

    extensions = config.get("input_extensions", DEFAULT_EXTENSIONS)
    sources = collect_sources(args.input_files, args.input_dir, extensions, sample_image)
    if not sources:
        raise SystemExit("No supported homework files found.")

    manifests = []
    # 一个源文件对应一个独立 job 和一个 manifest。
    for source in sources:
        job_dir = output_folder_for(source, config, args.output_dir)
        manifests.append(prepare_job(source, job_dir, sample_image, reference_notes, reference_note_records, config, tool))

    print(f"Jobs: {len(manifests)}")
    # 输出简短摘要，方便用户找到每个 job 的 manifest 和提示词。
    for manifest in manifests:
        print(f"{manifest['job_id']}: {manifest['job_dir']}")
        print(f"  generator: {manifest['generation']['tool']} ({manifest['generation']['status']})")
        print(f"  manifest: {Path(manifest['job_dir']) / config.get('prompt_cache', {}).get('image2_manifest', 'image2_manifest.json')}")
        if sample_image:
            print(f"  handwriting sample: {sample_image}")
        print(f"  reference notes: {len(reference_note_records)} item(s), {len(reference_notes)} chars")
    return 0


if __name__ == "__main__":
    # 作为脚本执行时返回 main() 的退出码。
    raise SystemExit(main())
