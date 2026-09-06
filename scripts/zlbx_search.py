#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""知了标讯检索适配器：唯一信源，产出统一候选目录。

接口约束、实测行为与计费口径见 references/zlbx.md。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from tender_identity import IdentityIndex
from tender_ledger import read_ledger, LedgerError

from search_common import (
    compact_text,
    screen_domain,
    write_candidates,
)


ROOT = Path(__file__).resolve().parent.parent
REFERENCE_FILE = ROOT / "references" / "keywords.md"
CONFIG_FILE = ROOT / "config" / "zlbx.json"
# 每词每天命中数，由适配器自己维护，用于装箱降低调用次数（见 plan_batches）。
HIT_COUNTS_FILE = ROOT / "data" / "query_hits.json"
BASE_URL = "https://mcp-server.zhiliaobiaoxun.com/api_v2"

# 可行动阶段：采购意向 / 预招标 / 招标 / 变更公告。
# 服务端直接滤掉中标、合同、废标等已有结论的公告，省掉取回本地再丢弃的开销。
DEFAULT_BID_PROCESS = [1, 2, 4, 5]
# 实测上限 50，传 100 返回 INVALID_PARAMETER。
MAX_PAGE_SIZE = 50
# 默认匹配模式不是全文（默认/all/sm 三者等价），必须显式传 fulltext，
# 否则召回掉一个数量级（2026-09-05 实测 `过敏` 8 vs 116）。
MATCH_MODES = ["fulltext"]

NETWORK_RETRIES = 2


class ZlbxError(Exception):
    """适配器可预期的失败。"""


class ZlbxAuthError(ZlbxError):
    """凭证缺失或被拒；退出码 3，必须与“今天没有新公告”区分开。"""


class ZlbxNetworkError(ZlbxError):
    """网络层瞬时故障，可退避重试。"""


def load_api_key():
    """环境变量优先，其次 config/zlbx.json。命令行始终不接受明文。"""
    key = (os.environ.get("ZLBX_API_KEY") or "").strip()
    if key:
        return key
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ZlbxAuthError(f"{CONFIG_FILE} 不是合法 JSON：{exc}") from exc
        key = str(data.get("api_key") or "").strip()
        if key:
            return key
    raise ZlbxAuthError(
        "缺少知了标讯 API Key：设置环境变量 ZLBX_API_KEY，"
        f"或在 {CONFIG_FILE} 写 {{\"api_key\": \"...\"}}（权限 0600，勿提交仓库）"
    )


def mask_key(key):
    """日志与摘要里只留前缀，Key 不进任何落盘文件。"""
    key = str(key or "")
    return f"{key[:9]}…" if len(key) > 9 else "…"


def parse_queries():
    """从 keywords.md 读检索清单；本适配器不另存副本。"""
    text = REFERENCE_FILE.read_text(encoding="utf-8")
    marker = "## 检索 Query 清单"
    if marker not in text:
        raise ZlbxError(f"{REFERENCE_FILE} 缺少“{marker}”")
    section = text.split(marker, 1)[1].split("\n## ", 1)[0]
    queries = [query.strip() for _, query in re.findall(r"^(\d+)\.\s+(\S.*?)\s*$", section, re.M)]
    if not queries:
        raise ZlbxError("检索 Query 清单为空")
    return queries


def parse_time_range(value, now=None):
    now = now or datetime.now().replace(microsecond=0)
    value = str(value or "72h").strip()
    match = re.fullmatch(r"(\d+)h", value)
    if match:
        if int(match.group(1)) < 1:
            raise ZlbxError("--time-range 小时数必须大于0")
        return now - timedelta(hours=int(match.group(1))), now
    match = re.fullmatch(r"(\d+)d", value)
    if match:
        if int(match.group(1)) < 1:
            raise ZlbxError("--time-range 天数必须大于0")
        return now - timedelta(days=int(match.group(1))), now
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})", value)
    if match:
        start = datetime.fromisoformat(match.group(1))
        end = datetime.fromisoformat(match.group(2)) + timedelta(days=1) - timedelta(seconds=1)
        if start > end:
            raise ZlbxError(f"时间范围起点晚于终点：{value}")
        return start, end
    raise ZlbxError(f"无法解析 --time-range：{value}；用 72h、3d 或 YYYY-MM-DD..YYYY-MM-DD")


class ZlbxClient:
    """最小 HTTP 客户端；累计 cost_units，Key 只在 Header 里出现。"""

    def __init__(self, api_key, delay=0.25, timeout=60):
        self._api_key = api_key
        self.delay = delay
        self.timeout = timeout
        self.request_count = 0
        self.cost_units = 0.0

    def call(self, tool, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{BASE_URL}/{tool}",
            data=body,
            headers={"X-API-Key": self._api_key, "Content-Type": "application/json"},
            method="POST",
        )
        last_error = None
        for attempt in range(NETWORK_RETRIES + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    out = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:300]
                if exc.code in (401, 403):
                    raise ZlbxAuthError(f"知了标讯拒绝该 API Key（HTTP {exc.code}）：{detail}") from exc
                if exc.code == 429 or exc.code >= 500:
                    last_error = ZlbxNetworkError(f"HTTP {exc.code}：{detail}")
                else:
                    raise ZlbxError(f"HTTP {exc.code}：{detail}") from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = ZlbxNetworkError(repr(exc))
            if attempt < NETWORK_RETRIES:
                time.sleep(4 * (attempt + 1))
        else:
            raise last_error
        self.request_count += 1
        time.sleep(self.delay)

        meta = out.get("meta") or {}
        self.cost_units += float(meta.get("cost_units") or 0)
        error = out.get("error")
        if error:
            code = (error or {}).get("code") if isinstance(error, dict) else ""
            message = (error or {}).get("message") if isinstance(error, dict) else str(error)
            if str(code).upper() in {"UNAUTHORIZED", "INVALID_API_KEY", "FORBIDDEN"}:
                raise ZlbxAuthError(f"{code}: {message}")
            if str(code).upper() == "INSUFFICIENT_BALANCE":
                raise ZlbxAuthError(f"账户积分不足：{message}")
            raise ZlbxError(f"{code}: {message}")
        return out.get("data") or {}


def _clean(value):
    """接口偶发把 HTML 片段带进结构化字段（实测 bid_no 出现过 `</span>…`）。"""
    text = re.sub(r"<[^>]*>", "", str(value or ""))
    return compact_text(text)


# 接口的 city 多数是不带后缀的裸名（`滁州`、`北京`），补后缀时**不能只看末字**：
# `滁州`/`广州`/`苏州` 的「州」是名字的一部分，仍要补「市」；`凉山彝族自治州`、
# `阿拉善盟`、`大兴安岭地区` 才是已经带了行政区后缀。
_CITY_SUFFIX_RE = re.compile(r"(?:市|盟|地区|自治州|林区|特区)$")


def _locality(item):
    """地区只给“市+区县”，省级全称由管线的 normalize_region_location 补。"""
    province = _clean(item.get("province"))
    city = _clean(item.get("city"))
    county = _clean(item.get("county"))
    parts = []
    if city and city != province:
        parts.append(city if _CITY_SUFFIX_RE.search(city) else f"{city}市")
    if county:
        parts.append(county)
    return "".join(parts)


def _budget(item):
    """money 单位是元（money_wan 是同值的万元表示），不做换算。"""
    money = item.get("money")
    try:
        value = float(money)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    return str(int(value)) if value == int(value) else f"{value:.2f}"


def _iso_datetime(text):
    match = re.search(
        r"(20\d{2})\s*[年/-]\s*([01]?\d)\s*[月/-]\s*([0-3]?\d)\s*[日号]?"
        r"(?:\s+|T)?([0-2]?\d)?\s*[:时]?\s*([0-5]\d)?",
        text,
    )
    if not match:
        return ""
    value = f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    if match.group(4) is not None and match.group(5) is not None:
        value += f"T{int(match.group(4)):02d}:{int(match.group(5)):02d}"
    return value


# 政府采购公告有两种写法。带冒号那种旧规则一直能抽：
#     投标截止时间：2026年09月16日 09时00分
# 但国办标准模板是小节标题换行给值、没有冒号，旧规则整块抽不到：
#     四、提交投标文件截止时间、开标时间和地点
#     2026年09月16日 09时00分00秒 （北京时间）
# 2026-08-27 实测：226 条正文里 117 条有截止语句，旧规则只抽到 27 条。
_DEADLINE_LABEL = (
    r"(?:提交投标文件截止时间|投标文件递交截止时间|响应文件提交截止时间"
    r"|投标截止时间|响应文件开启时间)"
)
_DEADLINE_DT = (
    r"20\d{2}\s*[-/年]\s*\d{1,2}\s*[-/月]\s*\d{1,2}\s*日?"
    # 秒位的分隔符与数字之间不允许空白：允许了就会把「…11时00分 2026年…」里的
    # 「分 20」当成秒吃掉，match.end() 越过日期边界，并排写的第二个时间就检测不到，
    # 于是更正公告返回的是那个已经作废的旧时间。填错的截止时间比留空危险。
    r"\s*(?:[^\n\d]{0,4})?[0-2]?\d\s*[:时]\s*[0-5]\d(?:[:分][0-5]\d)?\s*秒?"
)
_DEADLINE_COLON_RE = re.compile(_DEADLINE_LABEL + r"\s*[：:]\s*(" + _DEADLINE_DT + r")")
# 标签到日期之间只放非数字字符——放开数字会跨过第一个日期抓到下一个
_DEADLINE_HEADING_RE = re.compile(_DEADLINE_LABEL + r"[^\n\d]{0,24}?\s*(" + _DEADLINE_DT + r")")
_DEADLINE_DT_RE = re.compile(_DEADLINE_DT)


def _extract_deadline(full_text):
    """返回 (值, 证据)；取不到或有歧义时返回 ("", "")。"""
    match = _DEADLINE_COLON_RE.search(full_text)
    if not match:
        match = _DEADLINE_HEADING_RE.search(full_text)
        if not match:
            return "", ""
        # 更正公告把原/现两个时间并排写：
        #     投标文件递交截止时间 2026年09月01日11时00分 2026年09月11日11时00分
        # 抓到的第一个正是作废的旧时间。填错的截止时间比留空危险，宁可不填。
        if _DEADLINE_DT_RE.search(full_text[match.end():match.end() + 80]):
            return "", ""
    raw = match.group(1).strip()
    # 飞书侧按文本消费、不依赖格式，所以 ISO 解不出来时保留原文，好过整条丢掉。
    return (_iso_datetime(raw) or raw), match.group(0)


def _deadline(item):
    """只取投标/响应截止；报名截止另记，不冒充截止时间（schema.md 易错字段）。"""
    value = _clean(item.get("tender_time"))
    match = re.search(r"20\d{2}-[01]\d-[0-3]\d(?:[ T][0-2]\d:[0-5]\d)?", value)
    return match.group(0).replace(" ", "T") if match else ""


def source_fields_from(item):
    """把列表项映射成十六字段里可结构化直取的那部分。"""
    fields = {
        "项目编号": _clean(item.get("bid_no")),
        "单位": _clean(item.get("caller_name")),
        "所属省/市": _clean(item.get("province")),
        "地区": _locality(item),
        "预算": _budget(item),
        "采购方式": _clean(item.get("bid_method")),
        "截止时间": _deadline(item),
    }
    return {key: value for key, value in fields.items() if value}


def field_evidence_from(item, fields):
    """结构化字段的证据即接口字段名——它本身就是一手取值，不需要正文正则。"""
    labels = {
        "项目编号": "bid_no", "单位": "caller_name", "所属省/市": "province",
        "地区": "province/city/county", "预算": "money（元）",
        "采购方式": "bid_method", "截止时间": "tender_time",
    }
    return {
        key: f"知了标讯结构化字段 {labels[key]}：{value}"
        for key, value in fields.items() if key in labels
    }


def product_list_of(item):
    """标的物 + 品牌，拉平成一串，供预筛与核实阶段定品类。"""
    names = list(item.get("sm_names") or []) + list(item.get("brand_names") or [])
    return "、".join(_clean(name) for name in names if _clean(name))


def signup_note(item):
    value = _clean(item.get("signup_time"))
    return f"报名/购买标书截止：{value}" if value else ""


def search_batch(client, keywords, start, end, page, page_size):
    payload = {
        "keywords": list(keywords),
        "match_modes": MATCH_MODES,
        "bid_type": "全部",
        "bid_process": DEFAULT_BID_PROCESS,
        "begin_date": start.date().isoformat(),
        "end_date": end.date().isoformat(),
        "page": page,
        "page_size": page_size,
    }
    data = client.call("search_bids", payload)
    return data.get("items") or [], int(data.get("total") or 0)


def load_hit_counts():
    """上次跑出来的「每词每天命中数」。文件缺失或损坏都退回空表，不影响检索。"""
    if not HIT_COUNTS_FILE.exists():
        return {}
    try:
        data = json.loads(HIT_COUNTS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    counts = data.get("per_day") if isinstance(data, dict) else None
    return {str(k): float(v) for k, v in counts.items()} if isinstance(counts, dict) else {}


def save_hit_counts(observed_per_day):
    """只覆盖本次真正单独查过的词，其余保留旧值——一次运行不会查遍所有词。"""
    merged = load_hit_counts()
    merged.update(observed_per_day)
    HIT_COUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    HIT_COUNTS_FILE.write_text(
        json.dumps({
            "note": "每词每天命中数，供 plan_batches 装箱；由 zlbx_search 自动维护",
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "per_day": {k: round(v, 2) for k, v in sorted(merged.items())},
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def plan_batches(queries, counts, days, page_size, batch_size):
    """按已知命中数装箱，让每批的预期命中落在单页以内。

    自适应切半能保证正确性，但**发现成本很高**：它对每个词一无所知，只能先打一枪看
    `total` 再决定切不切。2026-09-05 同口径实测（5 天窗口、只跑列表），自适应 50 次
    调用，而拿实测命中数预先装箱只要 27 次——多出来的 23 次全是探路。

    适配器每跑一次都会产出这份数据（见 collect_listings 的 observed），存下来下次直接用。
    没有历史数据的词按 `batch_size` 均摊估一个保守权重，装错了仍由切半兜底。

    OR 的命中数不是各词命中数之和（同一条公告会被多个词命中），所以按和装箱是保守的：
    实际 total 只会更小，不会更大。
    """
    cap = max(1, int(page_size * 0.9))          # 留一成余量，避免刚好压线
    weights = {q: counts[q] * days for q in queries if q in counts}
    # **没有历史数据的词不参与按重装箱**：它们没有可信的权重，硬给一个估值只会把
    # 装箱结果推向两个极端。估大了每个词单独成组（首跑实测退化成 85 组 / 92 次调用），
    # 估小了塞爆一批再靠切半兜底。改为按 batch_size 平铺，与无数据时的朴素分批一致，
    # 跑完这一轮它们就有真实命中数了。
    unknown = [q for q in queries if q not in counts]
    wide = [q for q in queries if q in counts and weights[q] > cap]
    known_small = sorted(
        (q for q in queries if q in counts and weights[q] <= cap), key=lambda q: -weights[q]
    )

    bins = []
    for query in known_small:                   # 首次适应递减
        for group in bins:
            if sum(weights[q] for q in group) + weights[query] <= cap:
                group.append(query)
                break
        else:
            bins.append([query])
    chunks = [unknown[i:i + batch_size] for i in range(0, len(unknown), batch_size)]
    return [[q] for q in wide] + bins + chunks


def collect_listings(client, queries, start, end, batch_size, page_size, stats,
                     counts=None, days=1):
    """按已知命中数装箱，并对装错的批切半重跑；绝不深翻页。

    2026-09-05 实测，接口的排序没有稳定 tiebreaker：同一组词同一参数连打三次，
    `total` 恒为 324，但每次取回的 324 条里去重后只有 312~317 条——重复的文档
    全部落在相邻页，翻页时记录跨页漂移，一部分返回两次、等量的另一部分一次都不返回。
    因此代价不在“词多”而在“翻页”：只要让每批命中数落在单页以内，就没有漂移。

    单个词自己就超过一页时无法再切，只能翻页并接受约 0.4% 的漂移。
    """
    found = {}
    observed = {}

    def take(items):
        for item in items:
            bid_id = item.get("bid_id")
            if bid_id is not None:
                found.setdefault(bid_id, item)

    def run(keywords):
        items, total = search_batch(client, keywords, start, end, 1, page_size)
        take(items)
        if len(keywords) == 1:
            # 单词查询才能把命中数归因到具体的词，供下次装箱用。
            observed[keywords[0]] = total / max(1, days)
        if total == 0:
            stats["empty_batches"] += 1
            return
        if total <= page_size:
            return
        if len(keywords) > 1:
            # 切半重跑。首页结果已经收下，不浪费这次调用。
            stats["split_batches"] += 1
            middle = len(keywords) // 2
            run(keywords[:middle])
            run(keywords[middle:])
            return
        # 单词超页，只能翻页
        stats["paged_queries"] += 1
        page = 2
        taken = len(items)
        while taken < total and page <= 40:
            items, total = search_batch(client, keywords, start, end, page, page_size)
            if not items:
                break
            take(items)
            taken += len(items)
            page += 1

    groups = plan_batches(queries, counts or {}, days, page_size, batch_size)
    stats["planned_groups"] = len(groups)
    for group in groups:
        run(group)
    stats["observed_hit_counts"] = observed
    return found


def fetch_detail(client, item):
    """取正文、原始站点链接与附件。只对通过预筛的候选调用。"""
    bid_type = 1 if _clean(item.get("bid_type")) == "招标" else 2
    return client.call("get_bid_detail", {"bid_id": item.get("bid_id"), "bid_type": bid_type})


def html_to_text(value):
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", str(value or ""))
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    text = re.sub(r"[ \t　]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def build_candidate(item, detail, query_hits):
    """按统一候选契约组装一条候选。"""
    title = _clean(item.get("title")) or _clean((detail or {}).get("title"))
    detail = detail or {}
    body = html_to_text(detail.get("source"))
    products = product_list_of(item) or product_list_of(detail)
    fields = source_fields_from(item)
    # 详情里的结构化字段更全（列表接口不返回 county / agency_name）
    for key, value in source_fields_from(detail).items():
        fields.setdefault(key, value)
    evidence = field_evidence_from(item, fields)
    # `tender_time` 常常是空的，而正文里往往写着「提交投标文件截止时间…」。
    # 兜底抽一次；更正公告并排写原/现两个时间时 _extract_deadline 返回空，不猜。
    if not fields.get("截止时间") and body:
        deadline, deadline_evidence = _extract_deadline(body)
        if deadline:
            fields["截止时间"] = deadline
            evidence["截止时间"] = f"正文：{deadline_evidence}"
    note = signup_note(item) or signup_note(detail)

    summary_parts = [part for part in (products, note) if part]
    summary = compact_text("；".join(summary_parts) or body, 2000)

    # 原始站点链接优先：发给销售的链接要能匿名打开，知了站内链接需要登录。
    source_url = _clean(detail.get("source_url"))
    fallback_url = _clean(detail.get("url")) or _clean(item.get("url"))
    url = source_url or fallback_url
    if not url:
        return None

    return {
        "source": "zlbx",
        "bid_id": item.get("bid_id") or detail.get("bid_id") or "",
        "title": title,
        "site_name": "知了标讯",
        "url": url,
        "publish_time": _clean(item.get("pub_time")) or _clean(detail.get("pub_time")),
        "summary": summary,
        "content": body,
        "product_list": products,
        "source_fields": fields,
        "field_evidence": evidence,
        "attachments": [
            _clean(a) for a in (detail.get("attachment_urls") or []) if _clean(a)
        ],
        # 契约：found_by_query 是 Query 编号（整数），found_by_source_query 是带
        # 原始词的字典——tender_pipeline.matched_query_keywords 靠后者绑定「命中关键词」。
        "found_by_query": sorted(number for number, _ in query_hits),
        "found_by_source_query": [
            {"source": "zlbx", "query_number": number, "query": word}
            for number, word in sorted(query_hits)
        ],
        # 适配器已保存完整正文，核实阶段不必再打开链接。
        "retrieval_verified": bool(body),
        "content_access": "public_full" if body else "metadata_only",
        "date_authoritative": True,
        "source_priority": 400,
        "alternate_sources": [
            {"source": "zlbx", "site_name": "知了标讯站内", "url": fallback_url}
        ] if source_url and fallback_url and fallback_url != source_url else [],
    }


def window_days(start, end):
    """装箱权重按「每天命中数 × 天数」估，窗口越宽每批要装的词越少。"""
    return max(1, (end.date() - start.date()).days + 1)


def collect(client, queries, start, end, batch_size, page_size, max_details, seen_path=None):
    known = IdentityIndex()
    if seen_path is not None:
        known = IdentityIndex(r for r in read_ledger(seen_path)["records"] if r.get("_pushed") is True)
    stats = {"empty_batches": 0, "split_batches": 0, "paged_queries": 0}
    days = window_days(start, end)
    listings = collect_listings(
        client, queries, start, end, batch_size, page_size, stats,
        counts=load_hit_counts(), days=days,
    )
    # 本次单独查过的词，命中数存回去供下次装箱；一次运行查不遍所有词，故为增量合并。
    observed = stats.pop("observed_hit_counts", {})
    if observed:
        save_hit_counts(observed)
    stats["hit_counts_learned"] = len(observed)

    # 命中归因：列表接口不回传是哪个词命中的，本地按标的物与标题回推，
    # 记成 (Query 编号, 词) 以满足候选契约。正文里才命中的词由统一层再补一次。
    numbered = list(enumerate(queries, 1))
    hits = {}
    for bid_id, item in listings.items():
        haystack = "\n".join((
            _clean(item.get("title")), product_list_of(item), _clean(item.get("caller_name")),
        )).lower()
        hits[bid_id] = {(number, word) for number, word in numbered if word.lower() in haystack}

    prefilter_dropped = []
    already_seen = []
    kept = []
    for bid_id, item in listings.items():
        title = _clean(item.get("title"))
        screen = screen_domain(title, product_list_of(item))
        if not screen["keep"] and not screen["signals"]:
            # 列表层只有标题与标的物；正文可能还有信号，先记一笔再看是否值得取详情。
            prefilter_dropped.append({
                "bid_id": bid_id, "title": title, "reason": screen["reason"],
            })
            continue
        duplicate, reason = known.find({
            "title": title, "bid_id": bid_id, "url": item.get("url"),
            "publish_time": item.get("pub_time"), "source_fields": source_fields_from(item),
        })
        if duplicate is not None:
            already_seen.append({"bid_id": bid_id, "title": title, "reason": reason,
                                 "matched_feishu_id": duplicate.get("_feishu_id")})
            continue
        kept.append((bid_id, item))

    kept.sort(key=lambda row: _clean(row[1].get("pub_time")), reverse=True)
    candidates = []
    detail_calls = 0
    dropped_no_url = 0
    for bid_id, item in kept:
        detail = None
        if detail_calls < max_details:
            try:
                detail = fetch_detail(client, item)
                detail_calls += 1
            except ZlbxAuthError:
                raise
            except ZlbxError as exc:
                print(f"警告：标讯 {bid_id} 详情获取失败：{exc}", file=sys.stderr)
        candidate = build_candidate(item, detail, hits.get(bid_id, set()))
        if candidate is None:
            dropped_no_url += 1
            continue
        candidates.append(candidate)

    stats.update({
        "detail_calls": detail_calls,
        "dropped_no_url": dropped_no_url,
        "prefilter_dropped": prefilter_dropped[:200],
        "prefilter_dropped_count": len(prefilter_dropped),
        "raw_result_count": len(listings),
        "already_seen_before_detail_count": len(already_seen),
        "already_seen_before_detail": already_seen,
    })
    return candidates, stats


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="知了标讯检索适配器")
    parser.add_argument("--time-range", default="72h", help="72h / 3d / YYYY-MM-DD..YYYY-MM-DD")
    parser.add_argument("--out-dir")
    parser.add_argument("--queries", help="逗号分隔；默认读 references/keywords.md")
    parser.add_argument("--batch-size", type=int, default=8,
                        help="初始每批词数；命中超过单页会自动切半，见 collect_listings")
    parser.add_argument("--page-size", type=int, default=MAX_PAGE_SIZE)
    parser.add_argument("--max-details", type=int, default=60,
                        help="get_bid_detail 调用上限，每次 1 积分")
    parser.add_argument("--delay", type=float, default=0.25)
    parser.add_argument("--seen", default=str(ROOT / "data/seen.json"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        start, end = parse_time_range(args.time_range)
        queries = ([item.strip() for item in args.queries.split(",") if item.strip()]
                   if args.queries else parse_queries())
        if not queries:
            raise ZlbxError("检索 Query 为空")
        if not 1 <= args.page_size <= MAX_PAGE_SIZE:
            raise ZlbxError(f"--page-size 必须在 1~{MAX_PAGE_SIZE}（接口上限 {MAX_PAGE_SIZE}）")
        if args.batch_size < 1:
            raise ZlbxError("--batch-size 必须大于 0")
        if args.max_details < 0:
            raise ZlbxError("--max-details 不能为负")

        if args.dry_run:
            print(json.dumps({
                "source": "zlbx",
                "query_count": len(queries),
                "queries": queries,
                "batch_size": args.batch_size,
                "bid_process": DEFAULT_BID_PROCESS,
                "match_modes": MATCH_MODES,
                "start": start.isoformat(timespec="seconds"),
                "end": end.isoformat(timespec="seconds"),
                "api_key_present": bool(
                    os.environ.get("ZLBX_API_KEY") or CONFIG_FILE.exists()
                ),
            }, ensure_ascii=False, indent=2))
            return 0

        api_key = load_api_key()
        out_dir = (Path(args.out_dir) if args.out_dir
                   else ROOT / ".tmp" / "search" / date.today().isoformat() / ".sources" / "zlbx")
        out_dir.mkdir(parents=True, exist_ok=True)

        client = ZlbxClient(api_key, delay=args.delay)
        started = time.time()
        candidates, stats = collect(
            client, queries, start, end,
            batch_size=args.batch_size, page_size=args.page_size,
            max_details=args.max_details,
            seen_path=args.seen,
        )
        index = write_candidates(candidates, out_dir, date.today().isoformat())
        summary = {
            "schema_version": 1,
            "source": "zlbx",
            "run_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "time_range": f"{start.isoformat(timespec='seconds')}..{end.isoformat(timespec='seconds')}",
            "api_key": mask_key(api_key),
            "query_count": len(queries),
            "request_count": client.request_count,
            "cost_units": client.cost_units,
            "candidate_count": len(index),
            "elapsed_seconds": round(time.time() - started, 1),
            **stats,
        }
        (out_dir / "search_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(
            f"知了标讯：{client.request_count} 次调用 / {client.cost_units:.0f} 积分，"
            f"全库命中 {stats['raw_result_count']} 条，预筛丢弃 "
            f"{stats['prefilter_dropped_count']} 条，候选 {len(index)} 条"
        )
        print(f"候选目录：{out_dir}")
        return 0
    except ZlbxAuthError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 3
    except (ZlbxError, LedgerError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
