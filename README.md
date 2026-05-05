# 古法作业.skill
还在用传统方式写作业吗？


适用人群：老师要求拍照上传手写作业，但你懒得写。

支持批量处理作业文件，提取题目内容，基于codex的imagegen生成仿真手写答案，并自动打包成pdf。

## 如何使用
将要处理的作业存在一个文件夹里，将文件地址发给codex并让其调用古法作业.skill做题（不明确说调用有概率不会调用）。

homework_config.yaml中可修改生图提示词，包括答题要求和字体风格。

handwriting-example.jpg可更换为之前写的作业，用来做笔记参考。

## 说明
image2解决了中文生成问题，但生成潦草答案的效果不是很好。handwriting-example的效果也十分有限。

本项目基于codex内置的imagegen功能，如果使用claude、glm等模型需要自己调用api。

如果有其他需求让ai改就好[狗头]。

本项目纯整活。

## 功能

- 批量识别本地作业文件或文件夹
- 为每个作业文件创建独立输出目录
- 复制源文件为 `source.<ext>`
- 提取 DOCX/PDF 文本或图片元数据
- 生成分页规划提示词 `page_plan.prompt.txt`
- 根据 `page_plan.json` 生成每页 imagegen prompt
- 生成 `image2_manifest.json`，记录 prompt、附件和目标 PNG 路径
- 将真实 imagegen 输出的 PNG 页面打包为 PDF
- 校验 job 是否完整、是否存在跨 job 混入文件

## 支持格式

输入格式：

- `.png`
- `.jpg`
- `.jpeg`
- `.pdf`
- `.docx`

输出格式：

- `.pdf`

## 项目结构

```text
古法作业.skill/
├── SKILL.md
├── README.md
├── homework_config.yaml
├── handwriting-example.jpg
├── requirements.txt
├── scripts/
│   ├── extract_sources.py
│   ├── generate_prompts.py
│   ├── package_outputs.py
│   └── validate_job.py
└── tests/
    ├── test_generate_prompts.py
    ├── test_skill_invocation_format.py
    └── test_validate_job.py

## 环境依赖
建议python 3.10+
