"""知了标讯适配器回归。

分批与字段映射的用例都对应 2026-09-05 在真实接口上量到的行为，别随手放宽。
"""
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from zlbx_search import (  # noqa: E402
    plan_batches,
    window_days,
    DEFAULT_BID_PROCESS,
    MATCH_MODES,
    MAX_PAGE_SIZE,
    _budget,
    _clean,
    _extract_deadline,
    _locality,
    build_candidate,
    collect_listings,
    html_to_text,
    mask_key,
    parse_queries,
    parse_time_range,
    product_list_of,
    source_fields_from,
)


class FakeClient:
    """按「关键词集合 -> 命中列表」回放接口，并记录每次调用的词与页码。"""

    def __init__(self, corpus):
        self.corpus = corpus
        self.calls = []

    def call(self, tool, payload):
        keywords = tuple(payload["keywords"])
        page, size = payload["page"], payload["page_size"]
        self.calls.append((keywords, page))
        hits = []
        seen = set()
        for keyword in keywords:
            for doc in self.corpus.get(keyword, []):
                if doc["bid_id"] not in seen:
                    seen.add(doc["bid_id"])
                    hits.append(doc)
        start = (page - 1) * size
        return {"total": len(hits), "items": hits[start:start + size]}


def docs(prefix, count):
    return [{"bid_id": f"{prefix}{i}", "title": f"{prefix}-{i}"} for i in range(count)]


class QueryListTests(unittest.TestCase):
    def test_queries_come_from_keywords_md(self):
        queries = parse_queries()
        self.assertGreater(len(queries), 50)
        self.assertIn("过敏", queries)
        self.assertIn("印迹", queries)
        # 清单里每条都必须是不含空格的单词
        self.assertTrue(all(" " not in query for query in queries))

    def test_time_range_forms(self):
        start, end = parse_time_range("2026-09-01..2026-09-05")
        self.assertEqual(start.date().isoformat(), "2026-09-01")
        self.assertEqual(end.date().isoformat(), "2026-09-05")
        with self.assertRaises(Exception):
            parse_time_range("2026-09-05..2026-09-01")


class BatchingTests(unittest.TestCase):
    """分页不稳定是实测结论，分批策略必须保证「每批落在单页内」。"""

    def test_batch_within_one_page_costs_one_call(self):
        corpus = {"a": docs("a", 10), "b": docs("b", 10)}
        client = FakeClient(corpus)
        stats = {"empty_batches": 0, "split_batches": 0, "paged_queries": 0}
        found = collect_listings(client, ["a", "b"], _t(), _t(), 8, MAX_PAGE_SIZE, stats)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len(found), 20)
        self.assertEqual(stats["split_batches"], 0)

    def test_oversized_batch_is_split_instead_of_paged(self):
        """整批超过单页时切半重跑，绝不翻页——翻页会丢结果。"""
        corpus = {"a": docs("a", 40), "b": docs("b", 40)}
        client = FakeClient(corpus)
        stats = {"empty_batches": 0, "split_batches": 0, "paged_queries": 0}
        found = collect_listings(client, ["a", "b"], _t(), _t(), 8, MAX_PAGE_SIZE, stats)
        self.assertEqual(stats["split_batches"], 1)
        self.assertEqual(stats["paged_queries"], 0)
        # 首批的首页结果也收下了，不浪费那次调用；切半后两批各自单页取完
        self.assertEqual(len(found), 80)
        self.assertTrue(all(page == 1 for _, page in client.calls))

    def test_single_wide_keyword_falls_back_to_paging(self):
        """单个词自己就超一页时无法再切，只能翻页。"""
        corpus = {"过敏": docs("g", 120)}
        client = FakeClient(corpus)
        stats = {"empty_batches": 0, "split_batches": 0, "paged_queries": 0}
        found = collect_listings(client, ["过敏"], _t(), _t(), 8, MAX_PAGE_SIZE, stats)
        self.assertEqual(stats["paged_queries"], 1)
        self.assertEqual(len(found), 120)

    def test_empty_batch_is_counted_and_costs_one_call(self):
        client = FakeClient({})
        stats = {"empty_batches": 0, "split_batches": 0, "paged_queries": 0}
        found = collect_listings(client, ["无此词"], _t(), _t(), 8, MAX_PAGE_SIZE, stats)
        self.assertEqual(found, {})
        self.assertEqual(stats["empty_batches"], 1)

    def test_request_uses_fulltext_and_actionable_stages(self):
        """默认匹配模式不是全文；不显式传 fulltext 召回掉一个数量级。"""
        client = FakeClient({"a": docs("a", 1)})
        captured = {}

        def spy(tool, payload):
            captured.update(payload)
            return {"total": 1, "items": docs("a", 1)}

        client.call = spy
        collect_listings(client, ["a"], _t(), _t(), 8, MAX_PAGE_SIZE,
                         {"empty_batches": 0, "split_batches": 0, "paged_queries": 0})
        self.assertEqual(captured["match_modes"], MATCH_MODES)
        self.assertEqual(captured["match_modes"], ["fulltext"])
        self.assertEqual(captured["bid_process"], DEFAULT_BID_PROCESS)
        self.assertLessEqual(captured["page_size"], MAX_PAGE_SIZE)


class FieldMappingTests(unittest.TestCase):
    def test_html_fragments_are_stripped_from_structured_fields(self):
        """实测 bid_no 返回过 `</span>2641STC60596`。"""
        self.assertEqual(_clean("</span>2641STC60596"), "2641STC60596")

    def test_money_is_yuan_and_not_divided(self):
        self.assertEqual(_budget({"money": 985000}), "985000")
        self.assertEqual(_budget({"money": 0}), "")
        self.assertEqual(_budget({"money": None}), "")

    def test_locality_appends_city_suffix_but_not_to_real_suffixes(self):
        self.assertEqual(_locality({"province": "安徽", "city": "滁州", "county": "凤阳县"}),
                         "滁州市凤阳县")
        self.assertEqual(_locality({"province": "内蒙古", "city": "阿拉善盟", "county": ""}),
                         "阿拉善盟")
        # 直辖市的 city 与 province 同名，不重复输出
        self.assertEqual(_locality({"province": "北京", "city": "北京", "county": "朝阳区"}),
                         "朝阳区")

    def test_signup_time_never_becomes_deadline(self):
        """signup_time 是报名截止，schema 明确禁止当成投标截止。"""
        fields = source_fields_from({"signup_time": "2026-09-11 16:00", "tender_time": ""})
        self.assertNotIn("截止时间", fields)

    def test_deadline_prefers_structured_tender_time(self):
        fields = source_fields_from({"tender_time": "2026-09-10 11:00:00"})
        self.assertEqual(fields["截止时间"], "2026-09-10T11:00")

    def test_body_deadline_fallback_when_tender_time_missing(self):
        item = {"bid_id": 1, "title": "某医院过敏原试剂采购公告", "sm_names": ["过敏原检测试剂"]}
        detail = {
            "source": "四、提交投标文件截止时间、开标时间和地点 2026年09月16日 09时00分00秒",
            "source_url": "https://example.gov.cn/a",
        }
        candidate = build_candidate(item, detail, {(1, "过敏")})
        self.assertEqual(candidate["source_fields"]["截止时间"], "2026-09-16T09:00")
        self.assertIn("正文", candidate["field_evidence"]["截止时间"])

    def test_correction_notice_with_two_datetimes_yields_no_deadline(self):
        """更正公告并排写原/现两个时间，抓第一个就是作废的旧时间。"""
        self.assertEqual(
            _extract_deadline("投标文件递交截止时间 2026年09月01日11时00分 2026年09月11日11时00分")[0],
            "",
        )

    def test_product_list_merges_subject_and_brand(self):
        self.assertEqual(
            product_list_of({"sm_names": ["过敏原检测试剂"], "brand_names": ["浩欧博"]}),
            "过敏原检测试剂、浩欧博",
        )

    def test_html_body_becomes_plain_text(self):
        text = html_to_text("<div><p>项目编号：ABC-1</p><br><script>x=1</script>正文</div>")
        self.assertIn("项目编号：ABC-1", text)
        self.assertNotIn("script", text)


class CandidateContractTests(unittest.TestCase):
    def test_source_url_is_preferred_over_site_link(self):
        """发给销售的链接要能匿名打开，知了站内链接需要登录。"""
        item = {"bid_id": 1, "title": "某医院自身抗体试剂采购公告",
                "url": "https://www.zhiliaobiaoxun.com/content/1/b1?sk=A"}
        detail = {"source": "正文", "source_url": "https://example.gov.cn/real",
                  "url": "https://www.zhiliaobiaoxun.com/content/1/b1?sk=B"}
        candidate = build_candidate(item, detail, set())
        self.assertEqual(candidate["url"], "https://example.gov.cn/real")
        self.assertEqual(len(candidate["alternate_sources"]), 1)

    def test_candidate_without_any_url_is_dropped_not_faked(self):
        self.assertIsNone(build_candidate({"bid_id": 1, "title": "x"}, {}, set()))

    def test_missing_detail_degrades_to_metadata_only(self):
        candidate = build_candidate(
            {"bid_id": 1, "title": "某医院试剂采购", "url": "https://example.test/a"}, None, set())
        self.assertEqual(candidate["content_access"], "metadata_only")
        self.assertFalse(candidate["retrieval_verified"])


class CredentialTests(unittest.TestCase):
    def test_api_key_is_masked(self):
        """摘要与日志只留前缀；Key 不进任何落盘文件，也不进测试固件。"""
        masked = mask_key("zlbx_" + "A" * 30 + "SECRETTAIL")
        self.assertTrue(masked.startswith("zlbx_AAAA"))
        self.assertNotIn("SECRETTAIL", masked)
        self.assertLess(len(masked), 12)


def _t():
    from datetime import datetime
    return datetime(2026, 9, 5)


if __name__ == "__main__":
    unittest.main()


class QueryAttributionTests(unittest.TestCase):
    """候选契约要求 found_by_query 是整数编号，found_by_source_query 是带词的字典。"""

    def test_query_hits_are_split_into_numbers_and_words(self):
        candidate = build_candidate(
            {"bid_id": 1, "title": "某医院过敏原试剂采购", "url": "https://example.test/a"},
            {"source": "正文", "source_url": "https://example.gov.cn/a"},
            {(4, "IgE"), (1, "过敏")},
        )
        self.assertEqual(candidate["found_by_query"], [1, 4])
        self.assertEqual(
            candidate["found_by_source_query"],
            [{"source": "zlbx", "query_number": 1, "query": "过敏"},
             {"source": "zlbx", "query_number": 4, "query": "IgE"}],
        )


class BatchPlanningTests(unittest.TestCase):
    """装箱降低「发现成本」：自适应切半正确但要先打一枪才知道该不该切。

    2026-09-05 同口径实测（5 天窗口、只跑列表）：纯自适应 50 次调用，
    拿实测命中数预先装箱只要 27 次，多出来的 23 次全是探路。
    """

    def test_no_history_falls_back_to_flat_batches(self):
        groups = plan_batches(list("abcde"), {}, days=1, page_size=50, batch_size=8)
        self.assertEqual([sorted(g) for g in groups], [list("abcde")])

    def test_wide_keyword_gets_its_own_group(self):
        groups = plan_batches(list("abcde"), {"a": 100}, days=1, page_size=50, batch_size=8)
        self.assertIn(["a"], groups)
        self.assertTrue(any(set(g) == set("bcde") for g in groups))

    def test_window_length_shrinks_batches(self):
        counts = {q: 20 for q in "abcde"}
        one_day = plan_batches(list("abcde"), counts, days=1, page_size=50, batch_size=8)
        five_day = plan_batches(list("abcde"), counts, days=5, page_size=50, batch_size=8)
        self.assertLess(len(one_day), len(five_day))

    def test_packed_groups_stay_within_one_page(self):
        """OR 的 total 不会超过各词命中数之和，所以按和装箱是保守的。"""
        counts = {"a": 30, "b": 12, "c": 8, "d": 3, "e": 1}
        groups = plan_batches(list("abcde"), counts, days=1, page_size=50, batch_size=8)
        for group in groups:
            if len(group) > 1:
                self.assertLessEqual(sum(counts[q] for q in group), 45)

    def test_window_days_is_inclusive(self):
        from datetime import datetime
        self.assertEqual(window_days(datetime(2026, 9, 3), datetime(2026, 9, 5)), 3)
        self.assertEqual(window_days(datetime(2026, 9, 5), datetime(2026, 9, 5)), 1)

    def test_single_word_query_totals_are_recorded_for_next_run(self):
        client = FakeClient({"过敏": docs("g", 7)})
        stats = {"empty_batches": 0, "split_batches": 0, "paged_queries": 0}
        collect_listings(client, ["过敏"], _t(), _t(), 8, MAX_PAGE_SIZE, stats,
                         counts={"过敏": 7}, days=1)
        self.assertEqual(stats["observed_hit_counts"], {"过敏": 7.0})
