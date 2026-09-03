import sys
import unittest
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from plap_search import (  # noqa: E402
    build_search_url,
    build_tasks,
    collect,
    parse_time_range,
    parse_title_queries,
    row_to_candidate,
    screen_row,
)


NOTICE_ID = "8a1d04009fd98fe401a03138aab456cf"


def sample_row(**changes):
    row = {
        "noticeId": NOTICE_ID,
        "title": "某单位全自动免疫荧光图像分析系统采购项目采购结果公示(2026-JKBNZE-W4005)",
        "noticeTime": "2026-08-24 10:11:45",
        "openTenderCode": "2026-JKBNZE-W4005",
        "regionName": "江苏省",
        "noticeType": "001024",
        "pageurl": f"/freecms/site/juncai/ggxx/info/2026/{NOTICE_ID}.html",
        "description": "某单位全自动免疫荧光图像分析系统采购项目成交候选人公示",
        "content": (
            "<p>项目预算：12.5万元</p><p>采购方式：询价</p>"
            "<p>采购单位：某医学中心</p><p>投标截止时间：2026年09月01日 09时30分</p>"
        ),
        "attchs": None,
    }
    row.update(changes)
    return row


class PLAPSearchTests(unittest.TestCase):
    def test_title_queries_are_clean_words_not_doc_prose(self):
        # 清单从 markdown 的编号列表解析，同一小节里任何编号行都会被当成 query。
        # 2026-09-03 就因为在该小节加了编号说明，把两行正文读成了检索词。
        queries = parse_title_queries()
        self.assertGreaterEqual(len(queries), 10)
        for query in queries:
            self.assertNotIn(" ", query)
            self.assertNotIn("`", query)
            self.assertLessEqual(len(query), 12, query)
        for banned in ("化学发光", "酶标"):
            self.assertNotIn(banned, queries)

    def test_24h_is_an_exact_rolling_window(self):
        now = datetime(2026, 8, 25, 15, 30, 0)
        start, end = parse_time_range("24h", now=now)
        self.assertEqual(start, datetime(2026, 8, 24, 15, 30, 0))
        self.assertEqual(end, now)

    def test_search_url_uses_title_and_second_precision_range(self):
        task = build_tasks(["免疫荧光"], "title")[0]
        url = build_search_url(
            task, datetime(2026, 8, 24, 1, 2, 3), datetime(2026, 8, 25, 4, 5, 6), 2, 20
        )
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["title"], ["免疫荧光"])
        self.assertEqual(query["operationStartTime"], ["2026-08-24 01:02:03"])
        self.assertEqual(query["operationEndTime"], ["2026-08-25 04:05:06"])
        self.assertEqual(query["currPage"], ["2"])

    def test_public_body_maps_to_partial_verified_candidate(self):
        candidate = row_to_candidate(
            sample_row(), [{"source": "plap", "query": "免疫荧光", "query_mode": "title"}]
        )
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["content_access"], "public_partial")
        self.assertTrue(candidate["retrieval_verified"])
        self.assertEqual(candidate["source_fields"]["项目编号"], "2026-JKBNZE-W4005")
        self.assertEqual(candidate["source_fields"]["地区"], "江苏省")
        self.assertEqual(candidate["source_fields"]["所属省/市"], "江苏")
        self.assertEqual(candidate["source_fields"]["公告类型"], "采购结果公示")
        self.assertEqual(candidate["source_fields"]["预算"], "125000")
        self.assertEqual(candidate["source_fields"]["采购方式"], "询价")
        self.assertEqual(candidate["source_fields"]["单位"], "某医学中心")
        self.assertEqual(candidate["source_fields"]["截止时间"], "2026-09-01T09:30")
        self.assertNotIn("noticeType", candidate["url"])
        self.assertTrue(any(hit.get("query_mode") == "local_content_filter" for hit in candidate["found_by_source_query"]))

    def test_empty_public_body_is_metadata_only(self):
        candidate = row_to_candidate(
            sample_row(content="", description="自身抗体试剂采购结果公示"), []
        )
        self.assertEqual(candidate["content_access"], "metadata_only")
        self.assertFalse(candidate["retrieval_verified"])

    def test_hard_excluded_row_is_dropped_even_with_a_target_signal(self):
        # 回归：此前 row_to_candidate 只判品类信号，硬排除只影响归因不影响去留，
        # 含「化学发光」「酶标仪」的公告靠别的正向信号照样进候选。
        candidate = row_to_candidate(sample_row(
            title="某单位全自动化学发光免疫分析仪采购公告",
            description="自身抗体检测配套化学发光免疫分析仪",
            content="<p>采购全自动化学发光免疫分析仪一台</p>",
        ), [])
        self.assertIsNone(candidate)

    def test_screen_row_reports_why_a_row_was_dropped(self):
        keep, reason = screen_row(sample_row(
            title="酶标仪采购公告", description="酶标仪", content="<p>酶标仪一台</p>"))
        self.assertFalse(keep)
        self.assertEqual(reason, "硬排除:酶标仪")
        keep, reason = screen_row(sample_row(
            title="办公家具采购公告", description="办公桌椅", content="<p>办公家具</p>"))
        self.assertFalse(keep)
        self.assertEqual(reason, "无目标品类信号")
        self.assertEqual(screen_row(sample_row()), (True, ""))

    def test_non_target_row_is_filtered(self):
        candidate = row_to_candidate(sample_row(
            title="办公家具采购结果公示", description="办公桌椅", content="<p>办公家具</p>"
        ), [])
        self.assertIsNone(candidate)

    def test_collect_deduplicates_title_and_notice_type_hits(self):
        class FakeClient:
            def __init__(self):
                self.calls = 0

            def get_json(self, _url):
                self.calls += 1
                return {"code": "200", "total": 1, "data": [sample_row()]}

        tasks = [
            {"mode": "title", "query": "免疫荧光", "notice_type": ""},
            {"mode": "notice_type", "query": "采购需求公示", "notice_type": "00105E"},
        ]
        candidates, failures, raw_count, filtered, reasons, survival = collect(
            FakeClient(), tasks, datetime(2026, 8, 24), datetime(2026, 8, 25), page_size=20
        )
        self.assertEqual(failures, [])
        self.assertEqual(raw_count, 2)
        self.assertEqual(filtered, 0)
        self.assertEqual(len(candidates), 1)
        modes = {hit["query_mode"] for hit in candidates[0]["found_by_source_query"]}
        self.assertTrue({"title", "notice_type", "local_content_filter"} <= modes)


if __name__ == "__main__":
    unittest.main()
