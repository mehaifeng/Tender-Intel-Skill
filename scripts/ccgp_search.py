#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""中国政府采购网公开采购公告 HTTP 检索适配器。"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import time
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener

from search_common import canonical_url, compact_text, write_candidates


ROOT = Path(__file__).resolve().parent.parent
REFERENCE_FILE = ROOT / "references" / "ccgp.md"
SEARCH_ENDPOINT = "https://search.ccgp.gov.cn/bxsearch"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
ATTACHMENT_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".rar", ".7z", ".txt",
}


class CCGPError(Exception):
    pass


def parse_query_list():
    text = REFERENCE_FILE.read_text(encoding="utf-8")
    marker = "## 默认单词 Query"
    if marker not in text:
        raise CCGPError(f"{REFERENCE_FILE} 缺少“{marker}”")
    section = text.split(marker, 1)[1].split("\n## ", 1)[0]
    queries = [query.strip() for _, query in re.findall(r"^(\d+)\.\s+(\S.*?)\s*$", section, re.M)]
    if not queries:
        raise CCGPError("CCGP 默认 Query 清单为空")
    if any(len(query.split()) > 1 for query in queries):
        raise CCGPError("CCGP Query 必须为单个产品词，不能堆叠空格分隔词")
    return queries


def parse_time_range(value):
    value = str(value or "72h").strip()
    match = re.fullmatch(r"(\d+)h", value)
    if match:
        days = (int(match.group(1)) + 23) // 24
        end = date.today()
        return end - timedelta(days=days), end
    match = re.fullmatch(r"(\d+)d", value)
    if match:
        end = date.today()
        return end - timedelta(days=int(match.group(1))), end
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})", value)
    if match:
        start, end = date.fromisoformat(match.group(1)), date.fromisoformat(match.group(2))
        if start > end:
            raise CCGPError(f"时间范围起点晚于终点：{value}")
        return start, end
    raise CCGPError("--time-range 支持 72h、3d 或 YYYY-MM-DD..YYYY-MM-DD")


def build_search_url(query, start, end, page):
    params = {
        "searchtype": 2,
        "page_index": page,
        "bidSort": 0,
        "buyerName": "",
        "projectId": "",
        "pinMu": 0,
        "bidType": 0,
        "dbselect": "bidx",
        "kw": query,
        "start_time": start.strftime("%Y:%m:%d"),
        "end_time": end.strftime("%Y:%m:%d"),
        "timeType": 1,
        "displayZone": "",
        "zoneId": "",
        "pppStatus": 0,
        "agentName": "",
    }
    return SEARCH_ENDPOINT + "?" + urlencode(params)


class CCGPClient:
    def __init__(self, delay=2.0, timeout=30):
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))
        self.delay = max(0.0, float(delay))
        self.timeout = timeout
        self.last_request_at = 0.0

    def get(self, url, referer="https://www.ccgp.gov.cn/"):
        wait = self.delay - (time.monotonic() - self.last_request_at)
        if wait > 0:
            time.sleep(wait)
        request = Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
            "Accept-Encoding": "gzip",
            "Referer": referer,
        })
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                limit = 10 * 1024 * 1024
                raw = response.read(limit + 1)
                if len(raw) > limit:
                    raise CCGPError(f"HTML响应超过{limit // 1024 // 1024}MB，拒绝截断正文：{url}")
                if response.headers.get("Content-Encoding", "").lower() == "gzip":
                    raw = gzip.decompress(raw)
                    if len(raw) > limit:
                        raise CCGPError(f"解压后的HTML超过{limit // 1024 // 1024}MB，拒绝截断正文：{url}")
                encoding = response.headers.get_content_charset() or "utf-8"
                text = raw.decode(encoding, errors="replace")
        except HTTPError as exc:
            raise CCGPError(f"HTTP {exc.code}: {url}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise CCGPError(f"网络错误：{exc}") from exc
        finally:
            self.last_request_at = time.monotonic()
        if "您的访问过于频繁" in text or "频繁访问!中国政府采购网" in text:
            raise CCGPError("中国政府采购网返回访问频繁页；本轮停止，不连续重试")
        return text


class SearchResultParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_result_list = False
        self.list_depth = 0
        self.current = None
        self.capture = None
        self.results = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set((attrs.get("class") or "").split())
        if tag == "ul" and "vT-srch-result-list-bid" in classes:
            self.in_result_list = True
            self.list_depth = 1
            return
        if self.in_result_list and tag in {"ul", "ol"}:
            self.list_depth += 1
        if not self.in_result_list:
            return
        if tag == "li" and self.current is None:
            self.current = {"href": "", "title_parts": [], "snippet_parts": [], "meta_parts": []}
        elif self.current is not None and tag == "a" and not self.current["href"]:
            self.current["href"] = attrs.get("href") or ""
            self.capture = "title_parts"
        elif self.current is not None and tag == "p":
            self.capture = "snippet_parts"
        elif self.current is not None and tag == "span":
            self.capture = "meta_parts"

    def handle_endtag(self, tag):
        if not self.in_result_list:
            return
        if tag == "li" and self.current is not None:
            title = compact_text("".join(self.current["title_parts"]))
            if self.current["href"] and title:
                meta = compact_text(" ".join(self.current["meta_parts"]))
                self.results.append({
                    "url": canonical_url(urljoin("https://www.ccgp.gov.cn/", self.current["href"])),
                    "title": title,
                    "summary": compact_text(" ".join(self.current["snippet_parts"])),
                    "meta": meta,
                })
            self.current = None
            self.capture = None
        elif tag in {"a", "p", "span"}:
            self.capture = None
        if tag in {"ul", "ol"}:
            self.list_depth -= 1
            if self.list_depth <= 0:
                self.in_result_list = False

    def handle_data(self, data):
        if self.current is not None and self.capture:
            self.current[self.capture].append(data)


def parse_search_page(html):
    if "采购公告搜索" not in html or "共找到" not in html:
        raise CCGPError("搜索响应缺少预期结构，拒绝把异常页面当成零结果")
    parser = SearchResultParser()
    parser.feed(html)
    total_match = re.search(r"共找到\s*(?:</?[^>]+>\s*)*(\d+)\s*(?:</?[^>]+>\s*)*条内容", html, re.S)
    page_match = re.search(r"Pager\s*\(\s*\{.*?size\s*:\s*(\d+)", html, re.S)
    total = int(total_match.group(1)) if total_match else len(parser.results)
    pages = int(page_match.group(1)) if page_match else (1 if total else 0)
    for item in parser.results:
        date_match = re.search(r"(20\d{2})[.]([01]\d)[.]([0-3]\d)\s+([0-2]\d:[0-5]\d:[0-5]\d)", item["meta"])
        item["publish_time"] = (
            f"{date_match.group(1)}-{date_match.group(2)}-{date_match.group(3)} {date_match.group(4)}"
            if date_match else ""
        )
        for field, label in (("buyer", "采购人"), ("agency", "代理机构")):
            match = re.search(rf"{label}：\s*(.*?)\s*(?=\||$)", item["meta"])
            item[field] = compact_text(match.group(1)) if match else ""
        province_match = re.search(r"\|\s*([^|\s]{2,12})\s*\|", item["meta"])
        item["province"] = compact_text(province_match.group(1)) if province_match else ""
        type_match = re.search(
            r"(公开招标公告|竞争性磋商公告|竞争性谈判公告|询价公告|单一来源公告和公示|"
            r"中标公告|成交公告|更正公告|其他公告|终止公告|资格预审公告)",
            item["meta"],
        )
        item["notice_type"] = type_match.group(1) if type_match else ""
    return parser.results, total, pages


class DetailParser(HTMLParser):
    BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "table"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.text_parts = []
        self.meta = {}
        self.rows = []
        self.current_row = None
        self.current_cell = None
        self.anchors = []
        self.current_anchor = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "style"}:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag == "meta" and attrs.get("name"):
            self.meta[attrs["name"]] = attrs.get("content") or ""
        if tag in self.BLOCK_TAGS:
            self.text_parts.append("\n")
        if tag == "tr":
            self.current_row = []
        elif tag in {"td", "th"} and self.current_row is not None:
            self.current_cell = []
        elif tag == "a" and attrs.get("href"):
            self.current_anchor = {"href": attrs["href"], "text_parts": []}

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            if self.skip_depth:
                self.skip_depth -= 1
            return
        if self.skip_depth:
            return
        if tag in {"td", "th"} and self.current_cell is not None:
            self.current_row.append(compact_text("".join(self.current_cell)))
            self.current_cell = None
        elif tag == "tr" and self.current_row is not None:
            if any(self.current_row):
                self.rows.append(self.current_row)
            self.current_row = None
        elif tag == "a" and self.current_anchor is not None:
            self.current_anchor["text"] = compact_text("".join(self.current_anchor.pop("text_parts")))
            self.anchors.append(self.current_anchor)
            self.current_anchor = None
        if tag in self.BLOCK_TAGS:
            self.text_parts.append("\n")

    def handle_data(self, data):
        if self.skip_depth:
            return
        self.text_parts.append(data)
        if self.current_cell is not None:
            self.current_cell.append(data)
        if self.current_anchor is not None:
            self.current_anchor["text_parts"].append(data)


def _table_value(rows, label):
    for row in rows:
        for index, cell in enumerate(row[:-1]):
            if compact_text(cell).rstrip("：:") == label:
                return compact_text(row[index + 1])
    return ""


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


def _budget_yuan(text):
    match = re.search(r"[￥¥]?\s*([\d,.]+)\s*万元", text)
    if match:
        return str(int(round(float(match.group(1).replace(",", "")) * 10000)))
    match = re.search(r"预算金额(?:（元）|\(元\))?\s*[：:]?\s*[￥¥]?\s*([\d,.]+)", text)
    if match:
        return str(int(round(float(match.group(1).replace(",", "")))))
    return ""


def _province_short(value):
    text = compact_text(value)
    aliases = {
        "内蒙古自治区": "内蒙古", "广西壮族自治区": "广西", "西藏自治区": "西藏",
        "宁夏回族自治区": "宁夏", "新疆维吾尔自治区": "新疆",
        "北京市": "北京", "天津市": "天津", "上海市": "上海", "重庆市": "重庆",
    }
    for full_name, short_name in aliases.items():
        if text.startswith(full_name):
            return short_name
    province_match = re.match(r"^(.{2,3})省", text)
    if province_match:
        return province_match.group(1)
    return re.sub(r"省$", "", text)


def _extract_departments(text):
    values = []
    pattern = re.compile(
        r"(?:使用|需求|申请|申购|采购|项目|负责|归口)?科室\s*[：:]\s*"
        r"([^\n。；;，,|]{2,40})"
    )
    for match in pattern.finditer(text or ""):
        value = compact_text(match.group(1)).strip("：: -")
        value = re.split(r"\s+(?:采购|预算|项目|联系人|联系电话|地址|截止)", value, maxsplit=1)[0]
        if value and value not in values:
            values.append(value)
    return "、".join(values)


def parse_detail_page(html, url, search_item=None):
    if "您的访问过于频繁" in html:
        raise CCGPError("详情页返回访问频繁页")
    parser = DetailParser()
    parser.feed(html)
    full_text = "\n".join(
        line for line in (compact_text(part) for part in "".join(parser.text_parts).splitlines()) if line
    )
    title = compact_text(parser.meta.get("ArticleTitle") or _table_value(parser.rows, "采购项目名称"))
    search_item = search_item or {}
    fields = {}
    evidence = {}

    def put(field, value, proof):
        value = compact_text(value)
        if value:
            fields[field] = value
            evidence[field] = compact_text(proof)

    buyer = _table_value(parser.rows, "采购单位") or search_item.get("buyer")
    put("单位", buyer, f"采购单位：{buyer}" if buyer else "")
    province_raw = _table_value(parser.rows, "行政区域") or search_item.get("province")
    province = _province_short(province_raw)
    put("地区", province_raw, f"行政区域：{province_raw}" if province_raw else "")
    put("所属省/市", province, f"行政区域：{province_raw}" if province_raw else "")
    published = _table_value(parser.rows, "公告时间") or search_item.get("publish_time")
    published_iso = _iso_datetime(published)
    put("发布时间", published_iso[:10], f"公告时间：{published}" if published else "")

    project_match = re.search(
        r"(?:项目编号|采购项目编号|招标编号|项目编码)\s*[：:]\s*"
        r"([A-Za-z0-9][A-Za-z0-9._()（）/\-]{2,80})",
        full_text,
        re.I,
    )
    if project_match:
        put("项目编号", project_match.group(1), project_match.group(0))

    method_match = re.search(
        r"采购方式\s*[：:]\s*(公开招标|邀请招标|竞争性磋商|竞争性谈判|询价|单一来源|框架协议|电子卖场|其他)",
        full_text,
    )
    if method_match:
        put("采购方式", method_match.group(1), method_match.group(0))

    deadline_match = re.search(
        r"(?:提交投标文件截止时间|响应文件提交截止时间|投标截止时间|响应文件开启时间)\s*[：:]\s*"
        r"(20\d{2}[^\n]{0,30}?(?:[0-2]?\d\s*[:时]\s*[0-5]\d(?:\s*分)?))",
        full_text,
    )
    if deadline_match:
        put("截止时间", _iso_datetime(deadline_match.group(1)), deadline_match.group(0))

    budget_text = _table_value(parser.rows, "预算金额")
    if not budget_text:
        budget_match = re.search(r"预算金额(?:（元）|\(元\))?\s*[：:]?\s*[￥¥]?[\d,.]+\s*(?:万元|元)?", full_text)
        budget_text = budget_match.group(0) if budget_match else ""
    budget = _budget_yuan(budget_text)
    put("预算", budget, f"预算金额：{budget_text}" if budget_text else "")
    notice_type = search_item.get("notice_type")
    put("公告类型", notice_type, f"检索栏目：{notice_type}" if notice_type else "")
    departments = _extract_departments(full_text)
    put("科室", departments, f"正文明确科室：{departments}" if departments else "")

    attachments = []
    for anchor in parser.anchors:
        absolute = canonical_url(urljoin(url, anchor["href"]))
        suffix = Path(urlsplit(absolute).path).suffix.lower()
        if suffix not in ATTACHMENT_EXTENSIONS:
            continue
        attachments.append({"name": anchor.get("text") or Path(urlsplit(absolute).path).name, "url": absolute})

    return {
        "title": title or search_item.get("title") or "",
        "content": full_text,
        "source_fields": fields,
        "field_evidence": evidence,
        "attachments": attachments,
    }


def collect(client, queries, start, end, max_pages_per_query=100):
    by_url = {}
    failures = []
    raw_count = 0
    stop_source = False
    for query in queries:
        page = 1
        pages = 1
        while page <= pages and page <= max_pages_per_query:
            try:
                html = client.get(build_search_url(query, start, end, page))
                results, _, pages = parse_search_page(html)
            except CCGPError as exc:
                failures.append({"query": query, "page": page, "error": str(exc)})
                if "访问频繁" in str(exc) or "缺少预期结构" in str(exc):
                    stop_source = True
                break
            raw_count += len(results)
            for item in results:
                url = canonical_url(item["url"])
                if url not in by_url:
                    by_url[url] = item | {"found_by_source_query": []}
                hit = {"source": "ccgp", "query": query}
                if hit not in by_url[url]["found_by_source_query"]:
                    by_url[url]["found_by_source_query"].append(hit)
            page += 1
        if pages > max_pages_per_query:
            failures.append({
                "query": query,
                "error": f"结果共{pages}页，超过安全上限{max_pages_per_query}页；该query未完整收集",
            })
        if stop_source:
            break

    candidates = []
    for url, item in by_url.items():
        try:
            detail_html = client.get(url, referer=SEARCH_ENDPOINT)
            detail = parse_detail_page(detail_html, url, item)
            verified = True
        except CCGPError as exc:
            failures.append({"url": url, "error": str(exc)})
            detail = {
                "title": item["title"], "content": "", "source_fields": {},
                "field_evidence": {}, "attachments": [],
            }
            verified = False
        candidates.append({
            "title": detail["title"] or item["title"],
            "site_name": "中国政府采购网",
            "url": url,
            "publish_time": item.get("publish_time") or detail["source_fields"].get("发布时间", ""),
            "auth_info_level": 4,
            "auth_info_des": "官方权威",
            "rank_score": None,
            "summary": item.get("summary") or "",
            "content": detail["content"],
            "source_fields": detail["source_fields"],
            "field_evidence": detail["field_evidence"],
            "attachments": detail["attachments"],
            "found_by_query": [],
            "found_by_source_query": item["found_by_source_query"],
            "source": "ccgp",
            "sources": ["ccgp"],
            "date_authoritative": True,
            "retrieval_verified": verified,
            "content_access": "public_full" if verified else "metadata_only",
        })
    return candidates, failures, raw_count


def main():
    parser = argparse.ArgumentParser(description="中国政府采购网普通 HTTP 检索适配器")
    parser.add_argument("--query", help="单个即席产品词")
    parser.add_argument("--queries", help="逗号分隔的单词 Query；默认读取 references/ccgp.md")
    parser.add_argument("--time-range", default="72h", help="72h / 3d / YYYY-MM-DD..YYYY-MM-DD")
    parser.add_argument("--out-dir", help="输出目录；默认 .tmp/search/<日期>/.sources/ccgp")
    parser.add_argument("--delay", type=float, default=2.0, help="同域请求间隔秒数，默认2")
    parser.add_argument("--max-pages-per-query", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        start, end = parse_time_range(args.time_range)
        if args.query:
            queries = [args.query.strip()]
        elif args.queries:
            queries = [item.strip() for item in args.queries.split(",") if item.strip()]
        else:
            queries = parse_query_list()
        if not queries:
            raise CCGPError("Query 不能为空")
        if args.max_pages_per_query < 1:
            raise CCGPError("--max-pages-per-query 必须大于0")
        if args.dry_run:
            print(json.dumps({
                "source": "ccgp", "queries": queries,
                "start": start.isoformat(), "end": end.isoformat(),
                "sample_url": build_search_url(queries[0], start, end, 1),
            }, ensure_ascii=False, indent=2))
            return 0

        out_dir = Path(args.out_dir) if args.out_dir else ROOT / ".tmp" / "search" / date.today().isoformat() / ".sources" / "ccgp"
        out_dir.mkdir(parents=True, exist_ok=True)
        started = time.time()
        candidates, failures, raw_count = collect(
            CCGPClient(delay=args.delay), queries, start, end, args.max_pages_per_query
        )
        index = write_candidates(candidates, out_dir, date.today().isoformat())
        summary = {
            "schema_version": 1,
            "source": "ccgp",
            "run_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "time_range": f"{start.isoformat()}..{end.isoformat()}",
            "query_count": len(queries),
            "query_failed": len({row.get("query") for row in failures if row.get("query")}),
            "raw_result_count": raw_count,
            "candidate_count": len(index),
            "failures": failures,
        }
        (out_dir / "search_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(
            f"CCGP：{len(queries)} 个单词 Query，原始 {raw_count} 条，"
            f"按公告 URL 去重后 {len(index)} 条，失败 {len(failures)} 项，耗时 {time.time() - started:.1f}s"
        )
        print(f"落盘：{out_dir}")
        return 0 if not failures or candidates else 2
    except CCGPError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
