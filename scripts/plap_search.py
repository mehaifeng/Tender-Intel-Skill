#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""军队采购网（PLAP）匿名公开信息检索适配器。

只调用门户搜索页自身使用的公开 GET 接口，不登录、不携带 access_token，
也不尝试读取“用户登录后显示完整信息”的受限内容。
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import re
import sys
import time
from datetime import date, datetime, timedelta
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

from search_common import canonical_url, compact_text, target_category_signals, write_candidates


ROOT = Path(__file__).resolve().parent.parent
REFERENCE_FILE = ROOT / "references" / "plap.md"
BASE_URL = "https://www.plap.mil.cn"
SEARCH_PAGE = BASE_URL + "/freecms/site/juncai/qwjsy/index.html"
SEARCH_ENDPOINT = BASE_URL + "/freecms/rest/v1/notice/selectInfoMoreChannel.do"
SITE_ID = "404bb030-5be9-4070-85bd-c94b1473e8de"
PURCHASE_CHANNEL = "c5bff13f-21ca-4dac-b158-cb40accd3035"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

# 这些类型量相对可控，默认按时间枚举后在本地做目标品类过滤，补足标题搜索漏召回。
ENUMERATED_NOTICE_TYPES = [
    ("采购意向公开", "59"),
    ("采购需求公示", "00105E"),
    ("征集方案", "001068"),
    ("征求意见", "001069"),
    ("单一来源公示", "001051"),
]
NOTICE_TYPE_PREFIXES = [
    ("59", "采购意向公开"),
    ("00105E", "采购需求公示"),
    ("001068", "征集方案"),
    ("001069", "征求意见"),
    ("001051", "单一来源公示"),
    ("00101", "采购公告"),
    ("001052", "采购公告"),
    ("00105B", "采购公告"),
    ("001031", "采购公告"),
    ("00102", "采购结果公示"),
    ("001060", "采购结果公示"),
    ("00106B", "采购结果公示"),
    ("001006", "采购结果公示"),
]
TARGET_TERMS = [
    ("过敏原", re.compile(r"过敏原", re.I)),
    ("过敏源", re.compile(r"过敏源", re.I)),
    ("变应原", re.compile(r"变应原", re.I)),
    ("特异性IgE", re.compile(r"特异性\s*IgE", re.I)),
    ("sIgE", re.compile(r"\bsIgE\b", re.I)),
    ("总IgE", re.compile(r"总\s*IgE|\btIgE\b", re.I)),
    ("自身抗体", re.compile(r"自身抗体", re.I)),
    ("自身免疫", re.compile(r"自身免疫", re.I)),
    ("抗核抗体", re.compile(r"抗核抗体|\bANA\b", re.I)),
    ("双链DNA", re.compile(r"双链\s*DNA|\bdsDNA\b", re.I)),
    ("ANCA", re.compile(r"\bANCA\b", re.I)),
    ("抗磷脂抗体", re.compile(r"抗磷脂抗体|心磷脂抗体", re.I)),
    ("自免肝", re.compile(r"自免肝", re.I)),
    ("肌炎抗体", re.compile(r"肌炎抗体", re.I)),
    ("PLA2R", re.compile(r"\bPLA2R\b", re.I)),
    ("细胞因子", re.compile(r"细胞因子", re.I)),
    ("IgG亚类", re.compile(r"IgG\s*亚类", re.I)),
    ("酶联免疫", re.compile(r"酶联免疫|\bELISA\b", re.I)),
    ("化学发光", re.compile(r"化学发光|发光免疫", re.I)),
    ("免疫荧光", re.compile(r"免疫荧光", re.I)),
    ("免疫印迹", re.compile(r"免疫印迹", re.I)),
    ("免疫分析仪", re.compile(r"免疫分析仪", re.I)),
    ("酶免仪", re.compile(r"全自动酶免|酶免工作站|酶免仪", re.I)),
    ("酶标仪", re.compile(r"酶标仪", re.I)),
    ("洗板机", re.compile(r"洗板机", re.I)),
]
PROVINCE_SHORT = {
    "北京市": "北京", "天津市": "天津", "上海市": "上海", "重庆市": "重庆",
    "河北省": "河北", "山西省": "山西", "辽宁省": "辽宁", "吉林省": "吉林",
    "黑龙江省": "黑龙江", "江苏省": "江苏", "浙江省": "浙江", "安徽省": "安徽",
    "福建省": "福建", "江西省": "江西", "山东省": "山东", "河南省": "河南",
    "湖北省": "湖北", "湖南省": "湖南", "广东省": "广东", "广西壮族自治区": "广西",
    "海南省": "海南", "四川省": "四川", "贵州省": "贵州", "云南省": "云南",
    "西藏自治区": "西藏", "陕西省": "陕西", "甘肃省": "甘肃", "青海省": "青海",
    "宁夏回族自治区": "宁夏", "新疆维吾尔自治区": "新疆", "内蒙古自治区": "内蒙古",
}
REGION_NORMALIZATION = {
    "广西自治区": "广西壮族自治区",
    "宁夏自治区": "宁夏回族自治区",
    "新疆自治区": "新疆维吾尔自治区",
}


class PLAPError(Exception):
    pass


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


def parse_title_queries():
    text = REFERENCE_FILE.read_text(encoding="utf-8")
    marker = "## 默认标题 Query"
    if marker not in text:
        raise PLAPError(f"{REFERENCE_FILE} 缺少“{marker}”")
    section = text.split(marker, 1)[1].split("\n## ", 1)[0]
    queries = [query.strip() for _, query in re.findall(r"^(\d+)\.\s+(\S.*?)\s*$", section, re.M)]
    if not queries:
        raise PLAPError("PLAP 默认标题 Query 清单为空")
    return queries


def parse_time_range(value, now=None):
    value = str(value or "72h").strip()
    now = now or datetime.now().astimezone().replace(tzinfo=None)
    match = re.fullmatch(r"(\d+)h", value)
    if match:
        hours = int(match.group(1))
        if hours < 1:
            raise PLAPError("--time-range 小时数必须大于0")
        return now - timedelta(hours=hours), now
    match = re.fullmatch(r"(\d+)d", value)
    if match:
        days = int(match.group(1))
        if days < 1:
            raise PLAPError("--time-range 天数必须大于0")
        return now - timedelta(days=days), now
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})", value)
    if match:
        start = datetime.fromisoformat(match.group(1))
        end = datetime.fromisoformat(match.group(2)) + timedelta(days=1) - timedelta(seconds=1)
        if start > end:
            raise PLAPError(f"时间范围起点晚于终点：{value}")
        return start, end
    raise PLAPError("--time-range 支持 72h、3d 或 YYYY-MM-DD..YYYY-MM-DD")


def build_tasks(queries, strategy="hybrid"):
    tasks = []
    if strategy in {"title", "hybrid"}:
        tasks.extend({"mode": "title", "query": query, "notice_type": ""} for query in queries)
    if strategy in {"enumerate", "hybrid"}:
        tasks.extend({"mode": "notice_type", "query": name, "notice_type": codes}
                     for name, codes in ENUMERATED_NOTICE_TYPES)
    return tasks


def build_search_url(task, start, end, page, page_size):
    params = {
        "siteId": SITE_ID,
        "channel": PURCHASE_CHANNEL,
        "searchKey": "",
        "title": task["query"] if task["mode"] == "title" else "",
        "content": "",
        "regionCode": "",
        "noticeType": task["notice_type"],
        "operationStartTime": start.strftime("%Y-%m-%d %H:%M:%S"),
        "operationEndTime": end.strftime("%Y-%m-%d %H:%M:%S"),
        "selectTimeName": "noticeTime",
        "currPage": page,
        "pageSize": page_size,
    }
    return SEARCH_ENDPOINT + "?" + urlencode(params)


class PLAPClient:
    def __init__(self, delay=1.0, timeout=30, max_bytes=20 * 1024 * 1024):
        self.delay = max(0.0, float(delay))
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.last_request_at = 0.0

    def get_json(self, url):
        wait = self.delay - (time.monotonic() - self.last_request_at)
        if wait > 0:
            time.sleep(wait)
        request = Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "gzip",
            "Referer": SEARCH_PAGE,
        })
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read(self.max_bytes + 1)
                if len(raw) > self.max_bytes:
                    raise PLAPError(f"JSON响应超过{self.max_bytes // 1024 // 1024}MB")
                if response.headers.get("Content-Encoding", "").lower() == "gzip":
                    raw = gzip.decompress(raw)
                encoding = response.headers.get_content_charset() or "utf-8"
                payload = json.loads(raw.decode(encoding, errors="replace"))
        except HTTPError as exc:
            raise PLAPError(f"HTTP {exc.code}: {url}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise PLAPError(f"网络错误：{exc}") from exc
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise PLAPError(f"返回内容不是有效JSON：{exc}") from exc
        finally:
            self.last_request_at = time.monotonic()
        if str(payload.get("code")) != "200" or not isinstance(payload.get("data"), list):
            raise PLAPError(f"公开列表接口结构异常：code={payload.get('code')!r}")
        return payload


def notice_type_name(code):
    code = str(code or "")
    for prefix, name in NOTICE_TYPE_PREFIXES:
        if code.startswith(prefix):
            return name
    return ""


def matched_target_terms(text):
    return [name for name, pattern in TARGET_TERMS if pattern.search(text or "")]


def province_short(region):
    region = compact_text(region)
    if region in PROVINCE_SHORT:
        return PROVINCE_SHORT[region]
    for full, short in PROVINCE_SHORT.items():
        if region.startswith(full) or region.startswith(short):
            return short
    return ""


def normalize_budget(value, text):
    raw = str(value or "").replace(",", "").strip()
    unit = "元"
    if not raw:
        match = re.search(
            r"(?:预算金额|项目预算|采购预算|预算|最高限价)\s*[：:]\s*(?:人民币)?\s*"
            r"([\d,.]+)\s*(万元|元)?",
            text,
            re.I,
        )
        if not match:
            return "", ""
        raw = match.group(1).replace(",", "")
        unit = match.group(2) or "元"
        evidence = compact_text(match.group(0))
    else:
        evidence = f"公开列表预算字段：{value}"
    try:
        number = float(raw) * (10000 if unit == "万元" else 1)
    except ValueError:
        return "", ""
    normalized = str(int(number)) if number.is_integer() else (f"{number:.2f}".rstrip("0").rstrip("."))
    return normalized, evidence


def extract_labeled_field(text, labels, limit=120):
    label_pattern = "|".join(re.escape(label) for label in labels)
    match = re.search(rf"(?:{label_pattern})\s*[：:]\s*([^\n；;]{{2,{limit}}})", text, re.I)
    return (compact_text(match.group(1)), compact_text(match.group(0))) if match else ("", "")


def extract_deadline(text):
    match = re.search(
        r"(?:投标截止时间|报价截止时间|响应文件(?:提交)?截止时间|提交投标文件截止时间)\s*[：:]\s*"
        r"(20\d{2})[年\-/](\d{1,2})[月\-/](\d{1,2})日?\s*"
        r"(\d{1,2})[时:]\s*(\d{1,2})?分?",
        text,
        re.I,
    )
    if not match:
        return "", ""
    minute = int(match.group(5) or 0)
    value = f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}T{int(match.group(4)):02d}:{minute:02d}"
    return value, compact_text(match.group(0))


def extract_procurement_method(title, text):
    methods = ("公开招标", "邀请招标", "竞争性磋商", "竞争性谈判", "询价", "单一来源", "框架协议", "比价", "竞价", "遴选")
    for method in methods:
        if method in title:
            return method, f"公开标题出现：{method}"
    match = re.search(
        r"(?:采购方式|采购方法)\s*[：:]\s*"
        r"(公开招标|邀请招标|竞争性磋商|竞争性谈判|询价|单一来源|框架协议|比价|竞价|遴选)",
        text,
        re.I,
    )
    if match:
        return match.group(1), compact_text(match.group(0))
    return "", ""


def normalize_attachments(value):
    rows = []
    values = value if isinstance(value, list) else ([value] if value else [])
    for item in values:
        if isinstance(item, str):
            url, name = item, ""
        elif isinstance(item, dict):
            url = next((item.get(key) for key in ("url", "fileUrl", "path", "downloadUrl") if item.get(key)), "")
            name = next((item.get(key) for key in ("name", "fileName", "title") if item.get(key)), "")
        else:
            continue
        if not url:
            continue
        absolute = canonical_url(urljoin(BASE_URL, str(url)))
        if absolute.startswith("https://www.plap.mil.cn/") or absolute.startswith("https://plap.mil.cn/"):
            row = {"url": absolute}
            if name:
                row["name"] = compact_text(name)
            if row not in rows:
                rows.append(row)
    return rows


def row_to_candidate(row, hits):
    title = compact_text(row.get("title"))
    summary = html_to_text(row.get("description"))
    content = html_to_text(row.get("content"))
    if not summary and content:
        summary = compact_text(content, 2000)
    search_text = "\n".join((title, summary, content))
    signals = target_category_signals(search_text)
    if not signals:
        return None

    source_hits = list(hits)
    for term in matched_target_terms(search_text):
        hit = {"source": "plap", "query": term, "query_mode": "local_content_filter"}
        if hit not in source_hits:
            source_hits.append(hit)

    raw_url = row.get("pageurl") or row.get("htmlpath") or ""
    if not raw_url and row.get("noticeId"):
        year_match = re.search(r"20\d{2}", str(row.get("noticeTime") or ""))
        if year_match:
            raw_url = f"/freecms/site/juncai/ggxx/info/{year_match.group(0)}/{row['noticeId']}.html"
    if not raw_url:
        return None
    url = canonical_url(urljoin(BASE_URL, raw_url))
    fields = {}
    evidence = {}
    project_id = compact_text(row.get("openTenderCode"))
    if project_id:
        fields["项目编号"] = project_id
        evidence["项目编号"] = f"公开列表项目编号：{project_id}"
    publish_time = compact_text(row.get("noticeTime"))
    if publish_time:
        fields["发布时间"] = publish_time
        evidence["发布时间"] = f"公开列表发布时间：{publish_time}"
    region = REGION_NORMALIZATION.get(compact_text(row.get("regionName")), compact_text(row.get("regionName")))
    if region and region not in {"无", "暂无数据", "null"}:
        fields["地区"] = region
        evidence["地区"] = f"公开列表地区：{region}"
        short = province_short(region)
        if short:
            fields["所属省/市"] = short
            evidence["所属省/市"] = f"由公开列表地区规范化：{region}"
    notice_name = notice_type_name(row.get("noticeType"))
    if notice_name:
        fields["公告类型"] = notice_name
        evidence["公告类型"] = f"军队采购网公告类型编码：{row.get('noticeType')}"

    budget, budget_evidence = normalize_budget(row.get("budget"), search_text)
    if budget:
        fields["预算"] = budget
        evidence["预算"] = budget_evidence
    method, method_evidence = extract_procurement_method(title, search_text)
    if method:
        fields["采购方式"] = method
        evidence["采购方式"] = method_evidence
    unit, unit_evidence = extract_labeled_field(search_text, ("采购单位名称", "采购单位", "采购人名称", "招标人"))
    if unit:
        fields["单位"] = unit
        evidence["单位"] = unit_evidence
    deadline, deadline_evidence = extract_deadline(search_text)
    if deadline:
        fields["截止时间"] = deadline
        evidence["截止时间"] = deadline_evidence

    content_access = "public_partial" if content else "metadata_only"
    return {
        "title": title,
        "site_name": "军队采购网",
        "url": url,
        "publish_time": publish_time,
        "auth_info_level": 4,
        "auth_info_des": "官方权威",
        "rank_score": None,
        "summary": summary,
        "content": content,
        "source_fields": fields,
        "field_evidence": evidence,
        "attachments": normalize_attachments(row.get("attchs")),
        "found_by_query": [],
        "found_by_source_query": source_hits,
        "source": "plap",
        "sources": ["plap"],
        "source_priority": 400,
        "date_authoritative": True,
        "retrieval_verified": bool(content),
        "content_access": content_access,
    }


def collect(client, tasks, start, end, page_size=20, max_pages_per_task=100):
    by_notice = {}
    failures = []
    raw_count = 0
    stopped = False
    for task in tasks:
        page = 1
        pages = 1
        while page <= pages and page <= max_pages_per_task:
            try:
                payload = client.get_json(build_search_url(task, start, end, page, page_size))
            except PLAPError as exc:
                failures.append({"task": task, "page": page, "error": str(exc)})
                stopped = True
                break
            results = payload["data"]
            total = int(payload.get("total") or len(results))
            pages = math.ceil(total / page_size) if total else 0
            raw_count += len(results)
            for row in results:
                if not isinstance(row, dict):
                    continue
                raw_url = row.get("pageurl") or row.get("htmlpath") or ""
                key = compact_text(row.get("noticeId")) or canonical_url(urljoin(BASE_URL, raw_url))
                if not key:
                    continue
                if key not in by_notice:
                    by_notice[key] = {"row": row, "hits": []}
                hit = {
                    "source": "plap",
                    "query": task["query"],
                    "query_mode": task["mode"],
                }
                if hit not in by_notice[key]["hits"]:
                    by_notice[key]["hits"].append(hit)
            page += 1
        if pages > max_pages_per_task:
            failures.append({
                "task": task,
                "error": f"结果共{pages}页，超过安全上限{max_pages_per_task}页；该任务未完整收集",
            })
        if stopped:
            break

    candidates = []
    filtered_out = 0
    for item in by_notice.values():
        candidate = row_to_candidate(item["row"], item["hits"])
        if candidate:
            candidates.append(candidate)
        else:
            filtered_out += 1
    return candidates, failures, raw_count, filtered_out


def main():
    parser = argparse.ArgumentParser(description="军队采购网匿名公开信息检索适配器")
    parser.add_argument("--query", help="单个即席标题 Query；只运行标题检索")
    parser.add_argument("--queries", help="逗号分隔的标题 Query；默认读取 references/plap.md")
    parser.add_argument("--strategy", choices=("hybrid", "title", "enumerate"), default="hybrid")
    parser.add_argument("--time-range", default="72h", help="72h / 3d / YYYY-MM-DD..YYYY-MM-DD")
    parser.add_argument("--out-dir", help="输出目录；默认 .tmp/search/<日期>/.sources/plap")
    parser.add_argument("--delay", type=float, default=1.0, help="同域请求间隔秒数，默认1")
    parser.add_argument("--page-size", type=int, default=20)
    parser.add_argument("--max-pages-per-task", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        start, end = parse_time_range(args.time_range)
        if args.query:
            queries = [args.query.strip()]
            strategy = "title"
        elif args.queries:
            queries = [item.strip() for item in args.queries.split(",") if item.strip()]
            strategy = args.strategy
        else:
            queries = parse_title_queries()
            strategy = args.strategy
        tasks = build_tasks(queries, strategy)
        if not tasks:
            raise PLAPError("检索任务为空")
        if not 1 <= args.page_size <= 50:
            raise PLAPError("--page-size 必须在1到50之间")
        if args.max_pages_per_task < 1:
            raise PLAPError("--max-pages-per-task 必须大于0")
        if args.dry_run:
            print(json.dumps({
                "source": "plap",
                "strategy": strategy,
                "tasks": tasks,
                "start": start.isoformat(timespec="seconds"),
                "end": end.isoformat(timespec="seconds"),
                "sample_url": build_search_url(tasks[0], start, end, 1, args.page_size),
                "authentication": "anonymous_public_only",
            }, ensure_ascii=False, indent=2))
            return 0

        out_dir = Path(args.out_dir) if args.out_dir else ROOT / ".tmp" / "search" / date.today().isoformat() / ".sources" / "plap"
        out_dir.mkdir(parents=True, exist_ok=True)
        started = time.time()
        candidates, failures, raw_count, filtered_out = collect(
            PLAPClient(delay=args.delay), tasks, start, end, args.page_size, args.max_pages_per_task
        )
        index = write_candidates(candidates, out_dir, date.today().isoformat())
        summary = {
            "schema_version": 1,
            "source": "plap",
            "run_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "time_range": f"{start.isoformat(timespec='seconds')}..{end.isoformat(timespec='seconds')}",
            "strategy": strategy,
            "query_count": len(tasks),
            "query_failed": len(failures),
            "raw_result_count": raw_count,
            "prefilter_excluded": filtered_out,
            "candidate_count": len(index),
            "content_access": {
                "public_partial": sum(1 for item in index if item.get("content_access") == "public_partial"),
                "metadata_only": sum(1 for item in index if item.get("content_access") == "metadata_only"),
            },
            "failures": failures,
        }
        (out_dir / "search_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(
            f"PLAP：{len(tasks)} 个公开检索任务，原始 {raw_count} 条，"
            f"目标品类预筛后 {len(index)} 条，排除 {filtered_out} 条，"
            f"失败 {len(failures)} 项，耗时 {time.time() - started:.1f}s"
        )
        print(f"落盘：{out_dir}")
        return 0 if not failures or candidates else 2
    except PLAPError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
