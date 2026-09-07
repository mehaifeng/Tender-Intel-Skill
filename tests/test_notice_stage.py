import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from search_common import notice_family  # noqa: E402
from tender_pipeline import has_procurement_intent, terminal_notice_family  # noqa: E402


class TerminalNoticeGateTests(unittest.TestCase):
    """标的已经定了的公告不进核实——2026-08-27 实测占过筛候选 45%，且已漏推 3/10 条。"""

    def test_result_and_dead_tender_notices_are_blocked(self):
        for title in (
            "海伦市人民医院采购过敏原检验试剂及耗材(二次)中标（成交）结果公告",
            "宾县人民医院检验试剂采购项目（单一来源）(二次)结果公告",
            "全自动化学发光免疫分析仪采购项目公开招标中标公告",
            "烟台海关技术中心试剂耗材采购项目（二次）废标公告",
        ):
            with self.subTest(title=title):
                self.assertTrue(terminal_notice_family(title))

    def test_open_tenders_and_leads_survive(self):
        """在售标的、意向和更正必须留下——提前量是本管线的全部价值。"""
        for title in (
            "【招标公告】宁夏回族自治区人民医院总IGE过敏原检测试剂采购及全自动荧光免疫分析仪租赁项目",
            "2026年医疗设备市场调研公告十二",
            "全自动化学发光免疫分析仪项目比选公告",
            "某医院过敏原试剂采购项目更正公告",
            "余姚市妇幼保健院配套发光检验试剂采购项目单一来源采购公示",
        ):
            with self.subTest(title=title):
                self.assertEqual(terminal_notice_family(title), "")

    def test_contract_titles_notice_family_misses_are_still_blocked(self):
        """notice_family 的 合同 族只认「合同公告/采购合同」，以「…合同」收尾的标题它判不出来。"""
        for title in (
            "大同市公安局2026年DNA实验室试剂耗材合同",
            "绍兴市中心医院医共体齐贤分院化学发光试剂采购项目（重招）合同",
            "宣城市疾控中心血清学检测试剂耗材采购项目合同备案",
            "萍乡市妇幼保健院遗传实验室试剂耗材采购项目结果公示",
            "广元市疾病预防控制中心试剂耗材采购结果更正公告（第一次）",
        ):
            with self.subTest(title=title):
                self.assertNotIn(notice_family(title), ("结果", "终止", "合同"))
                self.assertTrue(terminal_notice_family(title))

    def test_gate_is_separate_from_intent_check(self):
        """意图词表照旧认这些是招采信息；拦下它们的是阶段闸门，不是意图判断。"""
        title = "某医院检验试剂采购项目中标公告"
        self.assertTrue(has_procurement_intent(title))
        self.assertTrue(terminal_notice_family(title))


if __name__ == "__main__":
    unittest.main()
