# -*- coding: utf-8 -*-
"""查重漏斗的分层契约：定案尽量落在确定性的前三层，模型只接窄带。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from tender_identity import buyer_key, fingerprint, scope
from dedup_match import (CONTENT_HIGH, TITLE_HIGH, TITLE_LOW, TOP_K, LedgerMatcher,
                         similarity)


def ledger(标题="甲医院2026年度过敏原检测试剂采购项目公开招标公告", 链接="https://x.org/old",
           单位="甲医院", 发布时间="2026-09-04", 编号="ZB-0001", 内容="", 项目编号=""):
    return {"标题": 标题, "链接": 链接, "单位": 单位, "发布时间": 发布时间, "内容": 内容,
            "项目编号": 项目编号, "_feishu_id": 编号, "_pushed": True}


def candidate(标题="甲医院2026年度过敏原检测试剂采购项目公开招标公告", 链接="https://y.org/new",
              单位="甲医院", 发布时间="2026-09-04", 内容="", 项目编号="", cid="C1"):
    return {"标题": 标题, "链接": 链接, "单位": 单位, "发布时间": 发布时间,
            "内容": 内容, "项目编号": 项目编号, "candidate_id": cid}


class FunnelLayerTests(unittest.TestCase):
    def check(self, rows, item, decisions=None):
        return LedgerMatcher(rows, decisions).check(item)

    # ---- L1：强身份，零模型 ----

    def test_same_url_is_settled_at_layer_one(self):
        match = self.check([ledger()], candidate(链接="https://x.org/old"))
        self.assertEqual((match.verdict, match.layer), ("duplicate", "L1"))

    def test_tracking_parameters_do_not_create_a_new_identity(self):
        match = self.check([ledger(链接="https://x.org/old?id=9")],
                           candidate(链接="http://x.org/old?id=9&utm_source=wx"))
        self.assertEqual(match.verdict, "duplicate")

    def test_project_number_settles_a_fully_rewritten_title(self):
        # 标题面目全非、链接不同、采购人也没存，但项目编号、阶段、轮次一致。
        rows = [ledger(标题="关于开展检验试剂采购的公告", 单位="", 项目编号="ZB-2026-77")]
        match = self.check(rows, candidate(标题="甲医院过敏原检测试剂采购项目公开招标公告",
                                           项目编号="ZB-2026-77"))
        self.assertEqual((match.verdict, match.layer), ("duplicate", "L1"))
        self.assertIn("项目编号", match.reason)

    def test_conflicting_project_numbers_are_never_merged(self):
        rows = [ledger(项目编号="P1")]
        match = self.check(rows, candidate(项目编号="P2"))
        self.assertEqual(match.verdict, "new")

    # ---- L1 后续阶段：同一招标已推给销售，更正不再推 ----

    def base(self, 标题="甲医院过敏原检测试剂采购项目公开招标公告", **kw):
        return ledger(标题=标题, **kw)

    def correction(self, 标题="关于甲医院过敏原检测试剂采购项目的更正公告", **kw):
        return candidate(标题=标题, 链接="https://y.org/correction", **kw)

    def test_correction_of_a_pushed_tender_is_suppressed(self):
        match = self.check([self.base()], self.correction(发布时间="2026-09-11"))
        self.assertEqual((match.verdict, match.layer), ("duplicate", "L1-后续阶段"))
        self.assertIn("不再推送", match.reason)

    def test_correction_is_pushed_when_the_tender_was_never_pushed(self):
        # 台账里没有这个招标，更正公告本身就是第一次捞到它，必须照推。
        match = self.check([ledger(标题="乙医院医用耗材配送服务采购公告", 单位="乙医院")],
                           self.correction())
        self.assertEqual(match.verdict, "new")

    def test_correction_beyond_the_window_is_pushed_again(self):
        match = self.check([self.base(发布时间="2026-01-01")],
                           self.correction(发布时间="2026-09-04"))
        self.assertEqual(match.verdict, "new")

    def test_correction_of_another_package_is_not_suppressed(self):
        # 一包已推，三包的更正是另一次投标机会，不能顺手压掉。
        rows = [self.base(标题="甲医院过敏原检测试剂采购项目(第一包)公开招标公告")]
        match = self.check(rows, self.correction(
            标题="关于甲医院过敏原检测试剂采购项目(第三包)的更正公告"))
        self.assertEqual(match.verdict, "new")

    def test_second_correction_is_suppressed_by_the_first_one(self):
        # 「第2次更正」的次数是改了第几回，不是招标轮次。把它算进轮次，
        # 第1次与第2次更正就成了「轮次不同」，同一个标会被反复推给销售。
        first = ledger(
            标题="甲医院检验科设备采购项目（第四包全自动免疫印迹仪、酶联免疫分析系统）第1次更正",
            链接="https://x.org/fix1", 发布时间="2026-09-04")
        match = self.check([first], candidate(
            标题="甲医院检验科设备采购项目（第四包全自动免疫印迹仪，酶联免疫分析系统）第2次更正",
            链接="https://y.org/fix2", 发布时间="2026-09-08"))
        self.assertEqual((match.verdict, match.layer), ("duplicate", "L1-后续阶段"))

    def test_retender_round_survives_the_revision_counter_rule(self):
        # 「（第二次）」独立成轮次，仍是重招、仍是新机会，不能被上面那条规则顺手吃掉。
        self.assertEqual(scope("甲医院试剂采购项目（第二次）公开招标公告"), ("2次",))
        self.assertEqual(scope("甲医院试剂采购项目（第四包…）第2次更正"), ("4包",))

    def test_correction_of_one_package_is_suppressed_by_the_umbrella_tender(self):
        # 总招标一个包号都不写，更正才点名「第四包」。轮次门卡住这一对的话，
        # 同一个标的每个包的每次更正都能重推一遍。
        rows = [self.base(标题="甲医院2026年检验科设备更新采购项目招标公告")]
        match = self.check(rows, self.correction(
            标题="甲医院2026年检验科设备更新采购项目（第四包全自动免疫印迹仪、酶联免疫分析系统）第1次更正"))
        self.assertEqual((match.verdict, match.layer), ("duplicate", "L1-后续阶段"))

    def test_umbrella_relaxation_does_not_swallow_a_retender_correction(self):
        # 放行只给包号。候选带的是轮次（重招）时照旧卡住——那是新的投标机会。
        rows = [self.base(标题="甲医院2026年检验科设备更新采购项目招标公告")]
        match = self.check(rows, self.correction(
            标题="甲医院2026年检验科设备更新采购项目（第二次）第1次更正"))
        self.assertEqual(match.verdict, "new")

    def test_umbrella_relaxation_needs_a_long_enough_subject(self):
        # 台账主体短到只剩通用写法时，子串关系不足以判重，否则同院不同标会被并成一条。
        rows = [ledger(标题="甲医院设备采购公告", 单位="甲医院")]
        match = self.check(rows, self.correction(
            标题="甲医院设备采购项目（第二包过敏原检测试剂）第1次更正"))
        self.assertEqual(match.verdict, "new")

    def test_retender_is_not_suppressed_by_the_first_round(self):
        rows = [self.base()]
        match = self.check(rows, candidate(
            标题="甲医院过敏原检测试剂采购项目(第二次)公开招标公告",
            链接="https://y.org/second"))
        self.assertEqual(match.verdict, "new")

    def test_project_number_anchors_a_correction_with_a_rewritten_title(self):
        rows = [self.base(标题="关于开展检验试剂采购的公告", 单位="",
                          项目编号="ZB-2026-77")]
        match = self.check(rows, self.correction(
            标题="甲医院过敏原检测试剂采购项目更正公告", 项目编号="ZB-2026-77",
            发布时间="2026-09-20"))
        self.assertEqual((match.verdict, match.layer), ("duplicate", "L1-后续阶段"))

    def test_clarification_and_supplement_count_as_corrections(self):
        for 标题 in ("关于甲医院过敏原检测试剂采购项目的澄清公告",
                     "甲医院过敏原检测试剂采购项目补充公告",
                     "甲医院过敏原检测试剂采购项目答疑纪要"):
            match = self.check([self.base()], self.correction(标题=标题))
            self.assertEqual(match.verdict, "duplicate", 标题)

    def test_a_different_tender_from_the_same_buyer_is_not_suppressed(self):
        match = self.check([self.base()],
                           self.correction(标题="关于甲医院全自动生化分析仪采购项目的更正公告"))
        self.assertEqual(match.verdict, "new")

    # ---- L2：字符级，零模型 ----

    def test_character_level_high_similarity_settles_without_the_model(self):
        # 两个来源只差一个「的」字：指纹不等，但字符级足以定案，不该占用模型。
        rewritten = "甲医院2026年度过敏原检测试剂采购项目的公开招标公告"
        self.assertGreaterEqual(
            similarity("甲医院2026年度过敏原检测试剂采购项目公开招标公告", rewritten), TITLE_HIGH)
        match = self.check([ledger()], candidate(标题=rewritten))
        self.assertEqual((match.verdict, match.layer), ("duplicate", "L2"))

    def test_a_province_prefixed_buyer_is_the_same_buyer(self):
        # 知了给的是「云南省玉溪市人民医院」，手工录入的台账写「玉溪市人民医院」。
        # 硬门把这当成两个采购人时，标题一字不差、同日发布的公告也会被重新推一遍。
        title = "玉溪市人民医院拟采购全自动化学发光免疫分析仪的采购信息征询"
        rows = [ledger(标题=title, 单位="玉溪市人民医院")]
        match = self.check(rows, candidate(标题=title, 单位="云南省玉溪市人民医院"))
        self.assertEqual((match.verdict, match.layer), ("duplicate", "L1"))

    def test_a_prefix_is_not_stripped_into_an_ambiguous_name(self):
        # 剥完对不上医院库唯一整名就保持原样：省立医院不是「第一人民医院」。
        self.assertEqual(buyer_key("云南省第一人民医院"), fingerprint("云南省第一人民医院"))
        self.assertNotEqual(buyer_key("云南省第一人民医院"), buyer_key("第一人民医院"))

    def test_unrelated_titles_are_settled_as_new_without_the_model(self):
        rows = [ledger(标题="乙医院医用耗材配送服务采购公告", 单位="乙医院")]
        match = self.check(rows, candidate(标题="甲医院2026年度过敏原检测试剂采购项目公开招标公告"))
        self.assertEqual(match.verdict, "new")
        self.assertLess(similarity("乙医院医用耗材配送服务采购公告",
                                   "甲医院2026年度过敏原检测试剂采购项目公开招标公告"), TITLE_LOW)

    def test_high_similarity_without_buyer_evidence_goes_to_the_model(self):
        # 台账没存采购人、标题也短，字符级再像也不敢直接判重。
        rows = [ledger(标题="医用耗材公开遴选公告", 单位="")]
        match = self.check(rows, candidate(标题="医用耗材公开遴选公告", 单位="乙医院"))
        self.assertEqual((match.verdict, match.layer), ("semantic", "L4"))

    # ---- L3：正文，零模型 ----

    def test_body_text_settles_the_middle_band_as_duplicate(self):
        body = "采购化学发光法过敏原特异性IgE检测试剂共20项，预算48.76万元，合同期三年。"
        rows = [ledger(标题="甲医院2026年度过敏原检测试剂采购项目公开招标公告", 内容=body)]
        item = candidate(标题="关于甲医院过敏原检测试剂采购的招标公告", 内容=body)
        title_sim = similarity("甲医院2026年度过敏原检测试剂采购项目公开招标公告",
                               "关于甲医院过敏原检测试剂采购的招标公告")
        self.assertTrue(TITLE_LOW <= title_sim < TITLE_HIGH, title_sim)
        match = self.check(rows, item)
        self.assertEqual((match.verdict, match.layer), ("duplicate", "L3"))

    def test_body_text_settles_the_middle_band_as_new(self):
        rows = [ledger(标题="甲医院2026年度过敏原检测试剂采购项目公开招标公告",
                       内容="采购全自动化学发光免疫分析仪一台及配套耗材，预算120万元。")]
        item = candidate(标题="关于甲医院过敏原检测试剂采购的招标公告",
                         内容="本项目采购微生物培养基与药敏纸片，共计三个品目，预算8万元。")
        match = self.check(rows, item)
        self.assertEqual(match.verdict, "new")

    def test_middle_band_without_body_text_goes_to_the_model(self):
        rows = [ledger(标题="甲医院2026年度过敏原检测试剂采购项目公开招标公告")]
        match = self.check(rows, candidate(标题="关于甲医院过敏原检测试剂采购的招标公告"))
        self.assertEqual((match.verdict, match.layer), ("semantic", "L4"))
        self.assertTrue(match.pairs)

    # ---- 硬门：命中就不进相似度计算 ----

    def test_hard_gates_keep_pairs_out_of_the_model(self):
        base = "甲医院2026年度过敏原检测试剂采购项目公开招标公告"
        cases = {
            "阶段不同": ledger(标题=base.replace("公开招标公告", "更正公告")),
            "采购人不同": ledger(单位="乙医院"),
            "轮次不同": ledger(标题=base.replace("公开招标公告", "公开招标公告(第二次)")),
            "日期超窗": ledger(发布时间="2026-08-01"),
        }
        for name, row in cases.items():
            match = self.check([row], candidate(标题=base))
            self.assertEqual(match.verdict, "new", name)

    # ---- 交给模型的量必须可控 ----

    def test_at_most_top_k_ledger_rows_are_handed_to_the_model(self):
        rows = [ledger(标题="医用耗材公开遴选公告", 单位="", 链接=f"https://x.org/{i}",
                       编号=f"ZB-{i:04d}") for i in range(8)]
        match = self.check(rows, candidate(标题="医用耗材公开遴选公告", 单位="乙医院"))
        self.assertEqual(match.verdict, "semantic")
        self.assertLessEqual(len(match.pairs), TOP_K)

    def test_blank_auto_numbers_fall_back_to_unique_record_ids(self):
        rows = [ledger(标题="医用耗材公开遴选公告", 单位="", 链接=f"https://x.org/{i}", 编号="")
                for i in range(2)]
        for i, row in enumerate(rows):
            row["_record_id"] = f"rec-{i}"
        match = self.check(rows, candidate(标题="医用耗材公开遴选公告", 单位="乙医院"))
        self.assertEqual(match.verdict, "semantic")
        self.assertEqual(len({pair.pair_id for pair in match.pairs}), len(match.pairs))

    def test_recorded_decision_removes_the_pair_from_the_model_queue(self):
        rows = [ledger(标题="医用耗材公开遴选公告", 单位="")]
        item = candidate(标题="医用耗材公开遴选公告", 单位="乙医院")
        pending = self.check(rows, item)
        pair = pending.pairs[0].pair_id

        same = self.check(rows, item, [{"pair_id": pair, "same": True, "note": "同一条"}])
        self.assertEqual((same.verdict, same.layer), ("duplicate", "decision"))

        different = self.check(rows, item, [{"pair_id": pair, "same": False, "note": "两家医院"}])
        self.assertEqual(different.verdict, "new")

    def test_thresholds_leave_a_real_band_for_the_model(self):
        self.assertLess(TITLE_LOW, TITLE_HIGH)
        self.assertLessEqual(CONTENT_HIGH, 1.0)


if __name__ == "__main__":
    unittest.main()
