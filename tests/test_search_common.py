import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from search_common import (  # noqa: E402
    canonical_url,
    excluded_domain_term,
    merge_source_dirs,
    plap_notice_id,
    target_category_signals,
    write_candidates,
)
from tender_pipeline import canonicalize_create, historical_identity_keys  # noqa: E402


class ProductDomainScreenTests(unittest.TestCase):
    """三个来源共用同一份硬排除（2026-09-03 起定义在 search_common）。"""

    def test_chemiluminescence_is_not_hard_excluded(self):
        # 2026-09-03 试过把化学发光加进硬排除，回跑历史记录时拦下了一条 54 万元的
        # 血管炎自身抗体试剂盒招标——该词只出现在「可与…化学发光仪配套使用」的资格
        # 条款里。改为只摘正向信号：去留交给品类信号闸与核实阶段。
        for text in (
            "某医院全自动化学发光免疫分析仪采购项目",
            "血管炎自身抗体检测试剂盒招标，可与现有化学发光仪配套使用",
            "磁微粒发光免疫分析系统采购",
        ):
            self.assertFalse(excluded_domain_term(text), text)

    def test_reagent_tender_survives_a_chemiluminescence_compatibility_clause(self):
        text = ("采购血管炎自身抗体检测试剂盒项目招标公告\n"
                "若所投产品可与科室现有品牌型号化学发光仪配套使用，则设备可无需单独报价")
        self.assertFalse(excluded_domain_term(text))
        self.assertIn("自身抗体/自身免疫", target_category_signals(text))

    def test_bare_chemiluminescence_still_fails_the_signal_gate(self):
        # 只提化学发光、没有任何目标品类信号的公告，由品类信号闸挡下，不需要硬排除。
        self.assertEqual(target_category_signals("艾滋相关试剂耗材（化学发光）询价公告"), [])

    def test_chemiluminescence_is_no_longer_its_own_positive_signal(self):
        # 「化学发光免疫」这条信号已删；仪器整机仍会命中「免疫分析仪器」，但硬排除
        # 在所有调用点都跑在信号之前，所以命中与否不影响去留。
        self.assertNotIn("化学发光免疫", target_category_signals("全自动化学发光免疫分析仪"))
        self.assertNotIn("化学发光免疫", target_category_signals("自身抗体化学发光检测试剂"))

    def test_core_product_domain_still_passes(self):
        for text in ("过敏原特异性IgE检测试剂采购", "抗核抗体检测试剂招标",
                     "酶联免疫检测试剂采购", "全自动酶免仪及配套试剂"):
            self.assertFalse(excluded_domain_term(text), text)
            self.assertTrue(target_category_signals(text), text)

    def test_out_of_domain_terms_stay_excluded(self):
        for text in ("酶标仪采购", "兽用检测试剂", "新冠核酸PCR检测试剂",
                     "全自动电泳仪", "免疫组化二抗"):
            self.assertTrue(excluded_domain_term(text), text)


class SearchMergeTests(unittest.TestCase):
    def test_ccgp_http_is_canonicalized_to_https(self):
        self.assertEqual(
            canonical_url("http://www.ccgp.gov.cn/a.htm?utm_source=x"),
            "https://www.ccgp.gov.cn/a.htm",
        )

    def test_plap_url_drops_routing_parameters_and_keeps_notice_id(self):
        raw = (
            "https://www.plap.mil.cn/freecms/site/juncai/ggxx/info/2026/"
            "8a1d04009fd98fe401a03138aab456cf.html?noticeType=001024&channel=abc"
        )
        normalized = canonical_url(raw)
        self.assertNotIn("noticeType", normalized)
        self.assertNotIn("channel", normalized)
        self.assertEqual(plap_notice_id(normalized), "8a1d04009fd98fe401a03138aab456cf")

    def test_cross_source_duplicate_prefers_ccgp_and_keeps_jrbx_attribution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jrbx = root / "jrbx"
            ccgp = root / "ccgp"
            write_candidates([{
                "title": "某医院过敏原试剂公开招标公告",
                "site_name": "转载站",
                "url": "https://mirror.example/tender/1",
                "publish_time": "2026-08-21",
                "summary": "项目编号：ABC-2026-01",
                "content": "项目编号：ABC-2026-01 采购方式：公开招标",
                "found_by_query": [1, 3],
                "found_by_source_query": [{"source": "jrbx", "query_number": 1}],
                "source": "jrbx",
                "sources": ["jrbx"],
            }], jrbx, "2026-08-23")
            write_candidates([{
                "title": "某医院过敏原试剂公开招标公告",
                "site_name": "中国政府采购网",
                "url": "http://www.ccgp.gov.cn/cggg/dfgg/gkzb/202608/t20260821_27184986.htm",
                "publish_time": "2026-08-21 19:47:44",
                "summary": "官方摘要",
                "content": "官方完整正文",
                "source_fields": {"项目编号": "ABC-2026-01", "公告类型": "公开招标公告"},
                "found_by_source_query": [{"source": "ccgp", "query": "过敏原"}],
                "source": "ccgp",
                "sources": ["ccgp"],
                "date_authoritative": True,
                "retrieval_verified": True,
            }], ccgp, "2026-08-23")

            merged = merge_source_dirs([jrbx, ccgp])
            self.assertEqual(len(merged), 1)
            self.assertEqual(merged[0]["source"], "ccgp")
            self.assertEqual(merged[0]["content"], "官方完整正文")
            self.assertEqual(merged[0]["found_by_query"], [1, 3])
            self.assertEqual(set(merged[0]["sources"]), {"ccgp", "jrbx"})
            self.assertEqual(len(merged[0]["alternate_sources"]), 1)

    def test_same_project_different_notice_stage_is_not_merged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first"
            second = root / "second"
            common = {"site_name": "中国政府采购网", "publish_time": "2026-08-21", "source": "ccgp"}
            write_candidates([common | {
                "title": "某项目公开招标公告", "url": "https://example.test/a",
                "content": "项目编号：ABC-1", "source_fields": {"项目编号": "ABC-1", "公告类型": "公开招标公告"},
            }], first, "2026-08-23")
            write_candidates([common | {
                "title": "某项目中标公告", "url": "https://example.test/b",
                "content": "项目编号：ABC-1", "source_fields": {"项目编号": "ABC-1", "公告类型": "中标公告"},
            }], second, "2026-08-23")
            self.assertEqual(len(merge_source_dirs([first, second])), 2)

    def test_plap_official_candidate_wins_over_jrbx_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jrbx = root / "jrbx"
            plap = root / "plap"
            title = "某医院自身抗体试剂采购公告"
            write_candidates([{
                "title": title, "url": "https://mirror.example/1", "source": "jrbx",
                "summary": "转载摘要", "content": "转载正文", "retrieval_verified": False,
            }], jrbx, "2026-08-25")
            write_candidates([{
                "title": title,
                "url": "https://www.plap.mil.cn/freecms/site/juncai/ggxx/info/2026/8a1d04009fd98fe401a03138aab456cf.html",
                "source": "plap", "sources": ["plap"], "source_priority": 400,
                "summary": "官方摘要", "content": "官方公开正文",
                "retrieval_verified": True, "content_access": "public_partial",
            }], plap, "2026-08-25")
            merged = merge_source_dirs([jrbx, plap])
            self.assertEqual(len(merged), 1)
            self.assertEqual(merged[0]["source"], "plap")
            self.assertEqual(merged[0]["content_access"], "public_partial")

    def test_seen_identity_requires_same_date_for_title_only_match(self):
        first = historical_identity_keys("某医院试剂采购公告", "https://a.test/1", "2026-08-21")
        mirror = historical_identity_keys("某医院试剂采购公告", "https://b.test/2", "2026-08-21")
        next_year = historical_identity_keys("某医院试剂采购公告", "https://b.test/3", "2027-08-21")
        self.assertTrue(first & mirror)
        self.assertFalse(first & next_year)

    def test_verified_ccgp_fields_prefill_record_and_authoritative_date_wins(self):
        candidate = {
            "title": "某医院过敏原试剂公开招标公告",
            "site_name": "中国政府采购网",
            "url": "https://www.ccgp.gov.cn/cggg/dfgg/gkzb/202608/t20260821_1.htm",
            "publish_time": "2026-08-21 19:47:44",
            "date_authoritative": True,
            "retrieval_verified": True,
            "source_fields": {
                "单位": "某医院", "地区": "新疆维吾尔自治区", "所属省/市": "新疆",
                "截止时间": "2026-09-10T11:00", "预算": "200000",
                "采购方式": "公开招标", "科室": "医学检验科",
            },
            "field_evidence": {
                "单位": "采购单位：某医院", "地区": "行政区域：新疆维吾尔自治区",
                "所属省/市": "行政区域：新疆维吾尔自治区",
                "截止时间": "提交投标文件截止时间：2026年09月10日 11:00",
                "预算": "预算金额：20万元", "采购方式": "采购方式：公开招标",
                "科室": "使用科室：医学检验科",
            },
            "search_evidence": {
                "summary": "官方检索摘要", "matched_keywords": ["过敏原"],
                "departments": ["医学检验科"],
            },
        }
        row = {
            "record": {"发布时间": "2026-08-20"},
            "evidence": {
                "source_verified": True,
                "checked_at": "2026-08-23T23:00:00+08:00",
                "field_evidence": {},
            },
        }
        record, _ = canonicalize_create(row, candidate)
        self.assertEqual(record["发布时间"], "2026-08-21")
        self.assertEqual(record["单位"], "某医院")
        self.assertEqual(record["地区"], "新疆维吾尔自治区")
        self.assertEqual(record["所属省/市"], "新疆")
        self.assertEqual(record["截止时间"], "2026-09-10T11:00")
        self.assertEqual(record["预算"], "200000")
        self.assertEqual(record["采购方式"], "公开招标")
        self.assertEqual(record["科室"], "医学检验科")
        self.assertEqual(record["命中关键词"], "过敏原")


if __name__ == "__main__":
    unittest.main()
