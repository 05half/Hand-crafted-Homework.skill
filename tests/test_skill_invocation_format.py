import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_skill_frontmatter_and_body():
    text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise AssertionError("SKILL.md must start with YAML frontmatter")
    end = lines.index("---", 1)
    frontmatter = {}
    for line in lines[1:end]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        frontmatter[key.strip()] = value.strip()
    body = "\n".join(lines[end + 1 :])
    return frontmatter, body


class SkillInvocationFormatTests(unittest.TestCase):
    def test_frontmatter_description_uses_path_plus_do_homework_format(self):
        frontmatter, _body = read_skill_frontmatter_and_body()

        self.assertIn("文件路径+做题", frontmatter["description"])
        self.assertIn("必须使用本 skill", frontmatter["description"])

    def test_body_documents_standard_invocation_format(self):
        _frontmatter, body = read_skill_frontmatter_and_body()

        self.assertIn("## 调用格式", body)
        self.assertIn("标准调用格式是：`文件路径+做题`", body)
        self.assertIn("做题`", body)


if __name__ == "__main__":
    unittest.main()
