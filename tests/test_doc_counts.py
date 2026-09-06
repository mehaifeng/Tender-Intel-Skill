"""文档里写死的计数必须和代码一致。

这些数是给人读的口径：改代码前先读文档，读到的是旧数就会按错的前提动手。
实际漂移过两处——`风湿(宽片段)` 从 `类风湿谱` 拆出去之后 SKILL.md 仍写 17 组，
前缀合并定稿到 85 条之后 keywords.md 仍写 86 条。两处都不会让任何功能用例失败，
所以只能在这里钉住。
"""
import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from search_common import TARGET_CATEGORY_PATTERNS  # noqa: E402
from zlbx_search import parse_queries  # noqa: E402


DOCS = ("SKILL.md", "README.md", "AGENT_HANDOFF.md",
        "references/keywords.md", "references/zlbx.md",
        "references/schema.md", "references/verification.md")


def doc_text(name):
    return (ROOT / name).read_text(encoding="utf-8")


class DocCountTests(unittest.TestCase):
    def test_stated_query_count_matches_keywords_md(self):
        """「N 条清单」在四处文档里出现，全部要等于 keywords.md 真实的清单长度。"""
        actual = len(parse_queries())
        found = []
        for name in DOCS:
            for match in re.finditer(r"(\d+)\s*条清单", doc_text(name)):
                found.append((name, int(match.group(1))))
                self.assertEqual(int(match.group(1)), actual,
                                 f"{name} 写的清单条数与 keywords.md 实际条数不符")
        self.assertTrue(found, "没有任何文档写清单条数，这条守卫失效了")

    def test_prefix_merge_result_matches_the_list(self):
        """keywords.md 的「104 → N 条」是前缀合并的结论，必须落到清单本身。"""
        match = re.search(r"清单因此 104 → (\d+) 条", doc_text("references/keywords.md"))
        self.assertIsNotNone(match, "keywords.md 缺少前缀合并的条数结论")
        self.assertEqual(int(match.group(1)), len(parse_queries()))

    def test_stated_category_group_count_matches_code(self):
        """SKILL.md 写的谱系组数要等于 TARGET_CATEGORY_PATTERNS 的实际组数。"""
        match = re.search(r"TARGET_CATEGORY_PATTERNS`（(\d+)\s*组", doc_text("SKILL.md"))
        self.assertIsNotNone(match, "SKILL.md 缺少 TARGET_CATEGORY_PATTERNS 的组数")
        self.assertEqual(int(match.group(1)), len(TARGET_CATEGORY_PATTERNS))


if __name__ == "__main__":
    unittest.main()
