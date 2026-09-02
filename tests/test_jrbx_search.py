import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from jrbx_search import (  # noqa: E402
    ACTIONABLE_NOTICE_TYPES,
    CHECK_TOKEN_EXIT_CODES,
    JrbxAuthError,
    JrbxError,
    article_url,
    check_token,
    build_candidate,
    collect,
    load_credentials,
    parse_queries,
    parse_time_range,
    passes_prefilter,
    split_terms,
    to_millis,
    token_expires_at,
)


NOTICE_ID = "3A53E748CCC9FABFDBEC7817542EAB4D"
CREDENTIALS = {"userId": "U1", "token": "T1", "openid": "O1"}


def sample_item(**changes):
    item = {
        "id": NOTICE_ID,
        "year": "2026",
        "title": "某某医院过敏原特异性IgE检测试剂采购公告",
        "product": "过敏原检测试剂",
        "titleProduct": "过敏原检测试剂",
        "noticeType": "招标",
        "noticeTypeCode": "20100",
        "bidType": "公开招标",
        "organization": "某某医院",
        "province": "广西",
        "city": "南宁市",
        "county": "青秀区",
        "budget": 985000,
        "publishTime": to_millis(datetime(2026, 8, 23, 9, 30)),
        "bidDeadline": to_millis(datetime(2026, 8, 30, 9, 0)),
        "score": 1.5,
    }
    item.update(changes)
    return item


def sample_detail(**changes):
    detail = {
        "title": "某某医院过敏原特异性IgE检测试剂采购公告",
        "content": "<p>项目编号：YSB230322</p><p>采购人：某某医院</p><p>过敏原特异性IgE检测试剂</p>",
        "simpleContent": "<p>某某医院拟采购过敏原检测试剂</p>",
        "attachments": [
            {"name": "招标文件.pdf", "originUrl": "https://zbb.example.gov.cn/f/a.pdf"},
            {"name": "无效附件", "originUrl": "/relative/path.pdf"},
        ],
    }
    detail.update(changes)
    return detail


class FakeClient:
    """按端点回放固定响应，用来验证配额与丢弃逻辑，不触网。"""

    def __init__(self, items, origin_results):
        self.items = items
        self.origin_results = dict(origin_results)
        self.request_count = 0
        self.origin_calls = []

    def search(self, terms, start, end, page, page_size, notice_types):
        self.request_count += 1
        if page > 1:
            return {"items": [], "totalPage": 1, "totalCount": len(self.items)}
        return {"items": self.items, "totalPage": 1, "totalCount": len(self.items)}

    def notice_detail(self, notice_id):
        self.request_count += 1
        return sample_detail()

    def original_url(self, notice_id):
        self.request_count += 1
        self.origin_calls.append(notice_id)
        return self.origin_results.get(notice_id, ("", True))


class TimeRangeTests(unittest.TestCase):
    def test_72h_is_an_exact_rolling_window(self):
        now = datetime(2026, 9, 2, 15, 30, 0)
        start, end = parse_time_range("72h", now=now)
        self.assertEqual(start, datetime(2026, 8, 30, 15, 30, 0))
        self.assertEqual(end, now)

    def test_explicit_date_range_covers_the_whole_end_day(self):
        start, end = parse_time_range("2026-08-30..2026-09-01")
        self.assertEqual(start, datetime(2026, 8, 30, 0, 0, 0))
        self.assertEqual(end, datetime(2026, 9, 1, 23, 59, 59))

    def test_invalid_range_is_rejected(self):
        with self.assertRaises(Exception):
            parse_time_range("最近三天")


class QueryTests(unittest.TestCase):
    def test_plus_sign_expands_to_and_terms(self):
        self.assertEqual(split_terms("过敏原+试剂"), ["过敏原", "试剂"])
        self.assertEqual(split_terms("过敏原"), ["过敏原"])
        self.assertEqual(split_terms(" 过敏原 + 试剂 "), ["过敏原", "试剂"])

    def test_default_queries_come_from_the_reference_file(self):
        queries = parse_queries()
        self.assertEqual(len(queries), 30)
        self.assertIn("过敏原", queries)
        # 与豆包不同，睿销不禁止空格，但清单仍应是干净的单词或 AND 组词
        for query in queries:
            self.assertTrue(query.strip())


class CredentialTests(unittest.TestCase):
    def test_missing_credentials_raise_auth_error(self):
        with self.assertRaises(JrbxAuthError):
            load_credentials({"JRBX_USER_ID": "U1"})

    def test_credentials_map_access_token_to_body_key_token(self):
        loaded = load_credentials(
            {"JRBX_USER_ID": "U1", "JRBX_TOKEN": "T1", "JRBX_OPENID": "O1"}
        )
        self.assertEqual(loaded, CREDENTIALS)

    def test_token_expiry_is_parsed_from_jwt(self):
        # {"exp": 1790061300}
        token = "eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjE3OTAwNjEzMDB9.sig"
        self.assertEqual(token_expires_at(token), datetime.fromtimestamp(1790061300))

    def test_unparseable_token_does_not_raise(self):
        self.assertIsNone(token_expires_at("not-a-jwt"))


class PrefilterTests(unittest.TestCase):
    def test_target_category_passes(self):
        self.assertTrue(passes_prefilter(sample_item()))

    def test_excluded_category_is_rejected_even_with_target_signal(self):
        item = sample_item(title="过敏原检测试剂及酶标仪采购", product="酶标仪")
        self.assertFalse(passes_prefilter(item))

    def test_unrelated_notice_is_rejected(self):
        item = sample_item(title="办公楼物业管理服务采购", product="物业服务", titleProduct="")
        self.assertFalse(passes_prefilter(item))


class CandidateTests(unittest.TestCase):
    def test_structured_fields_are_mapped_with_evidence(self):
        candidate = build_candidate(
            sample_item(), sample_detail(),
            "https://zbb.example.gov.cn/notice/123", [], ["过敏原"],
        )
        fields = candidate["source_fields"]
        self.assertEqual(fields["单位"], "某某医院")
        self.assertEqual(fields["发布时间"], "2026-08-23")
        self.assertEqual(fields["截止时间"], "2026-08-30T09:00")
        self.assertEqual(fields["预算"], "985000")
        self.assertEqual(fields["采购方式"], "公开招标")
        self.assertEqual(fields["公告类型"], "招标")
        self.assertEqual(fields["项目编号"], "YSB230322")
        for key in fields:
            self.assertIn(key, candidate["field_evidence"])

    def test_region_uses_full_province_name_and_short_province(self):
        candidate = build_candidate(
            sample_item(), sample_detail(), "https://zbb.example.gov.cn/n/1", [], [],
        )
        # schema.md 要求「地区」以省级全称开头，「所属省/市」只填简称
        self.assertEqual(candidate["source_fields"]["地区"], "广西壮族自治区南宁市青秀区")
        self.assertEqual(candidate["source_fields"]["所属省/市"], "广西")

    def _region_for_city(self, city):
        candidate = build_candidate(
            sample_item(city=city, county=""), sample_detail(),
            "https://zbb.example.gov.cn/n/1", [], [],
        )
        return candidate["source_fields"]["地区"]

    def test_city_without_a_division_suffix_gets_one(self):
        self.assertEqual(self._region_for_city("南昌"), "广西壮族自治区南昌市")

    def test_prefecture_cities_ending_in_zhou_still_get_shi(self):
        # 温州/亳州/杭州是地级市，名字自带「州」，不能当成已有行政区后缀
        for city in ("温州", "亳州", "杭州"):
            self.assertEqual(self._region_for_city(city), f"广西壮族自治区{city}市")

    def test_existing_division_suffixes_are_left_alone(self):
        for city in ("黔西南布依族苗族自治州", "南宁市", "阿拉善盟", "海东地区", "石家庄市"):
            self.assertTrue(self._region_for_city(city).endswith(city))

    def test_nationwide_notice_reports_no_region(self):
        candidate = build_candidate(
            sample_item(province="全国", city="", county=""), sample_detail(),
            "https://zbb.example.gov.cn/n/1", [], [],
        )
        self.assertNotIn("地区", candidate["source_fields"])

    def test_url_is_the_origin_site_not_jrbx(self):
        candidate = build_candidate(
            sample_item(), sample_detail(),
            "https://zbb.example.gov.cn/notice/123?spm=abc", [], [],
        )
        self.assertEqual(candidate["url"], "https://zbb.example.gov.cn/notice/123")
        self.assertEqual(candidate["site_name"], "zbb.example.gov.cn")
        self.assertNotIn("jrbx", candidate["url"])

    def test_relative_attachment_urls_are_dropped(self):
        candidate = build_candidate(
            sample_item(), sample_detail(), "https://zbb.example.gov.cn/n/1", [], [],
        )
        self.assertEqual(
            candidate["attachments"], [{"url": "https://zbb.example.gov.cn/f/a.pdf", "name": "招标文件.pdf"}]
        )

    def test_source_priority_sits_below_official_first_party(self):
        candidate = build_candidate(
            sample_item(), sample_detail(), "https://zbb.example.gov.cn/n/1", [], [],
        )
        self.assertEqual(candidate["source_priority"], 300)
        self.assertEqual(candidate["link_kind"], "origin")
        self.assertTrue(candidate["date_authoritative"])
        self.assertEqual(candidate["content_access"], "public_full")


class ArticleUrlFallbackTests(unittest.TestCase):
    def test_permalink_needs_both_id_and_year(self):
        self.assertEqual(
            article_url(sample_item()),
            f"https://www.jrbx.com/article/detail?id={NOTICE_ID}&year=2026",
        )
        self.assertEqual(article_url(sample_item(year="")), "")
        self.assertEqual(article_url(sample_item(id="")), "")

    def test_year_falls_back_to_the_detail_payload(self):
        item = sample_item(year="")
        self.assertEqual(
            article_url(item, {"year": "2025"}),
            f"https://www.jrbx.com/article/detail?id={NOTICE_ID}&year=2025",
        )

    def test_missing_origin_url_uses_the_jrbx_permalink(self):
        candidate = build_candidate(sample_item(), sample_detail(), "", [], [])
        self.assertEqual(
            candidate["url"], f"https://www.jrbx.com/article/detail?id={NOTICE_ID}&year=2026"
        )
        self.assertEqual(candidate["link_kind"], "jrbx_article")
        self.assertEqual(candidate["site_name"], "睿销")
        self.assertIn("需登录", candidate["auth_info_des"])

    def test_permalink_candidates_rank_below_origin_link_candidates(self):
        # 同一公告若两种链接都拿得到，合并时应保留可匿名访问的回源版本
        origin = build_candidate(sample_item(), sample_detail(), "https://a.gov.cn/1", [], [])
        fallback = build_candidate(sample_item(), sample_detail(), "", [], [])
        self.assertGreater(origin["source_priority"], fallback["source_priority"])
        self.assertEqual(fallback["source_priority"], 250)

    def test_full_text_is_still_saved_so_the_model_need_not_open_the_link(self):
        candidate = build_candidate(sample_item(), sample_detail(), "", [], [])
        self.assertTrue(candidate["retrieval_verified"])
        self.assertEqual(candidate["content_access"], "public_full")
        self.assertIn("过敏原", candidate["content"])


class QuotaTests(unittest.TestCase):
    def _run(self, origin_results, max_origin_lookups=10):
        items = [
            sample_item(id="N1", publishTime=to_millis(datetime(2026, 9, 1, 12, 0))),
            sample_item(id="N2", publishTime=to_millis(datetime(2026, 8, 31, 12, 0))),
            sample_item(id="N3", publishTime=to_millis(datetime(2026, 8, 30, 12, 0))),
        ]
        client = FakeClient(items, origin_results)
        candidates, failures, stats = collect(
            client, ["过敏原"],
            datetime(2026, 8, 30), datetime(2026, 9, 2),
            max_origin_lookups=max_origin_lookups,
        )
        return client, candidates, failures, stats

    def test_notice_without_origin_url_falls_back_to_the_permalink(self):
        _, candidates, _, stats = self._run({
            "N1": ("https://a.example.gov.cn/1", False),
            "N2": ("", False),
            "N3": ("https://c.example.gov.cn/3", False),
        })
        self.assertEqual(len(candidates), 3)
        self.assertEqual(
            [c["link_kind"] for c in candidates], ["origin", "jrbx_article", "origin"]
        )
        self.assertEqual(stats["fallback_article_url_count"], 1)
        self.assertEqual(stats["origin_url_count"], 2)
        self.assertEqual(stats["dropped_no_url"], 0)

    def test_quota_exhaustion_stops_lookups_but_keeps_producing_candidates(self):
        client, candidates, _, stats = self._run({
            "N1": ("https://a.example.gov.cn/1", False),
            "N2": ("", True),
        })
        # N2 触发配额耗尽后，N3 不再发起回源请求，但仍以主站链接产出
        self.assertEqual(client.origin_calls, ["N1", "N2"])
        self.assertTrue(stats["origin_quota_exhausted"])
        self.assertEqual(len(candidates), 3)
        self.assertEqual(stats["fallback_article_url_count"], 2)
        for candidate in candidates[1:]:
            self.assertIn("jrbx.com/article/detail", candidate["url"])

    def test_lookup_budget_is_respected(self):
        client, candidates, _, stats = self._run(
            {f"N{i}": (f"https://a.example.gov.cn/{i}", False) for i in (1, 2, 3)},
            max_origin_lookups=1,
        )
        self.assertEqual(client.origin_calls, ["N1"])
        self.assertEqual(stats["origin_lookups"], 1)
        self.assertEqual(len(candidates), 3)
        self.assertEqual(stats["fallback_article_url_count"], 2)

    def test_candidate_without_id_or_year_is_dropped_rather_than_faked(self):
        items = [sample_item(id="N1", year="")]
        client = FakeClient(items, {})
        candidates, _, stats = collect(
            client, ["过敏原"], datetime(2026, 8, 30), datetime(2026, 9, 2),
            max_origin_lookups=0,
        )
        self.assertEqual(candidates, [])
        self.assertEqual(stats["dropped_no_url"], 1)

    def test_zero_budget_skips_origin_lookups_entirely(self):
        client, candidates, _, stats = self._run(
            {f"N{i}": (f"https://a.example.gov.cn/{i}", False) for i in (1, 2, 3)},
            max_origin_lookups=0,
        )
        self.assertEqual(client.origin_calls, [])
        self.assertEqual(len(candidates), 3)
        self.assertEqual(stats["fallback_article_url_count"], 3)

    def test_origin_lookups_go_to_newest_notices_first(self):
        client, _, _, _ = self._run(
            {f"N{i}": (f"https://a.example.gov.cn/{i}", False) for i in (1, 2, 3)},
            max_origin_lookups=2,
        )
        self.assertEqual(client.origin_calls, ["N1", "N2"])


def jwt_expiring_in(days):
    import base64 as _b64
    import json as _json

    exp = int((datetime.now() + timedelta(days=days)).timestamp())
    body = _b64.urlsafe_b64encode(_json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"header.{body}.sig"


def credentials_expiring_in(days):
    return {"userId": "U1", "token": jwt_expiring_in(days), "openid": "O1"}


class ProbeClient:
    """--check-token 的探测桩：记录调用并按需抛出指定异常。"""

    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def __call__(self, credentials, delay=0.0):
        return self

    def search(self, terms, start, end, page, page_size, notice_types):
        self.calls.append({"terms": terms, "page_size": page_size})
        if self.error:
            raise self.error
        return {"items": [], "totalPage": 0, "totalCount": 0}


class CheckTokenTests(unittest.TestCase):
    def test_healthy_token_reports_ok(self):
        probe = ProbeClient()
        report = check_token(credentials_expiring_in(19), client_factory=probe)
        self.assertEqual(report["status"], "ok")
        self.assertTrue(report["server_accepted"])
        self.assertAlmostEqual(report["days_remaining"], 19, places=1)
        self.assertEqual(CHECK_TOKEN_EXIT_CODES[report["status"]], 0)

    def test_probe_is_cheap_and_does_not_touch_origin_quota(self):
        probe = ProbeClient()
        check_token(credentials_expiring_in(19), client_factory=probe)
        self.assertEqual(len(probe.calls), 1)
        self.assertEqual(probe.calls[0]["page_size"], 1)

    def test_expiring_soon_uses_a_distinct_exit_code(self):
        report = check_token(credentials_expiring_in(2), client_factory=ProbeClient())
        self.assertEqual(report["status"], "expiring_soon")
        self.assertTrue(report["expiring_soon"])
        self.assertEqual(CHECK_TOKEN_EXIT_CODES[report["status"]], 4)

    def test_expired_token_short_circuits_before_any_request(self):
        probe = ProbeClient()
        report = check_token(credentials_expiring_in(-1), client_factory=probe)
        self.assertEqual(report["status"], "expired")
        self.assertEqual(probe.calls, [])
        self.assertEqual(CHECK_TOKEN_EXIT_CODES[report["status"]], 3)

    def test_server_rejection_is_reported_as_needing_a_rescan(self):
        # 被别处重新扫码顶掉（返回码 08）：JWT 还没到期，但服务端已不认
        probe = ProbeClient(error=JrbxAuthError("睿销登录态失效（code=08）"))
        report = check_token(credentials_expiring_in(19), client_factory=probe)
        self.assertEqual(report["status"], "rejected")
        self.assertFalse(report["server_accepted"])
        self.assertEqual(CHECK_TOKEN_EXIT_CODES[report["status"]], 3)

    def test_network_failure_is_not_mistaken_for_a_dead_token(self):
        probe = ProbeClient(error=JrbxError("网络错误：timed out"))
        report = check_token(credentials_expiring_in(19), client_factory=probe)
        self.assertEqual(report["status"], "probe_failed")
        self.assertNotEqual(CHECK_TOKEN_EXIT_CODES[report["status"]], 3)

    def test_offline_mode_skips_the_probe(self):
        probe = ProbeClient(error=JrbxAuthError("不该被调用"))
        report = check_token(credentials_expiring_in(19), probe=False, client_factory=probe)
        self.assertEqual(report["status"], "ok")
        self.assertIsNone(report["server_accepted"])
        self.assertEqual(probe.calls, [])

    def test_unparseable_token_still_validates_against_the_server(self):
        report = check_token(
            {"userId": "U1", "token": "not-a-jwt", "openid": "O1"}, client_factory=ProbeClient()
        )
        self.assertEqual(report["status"], "unknown_expiry")
        self.assertTrue(report["server_accepted"])
        self.assertEqual(CHECK_TOKEN_EXIT_CODES[report["status"]], 0)


class NoticeTypeTests(unittest.TestCase):
    def test_default_notice_types_exclude_concluded_stages(self):
        # 已有结论的阶段进核实产不出可行动情报（SKILL.md「1. 检索与排队」）
        for concluded in ("20200", "20201", "20203", "20205", "20206", "20500", "20501", "20502"):
            self.assertNotIn(concluded, ACTIONABLE_NOTICE_TYPES)
        for actionable in ("20100", "20300", "20400", "20600"):
            self.assertIn(actionable, ACTIONABLE_NOTICE_TYPES)


if __name__ == "__main__":
    unittest.main()
