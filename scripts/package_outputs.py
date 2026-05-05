#!/usr/bin/env python3
"""把单个作业 job 的手写 PNG 页面打包成配置命名的 PDF。

这个脚本不依赖外部 PDF 库：它会读取 pages/ 中的 PNG，规范化文件名，
再用标准库手写一个简单 PDF。DOCX 生成函数保留兼容能力，但当前配置只交付 PDF。
"""

from __future__ import annotations

import argparse
import html
import re
import shutil
import sys
import zipfile
import zlib
from pathlib import Path
from xml.sax.saxutils import escape


# 默认配置：当 homework_config.yaml 不存在或缺少字段时使用。
DEFAULT_CONFIG = {
    "output_formats": ["pdf"],
    "naming": {
        "page_png": "{job_id}_page{page:03}.png",
        "pdf": "{job_id}_answer.pdf",
        "docx": "{job_id}_answer.docx",
    },
}


def parse_value(raw: str):
    """解析一个简单 YAML 标量/列表值。

    这是轻量级解析器，只覆盖本项目配置需要的字符串、整数和一维列表。
    """
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [parse_value(part.strip()) for part in inner.split(",")]
    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        return raw[1:-1]
    if raw.isdigit():
        return int(raw)
    return raw


def load_config(path: Path) -> dict:
    """读取 homework_config.yaml，并合并到默认配置上。"""
    config = {k: (v.copy() if isinstance(v, dict) else list(v) if isinstance(v, list) else v) for k, v in DEFAULT_CONFIG.items()}
    if not path.exists():
        # 配置文件缺失时使用 DEFAULT_CONFIG，保证脚本还能运行。
        return config

    current_section = None
    # 简单按缩进识别顶层键和二级键；不引入 PyYAML 依赖。
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if ":" not in stripped:
            continue
        key, raw_value = stripped.split(":", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if indent == 0 and not raw_value:
            config.setdefault(key, {})
            current_section = key
        elif indent == 0:
            config[key] = parse_value(raw_value)
            current_section = None
        elif current_section:
            section = config.setdefault(current_section, {})
            if isinstance(section, dict):
                section[key] = parse_value(raw_value)
    return config


def safe_job_id(value: str) -> str:
    """把文件名转换成 Windows/跨平台安全的 job_id。"""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .") or "homework"


def page_number(path: Path) -> int:
    """从页面文件名中提取最后一个数字作为排序页码。"""
    matches = re.findall(r"(\d+)", path.stem)
    return int(matches[-1]) if matches else 0


def find_pages(job_dir: Path) -> list[Path]:
    """查找 job 的 PNG 页面；优先使用 pages/，否则兼容 job 根目录。"""
    pages_dir = job_dir / "pages"
    source_dir = pages_dir if pages_dir.exists() else job_dir
    pages = [p for p in source_dir.glob("*.png") if p.is_file()]
    return sorted(pages, key=lambda p: (page_number(p), p.name.lower()))


def normalize_pages(pages: list[Path], output_dir: Path, pattern: str, job_id: str) -> list[Path]:
    """把页面复制/规范化到 output_dir/pages，并按配置模板重命名。"""
    target_dir = output_dir / "pages"
    target_dir.mkdir(parents=True, exist_ok=True)
    normalized = []
    for index, page in enumerate(pages, start=1):
        target = target_dir / pattern.format(job_id=job_id, page=index)
        if page.resolve() != target.resolve():
            # 不直接移动，避免破坏用户已有中间文件。
            shutil.copy2(page, target)
        normalized.append(target)
    return normalized


def parse_png(path: Path) -> tuple[int, int, bytes]:
    """解析 PNG 并返回宽、高和铺在白底上的 RGB 像素数据。

    支持非隔行 8 位 PNG；透明通道会混合到白底，便于嵌入 PDF。
    """
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise RuntimeError(f"Not a PNG file: {path}")
    pos = 8
    width = height = bit_depth = color_type = interlace = None
    compressed = bytearray()
    palette = None
    transparency = None

    while pos < len(data):
        # PNG 由一系列 chunk 组成：IHDR 描述尺寸，IDAT 存压缩像素，IEND 结束。
        length = int.from_bytes(data[pos : pos + 4], "big")
        chunk_type = data[pos + 4 : pos + 8]
        chunk = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if chunk_type == b"IHDR":
            width = int.from_bytes(chunk[0:4], "big")
            height = int.from_bytes(chunk[4:8], "big")
            bit_depth = chunk[8]
            color_type = chunk[9]
            interlace = chunk[12]
        elif chunk_type == b"PLTE":
            palette = chunk
        elif chunk_type == b"tRNS":
            transparency = chunk
        elif chunk_type == b"IDAT":
            compressed.extend(chunk)
        elif chunk_type == b"IEND":
            break

    if None in (width, height, bit_depth, color_type, interlace):
        raise RuntimeError(f"Invalid PNG header: {path}")
    if bit_depth != 8 or interlace != 0:
        raise RuntimeError(f"Only non-interlaced 8-bit PNG files are supported: {path}")

    channels_by_type = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
    if color_type not in channels_by_type:
        raise RuntimeError(f"Unsupported PNG color type {color_type}: {path}")
    channels = channels_by_type[color_type]
    stride = width * channels
    raw = zlib.decompress(bytes(compressed))
    rows = []
    prev = [0] * stride
    offset = 0

    for _ in range(height):
        # 逐行还原 PNG 过滤器编码后的扫描线。
        filter_type = raw[offset]
        offset += 1
        scan = list(raw[offset : offset + stride])
        offset += stride
        recon = [0] * stride
        for i, value in enumerate(scan):
            # 根据 PNG filter 类型使用左、上、左上像素预测值还原真实字节。
            left = recon[i - channels] if i >= channels else 0
            up = prev[i]
            up_left = prev[i - channels] if i >= channels else 0
            if filter_type == 0:
                recon[i] = value
            elif filter_type == 1:
                recon[i] = (value + left) & 0xFF
            elif filter_type == 2:
                recon[i] = (value + up) & 0xFF
            elif filter_type == 3:
                recon[i] = (value + ((left + up) // 2)) & 0xFF
            elif filter_type == 4:
                p = left + up - up_left
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - up_left)
                predictor = left if pa <= pb and pa <= pc else up if pb <= pc else up_left
                recon[i] = (value + predictor) & 0xFF
            else:
                raise RuntimeError(f"Unsupported PNG filter {filter_type}: {path}")
        rows.append(recon)
        prev = recon

    rgb = bytearray()
    # 统一转换为 RGB；灰度、调色板和带 alpha 的格式都在这里处理。
    for row in rows:
        if color_type == 0:
            for gray in row:
                rgb.extend((gray, gray, gray))
        elif color_type == 2:
            rgb.extend(row)
        elif color_type == 3:
            if palette is None:
                raise RuntimeError(f"Palette PNG missing PLTE chunk: {path}")
            for index in row:
                base = index * 3
                r, g, b = palette[base : base + 3]
                alpha = transparency[index] if transparency and index < len(transparency) else 255
                rgb.extend(blend_over_white(r, g, b, alpha))
        elif color_type == 4:
            for i in range(0, len(row), 2):
                gray, alpha = row[i], row[i + 1]
                rgb.extend(blend_over_white(gray, gray, gray, alpha))
        elif color_type == 6:
            for i in range(0, len(row), 4):
                rgb.extend(blend_over_white(row[i], row[i + 1], row[i + 2], row[i + 3]))

    return width, height, bytes(rgb)


def blend_over_white(r: int, g: int, b: int, alpha: int) -> tuple[int, int, int]:
    """把带透明度的像素混合到白底上。"""
    if alpha >= 255:
        return r, g, b
    return (
        int((r * alpha + 255 * (255 - alpha)) / 255),
        int((g * alpha + 255 * (255 - alpha)) / 255),
        int((b * alpha + 255 * (255 - alpha)) / 255),
    )


def png_dimensions(path: Path) -> tuple[int, int]:
    """读取 PNG 尺寸。"""
    width, height, _ = parse_png(path)
    return width, height


def pdf_obj(number: int, body: bytes) -> tuple[int, bytes]:
    """构造一个 PDF 对象字节块。"""
    return number, b"%d 0 obj\n" % number + body + b"\nendobj\n"


def make_pdf(pages: list[Path], target: Path) -> None:
    """把 PNG 页面嵌入到 A4 尺寸 PDF 中。"""
    if not pages:
        raise RuntimeError("No PNG pages found for PDF output.")

    objects: list[tuple[int, bytes]] = []
    page_refs = []
    next_obj = 3
    for page in pages:
        # 每页需要 3 个对象：图片对象、内容流对象、页面对象。
        width, height, rgb = parse_png(page)
        image_data = zlib.compress(rgb)
        image_obj = next_obj
        page_obj = next_obj + 1
        content_obj = next_obj + 2
        next_obj += 3
        display_width = 595.0
        display_height = display_width * height / max(width, 1)
        if display_height > 842.0:
            # 超过 A4 高度时按高度缩放，保持等比例居中。
            display_height = 842.0
            display_width = display_height * width / max(height, 1)
        x = (595.0 - display_width) / 2
        y = (842.0 - display_height) / 2
        content = f"q {display_width:.2f} 0 0 {display_height:.2f} {x:.2f} {y:.2f} cm /Im0 Do Q".encode("ascii")
        objects.append(
            pdf_obj(
                image_obj,
                b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode /Length %d >>\nstream\n"
                % (width, height, len(image_data))
                + image_data
                + b"\nendstream",
            )
        )
        objects.append(
            pdf_obj(
                content_obj,
                b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
            )
        )
        objects.append(
            pdf_obj(
                page_obj,
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /XObject << /Im0 %d 0 R >> >> /Contents %d 0 R >>"
                % (image_obj, content_obj),
            )
        )
        page_refs.append(page_obj)

    objects.insert(0, pdf_obj(1, b"<< /Type /Catalog /Pages 2 0 R >>"))
    kids = b" ".join(b"%d 0 R" % ref for ref in page_refs)
    objects.insert(1, pdf_obj(2, b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % len(page_refs)))

    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    objects.sort(key=lambda item: item[0])
    for number, obj in objects:
        # 记录每个对象在 PDF 字节流中的偏移，供 xref 表使用。
        if number != len(offsets):
            raise RuntimeError(f"PDF object numbering is not contiguous at object {number}.")
        offsets.append(len(output))
        output.extend(obj)
    xref_offset = len(output)
    output.extend(b"xref\n0 %d\n" % (len(objects) + 1))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(b"%010d 00000 n \n" % offset)
    output.extend(
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
        % (len(objects) + 1, xref_offset)
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(output)


def rel(path: str) -> str:
    """把 Windows 反斜杠路径转成 DOCX 内部使用的正斜杠路径。"""
    return path.replace("\\", "/")


def make_docx(pages: list[Path], target: Path, title: str) -> None:
    """把 PNG 页面写入一个简单 DOCX。

    当前最终交付不使用 DOCX；保留此函数是为了历史兼容或未来扩展。
    """
    if not pages:
        raise RuntimeError("No PNG pages found for DOCX output.")

    target.parent.mkdir(parents=True, exist_ok=True)
    rels = []
    body = [
        '<w:p><w:r><w:t>{}</w:t></w:r></w:p>'.format(escape(title)),
    ]
    content_types = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
        '<Default Extension="xml" ContentType="application/xml"/>',
        '<Default Extension="png" ContentType="image/png"/>',
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>',
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>',
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>',
        '</Types>',
    ]

    for index, page in enumerate(pages, start=1):
        # 每张图片创建一个关系 id，并按页面宽度等比例缩放。
        rid = f"rId{index}"
        media_name = f"image{index}.png"
        width, height = png_dimensions(page)
        max_width_emu = 6_300_000
        cx = max_width_emu
        cy = int(max_width_emu * height / max(width, 1))
        rels.append(
            f'<Relationship Id="{rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/{media_name}"/>'
        )
        body.append(
            f'''<w:p><w:r><w:drawing><wp:inline xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" distT="0" distB="0" distL="0" distR="0"><wp:extent cx="{cx}" cy="{cy}"/><wp:docPr id="{index}" name="Page {index}"/><a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:nvPicPr><pic:cNvPr id="{index}" name="{html.escape(page.name)}"/><pic:cNvPicPr/></pic:nvPicPr><pic:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill><pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>'''
        )

    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<w:body>{"".join(body)}<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="720" w:right="720" w:bottom="720" w:left="720"/></w:sectPr></w:body></w:document>'
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
        '</Relationships>'
    )
    doc_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(rels)
        + "</Relationships>"
    )
    core_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>'
        + escape(title)
        + '</dc:title></cp:coreProperties>'
    )
    app_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"><Application>古法作业.skill</Application></Properties>'
    )

    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as docx:
        # DOCX 是 zip 包，下面写入最小可打开的一组 XML 和图片文件。
        docx.writestr("[Content_Types].xml", "\n".join(content_types))
        docx.writestr("_rels/.rels", root_rels)
        docx.writestr("word/document.xml", document_xml)
        docx.writestr("word/_rels/document.xml.rels", doc_rels)
        docx.writestr("docProps/core.xml", core_xml)
        docx.writestr("docProps/app.xml", app_xml)
        for index, page in enumerate(pages, start=1):
            docx.write(page, f"word/media/image{index}.png")


def remove_stale_unconfigured_outputs(output_dir: Path, naming: dict, formats: set[str], job_id: str) -> None:
    """删除当前配置不再交付的旧 DOCX 输出。"""
    docx_name = naming.get("docx", DEFAULT_CONFIG["naming"]["docx"]).format(job_id=job_id)
    stale_docx = output_dir / docx_name
    if stale_docx.exists():
        stale_docx.unlink()


def main() -> int:
    """命令行入口：查找页面、规范化命名并生成最终 PDF。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-dir", required=True, type=Path, help="Folder containing one job's PNG pages or pages/ folder.")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "homework_config.yaml")
    parser.add_argument("--job-id", help="Output job id. Defaults to job folder name.")
    parser.add_argument("--output-dir", type=Path, help="Output folder. Defaults to job folder.")
    args = parser.parse_args()

    job_dir = args.job_dir.resolve()
    output_dir = (args.output_dir or job_dir).resolve()
    job_id = safe_job_id(args.job_id or job_dir.name)
    config = load_config(args.config)
    naming = config.get("naming", DEFAULT_CONFIG["naming"])
    formats = set(config.get("output_formats", DEFAULT_CONFIG["output_formats"]))

    source_pages = find_pages(job_dir)
    if not source_pages:
        # 没有 PNG 页面时无法打包，返回 2 表示输入状态不完整。
        print(f"No PNG pages found in {job_dir} or {job_dir / 'pages'}", file=sys.stderr)
        return 2

    output_dir.mkdir(parents=True, exist_ok=True)
    pages = normalize_pages(source_pages, output_dir, naming.get("page_png", DEFAULT_CONFIG["naming"]["page_png"]), job_id)
    print(f"PNG pages: {len(pages)}")

    if "pdf" in formats:
        # 当前 skill 的最终交付格式通常只有 PDF。
        pdf_path = output_dir / naming.get("pdf", DEFAULT_CONFIG["naming"]["pdf"]).format(job_id=job_id)
        make_pdf(pages, pdf_path)
        print(f"PDF: {pdf_path}")

    remove_stale_unconfigured_outputs(output_dir, naming, formats, job_id)

    return 0


if __name__ == "__main__":
    # 作为脚本执行时返回 main() 的退出码。
    raise SystemExit(main())
