#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""豆包搜索 Custom 版直连调用器（替代第三方 MCP）——tender-intel 阶段 1。

相对第三方 MCP 补齐的能力：
  - TimeRange 闭区间（YYYY-MM-DD..YYYY-MM-DD），MCP 只有滚动窗口
  - Sites 站点白名单（≤20）/ BlockHosts 屏蔽域名（≤5），MCP 未暴露
  - Count 上限 50（MCP 卡在 20）
  - NeedUrl / NeedContent / Industry / ContentFormats
  - AuthInfoLevel 用官方语义（0 不限制 / 1 仅非常权威），不是 MCP 自造的 1~4

顺带把阶段 1 的机械活接过来（见 SKILL.md 阶段 1、keywords.md §12）：
  - 执行前校验：query 条数连续、每条带意图词、长度 ≤100 字符
  - 跨 query 按 URL 去重，生成 found_by_query
  - 写 data/query_stats.json 的 raw / unique（pushed 由推送阶段回填）

用法:
  python scripts/doubao_search.py                          # 跑 keywords.md §5 全量清单
  python scripts/doubao_search.py --queries 1-4            # 只跑 1~4 条（定向查）
  python scripts/doubao_search.py --query "过敏原 采购公告" # 单条即席查询
  python scripts/doubao_search.py --time-range 2026-08-01..2026-08-10   # 补采指定区间
  python scripts/doubao_search.py --sites ccgp.gov.cn,zfcg.henan.gov.cn # 限定站点
  python scripts/doubao_search.py --dry-run                # 只校验清单与参数，不发请求

退出码: 0 成功 / 1 配置或参数错误 / 2 清单校验不通过 / 3 API 额度或鉴权失败
"""
import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from search_common import canonical_url

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
KEYWORDS_MD = ROOT / "references" / "keywords.md"
CONFIG_FILE = ROOT / "config" / "doubao.json"
STATS_FILE = ROOT / "data" / "query_stats.json"
DEFAULT_ENDPOINT = "https://open.feedcoopapi.com/search_api/web_search"

# 官方账号维度默认 5 QPS，留一档余量
QPS = 4.0
MAX_RETRY = 3

# keywords.md §4 意图词库。按长度倒序匹配，避免"采购公告"被"采购"抢先命中
INTENT_WORDS = sorted(
    ["招标公告", "采购公告", "询价公告", "磋商公告", "谈判公告", "采购意向", "招标", "采购"],
    key=len,
    reverse=True,
)

# 额度/鉴权类错误：立即中止，不要把剩余 query 全打成失败
FATAL_CODES = {10401, 10403, 10406, 10409, 10410, 10412}
RETRY_CODES = {10500, 700429}


class Fatal(Exception):
    pass


# ---------------------------------------------------------------- 配置

def load_api_key(cfg):
    key = os.environ.get("DOUBAO_SEARCH_API_KEY")
    if key:
        return key.strip(), "环境变量 DOUBAO_SEARCH_API_KEY"
    key = (cfg.get("api_key") or "").strip()
    if key and not key.startswith("填入"):
        return key, str(CONFIG_FILE.relative_to(ROOT))
    raise Fatal(
        "找不到 API Key。设置环境变量 DOUBAO_SEARCH_API_KEY，"
        f"或复制 config/doubao.example.json 为 {CONFIG_FILE.relative_to(ROOT)} 并填入 api_key。"
    )


def load_config():
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    return {}


# 优先级：命令行 > config/doubao.json 的 defaults > 这里的兜底值。
# 命令行未给的参数在 argparse 里是 None，据此区分"没给"和"显式给了 0/false"。
FALLBACK = {
    "count": 20,
    "time_range": "72h",
    "auth_level": 0,
    "need_url": 1,
    "need_content": 0,
    "sites": None,
    "block_hosts": None,
    "report_limit": 20,
}


def resolve_defaults(args, cfg):
    d = cfg.get("defaults") or {}
    for name, fallback in FALLBACK.items():
        if getattr(args, name) is not None:
            continue
        val = d.get(name, fallback)
        if name in ("sites", "block_hosts") and isinstance(val, list):
            val = ",".join(val) if val else None
        if name in ("need_url", "need_content") and isinstance(val, bool):
            val = int(val)
        setattr(args, name, val)
    return args


# ---------------------------------------------------------------- 清单

def parse_query_list():
    """从 keywords.md §5 解析每日 Query 清单。清单即配置，避免脚本与文档各存一份导致漂移。"""
    if not KEYWORDS_MD.exists():
        raise Fatal(f"找不到 {KEYWORDS_MD}")
    text = KEYWORDS_MD.read_text(encoding="utf-8")
    marker = "## 5. 每日 Query 清单"
    if marker not in text:
        raise Fatal(f"keywords.md 里找不到「{marker}」小节，无法解析清单")
    section = text.split(marker, 1)[1].split("\n## ", 1)[0]
    items = re.findall(r"^(\d+)\.\s+(\S.*?)\s*$", section, re.M)
    return [(int(n), q) for n, q in items]


def validate_queries(queries, expected_total=None, full_list=False):
    """执行前校验（SKILL.md 阶段 1）。返回问题列表，空列表即通过。

    编号连续性与条数只在跑全量清单时校验——子集选择（--queries 1-4）本来就不连续。
    意图词与长度对每条都查，子集同样适用。
    """
    problems = []
    nums = [n for n, _ in queries]
    if not nums:
        problems.append("清单为空，未解析到任何 query")
        return problems
    if full_list:
        if nums != list(range(1, len(nums) + 1)):
            missing = [i for i in range(1, max(nums) + 1) if i not in nums]
            problems.append(f"编号不连续：共 {len(nums)} 条，缺号 {missing or '无'}，最大编号 {max(nums)}")
        if expected_total and len(nums) != expected_total:
            problems.append(f"条数不符：解析到 {len(nums)} 条，keywords.md §0 声明 {expected_total} 条")
    for n, q in queries:
        if not any(q.endswith(w) for w in INTENT_WORDS):
            problems.append(f"#{n} 末尾缺意图词：{q}")
        if len(q) > 100:
            problems.append(f"#{n} 超过 API 的 100 字符上限（{len(q)} 字符），末尾意图词会被静默截断：{q}")
    return problems


def declared_total():
    """读 keywords.md §0 声明的每日条数，用于交叉核对解析结果。"""
    text = KEYWORDS_MD.read_text(encoding="utf-8")
    m = re.search(r"每日按「§5 每日 Query 清单」执行 \*\*(\d+)\s*条\*\*", text)
    return int(m.group(1)) if m else None


def select_queries(all_queries, spec):
    """--queries 1-4,39 这类选择表达式。"""
    if not spec or spec == "all":
        return all_queries
    wanted = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            wanted.update(range(int(a), int(b) + 1))
        elif part:
            wanted.add(int(part))
    picked = [(n, q) for n, q in all_queries if n in wanted]
    missing = wanted - {n for n, _ in picked}
    if missing:
        raise Fatal(f"--queries 指定的编号不存在：{sorted(missing)}")
    return picked


# ---------------------------------------------------------------- 参数

def build_time_range(value):
    """72h/3d → 以今天为终点的滚动窗口；枚举和日期闭区间原样透传。"""
    if not value:
        return None
    value = value.strip()
    if value in {"OneDay", "OneWeek", "OneMonth", "OneYear"}:
        return value
    m = re.fullmatch(r"(\d+)h", value)
    if m:
        hours = int(m.group(1))
        if hours < 1:
            raise Fatal(f"--time-range 小时数必须大于0：{value}")
        days = (hours + 23) // 24
        end = date.today()
        start = end - timedelta(days=days)
        return f"{start.isoformat()}..{end.isoformat()}"
    m = re.fullmatch(r"(\d+)d", value)
    if m:
        days = int(m.group(1))
        end = date.today()
        start = end - timedelta(days=days)
        return f"{start.isoformat()}..{end.isoformat()}"
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})", value)
    if m:
        a, b = m.group(1), m.group(2)
        if a > b:
            raise Fatal(f"--time-range 区间起点晚于终点：{value}")
        return value
    raise Fatal(
        f"--time-range 取值非法：{value}。"
        "支持 72h/3d（滚动窗口）/ OneDay|OneWeek|OneMonth|OneYear / 2026-08-01..2026-08-10"
    )


def build_payload(query, args, time_range):
    filt = {"NeedUrl": bool(args.need_url), "NeedContent": bool(args.need_content)}
    if args.auth_level:
        filt["AuthInfoLevel"] = int(args.auth_level)
    if args.sites:
        sites = [s.strip() for s in args.sites.split(",") if s.strip()]
        if len(sites) > 20:
            raise Fatal(f"--sites 最多 20 个域名，给了 {len(sites)} 个")
        filt["Sites"] = "|".join(sites)
    if args.block_hosts:
        hosts = [s.strip() for s in args.block_hosts.split(",") if s.strip()]
        if len(hosts) > 5:
            raise Fatal(f"--block-hosts 最多 5 个域名，给了 {len(hosts)} 个")
        filt["BlockHosts"] = "|".join(hosts)

    payload = {
        "Query": query,
        "SearchType": "web",
        "Count": int(args.count),
        "Filter": filt,
        "ContentFormats": args.content_format,
    }
    if time_range:
        payload["TimeRange"] = time_range
    if args.industry:
        payload["Industry"] = args.industry
    return payload


# ---------------------------------------------------------------- 调用

class RateLimiter:
    def __init__(self, qps):
        self.interval = 1.0 / qps
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            sleep_for = max(0.0, self.next_at - now)
            self.next_at = max(now, self.next_at) + self.interval
        if sleep_for:
            time.sleep(sleep_for)


def call_once(endpoint, api_key, payload, limiter):
    limiter.wait()
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        endpoint, data=data, method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=40) as response:
            status = response.getcode()
            raw = response.read()
    except HTTPError as e:
        detail = e.read(200).decode("utf-8", errors="replace")
        return None, f"HTTP {e.code}: {detail}", None
    except (URLError, TimeoutError, OSError) as e:
        return None, f"网络错误: {e}", None
    if status != 200:
        return None, f"HTTP {status}: {raw[:200].decode('utf-8', errors='replace')}", None
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        return None, f"响应JSON无效: {e}", None
    err = (body.get("ResponseMetadata") or {}).get("Error")
    if err:
        code = err.get("CodeN") or err.get("Code")
        try:
            code = int(code)
        except (TypeError, ValueError):
            code = None
        return None, f"[{err.get('Code')}] {err.get('Message')}", code
    return body.get("Result") or {}, None, None


def run_query(endpoint, api_key, num, query, args, time_range, limiter, abort):
    payload = build_payload(query, args, time_range)
    last_err = None
    for attempt in range(1, MAX_RETRY + 1):
        if abort.is_set():
            return {"num": num, "query": query, "error": "已中止（其他 query 触发致命错误）", "results": []}
        result, err, code = call_once(endpoint, api_key, payload, limiter)
        if err is None:
            return {
                "num": num,
                "query": query,
                "error": None,
                "result_count": result.get("ResultCount", 0),
                "time_cost_ms": result.get("TimeCost"),
                "log_id": result.get("LogId"),
                "results": result.get("WebResults") or [],
            }
        last_err = err
        if code in FATAL_CODES:
            abort.set()
            return {"num": num, "query": query, "error": err, "fatal": True, "results": []}
        if code in RETRY_CODES or code is None:
            if attempt < MAX_RETRY:
                time.sleep(1.5 * attempt)
                continue
        break
    return {"num": num, "query": query, "error": last_err, "results": []}


# ---------------------------------------------------------------- 聚合

def dedup_candidates(runs):
    """跨 query 按 URL 去重，生成 found_by_query。同一 URL 被多条命中时全部记上。"""
    by_url = {}
    order = []
    for run in runs:
        for item in run["results"]:
            url = canonical_url(item.get("Url") or "")
            if not url:
                continue
            if url not in by_url:
                by_url[url] = {
                    "title": item.get("Title") or "",
                    "site_name": item.get("SiteName") or "",
                    "url": url,
                    "publish_time": item.get("PublishTime") or "",
                    "auth_info_level": item.get("AuthInfoLevel"),
                    "auth_info_des": item.get("AuthInfoDes") or "",
                    "rank_score": item.get("RankScore"),
                    "summary": item.get("Summary") or item.get("Snippet") or "",
                    "content": item.get("Content") or "",
                    "found_by_query": [],
                    "found_by_source_query": [],
                }
                order.append(url)
            fbq = by_url[url]["found_by_query"]
            if run["num"] not in fbq:
                fbq.append(run["num"])
            source_hit = {
                "source": "doubao",
                "query_number": run["num"],
                "query": run["query"],
            }
            if source_hit not in by_url[url]["found_by_source_query"]:
                by_url[url]["found_by_source_query"].append(source_hit)
    for url in order:
        by_url[url]["found_by_query"].sort()
    return [by_url[u] for u in order]


def candidate_id(url):
    """稳定候选编号；同一 URL 跨运行保持一致，便于按需读取正文。"""
    return "C" + hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()[:12].upper()


def title_fingerprint(title):
    """保守的转载聚类键：只折叠空白和常见标点，不做模糊合并。"""
    return re.sub(r"[\s\W_]+", "", (title or "").lower(), flags=re.UNICODE)


def write_candidate_artifacts(candidates, out_dir, run_date):
    """正文逐条落盘，另写不含全文的轻量索引，禁止模型整体读取正文库。"""
    content_dir = out_dir / "content"
    content_dir.mkdir(parents=True, exist_ok=True)
    index = []
    for item in candidates:
        cid = candidate_id(item["url"])
        content_rel = f"content/{cid}.json"
        full = {
            "candidate_id": cid,
            "title": item["title"],
            "source_url": item["url"],
            "summary": item["summary"],
            "content": item["content"],
            "source_fields": {},
            "field_evidence": {},
            "attachments": [],
            "sources": ["doubao"],
            "alternate_sources": [],
            "found_by_source_query": item["found_by_source_query"],
        }
        (out_dir / content_rel).write_text(
            json.dumps(full, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        teaser_source = item["summary"] or item["content"]
        index.append({
            "candidate_id": cid,
            "title": item["title"],
            "title_fingerprint": title_fingerprint(item["title"]),
            "site_name": item["site_name"],
            "url": item["url"],
            "publish_time": item["publish_time"],
            "auth_info_level": item["auth_info_level"],
            "auth_info_des": item["auth_info_des"],
            "rank_score": item["rank_score"],
            "found_by_query": item["found_by_query"],
            "found_by_source_query": full["found_by_source_query"],
            "source": "doubao",
            "sources": ["doubao"],
            "source_fields": {},
            "field_evidence": {},
            "attachments": [],
            "date_authoritative": False,
            "retrieval_verified": False,
            "alternate_sources": [],
            "teaser": truncate(teaser_source, 240),
            "content_path": content_rel,
        })

    with (out_dir / "candidate_index.jsonl").open("w", encoding="utf-8") as f:
        for item in index:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    # 保留原文件名供旧流程兼容，但内容改为轻量索引，不再复制正文。
    (out_dir / "candidates.json").write_text(
        json.dumps({"run_date": run_date, "count": len(index), "candidates": index},
                   ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return index


def compute_stats(runs, candidates):
    """raw = 该 query 返回条数；unique = 去重后仅由该 query 命中的 URL 数。"""
    unique = {}
    for c in candidates:
        if len(c["found_by_query"]) == 1:
            unique[c["found_by_query"][0]] = unique.get(c["found_by_query"][0], 0) + 1
    return {
        str(run["num"]): {
            "raw": len(run["results"]),
            "unique": unique.get(run["num"], 0),
            "pushed": 0,
        }
        for run in runs
    }


def write_stats(stats, run_date):
    if not STATS_FILE.exists():
        raise Fatal(f"找不到 {STATS_FILE.relative_to(ROOT)}，无法写归因统计")
    data = json.loads(STATS_FILE.read_text(encoding="utf-8"))
    data.setdefault("days", {})[run_date] = stats
    STATS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


# ---------------------------------------------------------------- 输出

def truncate(s, n):
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def report(runs, candidates, stats, out_dir, args, time_range, key_source):
    ok = [r for r in runs if not r["error"]]
    failed = [r for r in runs if r["error"]]
    total_raw = sum(len(r["results"]) for r in runs)

    print(f"Key 来源     : {key_source}")
    print(f"TimeRange    : {time_range or '（不限制）'}")
    print(f"Count/条     : {args.count}   AuthInfoLevel: {args.auth_level}   NeedUrl: {args.need_url}")
    if args.sites:
        print(f"Sites        : {args.sites}")
    if args.block_hosts:
        print(f"BlockHosts   : {args.block_hosts}")
    print(f"执行 query   : {len(runs)} 条（成功 {len(ok)}，失败 {len(failed)}）")
    print(f"原始结果     : {total_raw} 条  →  跨 query 去重后 {len(candidates)} 条候选")
    print(f"落盘         : {out_dir}")

    if failed:
        print("\n失败的 query：")
        for r in failed:
            print(f"  #{r['num']:<3} {truncate(r['query'], 40):<42} {r['error']}")

    zero = [n for n, s in sorted(stats.items(), key=lambda kv: int(kv[0])) if s["unique"] == 0]
    contrib = [
        (int(n), s["unique"])
        for n, s in sorted(stats.items(), key=lambda kv: int(kv[0]))
        if s["unique"] > 0
    ]
    print("\n归因（unique = 仅该条 query 捞到的 URL 数）：")
    print("  有独有贡献 : " + (", ".join(f"#{n}:{u}" for n, u in contrib) or "无"))
    print("  零贡献     : " + (", ".join(f"#{n}" for n in zero) or "无"))
    print("  注：裁撤门槛是连续 14 个运行日 unique 为 0，不要据单日结果增删 query（keywords.md §12）")

    levels = {}
    for c in candidates:
        levels[c["auth_info_des"] or "未知"] = levels.get(c["auth_info_des"] or "未知", 0) + 1
    print("\n候选按信源权威度：" + "，".join(f"{k} {v}" for k, v in sorted(levels.items())))

    limit = len(candidates) if args.report_all else min(args.report_limit, len(candidates))
    print(f"\n候选预览（显示 {limit}/{len(candidates)} 条；完整轻量索引见 candidate_index.jsonl）：")
    for i, c in enumerate(candidates[:limit], 1):
        fbq = ",".join(f"#{n}" for n in c["found_by_query"])
        print(f"{i:>3}. {truncate(c['title'], 48)}")
        print(
            f"     {truncate(c['site_name'], 16):<18} {(c['publish_time'] or '无日期')[:10]:<12}"
            f" {c['auth_info_des'] or '-':<6} {fbq}"
        )
        print(f"     {c['url']}")
    if limit < len(candidates):
        print(f"  … 其余 {len(candidates) - limit} 条未写入 stdout，避免占用模型上下文")


# ---------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(description="豆包搜索 Custom 版直连调用器")
    p.add_argument("--query", help="单条即席查询，绕过 keywords.md 清单")
    p.add_argument("--queries", default="all", help="清单选择：all（默认）或 1-4,39 这类表达式")
    p.add_argument("--time-range",
                   help="72h 滚动窗口（默认）/ 3d / OneDay|OneWeek|OneMonth|OneYear / 2026-08-01..2026-08-10 闭区间 / none 不限制")
    p.add_argument("--count", type=int, help="每条返回数，1~50（默认 20）")
    p.add_argument("--auth-level", type=int, choices=[0, 1],
                   help="官方语义：0 不限制（默认，本管线要的）/ 1 仅非常权威")
    p.add_argument("--sites", help="站点白名单，逗号分隔，最多 20 个完整域名。注意是排他性限定")
    p.add_argument("--block-hosts", help="屏蔽域名，逗号分隔，最多 5 个。默认取 config 里的噪声域名表")
    p.add_argument("--no-block", action="store_true", help="本次不屏蔽任何域名，覆盖 config 默认")
    p.add_argument("--industry", choices=["finance", "game", "gov"], help="行业限定；gov 比『非常权威』更严")
    p.add_argument("--content-format", default="markdown", choices=["text", "markdown"])
    p.add_argument("--need-url", type=int, choices=[0, 1], help="1=只要有落地页URL的结果（默认）")
    p.add_argument("--need-content", type=int, choices=[0, 1], help="1=只要有正文的结果")
    p.add_argument("--out-dir", help="落盘目录，默认 .tmp/search/<日期>")
    p.add_argument("--report-limit", type=int, help="stdout 最多预览候选数（默认 20）")
    p.add_argument("--report-all", action="store_true", help="打印全部候选；模型运行时禁止使用")
    p.add_argument("--no-stats", action="store_true", help="不写 data/query_stats.json")
    p.add_argument("--dry-run", action="store_true", help="只校验清单与参数，不发请求")
    args = p.parse_args()

    try:
        cfg = load_config()
        args = resolve_defaults(args, cfg)
        if args.no_block:
            args.block_hosts = None

        if not 1 <= args.count <= 50:
            print(f"错误：--count 需在 1~50 之间（官方上限 50），给了 {args.count}", file=sys.stderr)
            return 1
        if args.report_limit < 0:
            print(f"错误：--report-limit 不能小于 0，给了 {args.report_limit}", file=sys.stderr)
            return 1

        time_range = None if args.time_range == "none" else build_time_range(args.time_range)

        if args.query:
            queries = [(0, args.query.strip())]
            problems = validate_queries([(1, args.query.strip())])
        else:
            full = args.queries in (None, "", "all")
            queries = select_queries(parse_query_list(), args.queries)
            problems = validate_queries(
                queries, declared_total() if full else None, full_list=full
            )

        if problems:
            print("清单校验不通过：", file=sys.stderr)
            for x in problems:
                print(f"  - {x}", file=sys.stderr)
            if not args.query:
                return 2
            print("（即席查询，仅告警不中止）", file=sys.stderr)

        if args.dry_run:
            print(f"DRY-RUN（未发请求）  TimeRange={time_range or '不限制'}  Count={args.count}")
            print(f"清单校验通过：{len(queries)} 条")
            sample = build_payload(queries[0][1], args, time_range)
            print("首条载荷：\n" + json.dumps(sample, ensure_ascii=False, indent=2))
            return 0

        api_key, key_source = load_api_key(cfg)
        endpoint = cfg.get("endpoint") or DEFAULT_ENDPOINT

        limiter = RateLimiter(QPS)
        abort = threading.Event()
        started = time.time()
        with ThreadPoolExecutor(max_workers=4) as pool:
            runs = list(pool.map(
                lambda nq: run_query(endpoint, api_key, nq[0], nq[1],
                                     args, time_range, limiter, abort),
                queries,
            ))
        runs.sort(key=lambda r: r["num"])
        elapsed = time.time() - started

        fatal = [r for r in runs if r.get("fatal")]
        candidates = dedup_candidates(runs)
        stats = compute_stats(runs, candidates)

        run_date = date.today().isoformat()
        out_dir = Path(args.out_dir) if args.out_dir else ROOT / ".tmp" / "search" / run_date
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "raw.json").write_text(
            json.dumps({"run_at": datetime.now().isoformat(timespec="seconds"),
                        "time_range": time_range, "count": args.count,
                        "auth_level": args.auth_level, "runs": runs},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        lightweight_candidates = write_candidate_artifacts(candidates, out_dir, run_date)
        failed_runs = [r for r in runs if r.get("error")]
        (out_dir / "search_summary.json").write_text(
            json.dumps({
                "schema_version": 1,
                "run_date": run_date,
                "time_range": time_range,
                "query_count": len(runs),
                "query_succeeded": len(runs) - len(failed_runs),
                "query_failed": len(failed_runs),
                "failures": [
                    {"num": r["num"], "query": r["query"], "error": r["error"]}
                    for r in failed_runs
                ],
                "raw_result_count": sum(len(r["results"]) for r in runs),
                "candidate_count": len(lightweight_candidates),
                "query_stats": stats,
            }, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        if not args.no_stats and not args.query:
            write_stats(stats, run_date)

        report(runs, lightweight_candidates, stats, out_dir, args, time_range, key_source)
        print(f"\n耗时 {elapsed:.1f}s")

        if fatal:
            print("\n致命错误，已中止剩余 query：", file=sys.stderr)
            for r in fatal:
                print(f"  #{r['num']} {r['error']}", file=sys.stderr)
            return 3
        return 0

    except Fatal as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
