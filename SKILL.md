---
name: 古法作业.skill
description: 当用户按“文件路径+做题”格式调用，例如“D:\path\homework.pdf 做题”，或给出本地文件/文件夹路径并要求做题时，必须使用本 skill。批量处理 PNG、JPG、DOCX、PDF 作业文件，提取作业内容，调用 Codex 内置 imagegen 生成手写风格答案页，并把生成的 PNG 页面打包为配置命名的 PDF。每个源文件必须独立处理，不能混合作业内容或输出。
---

# 古法作业.skill

## 核心规则
批量处理 PNG、JPG、DOCX、PDF 作业文件，提取作业内容，调用 Codex 内置 imagegen 生成手写风格答案页，并把生成的 PNG 页面打包为配置命名的 PDF。每个源文件必须独立处理，不能混合作业内容或输出。

## 调用格式

标准调用格式是：`文件路径+做题`。

示例：

- `D:\作业\数学试卷.pdf 做题`
- `D:\作业\本周练习 做题`

当用户按该格式调用，或给出本地文件/文件夹路径并明确要求做题时，必须使用本 skill。



把用户在 IDE 聊天中提供的每一个完整输入文件路径视为一个独立作业任务。每个源文件都要在源文件同目录下创建一个独立输出文件夹。不同输入文件的题目、参考材料、图片提示词、页面图片和 PDF 不得混在一起。

开始前必须读取 `homework_config.yaml`。生成工具、手写提示词、输出格式、命名规则、支持的输入扩展名、默认输出文件夹命名和默认笔迹样例，都以该配置文件为准。

硬停止规则：如果链路中的任一必需步骤不能按本说明准确执行，必须立刻停止，报告阻塞步骤和原因，不得继续执行后续步骤。除非用户在明确得知原链路失败后再次授权具体替代方案，否则不得用其他生成器、本地绘图脚本、OCR 重构、手工页面、已有图片或其他生成图片冒充失败步骤的结果。

不允许调用之间的生成结果，每次必须重新生成。

## 最小任务状态

每个作业任务都要维护简短检查清单。处理多个输入文件时，必须按 `job_id` 分别记录以下状态：

- 已复制源文件
- 已运行本地提取
- 已提取内容或元数据
- 已生成直接提示词
- 已用 imagegen 生成页面并保存到目标文件夹
- 已从目标页面文件夹打包
- 已校验通过

## 工作流程

1. 为每个输入文件创建一个输出文件夹。

   - 使用输入文件名主干作为 `job_id`。
   - 默认输出目录为 `<source-file-parent>/<job_id>/`。
   - 如果该文件夹已经存在，重跑时可以复用。
   - 保留原始文件副本为 job 文件夹中的 `source.<ext>`。

2. 提取作业内容。

   - 在人工检查前，先运行 `scripts/extract_sources.py --input-files <file1> <file2>` 或 `scripts/extract_sources.py --input-dir <input-folder>`。
   - 当 `<job-folder>/extracted/source_text.txt` 和 `source_metadata.json` 中有可用文本或元数据时，后续步骤应使用这些文件。
   - 对 DOCX，脚本在本地提取文档正文。
   - 对 PDF，脚本在本地提取可选中文本；如果文本缺失或不完整，必须视觉检查页面。
   - 对 PNG/JPG，脚本只记录图片元数据；题目内容必须通过视觉检查识别。

3. 生成直接提示词文件，并用内置 imagegen 生成图片页。

   - 需要修改提示词时，先修改 `homework_config.yaml` 中的 `handwriting_prompt` 或 `prompt_template.exact_prompt`，再运行 `scripts/generate_prompts.py`。
   - `scripts/generate_prompts.py` 会生成 `<job-folder>/prompts/*.prompt.txt`。该文件只包含保留的手写风格提示词。
   - 优先使用 IDE 聊天中给出的完整文件路径：`scripts/generate_prompts.py --input-files <file1> <file2>`。
   - 需要按文件夹处理时，可以使用：`scripts/generate_prompts.py --input-dir <input-folder>`。
   - 如果用户提供参考笔记，除非用户明确改变工作流，否则不得把参考笔记拼入图像提示词。
   - 如果用户在 IDE 聊天中提供笔迹样例图，使用 `--sample-image <image-path>` 传入。
   - 如果用户没有提供笔迹样例图，并且 `handwriting_sample.default_path` 指向的 `SKILL.md` 同目录文件存在，则使用该默认样例图。
   - 脚本为每个 job 创建一个直接生成提示词。固定手写提示词只能来自 `prompt_template.exact_prompt`；题目图片和笔迹样例必须作为附件传递，不得追加到文本 prompt 中。
   - `generation.default_tool: imagegen` 表示 Codex 必须在 `scripts/generate_prompts.py` 写出 `image2_manifest.json` 后，对 manifest 中的每个条目调用一次内置 `imagegen`/`image_gen`。
   - 对每个 manifest 条目，读取 `prompt_path`，附上 `attach_source_file` 指向的作业源文件，并在存在时附上 `attach_sample_image`。要求 imagegen 只模仿样例图的笔迹风格，不复制样例图中的文字内容。
   - 必须把 imagegen 返回的 PNG 精确保存到该条目的 `target_png`。该路径位于 `<source-file-parent>/<job_id>/pages/`，也是后续打包唯一允许读取的目标页面目录。
   - Codex 内置 imagegen 默认会把生成图保存到 `$CODEX_HOME/generated_images/<run-id>/<image-id>.png`，Windows 通常是 `C:\Users\<user>\.codex\generated_images\<run-id>\<image-id>.png`。如果内置 imagegen 没有目标路径参数，必须先完成 imagegen 调用，再从 `$CODEX_HOME/generated_images` 中按最新生成时间定位本次生成的 PNG，复制到 manifest 条目的 `target_png`；保留 `.codex/generated_images` 下的原始生成图，不要删除或移动。
   - 复制生成图时可以使用等价于以下 PowerShell 的流程，但必须把 `$target` 替换为当前 manifest 条目的 `target_png`：
     ```powershell
     $target = '<target_png>'
     $latest = Get-ChildItem -LiteralPath "$env:USERPROFILE\.codex\generated_images" -Recurse -Filter '*.png' |
       Sort-Object LastWriteTime -Descending |
       Select-Object -First 1
     Copy-Item -LiteralPath $latest.FullName -Destination $target -Force
     ```
   - 复制后必须用 `view_image` 或等价方式检查 `target_png`，确认它是真实 imagegen 输出且内容属于当前 job；确认后才能把 manifest 中 `generation.status` 和 `imagegen.status` 标为 `completed`。
   - 只有当所有 `target_png` 文件都实际存在后，才能把 `image2_manifest.json` 中的 `generation.status` 和 `imagegen.status` 设置为 `completed`。
   - 对图片、PDF 等视觉源文件，只要生成器支持文件或图片附件，就必须把 manifest 条目的 `attach_source_file` 作为附件传给 imagegen。
   - 使用默认笔迹样例前，必须先用 `view_image` 查看样例图，使其作为样式参考可见。
   - 不得使用 `image2`、本地绘图脚本、PIL/canvas 渲染、OCR 重构或任何其他脚本生成图片来替代 imagegen。
   - 必须明确要求 imagegen 只模仿样例图的笔迹风格，不复制样例图的文字内容。

4. 打包生成的 PNG 页面。

   - 只从 `<source-file-parent>/<job_id>/pages/` 读取生成的 PNG 页面。不得从聊天附件、临时下载位置或其他目录打包。
   - 运行 `scripts/package_outputs.py --job-dir <source-file-parent>/<job_id> --job-id <job_id>`。
   - 最终交付物只能是 `homework_config.yaml` 中 `naming.pdf` 指定名称的 PDF。`pages/` 只是中间页面图片目录。
   - 运行 `scripts/validate_job.py --job-dir <source-file-parent>/<job_id> --job-id <job_id>`。

## 失败处理

- 如果任一工作流步骤失败、不可用、缺少必需附件支持、无法保存到指定路径或无法验证，必须停在该步骤。不得继续打包、校验或清理，从而制造任务已完成的假象。但停止之前你需要尝试各种方法解决问题。
- 禁止脚本生成答案图片。脚本可以准备 prompt、manifest、提取产物，并且可以打包真实 imagegen 输出，但不得自行合成手写答案 PNG。
- 如果本地提取失败、配置的生成器不可用，或题目无法识别，必须在 job 检查清单中记录原因。
- 如果 imagegen 不可用，不能接收源题目图片作为附件，或不能把返回的 PNG 保存到 `target_png`，必须把 job 标记为 blocked，不得声称完成。
- 如果页面 PNG 已经存在，也不能停在 prompt 阶段；只有在确认这些页面是按本流程真实 imagegen 生成的前提下，才可以继续打包和校验。

## 完成标准

一个 job 只有同时具备以下内容，才算完成：

- `source.<ext>`
- 提取产物
- prompt 文件
- `image2_manifest.json`
- PNG 页面
- 配置命名的 PDF 输出
- `validate_job.py` 校验通过结果

如果配置的生成器无法使用，该 job 是 blocked，不是 complete。

## 缓存友好的提示词流程

保持文本 prompt 稳定，以便生成器提示词缓存能在多页和重跑时命中：

- `scripts/generate_prompts.py` 把稳定的风格提示词写入 `<job-folder>/_image2_cache/base_prompt.txt`。
- 笔迹样例图只在 `<job-folder>/_image2_cache/sample_image.json` 中记录一次，并包含 SHA-256 哈希。
- 每页 prompt 都应与 `prompt_template.exact_prompt` 展开的基础 prompt 完全一致；源题目图片和笔迹样例通过 `image2_manifest.json` 中的附件字段引用。
- `image2_manifest.json` 记录生成工具、prompt 路径、目标 PNG 路径和样例图路径。
- 不要在共享基础 prompt 前内联变化路径、时间戳或逐页说明。

## 期望目录结构

使用普通文件夹和文件，确保该 skill 可在任意 IDE 或命令行环境中工作：

```text
source folder/
  math01.png
  physics01.pdf
  math01/
    _image2_cache/
      base_prompt.txt
      sample_image.json
    image2_manifest.json
    source.png
    extracted/
      source_text.txt
      source_metadata.json
    prompts/
      math01_page001.prompt.txt
    pages/
      math01_page001.png
    math01_answer.pdf
  physics01/
    source.pdf
    ...
```

最终 PDF 文件名来自 `homework_config.yaml` 的 `naming.pdf`。不得交付 DOCX 或任何其他名称的根目录 PDF 作为最终输出。

## 跨 IDE 要求

- 使用相对路径或明确的 CLI 参数。
- 接受 IDE 聊天中粘贴的绝对路径；脚本使用 `pathlib` 规范化路径。
- 不依赖 VS Code、Cursor、JetBrains 或任何编辑器私有 API。
- 不把状态写入隐藏 IDE 文件夹。
- 配置保持为普通 YAML。
- 脚本输出必须是可由文件管理器、IDE 或 shell 直接打开的普通文件。

## 脚本说明

- `scripts/extract_sources.py`：创建同目录 job 文件夹，复制源文件，并把 DOCX/PDF 文本或图片元数据本地提取到 `extracted/`。
- `scripts/generate_prompts.py`：创建同目录 job 文件夹，生成只包含手写提示词的缓存友好 prompt，记录题目和样例图附件，并写出 manifest，供 Codex 调用内置 imagegen 后把 PNG 保存到 `target_png`。
- `scripts/package_outputs.py`：规范化页面 PNG 文件名，并为单个 job 创建配置命名的 PDF 输出。
- `scripts/validate_job.py`：校验单个 job 文件夹是否包含预期 PNG 页面，并检查是否存在明显跨 job 文件名混入。

脚本尽量只使用 Python 标准库。可选本地读取能力包括：用于 PDF 文本提取的 `pypdf`，以及用于图片元数据提取的 Pillow。
