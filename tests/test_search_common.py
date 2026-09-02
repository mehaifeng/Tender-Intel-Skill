import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from search_common import canonical_url, merge_source_dirs, plap_notice_id, write_candidates  # noqa: E402
from tender_pipeline import canonicalize_create, historical_identity_keys  # noqa: E402


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
