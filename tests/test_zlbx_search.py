"""知了标讯适配器回归。

分批与字段映射的用例都对应 2026-09-05 在真实接口上量到的行为，别随手放宽。
"""
import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch


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
    request_window,
    source_fields_from,
)
import zlbx_search  # noqa: E402
from search_common import body_completeness  # noqa: E402


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

    def test_table_cells_keep_their_boundaries(self):
        """`</td>` 塌成空格会把整行连成长句，「命中关键词」就只剩碎片。"""
        html = ("<tr><td>4</td><td>过敏原特异性IgE抗体检测试剂盒</td>"
                "<td>用于定性检测人血清样本中的过敏原特异性IgE抗体。</td></tr>")
        self.assertIn("\n过敏原特异性IgE抗体检测试剂盒\n", zlbx_search.html_to_text(html))

    def test_parsed_attachment_rides_along_in_its_own_field(self):
        candidate = build_candidate(
            {"bid_id": 1, "title": "某医院新增医用耗材采购公告", "url": "https://example.test/a"},
            {"source": "正文：详见附件。", "source_ext": "4 过敏原特异性IgE抗体检测试剂盒"},
            set())
        self.assertEqual(candidate["attachment_text"], "4 过敏原特异性IgE抗体检测试剂盒")
        # 附件不得混进正文：content 是「公告正文本身取到没有」的唯一依据。
        self.assertEqual(candidate["content"], "正文：详见附件。")

    def test_a_rich_attachment_does_not_disguise_a_shell_body(self):
        shell = ("标题：某医院新增医用耗材一批采购项目遴选公告\n"
                 "发布时间：2026-09-08 00:00:00\n完整信息请查看原文： 原文链接")
        candidate = build_candidate(
            {"bid_id": 1, "title": "某医院试剂采购", "url": "https://example.test/a"},
            {"source": shell, "source_ext": "过敏原特异性IgE抗体检测试剂盒 " * 200}, set())
        self.assertEqual(candidate["content_access"], "public_partial")
        self.assertFalse(candidate["retrieval_verified"])

    def test_shell_body_is_not_treated_as_full_text(self):
        """「详情请求成功」不等于「正文足以核验」。真实样本：73 字的壳。"""
        shell = ("标题：过敏性疾病创新药物国家工程研究中心建设项目招标公告\n"
                 "发布时间：2026-09-03 00:00:00\n完整信息请查看原文： 原文链接")
        candidate = build_candidate(
            {"bid_id": 1, "title": "某医院试剂采购", "url": "https://example.test/a"},
            {"source": shell}, set())
        self.assertEqual(candidate["content_access"], "public_partial")
        self.assertFalse(candidate["retrieval_verified"])
        self.assertTrue(candidate["content_access_reason"])


class SearchWindowTests(unittest.TestCase):
    """pub_time 可能比实际发布日早一天，检索窗口必须比目标窗口往前多放一天。"""

    def test_request_window_starts_one_day_earlier(self):
        start, end = datetime(2026, 9, 3), datetime(2026, 9, 5, 23, 59, 59)
        self.assertEqual(request_window(start, end)[0], datetime(2026, 9, 2))
        self.assertEqual(request_window(start, end)[1], end)

    def test_collect_actually_requests_the_widened_window(self):
        seen = []

        def capture(client, queries, start, end, *args, **kwargs):
            seen.append((start.date(), end.date()))
            return {}

        with patch.object(zlbx_search, "collect_listings", side_effect=capture):
            _, stats = zlbx_search.collect(
                None, ["过敏"], datetime(2026, 9, 3), datetime(2026, 9, 5), 8, 50, 60)
        self.assertEqual(seen, [(date(2026, 9, 2), date(2026, 9, 5))])
        self.assertTrue(stats["request_time_range"].startswith("2026-09-02"))


class BodyCompletenessTests(unittest.TestCase):
    """阈值对着 2026-09-04 实测的 37 条正文定，别随手放宽。"""

    def test_short_but_complete_bidding_body_stays_full(self):
        # 145 字的竞价公告，正文里就是那张商品明细表——短不等于壳。
        body = ("竞价类型：科研服务\n竞价编号：J20260903002975\n结束时间：2026-09-08 10:11:57\n\n"
                "序号 商品名称 需求发布人 品牌要求 商品规格 货号要求 采购数量 采购要求 商品单位\n"
                "1 人组织样品全局组蛋白修饰定量分析 钟微课题组 钟微 30 暂无 例")
        self.assertEqual(body_completeness(body)[0], "public_full")

    def test_login_notice_inside_a_long_body_is_not_a_stub(self):
        # 638 字的招标代理选取公告里，「登录后查看」挡的是咨询电话而不是正文。
        body = "选取方式 择优+竞价\n" + "项目基本情况说明。" * 60 + "\n采购人业务咨询电话 （登录后查看）"
        self.assertEqual(body_completeness(body)[0], "public_full")

    def test_attachment_pointer_in_a_short_body_is_a_stub(self):
        self.assertEqual(body_completeness("一、项目概况\n二、采购需求：详见附件。")[0], "public_partial")

    def test_empty_body_is_metadata_only(self):
        self.assertEqual(body_completeness("")[0], "metadata_only")
        self.assertEqual(body_completeness("   ")[0], "metadata_only")


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


class ReopenForBodySignalTests(unittest.TestCase):
    """标的物是检验仪器或试剂、品类信号只在正文里的公告，必须打开正文再判。

    定标用例是 2026-09-07 实际漏掉的那条军队采购意向：sm_names 全是仪器名，
    唯一命中词 `变态反应` 只出现在正文里，结果在取详情之前就被整条丢掉。
    """

    MILITARY = {
        "bid_id": 1, "title": "一批医疗设备采购项目意向公开公示(2026-JQ08-W9075)",
        "pub_time": "2026-09-07", "url": "https://x.org/mil", "caller_name": "",
        "sm_names": ["多参数生物反馈仪", "全自动化学发光分析仪", "凝血分析仪"],
    }

    def run_collect(self, listing, body, ledger=(), attachment=""):
        details = {}

        def fake_detail(client, item):
            details[item["bid_id"]] = details.get(item["bid_id"], 0) + 1
            return {"source": body, "source_ext": attachment,
                    "source_url": "https://origin.example/1",
                    "bid_id": item["bid_id"], "title": item["title"]}

        with patch.object(zlbx_search, "collect_listings",
                          return_value={listing["bid_id"]: listing}), \
             patch.object(zlbx_search, "fetch_detail", side_effect=fake_detail):
            candidates, stats = zlbx_search.collect(
                None, ["变态反应", "过敏", "印迹"], datetime(2026, 9, 5), datetime(2026, 9, 8),
                8, 50, 60, ledger_records=list(ledger))
        return candidates, stats, details

    def test_body_only_signal_is_reopened_and_queued(self):
        body = "本项目采购变态反应科过敏原检测相关设备，含全自动化学发光分析仪一台。"
        candidates, stats, details = self.run_collect(self.MILITARY, body)
        self.assertEqual(stats["reopened_count"], 1)
        self.assertEqual(stats["reopened_kept_count"], 1)
        self.assertEqual(details, {1: 1})
        self.assertEqual(len(candidates), 1)
        self.assertEqual(stats["reopened_kept"][0]["gate"], "分析仪")
        # 命中归因要按正文重算，否则这条候选说不出「为什么会检索到它」。
        self.assertTrue(candidates[0]["found_by_source_query"])

    def test_body_with_only_broad_fragments_is_still_dropped(self):
        # 宽片段在几十行的设备清单里几乎必然出现一次，靠它放行等于全量取详情。
        body = "本项目采购免疫印迹成像仪一台，用于科研凝胶成像。"
        candidates, stats, _ = self.run_collect(self.MILITARY, body)
        self.assertEqual(stats["reopened_count"], 1)
        self.assertEqual(stats["reopened_kept_count"], 0)
        self.assertEqual(candidates, [])
        self.assertIn("宽片段", stats["reopened_dropped"][0]["reason"])

    def test_body_without_any_signal_is_dropped(self):
        candidates, stats, _ = self.run_collect(self.MILITARY, "本项目采购办公家具与空调。")
        self.assertEqual(stats["reopened_kept_count"], 0)
        self.assertEqual(candidates, [])

    def test_non_lab_items_never_reach_the_detail_call(self):
        listing = {"bid_id": 2, "title": "幼儿园班配教玩具采购项目竞争性谈判公告",
                   "pub_time": "2026-09-07", "url": "https://x.org/toy", "caller_name": "某教育局",
                   "sm_names": ["教学仪器", "塑料轮", "班配教玩具"]}
        candidates, stats, details = self.run_collect(listing, "含变态反应字样的无关正文")
        self.assertEqual(stats["reopened_count"], 0)
        self.assertEqual(stats["prefilter_dropped_count"], 1)
        self.assertEqual(details, {})
        self.assertEqual(candidates, [])

    def test_already_in_the_ledger_is_skipped_before_spending_a_detail_call(self):
        ledger = [{"标题": self.MILITARY["title"], "链接": "https://x.org/mil",
                   "单位": "", "发布时间": "2026-09-07", "_pushed": True,
                   "_feishu_id": "ZB-0001"}]
        candidates, stats, details = self.run_collect(
            self.MILITARY, "变态反应科过敏原检测设备", ledger=ledger)
        self.assertEqual(stats["already_seen_before_detail_count"], 1)
        self.assertEqual(stats["reopened_count"], 0)
        self.assertEqual(details, {})

    DENTAL = {
        "bid_id": 3, "title": "反角手机等一批耗材器械遴选公告", "pub_time": "2026-09-07",
        "url": "https://x.org/dental", "caller_name": "赣南医科大学第三附属医院",
        "sm_names": ["反角手机", "临时耗材器械"],
    }

    def test_hospital_supply_lot_is_reopened_even_without_a_lab_instrument(self):
        # 2026-09-07 漏掉的第二条：标的物只写「一批耗材器械」，
        # 抗核抗体谱只在正文明细表里。医院 + 耗材器械就够格打开正文。
        body = "序号2 抗核抗体谱（IgG）检测试剂 16人份/盒 检测方法：印迹法。"
        candidates, stats, details = self.run_collect(self.DENTAL, body)
        self.assertEqual(stats["reopened_kept_count"], 1)
        self.assertEqual(stats["reopened_kept"][0]["gate"], "耗材")
        self.assertEqual(details, {3: 1})
        self.assertEqual(len(candidates), 1)

    def test_supply_lot_from_a_non_medical_buyer_is_not_reopened(self):
        listing = dict(self.DENTAL, bid_id=4, caller_name="某市公安局刑侦支队",
                       title="一批耗材器械采购公告")
        candidates, stats, details = self.run_collect(listing, "含抗核抗体谱的无关正文")
        self.assertEqual(stats["reopened_count"], 0)
        self.assertEqual(stats["prefilter_dropped_count"], 1)
        self.assertEqual(details, {})
        self.assertEqual(candidates, [])

    SUPPLY_LOT = {
        "bid_id": 5, "title": "茂名市电白区人民医院新增医用耗材采购需求信息公告",
        "pub_time": "2026-09-07", "url": "https://x.org/mm",
        "caller_name": "茂名市电白区人民医院", "sm_names": ["医用耗材"],
    }
    # 正文只有报名须知，标的清单是一份 .xls；知了把它解析进 source_ext 一并回传。
    REGISTRATION_ONLY = ("二、项目内容及需求： 茂名市电白区人民医院新增医用耗材"
                         "项目内容及需求.xls 三、供应商资格条件 …… 四、报名资料要求")
    PARSED_ATTACHMENT = ("1.Sheet1 新增医用耗材项目内容及需求 3 传送导管 专用输送鞘管 "
                         "4 过敏原特异性IgE抗体检测试剂盒 用于定性检测人血清样本中的"
                         "过敏原特异性IgE抗体。")

    def test_signal_only_in_the_parsed_attachment_is_reopened_and_queued(self):
        """2026-09-09 复盘的茂名电白那条：详情早就带回附件解析文本，只是没人读。"""
        candidates, stats, details = self.run_collect(
            self.SUPPLY_LOT, self.REGISTRATION_ONLY, attachment=self.PARSED_ATTACHMENT)
        self.assertEqual(stats["reopened_count"], 1)
        self.assertEqual(stats["reopened_kept_count"], 1)
        self.assertEqual(stats["reopened_kept"][0]["gate"], "耗材")
        self.assertEqual(details, {5: 1})
        self.assertEqual(len(candidates), 1)
        # 命中归因同样要看得到附件，否则这条说不出「为什么会检索到它」。
        self.assertTrue(candidates[0]["found_by_source_query"])

    EARLY_STAGE = {
        "bid_id": 6, "title": "关于桂林市中医医院病房能力提升项目市场调研服务采购公告",
        "pub_time": "2026-09-07", "url": "https://x.org/gl",
        "caller_name": "桂林市中医医院", "sm_names": ["病房能力提升项目市场调研服务"],
    }

    def test_early_stage_hospital_notice_is_reopened_and_queued(self):
        """医院的前期公告：标的物就是项目名，50 项设备清单整份在附件里。"""
        candidates, stats, details = self.run_collect(
            self.EARLY_STAGE, "因医院工作需要，拟对本项目进行市场调研。采购设备 50项，详见附件1。",
            attachment=("桂林市中医医院病房改造提升项目设备购置清单\n序号\n设备名称\n数量\n"
                        "37\nPCR实验室相关设备\n1 批\n"
                        "38\n全自动化学发光免疫分析仪（过敏原检测）\n1 套\n"
                        "39\n血培养箱\n1 套"))
        self.assertEqual(stats["reopened_count"], 1)
        self.assertEqual(stats["reopened_kept_count"], 1)
        self.assertEqual(stats["reopened_kept"][0]["gate"], "市场调研")
        self.assertEqual(details, {6: 1})
        self.assertEqual(len(candidates), 1)
        # 清单里并列的 PCR 只写进 body_exclude_term，不得连坐整条混合包。
        self.assertIn("过敏原", candidates[0]["attachment_text"])

    def test_same_notice_without_the_attachment_is_still_dropped(self):
        """对照组：附件文本是唯一变量，去掉它这条就该照旧丢。"""
        candidates, stats, _ = self.run_collect(self.SUPPLY_LOT, self.REGISTRATION_ONLY)
        self.assertEqual(stats["reopened_count"], 1)
        self.assertEqual(stats["reopened_kept_count"], 0)
        self.assertEqual(candidates, [])
