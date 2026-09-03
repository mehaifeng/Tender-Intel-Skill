#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""睿销（jrbx）聚合站检索适配器。

本技能唯一携带用户登录态的来源：凭证只从环境变量读取，绝不落盘、不进候选、不进日志。
回源原始公告 URL 对免费账号是每日配额（实测约10次/天），因此只花在通过目标品类
预筛的候选上；配额耗尽后退回睿销主站正文永久链接，两种链接都拼不出时才丢弃，
任何情况下不伪造链接。细节见 references/jrbx.md。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from search_common import (
    canonical_url,
    compact_text,
    extract_project_id,
    excluded_domain_term,
    target_category_signals,
    write_candidates,
)


ROOT = Path(__file__).resolve().parent.parent
REFERENCE_FILE = ROOT / "references" / "jrbx.md"
BASE_URL = "https://www.jrbx360.cn"
WEB_ORIGIN = "https://www.jrbx.com"
SEARCH_ENDPOINT = "/integrated-search/v1/search"
DETAIL_ENDPOINT = "/integrated-search/v1/verify/noticeDetail"
ORIGIN_URL_ENDPOINT = "/integrated-search/v1/verify/getNoticeOriginalUrl"
# 睿销主站的公告正文永久链接；id 与 year 两个参数缺一不可（前端 811 模块 `pp`）。
ARTICLE_URL_TEMPLATE = WEB_ORIGIN + "/article/detail?id={notice_id}&year={year}"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

# 只保留仍可行动的公告阶段；已有结论的八类在服务端就排除（references/jrbx.md）。
ACTIONABLE_NOTICE_TYPES = ["20100", "20300", "20400", "20600"]
NOTICE_TYPE_NAMES = {
    "20100": "招标", "20200": "结果", "20201": "中标", "20203": "废标",
    "20205": "终止", "20206": "入围", "20300": "预告", "20400": "变更",
    "20500": "合同验收", "20501": "合同", "20502": "验收",
    "20600": "资格预审", "20700": "其他",
}

# 需要中止全流程的登录态故障，不得当成空结果吞掉。
FATAL_CODES = {
    "05": "token 无效，需重新扫码登录",
    "06": "登录已过期，需重新扫码登录",
    "08": "账号在别处登录被顶号，需重新扫码登录",
    "40": "账号未关注公众号，网页端完成关注后重试",
}
SUCCESS_CODE = "00"
NOT_FOUND_CODE = "04"
QUOTA_CODE = "07"
RATE_LIMIT_CODE = "1403"

SHORT_TO_FULL_PROVINCE = {
    "北京": "北京市", "天津": "天津市", "上海": "上海市", "重庆": "重庆市",
    "河北": "河北省", "山西": "山西省", "辽宁": "辽宁省", "吉林": "吉林省",
    "黑龙江": "黑龙江省", "江苏": "江苏省", "浙江": "浙江省", "安徽": "安徽省",
    "福建": "福建省", "江西": "江西省", "山东": "山东省", "河南": "河南省",
    "湖北": "湖北省", "湖南": "湖南省", "广东": "广东省", "广西": "广西壮族自治区",
    "海南": "海南省", "四川": "四川省", "贵州": "贵州省", "云南": "云南省",
    "西藏": "西藏自治区", "陕西": "陕西省", "甘肃": "甘肃省", "青海": "青海省",
    "宁夏": "宁夏回族自治区", "新疆": "新疆维吾尔自治区", "内蒙古": "内蒙古自治区",
}
PROCUREMENT_METHODS = (
    "公开招标", "邀请招标", "竞争性磋商", "竞争性谈判", "询价", "单一来源",
    "框架协议", "比选", "比价", "竞价", "遴选", "询比",
)


class JrbxError(Exception):
    """可继续处理其他任务的普通失败。"""


class JrbxAuthError(JrbxError):
    """登录态失效，必须中止并报警。"""


class _TextParser(HTMLParser):
    BLOCKS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "table"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag.lower() in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(value):
    parser = _TextParser()
    try:
        parser.feed(str(value or ""))
        raw = "".join(parser.parts)
    except Exception:
        raw = re.sub(r"<[^>]+>", " ", str(value or ""))
    lines = [compact_text(unescape(line)) for line in raw.splitlines()]
    return "\n".join(line for line in lines if line)


def load_credentials(env=None):
    """凭证只从环境变量读取；不接受命令行明文，避免进入进程列表和 shell 历史。"""
    env = env if env is not None else os.environ
    missing = [name for name in ("JRBX_USER_ID", "JRBX_TOKEN", "JRBX_OPENID") if not env.get(name)]
    if missing:
        raise JrbxAuthError(
            "缺少睿销凭证环境变量：" + "、".join(missing)
            + "；取值方法见 references/jrbx.md「凭证」"
        )
    return {
        "userId": env["JRBX_USER_ID"].strip(),
        "token": env["JRBX_TOKEN"].strip(),
        "openid": env["JRBX_OPENID"].strip(),
    }


def token_expires_at(token):
    """解析 JWT 的 exp，仅用于到期预警；解析失败返回 None，不影响主流程。"""
    try:
        payload = str(token or "").split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
        return datetime.fromtimestamp(int(claims["exp"]))
    except Exception:
        return None


def check_token(credentials, warn_days=3, probe=True, client_factory=None):
    """预检凭证健康度，不做检索也不消耗回源配额。

    只解析 JWT 的 exp 无法发现「被别处重新扫码顶掉」的 token（返回码 08），
    因此默认再打一次最小检索（pageSize=1）验证服务端是否仍然接受该凭证。
    """
    now = datetime.now()
    expires_at = token_expires_at(credentials["token"])
    report = {
        "user_id": credentials["userId"],
        "token_expires_at": expires_at.isoformat(timespec="seconds") if expires_at else None,
        "days_remaining": None,
        "warn_days": warn_days,
        "expired": False,
        "expiring_soon": False,
        "server_accepted": None,
        "status": "ok",
        "message": "",
    }
    if expires_at:
        remaining = expires_at - now
        report["days_remaining"] = round(remaining.total_seconds() / 86400, 2)
        report["expired"] = remaining.total_seconds() <= 0
        report["expiring_soon"] = 0 < remaining.total_seconds() <= warn_days * 86400
    if report["expired"]:
        report["status"] = "expired"
        report["message"] = f"token 已于 {expires_at:%Y-%m-%d %H:%M} 过期，需重新扫码登录"
        return report

    if probe:
        window_end = now
        window_start = now - timedelta(hours=1)
        client = (client_factory or JrbxClient)(credentials, delay=0.0)
        try:
            client.search(["试剂"], window_start, window_end, 1, 1, ACTIONABLE_NOTICE_TYPES)
            report["server_accepted"] = True
        except JrbxAuthError as exc:
            report["server_accepted"] = False
            report["status"] = "rejected"
            report["message"] = str(exc)
            return report
        except JrbxError as exc:
            # 网络或接口异常不等于凭证失效，单独标注，不误报为需要重新扫码。
            report["status"] = "probe_failed"
            report["message"] = f"凭证有效期正常，但探测请求失败：{exc}"
            return report

    if report["expiring_soon"]:
        report["status"] = "expiring_soon"
        report["message"] = (
            f"token 将于 {expires_at:%Y-%m-%d %H:%M} 过期"
            f"（剩余 {report['days_remaining']} 天），请重新扫码换发"
        )
    elif expires_at:
        report["message"] = f"token 正常，{report['days_remaining']} 天后到期"
    else:
        report["status"] = "unknown_expiry"
        report["message"] = "无法解析 token 有效期，但服务端接受该凭证"
    return report


# --check-token 的退出码：0 正常 / 3 需重新扫码 / 4 即将到期 / 2 探测失败
CHECK_TOKEN_EXIT_CODES = {
    "ok": 0,
    "unknown_expiry": 0,
    "expiring_soon": 4,
    "expired": 3,
    "rejected": 3,
    "probe_failed": 2,
}


def parse_queries():
    text = REFERENCE_FILE.read_text(encoding="utf-8")
    marker = "## 默认 Query"
    if marker not in text:
        raise JrbxError(f"{REFERENCE_FILE} 缺少“{marker}”")
    section = text.split(marker, 1)[1].split("\n## ", 1)[0]
    queries = [query.strip() for _, query in re.findall(r"^(\d+)\.\s+(\S.*?)\s*$", section, re.M)]
    if not queries:
        raise JrbxError("睿销默认 Query 清单为空")
    return queries


def split_terms(query):
    """`过敏原+试剂` -> ['过敏原', '试剂']，对应睿销 keywords 的 AND 语义。"""
    return [term.strip() for term in str(query or "").split("+") if term.strip()]


def parse_time_range(value, now=None):
    value = str(value or "72h").strip()
    now = now or datetime.now().replace(microsecond=0)
    match = re.fullmatch(r"(\d+)h", value)
    if match:
        if int(match.group(1)) < 1:
            raise JrbxError("--time-range 小时数必须大于0")
        return now - timedelta(hours=int(match.group(1))), now
    match = re.fullmatch(r"(\d+)d", value)
    if match:
        if int(match.group(1)) < 1:
            raise JrbxError("--time-range 天数必须大于0")
        return now - timedelta(days=int(match.group(1))), now
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})", value)
    if match:
        start = datetime.fromisoformat(match.group(1))
        end = datetime.fromisoformat(match.group(2)) + timedelta(days=1) - timedelta(seconds=1)
        if start > end:
            raise JrbxError(f"时间范围起点晚于终点：{value}")
        return start, end
    raise JrbxError("--time-range 支持 72h、3d 或 YYYY-MM-DD..YYYY-MM-DD")


def to_millis(moment):
    return int(moment.timestamp() * 1000)


def from_millis(value):
    try:
        value = int(value or 0)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(value / 1000) if value > 0 else None


class JrbxClient:
    def __init__(self, credentials, delay=1.2, timeout=30, max_bytes=20 * 1024 * 1024):
        self.credentials = credentials
        self.delay = max(0.0, float(delay))
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.last_request_at = 0.0
        self.request_count = 0

    def post(self, path, body, retry_on_rate_limit=2):
        wait = self.delay - (time.monotonic() - self.last_request_at)
        if wait > 0:
            time.sleep(wait)
        payload = dict(body)
        payload.update(self.credentials)
        request = Request(
            BASE_URL + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/json",
                "Accept": "application/json,text/plain,*/*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Origin": WEB_ORIGIN,
                "Referer": WEB_ORIGIN + "/business/search",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read(self.max_bytes + 1)
                if len(raw) > self.max_bytes:
                    raise JrbxError(f"响应超过{self.max_bytes // 1024 // 1024}MB：{path}")
                document = json.loads(raw.decode("utf-8", errors="replace"))
        except HTTPError as exc:
            raise JrbxError(f"HTTP {exc.code}: {path}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise JrbxError(f"网络错误：{exc}") from exc
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise JrbxError(f"返回内容不是有效JSON：{exc}") from exc
        finally:
            self.last_request_at = time.monotonic()
            self.request_count += 1

        code = str(document.get("code"))
        if code in FATAL_CODES:
            raise JrbxAuthError(f"睿销登录态失效（code={code}）：{FATAL_CODES[code]}")
        if code == RATE_LIMIT_CODE and retry_on_rate_limit > 0:
            time.sleep(max(3.0, self.delay * 3))
            return self.post(path, body, retry_on_rate_limit - 1)
        return code, document.get("content")

    def search(self, terms, start, end, page, page_size, notice_types):
        body = {
            "keywords": list(terms),
            "timeType": "custom",
            "startTime": to_millis(start),
            "endTime": to_millis(end),
            "pageNum": page,
            "pageSize": page_size,
        }
        if notice_types:
            body["noticeTypes"] = list(notice_types)
        code, content = self.post(SEARCH_ENDPOINT, body)
        if code != SUCCESS_CODE or not isinstance(content, dict):
            raise JrbxError(f"列表接口异常：code={code!r}")
        return content

    def notice_detail(self, notice_id):
        # 只传 noticeId：额外传 year 会返回 04（references/jrbx.md）。
        code, content = self.post(DETAIL_ENDPOINT, {"noticeId": notice_id})
        if code == NOT_FOUND_CODE:
            return None
        if code != SUCCESS_CODE or not isinstance(content, dict):
            raise JrbxError(f"详情接口异常：code={code!r} noticeId={notice_id}")
        return content

    def original_url(self, notice_id):
        """返回 (url, quota_exhausted)；配额耗尽时由调用方停止后续回源。"""
        code, content = self.post(ORIGIN_URL_ENDPOINT, {"noticeId": notice_id})
        if code == QUOTA_CODE:
            return "", True
        if code != SUCCESS_CODE or not isinstance(content, str) or not content.strip():
            return "", False
        return content.strip(), False


def matched_terms(text, terms):
    lowered = (text or "").lower()
    return [term for term in terms if term and term.lower() in lowered]


# 注意不能把「州」整体当后缀：温州、亳州、杭州都是地级市，名字自带「州」，
# 只有「自治州」才是真正的行政区后缀。
DIVISION_SUFFIXES = ("市", "自治州", "地区", "盟", "区", "县", "旗")


def with_division_suffix(name):
    """睿销的 city/county 有时省掉行政区后缀（如「南昌」「温州」），补「市」；
    自治州、地区、盟等已带后缀的原样保留，避免造出「黔西南布依族苗族自治州市」。"""
    name = compact_text(name)
    if not name or name.endswith(DIVISION_SUFFIXES):
        return name
    return name + "市"


def region_fields(item):
    province = compact_text(item.get("province"))
    city = with_division_suffix(item.get("city"))
    county = compact_text(item.get("county"))
    if province in {"", "全国"}:
        return "", ""
    full = SHORT_TO_FULL_PROVINCE.get(province, province)
    return full + city + county, province


def normalize_budget(value):
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return ""
    if number <= 0:
        return ""
    return str(int(number)) if float(number).is_integer() else f"{number:.2f}".rstrip("0").rstrip(".")


def pick_deadline(item):
    """投标/响应截止，不取报名与文件获取截止（schema.md 明确区分）。"""
    for key, label in (
        ("bidDeadline", "投标截止时间"),
        ("deliverBidDocDeadline", "响应文件提交截止时间"),
        ("quoteDeadline", "报价截止时间"),
    ):
        moment = from_millis(item.get(key))
        if moment:
            return moment.strftime("%Y-%m-%dT%H:%M"), f"睿销结构化字段 {key}（{label}）"
    return "", ""


def extract_procurement_method(title, bid_type, text):
    bid_type = compact_text(bid_type)
    if bid_type in PROCUREMENT_METHODS:
        return bid_type, f"睿销结构化字段 bidType：{bid_type}"
    for method in PROCUREMENT_METHODS:
        if method in (title or ""):
            return method, f"标题出现：{method}"
    match = re.search(
        r"(?:采购方式|招标方式)\s*[：:]\s*(" + "|".join(PROCUREMENT_METHODS) + r")", text or "", re.I
    )
    return (match.group(1), compact_text(match.group(0))) if match else ("", "")


def extract_labeled_field(text, labels, limit=120):
    pattern = "|".join(re.escape(label) for label in labels)
    match = re.search(rf"(?:{pattern})\s*[：:]\s*([^\n；;]{{2,{limit}}})", text or "", re.I)
    return (compact_text(match.group(1)), compact_text(match.group(0))) if match else ("", "")


def normalize_attachments(value):
    rows = []
    for entry in value if isinstance(value, list) else []:
        if not isinstance(entry, dict):
            continue
        url = compact_text(entry.get("originUrl") or entry.get("url"))
        if not url.lower().startswith(("http://", "https://")):
            continue
        row = {"url": canonical_url(url)}
        name = compact_text(entry.get("name"))
        if name:
            row["name"] = name
        if row not in rows:
            rows.append(row)
    return rows


def passes_prefilter(item, detail=None):
    """目标品类预筛：硬排除优先，其次要求至少一个目标品类信号。"""
    detail = detail or {}
    text = "\n".join((
        compact_text(item.get("title")),
        compact_text(item.get("product")),
        compact_text(item.get("titleProduct")),
        html_to_text(detail.get("simpleContent")),
        html_to_text(detail.get("content")),
    ))
    if excluded_domain_term(text):
        return False
    return bool(target_category_signals(text))


def article_url(item, detail=None):
    """睿销主站正文永久链接，回源配额耗尽时作为 source_url 兜底。"""
    detail = detail or {}
    notice_id = compact_text(item.get("id") or detail.get("id"))
    year = compact_text(item.get("year") or detail.get("year"))
    if not notice_id or not year:
        return ""
    return ARTICLE_URL_TEMPLATE.format(notice_id=notice_id, year=year)


def build_candidate(item, detail, origin_url, hits, all_terms):
    """把睿销列表项 + 详情正文映射为统一候选契约。

    `source_url` 优先用回源到原始站点的链接：它匿名可访问，且能与 CCGP/PLAP 在
    规范 URL 层直接命中同一身份键。回源配额耗尽时退回睿销主站正文永久链接——
    该链接需登录睿销才能打开，因此这类候选的 `source_priority` 再降一档。
    """
    detail = detail or {}
    title = compact_text(item.get("title") or detail.get("title"))
    content = html_to_text(detail.get("content"))
    summary = html_to_text(detail.get("simpleContent")) or compact_text(content, 2000)
    search_text = "\n".join((title, compact_text(item.get("product")), summary, content))

    source_hits = list(hits)
    for term in matched_terms(search_text, all_terms):
        hit = {"source": "jrbx", "query": term, "query_mode": "local_content_filter"}
        if hit not in source_hits:
            source_hits.append(hit)

    fields = {}
    evidence = {}
    published = from_millis(item.get("publishTime"))
    publish_time = published.strftime("%Y-%m-%d") if published else ""
    if publish_time:
        fields["发布时间"] = publish_time
        evidence["发布时间"] = f"睿销结构化字段 publishTime：{published.isoformat(timespec='minutes')}"

    organization = compact_text(item.get("organization") or detail.get("organization"))
    if organization:
        fields["单位"] = organization
        evidence["单位"] = f"睿销结构化字段 organization：{organization}"
    else:
        unit, unit_evidence = extract_labeled_field(
            search_text, ("采购人", "采购单位", "招标人", "采购人名称")
        )
        if unit:
            fields["单位"] = unit
            evidence["单位"] = unit_evidence

    region, province = region_fields(item)
    if region:
        fields["地区"] = region
        evidence["地区"] = f"睿销结构化字段 province/city/county：{region}"
    if province:
        fields["所属省/市"] = province
        evidence["所属省/市"] = f"睿销结构化字段 province：{province}"

    budget = normalize_budget(item.get("budget") or detail.get("budget"))
    if budget:
        fields["预算"] = budget
        evidence["预算"] = f"睿销结构化字段 budget：{budget}"

    deadline, deadline_evidence = pick_deadline({**item, **detail})
    if deadline:
        fields["截止时间"] = deadline
        evidence["截止时间"] = deadline_evidence

    method, method_evidence = extract_procurement_method(
        title, item.get("bidType") or detail.get("bidType"), search_text
    )
    if method:
        fields["采购方式"] = method
        evidence["采购方式"] = method_evidence

    notice_type = compact_text(item.get("noticeType")) or NOTICE_TYPE_NAMES.get(
        compact_text(item.get("noticeTypeCode")), ""
    )
    if notice_type:
        fields["公告类型"] = notice_type
        evidence["公告类型"] = f"睿销公告类型码 {item.get('noticeTypeCode')}：{notice_type}"

    project_id = extract_project_id(search_text)
    if project_id:
        fields["项目编号"] = project_id
        evidence["项目编号"] = f"正文项目编号：{project_id}"

    if origin_url:
        url = canonical_url(origin_url)
        site_name = urlsplit(url).netloc
        auth_info_des = "商业聚合库转载，链接已回源原始站点"
        # 低于 CCGP/PLAP 官方一手（400），高于无归属的泛搜结果（0）。
        source_priority = 300
        link_kind = "origin"
    else:
        # canonical_url("") 会返回 "/"，直接用会让缺 id/year 的候选带着假链接进队列。
        raw_article_url = article_url(item, detail)
        url = canonical_url(raw_article_url) if raw_article_url else ""
        site_name = "睿销"
        auth_info_des = "商业聚合库正文永久链接，需登录睿销账号打开"
        # 再降一档：同一公告若同时有回源链接版本，合并时优先保留可匿名访问的那条。
        source_priority = 250
        link_kind = "jrbx_article"
    return {
        "title": title,
        "site_name": site_name,
        "url": url,
        "publish_time": publish_time,
        "auth_info_level": 3,
        "auth_info_des": auth_info_des,
        "link_kind": link_kind,
        "rank_score": item.get("score"),
        "summary": summary,
        "content": content,
        "source_fields": fields,
        "field_evidence": evidence,
        "attachments": normalize_attachments(detail.get("attachments")),
        "found_by_query": [],
        "found_by_source_query": source_hits,
        "source": "jrbx",
        "sources": ["jrbx"],
        "source_priority": source_priority,
        "date_authoritative": True,
        "retrieval_verified": bool(content),
        "content_access": "public_full" if content else "metadata_only",
    }


def collect_listings(client, queries, start, end, page_size, max_pages_per_query, notice_types):
    """第一阶段：只跑列表接口（不计配额），按公告去重并累计 query 归因。"""
    by_notice = {}
    failures = []
    raw_count = 0
    for query in queries:
        terms = split_terms(query)
        if not terms:
            continue
        page = 1
        total_pages = 1
        while page <= total_pages and page <= max_pages_per_query:
            try:
                content = client.search(terms, start, end, page, page_size, notice_types)
            except JrbxAuthError:
                raise
            except JrbxError as exc:
                failures.append({"query": query, "page": page, "error": str(exc)})
                break
            items = content.get("items") or []
            total_pages = int(content.get("totalPage") or 0)
            raw_count += len(items)
            for item in items:
                if not isinstance(item, dict):
                    continue
                notice_id = compact_text(item.get("id"))
                if not notice_id:
                    continue
                slot = by_notice.setdefault(notice_id, {"item": item, "hits": []})
                hit = {"source": "jrbx", "query": query, "query_mode": "keywords_and"}
                if hit not in slot["hits"]:
                    slot["hits"].append(hit)
            if not items:
                break
            page += 1
    return by_notice, failures, raw_count


def collect(client, queries, start, end, page_size=100, max_pages_per_query=20,
            notice_types=None, max_origin_lookups=10):
    """两阶段：列表+正文不限量取，回源 URL 只花在预筛幸存者上。"""
    notice_types = notice_types if notice_types is not None else ACTIONABLE_NOTICE_TYPES
    by_notice, failures, raw_count = collect_listings(
        client, queries, start, end, page_size, max_pages_per_query, notice_types
    )

    all_terms = sorted({term for query in queries for term in split_terms(query)})
    # 先用列表元数据粗筛，再取正文复筛：正文不计配额，但省下的请求同样降低被限频的概率。
    shortlisted = [
        (notice_id, slot) for notice_id, slot in by_notice.items()
        if passes_prefilter(slot["item"])
    ]
    shortlisted.sort(key=lambda row: int(row[1]["item"].get("publishTime") or 0), reverse=True)
    prefilter_excluded = len(by_notice) - len(shortlisted)

    detailed = []
    for notice_id, slot in shortlisted:
        try:
            detail = client.notice_detail(notice_id)
        except JrbxAuthError:
            raise
        except JrbxError as exc:
            failures.append({"notice_id": notice_id, "stage": "detail", "error": str(exc)})
            continue
        if detail is None or not passes_prefilter(slot["item"], detail):
            prefilter_excluded += 1
            continue
        detailed.append((notice_id, slot, detail))

    candidates = []
    dropped_no_url = 0
    fallback_article_url = 0
    quota_exhausted = False
    origin_lookups = 0
    for notice_id, slot, detail in detailed:
        origin_url = ""
        # 配额耗尽或预算用完后不再空烧请求，直接走主站正文永久链接兜底。
        if not quota_exhausted and origin_lookups < max_origin_lookups:
            try:
                origin_url, quota_exhausted = client.original_url(notice_id)
                origin_lookups += 1
            except JrbxAuthError:
                raise
            except JrbxError as exc:
                failures.append({"notice_id": notice_id, "stage": "origin_url", "error": str(exc)})
        candidate = build_candidate(slot["item"], detail, origin_url, slot["hits"], all_terms)
        if not candidate or not candidate["url"]:
            # 连 id/year 都缺，拼不出永久链接：没有任何可用 source_url 才丢弃。
            dropped_no_url += 1
            continue
        if candidate["link_kind"] == "jrbx_article":
            fallback_article_url += 1
        candidates.append(candidate)

    stats = {
        "raw_result_count": raw_count,
        "unique_notice_count": len(by_notice),
        "prefilter_excluded": prefilter_excluded,
        "detail_fetched": len(detailed),
        "origin_lookups": origin_lookups,
        "origin_quota_exhausted": quota_exhausted,
        "origin_url_count": len(candidates) - fallback_article_url,
        "fallback_article_url_count": fallback_article_url,
        "dropped_no_url": dropped_no_url,
    }
    return candidates, failures, stats


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="睿销（jrbx）聚合站检索适配器")
    parser.add_argument("--query", help="单个即席 Query；`A+B` 表示 AND")
    parser.add_argument("--queries", help="逗号分隔的 Query；默认读取 references/jrbx.md")
    parser.add_argument("--time-range", default="72h", help="72h / 3d / YYYY-MM-DD..YYYY-MM-DD")
    parser.add_argument("--out-dir", help="输出目录；默认 .tmp/search/<日期>/.sources/jrbx")
    parser.add_argument("--delay", type=float, default=1.2, help="请求间隔秒数，默认1.2（有频控）")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-pages-per-query", type=int, default=20)
    parser.add_argument(
        "--max-origin-lookups", type=int, default=10,
        help="回源URL调用上限；免费账号实测约10次/天，超出一律返回07",
    )
    parser.add_argument(
        "--notice-types", default=",".join(ACTIONABLE_NOTICE_TYPES),
        help="公告类型码，逗号分隔；留空表示不做服务端类型过滤",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--check-token", action="store_true",
        help="只预检凭证健康度并退出：0 正常 / 4 即将到期 / 3 需重新扫码 / 2 探测失败",
    )
    parser.add_argument("--warn-days", type=int, default=3, help="--check-token 的到期告警阈值")
    parser.add_argument(
        "--offline", action="store_true",
        help="--check-token 时只解析 JWT 有效期，不发探测请求（查不出被顶号）",
    )
    args = parser.parse_args()

    try:
        if args.check_token:
            report = check_token(
                load_credentials(), warn_days=args.warn_days, probe=not args.offline
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            exit_code = CHECK_TOKEN_EXIT_CODES.get(report["status"], 2)
            if exit_code:
                print(f"睿销凭证预检：{report['message']}", file=sys.stderr)
            return exit_code

        start, end = parse_time_range(args.time_range)
        if args.query:
            queries = [args.query.strip()]
        elif args.queries:
            queries = [item.strip() for item in args.queries.split(",") if item.strip()]
        else:
            queries = parse_queries()
        if not queries:
            raise JrbxError("检索 Query 为空")
        if not 1 <= args.page_size <= 100:
            raise JrbxError("--page-size 必须在1到100之间")
        if args.max_pages_per_query < 1:
            raise JrbxError("--max-pages-per-query 必须大于0")
        if args.max_origin_lookups < 0:
            raise JrbxError("--max-origin-lookups 不能为负")
        notice_types = [code.strip() for code in args.notice_types.split(",") if code.strip()]

        if args.dry_run:
            # 干跑不读凭证也不发请求，便于在没有 token 的环境校验配置。
            print(json.dumps({
                "source": "jrbx",
                "queries": queries,
                "query_count": len(queries),
                "terms_per_query": {query: split_terms(query) for query in queries},
                "notice_types": [
                    {"code": code, "name": NOTICE_TYPE_NAMES.get(code, "?")} for code in notice_types
                ],
                "start": start.isoformat(timespec="seconds"),
                "end": end.isoformat(timespec="seconds"),
                "max_origin_lookups": args.max_origin_lookups,
                "authentication": "user_token_from_env",
                "credentials_present": all(
                    os.environ.get(name) for name in ("JRBX_USER_ID", "JRBX_TOKEN", "JRBX_OPENID")
                ),
            }, ensure_ascii=False, indent=2))
            return 0

        credentials = load_credentials()
        expires_at = token_expires_at(credentials["token"])
        if expires_at:
            remaining = expires_at - datetime.now()
            if remaining.total_seconds() <= 0:
                raise JrbxAuthError(f"睿销 token 已于 {expires_at:%Y-%m-%d %H:%M} 过期，需重新扫码登录")
            if remaining.days <= 3:
                print(
                    f"警告：睿销 token 将于 {expires_at:%Y-%m-%d %H:%M} 过期"
                    f"（剩余 {remaining.days} 天），请及时重新扫码",
                    file=sys.stderr,
                )

        out_dir = (
            Path(args.out_dir) if args.out_dir
            else ROOT / ".tmp" / "search" / date.today().isoformat() / ".sources" / "jrbx"
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        started = time.time()
        client = JrbxClient(credentials, delay=args.delay)
        candidates, failures, stats = collect(
            client, queries, start, end,
            page_size=args.page_size,
            max_pages_per_query=args.max_pages_per_query,
            notice_types=notice_types,
            max_origin_lookups=args.max_origin_lookups,
        )
        index = write_candidates(candidates, out_dir, date.today().isoformat())
        summary = {
            "schema_version": 1,
            "source": "jrbx",
            "run_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "time_range": f"{start.isoformat(timespec='seconds')}..{end.isoformat(timespec='seconds')}",
            "query_count": len(queries),
            "query_failed": len(failures),
            "notice_types": notice_types,
            "request_count": client.request_count,
            "candidate_count": len(index),
            "failures": failures,
        }
        summary.update(stats)
        if expires_at:
            summary["token_expires_at"] = expires_at.isoformat(timespec="seconds")
        (out_dir / "search_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(
            f"睿销：{len(queries)} 条 Query，原始 {stats['raw_result_count']} 条，"
            f"去重 {stats['unique_notice_count']} 条，预筛排除 {stats['prefilter_excluded']} 条，"
            f"取正文 {stats['detail_fetched']} 条，回源 {stats['origin_lookups']} 次，"
            f"最终 {len(index)} 条（回源链接 {stats['origin_url_count']} 条，"
            f"主站正文链接 {stats['fallback_article_url_count']} 条），"
            f"无可用链接丢弃 {stats['dropped_no_url']} 条，"
            f"失败 {len(failures)} 项，耗时 {time.time() - started:.1f}s"
        )
        if stats["fallback_article_url_count"]:
            print(
                f"提示：{stats['fallback_article_url_count']} 条候选使用睿销主站正文链接"
                f"（回源配额已耗尽或超出 --max-origin-lookups）；这些链接需登录睿销账号才能打开",
                file=sys.stderr,
            )
        print(f"落盘：{out_dir}")
        return 0 if candidates or not failures else 2
    except JrbxAuthError as exc:
        # 登录态问题必须以独立退出码暴露，不能被当成“今天没有新公告”。
        print(f"睿销登录态错误：{exc}", file=sys.stderr)
        return 3
    except JrbxError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
