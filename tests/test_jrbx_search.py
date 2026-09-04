import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from jrbx_search import (  # noqa: E402
    ACTIONABLE_NOTICE_TYPES,
    CHECK_TOKEN_EXIT_CODES,
    JrbxAuthError,
    JrbxClient,
    JrbxError,
    JrbxRateLimitError,
    article_url,
    check_credential_pool,
    check_token,
    build_candidate,
    collect,
    credentials_from_user_info,
    load_credential_pool,
    load_credentials,
    mask_user_id,
    parse_queries,
    parse_time_range,
    passes_prefilter,
    read_credential_pool_file,
    split_terms,
    to_millis,
    token_expires_at,
    write_credentials_file,
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

    def test_default_queries_come_from_the_shared_keyword_table(self):
        # 清单在 references/keywords.md，与 CCGP 共用一份；适配器不另存副本。
        from ccgp_search import parse_query_list

        queries = parse_queries()
        self.assertEqual(queries, parse_query_list())
        self.assertEqual(len(queries), len(set(queries)))
        for query in queries:
            self.assertTrue(query.strip())
            self.assertNotIn(" ", query)
            self.assertNotIn("`", query)

    def test_project_codes_are_searched_not_only_screened(self):
        # 表里一行一条 query，项目代号一律进检索。
        queries = parse_queries()
        for code in ("dsDNA", "Ro52", "gp210", "sp100", "ZnT8", "PLA2R", "MDA5",
                     "AMA-M2", "Dsg", "BP180", "IL-", "TNF", "IFN",
                     "CENP-B", "Scl", "Jo-1", "C1q", "RA33", "CCP"):
            self.assertIn(code, queries)

    def test_only_the_two_tables_feed_the_list(self):
        # 两张表以外的词——通用「试剂」词、方法学、仪器、甲状腺——都不在清单里。
        queries = parse_queries()
        for banned in ("试剂", "检测试剂", "免疫试剂", "检验科试剂", "检验试剂",
                       "体外诊断试剂", "抗体检测试剂", "生化免疫",
                       "酶免", "酶联免疫", "荧光免疫", "免疫荧光", "微流控",
                       "标本前处理", "标本后处理", "化学发光", "免疫分析仪",
                       "甲状腺球蛋白", "Anti-TPO"):
            self.assertNotIn(banned, queries)


class CredentialTests(unittest.TestCase):
    def test_missing_credentials_raise_auth_error(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(JrbxAuthError):
                load_credentials({"JRBX_USER_ID": "U1"}, path=Path(root) / "absent.json")

    def test_environment_wins_over_the_config_file(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "jrbx.json"
            write_credentials_file({"userId": "F1", "token": "F2", "openid": "F3"}, path)
            env = {"JRBX_USER_ID": "E1", "JRBX_TOKEN": "E2", "JRBX_OPENID": "E3"}
            self.assertEqual(
                load_credentials(env, path=path),
                {"userId": "E1", "token": "E2", "openid": "E3"},
            )

    def test_config_file_is_used_when_environment_is_empty(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "jrbx.json"
            written = write_credentials_file({"userId": "F1", "token": "F2", "openid": "F3"}, path)
            self.assertEqual(written, path)
            self.assertEqual(
                load_credentials({}, path=path),
                {"userId": "F1", "token": "F2", "openid": "F3"},
            )

    def test_partial_config_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "jrbx.json"
            path.write_text('{"userId": "F1", "token": ""}', encoding="utf-8")
            with self.assertRaises(JrbxAuthError):
                load_credentials({}, path=path)

    def test_user_info_blob_is_dug_out_of_any_nesting(self):
        # 浏览器里 USER_INFO#1 在不同版本里可能多包一层 data / userInfo
        blob = json.dumps({"code": "00", "data": {"userInfo": {
            "userId": "U1", "accessToken": "T1", "openid": "O1", "nickName": "张三"}}})
        self.assertEqual(
            credentials_from_user_info(blob),
            {"userId": "U1", "token": "T1", "openid": "O1"},
        )
        self.assertEqual(
            credentials_from_user_info('{"userId":"U1","token":"T1","openid":"O1"}'),
            {"userId": "U1", "token": "T1", "openid": "O1"},
        )

    def test_user_info_without_token_is_rejected(self):
        with self.assertRaises(JrbxAuthError):
            credentials_from_user_info('{"nickName": "张三"}')
        with self.assertRaises(JrbxAuthError):
            credentials_from_user_info("not json")

    def test_written_file_never_carries_the_credentials_into_the_repo(self):
        # config/jrbx.json 必须处在 .gitignore 覆盖内
        ignored = subprocess.run(
            ["git", "check-ignore", "config/jrbx.json"],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertEqual(ignored.returncode, 0, "config/jrbx.json 未被 .gitignore 覆盖")

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
    """passes_prefilter 返回 screen_domain 的结果 dict，不是 bool。"""

    def test_target_category_passes(self):
        self.assertTrue(passes_prefilter(sample_item())["keep"])

    def test_excluded_category_in_title_is_rejected(self):
        item = sample_item(title="过敏原检测试剂及酶标仪采购", product="酶标仪")
        screen = passes_prefilter(item)
        self.assertFalse(screen["keep"])
        self.assertIn("酶标仪", screen["reason"])

    def test_exclude_term_only_in_product_list_keeps_candidate(self):
        """`product` 是清单，属正文域：混合包不再被里面的一台 PCR 仪带走。

        原型是 2026-09-04 实跑漏掉的甘肃省妇幼保健院第十九批（screen_domain）。
        """
        item = sample_item(
            title="2026年甘肃省妇幼保健院医疗设备及相关服务第十九批采购项目招标公告",
            product="全自动体外过敏原筛查系统及其配套试剂(二次),梯度pcr,手术放大镜",
            titleProduct="",
        )
        screen = passes_prefilter(item)
        self.assertTrue(screen["keep"])
        self.assertEqual(screen["body_exclude_term"].lower(), "pcr")

    def test_unrelated_notice_is_rejected(self):
        item = sample_item(title="办公楼物业管理服务采购", product="物业服务", titleProduct="")
        self.assertFalse(passes_prefilter(item)["keep"])


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


POOL = [
    {"userId": "USER-1", "token": "T1", "openid": "O1"},
    {"userId": "USER-2", "token": "T2", "openid": "O2"},
]


class FakeResponse:
    def __init__(self, payload):
        self._raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self, size):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RecordingTransport:
    """按「第几次请求」回放返回码，并记下每次实际用的是哪个 token。"""

    def __init__(self, codes):
        self.codes = list(codes)
        self.tokens = []

    def __call__(self, request, timeout=None):
        self.tokens.append(json.loads(request.data.decode("utf-8"))["token"])
        position = len(self.tokens) - 1
        code = self.codes[position] if position < len(self.codes) else "00"
        return FakeResponse({"code": code, "content": {"items": [], "totalPage": 0, "totalCount": 0}})


class PoolProbe:
    """--check-token 的池级探测桩：按 userId 决定该账号探测时抛什么。"""

    def __init__(self, errors=None):
        self.errors = dict(errors or {})
        self.calls = []

    def __call__(self, credentials, delay=0.0):
        self.credentials = credentials
        return self

    def search(self, *args, **kwargs):
        self.calls.append(self.credentials["userId"])
        error = self.errors.get(self.credentials["userId"])
        if error:
            raise error
        return {"items": [], "totalPage": 0, "totalCount": 0}


class CredentialPoolTests(unittest.TestCase):
    def test_accounts_array_is_read_in_order(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "jrbx.json"
            path.write_text(json.dumps({"accounts": POOL}), encoding="utf-8")
            self.assertEqual(read_credential_pool_file(path), POOL)
            self.assertEqual(load_credential_pool({}, path=path), POOL)

    def test_legacy_flat_file_still_loads_as_a_single_account(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "jrbx.json"
            path.write_text(json.dumps(POOL[0]), encoding="utf-8")
            self.assertEqual(load_credential_pool({}, path=path), [POOL[0]])

    def test_incomplete_and_duplicate_accounts_are_dropped(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "jrbx.json"
            path.write_text(json.dumps({"accounts": [
                POOL[0], {"userId": "USER-3", "token": ""}, POOL[0], POOL[1],
            ]}), encoding="utf-8")
            self.assertEqual(read_credential_pool_file(path), POOL)

    def test_environment_expresses_exactly_one_account_and_still_wins(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "jrbx.json"
            path.write_text(json.dumps({"accounts": POOL}), encoding="utf-8")
            env = {"JRBX_USER_ID": "E1", "JRBX_TOKEN": "E2", "JRBX_OPENID": "E3"}
            self.assertEqual(
                load_credential_pool(env, path=path),
                [{"userId": "E1", "token": "E2", "openid": "E3"}],
            )

    def test_set_token_appends_a_new_account_and_keeps_rotation_order(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "jrbx.json"
            write_credentials_file(POOL[0], path)
            write_credentials_file(POOL[1], path)
            self.assertEqual(read_credential_pool_file(path), POOL)

    def test_rescanning_the_same_account_replaces_it_in_place(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "jrbx.json"
            write_credentials_file(POOL[0], path)
            write_credentials_file(POOL[1], path)
            write_credentials_file({"userId": "USER-1", "token": "T1b", "openid": "O1b"}, path)
            pool = read_credential_pool_file(path)
            self.assertEqual([account["userId"] for account in pool], ["USER-1", "USER-2"])
            self.assertEqual(pool[0]["token"], "T1b")


class RotationTests(unittest.TestCase):
    """1403 实测触发即废：不重试，换号原地重发同一请求。"""

    def client(self, codes, pool=None, **kwargs):
        transport = RecordingTransport(codes)
        client = JrbxClient(pool or POOL, delay=0.0, **kwargs)
        return client, transport

    def test_rate_limit_switches_account_and_replays_the_same_request(self):
        client, transport = self.client(["1403", "00"])
        with mock.patch("jrbx_search.urlopen", transport):
            code, _ = client.post("/x", {"pageNum": 7})
        self.assertEqual(code, "00")
        # 同一个请求体，先后用两个账号各发一次：不丢步、不重跑
        self.assertEqual(transport.tokens, ["T1", "T2"])
        self.assertEqual(len(client.retired), 1)
        self.assertEqual(client.retired[0]["reason"], "rate_limited")

    def test_the_rate_limited_account_is_never_retried(self):
        client, transport = self.client(["1403", "00", "00"])
        with mock.patch("jrbx_search.urlopen", transport):
            client.post("/x", {})
            client.post("/y", {})
        self.assertEqual(transport.tokens.count("T1"), 1)
        self.assertEqual(transport.tokens, ["T1", "T2", "T2"])

    def test_exhausting_the_pool_raises_a_dedicated_rate_limit_error(self):
        client, transport = self.client(["1403", "1403"])
        with mock.patch("jrbx_search.urlopen", transport):
            with self.assertRaises(JrbxRateLimitError):
                client.post("/x", {})
        self.assertEqual(transport.tokens, ["T1", "T2"])
        self.assertEqual(len(client.retired), 2)

    def test_rate_limit_aborts_collect_instead_of_being_logged_as_one_failed_query(self):
        # collect_listings 对普通 JrbxError 是「记一笔、换下一条 query 接着打」；
        # 池空之后再打就是往枪口上撞，必须一路抛穿到 main。
        class ExhaustedClient:
            request_count = 0

            def search(self, *args, **kwargs):
                raise JrbxRateLimitError("池空")

        with self.assertRaises(JrbxRateLimitError):
            collect(
                ExhaustedClient(), ["过敏", "自身抗体"],
                datetime(2026, 9, 1), datetime(2026, 9, 3),
            )

    def test_dead_login_state_also_rotates_before_giving_up(self):
        client, transport = self.client(["05", "00"])
        with mock.patch("jrbx_search.urlopen", transport):
            code, _ = client.post("/x", {})
        self.assertEqual(code, "00")
        self.assertEqual(client.retired[0]["reason"], "auth_failed")

    def test_single_account_pool_fails_exactly_as_before(self):
        client, transport = self.client(["05"], pool=[POOL[0]])
        with mock.patch("jrbx_search.urlopen", transport):
            with self.assertRaises(JrbxAuthError):
                client.post("/x", {})
        self.assertEqual(transport.tokens, ["T1"])

    def test_retired_rows_never_carry_the_credentials(self):
        client, transport = self.client(["1403", "1403"])
        with mock.patch("jrbx_search.urlopen", transport):
            with self.assertRaises(JrbxRateLimitError):
                client.post("/x", {})
        dumped = json.dumps(client.retired, ensure_ascii=False)
        for secret in ("T1", "T2", "O1", "O2", "USER-1", "USER-2"):
            self.assertNotIn(secret, dumped)
        self.assertEqual(client.retired[0]["user_id"], mask_user_id("USER-1"))


class PacingTests(unittest.TestCase):
    """固定间隔是明确的机器指纹，而 1403 一撞账号就废——宁可慢也别撞。"""

    def test_gap_is_jittered_around_the_floor(self):
        client = JrbxClient(POOL, delay=1.2, jitter=1.8, pause_every=0)
        gaps = {round(client._next_gap(), 4) for _ in range(200)}
        self.assertTrue(all(1.2 <= gap <= 3.0 for gap in gaps))
        self.assertGreater(len(gaps), 100, "间隔没有真正抖动")

    def test_a_long_pause_lands_every_pause_every_requests(self):
        client = JrbxClient(POOL, delay=1.2, jitter=1.8, pause_every=25, pause_seconds=20.0)
        client.request_count = 25
        self.assertTrue(15.0 <= client._next_gap() <= 30.0)
        client.request_count = 26
        self.assertTrue(1.2 <= client._next_gap() <= 3.0)

    def test_the_first_request_is_never_paused(self):
        client = JrbxClient(POOL, delay=1.2, pause_every=25, pause_seconds=20.0)
        self.assertTrue(1.2 <= client._next_gap() <= 3.0)

    def test_zero_delay_disables_jitter_and_pauses_for_probes(self):
        # --check-token 只发一次最小检索，不该被节流拖住。
        client = JrbxClient(POOL, delay=0.0)
        client.request_count = 25
        self.assertEqual(client._next_gap(), 0.0)


class PoolCheckTests(unittest.TestCase):
    def test_rate_limited_account_is_reported_as_needing_a_rescan(self):
        probe = ProbeClient(error=JrbxRateLimitError("池空"))
        report = check_token(credentials_expiring_in(19), client_factory=probe)
        self.assertEqual(report["status"], "rate_limited")
        self.assertFalse(report["server_accepted"])
        self.assertEqual(CHECK_TOKEN_EXIT_CODES[report["status"]], 3)

    def test_pool_status_follows_the_healthiest_account(self):
        pool = [
            {"userId": "USER-1", "token": jwt_expiring_in(19), "openid": "O1"},
            {"userId": "USER-2", "token": jwt_expiring_in(19), "openid": "O2"},
        ]
        probe = PoolProbe({"USER-1": JrbxRateLimitError("废了")})
        report = check_credential_pool(pool, client_factory=probe)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(CHECK_TOKEN_EXIT_CODES[report["status"]], 0)
        self.assertEqual(report["account_count"], 2)
        self.assertEqual(report["usable_account_count"], 1)
        # 还能跑，但缩水的那个必须点名，否则池会悄悄耗光
        self.assertIn("rate_limited", report["message"])
        self.assertIn(mask_user_id("USER-1"), report["message"])

    def test_pool_with_no_usable_account_demands_a_rescan(self):
        pool = [
            {"userId": "USER-1", "token": jwt_expiring_in(19), "openid": "O1"},
            {"userId": "USER-2", "token": jwt_expiring_in(-1), "openid": "O2"},
        ]
        probe = PoolProbe({"USER-1": JrbxRateLimitError("废了")})
        report = check_credential_pool(pool, client_factory=probe)
        self.assertEqual(CHECK_TOKEN_EXIT_CODES[report["status"]], 3)
        self.assertEqual(report["usable_account_count"], 0)

    def test_pool_reports_masked_user_ids_only(self):
        pool = [{"userId": "USER-1", "token": jwt_expiring_in(19), "openid": "O1"}]
        report = check_credential_pool(pool, client_factory=PoolProbe())
        dumped = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("USER-1", dumped)
        self.assertNotIn("O1", dumped)


class NoticeTypeTests(unittest.TestCase):
    def test_default_notice_types_exclude_concluded_stages(self):
        # 已有结论的阶段进核实产不出可行动情报（SKILL.md「1. 检索与排队」）
        for concluded in ("20200", "20201", "20203", "20205", "20206", "20500", "20501", "20502"):
            self.assertNotIn(concluded, ACTIONABLE_NOTICE_TYPES)
        for actionable in ("20100", "20300", "20400", "20600"):
            self.assertIn(actionable, ACTIONABLE_NOTICE_TYPES)


if __name__ == "__main__":
    unittest.main()
