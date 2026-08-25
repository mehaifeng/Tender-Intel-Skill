#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""医院官网检索适配器（hosp）——tender-intel 可插拔检索层的第四个来源。

打的是 CCGP 与 PLAP 结构上拿不到的那部分：医院自己发布的院内遴选、询比、
议价、竞价、供应商征集和市场调研。这类采购不走政府采购流程，只挂在医院官网。

实现方式是复用豆包搜索 API 的 Sites 白名单：域名清单来自
data/hospital_sites.min.json.gz，按 20 个一批轮转（API 上限）。Query 只用
少量宽词——医院单站公告量很小，精准词的价值在聚合站那种海量池子里筛，
这里上 30 个词纯属浪费额度。

选域规则见 references/hospital_sites.md：按 db>0 选（豆包索引到过招采页），
**不看 s（HTTP 可达性）**——两者是独立信号，27 个 HTTP 抓不到的域名豆包仍有产出。

用法:
  python scripts/hosp_search.py                          # 默认全量 db>0 域名
  python scripts/hosp_search.py --min-target 1           # 只打有检验类命中的域名（更省）
  python scripts/hosp_search.py --queries 招标,试剂        # 自定义宽词
  python scripts/hosp_search.py --dry-run                # 只算批次与成本，不发请求

退出码: 0 成功 / 1 配置或参数错误 / 2 无可用域名或全部批次失败 / 3 API 额度或鉴权失败
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from search_common import canonical_url, compact_text, target_category_signals, write_candidates
import doubao_search as db

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SITES_INDEX = ROOT / "data" / "hospital_sites.min.json.gz"
SITES_PER_CALL = 20                     # 官方 Sites 上限
DEFAULT_QUERIES = ["招标", "试剂"]
QPS = 4.0

# 医院自有站是公告的原始发布方，优先于豆包转载与聚合站；但低于 CCGP/PLAP——
# 后者会抓取完整详情页，这里只有搜索索引给的标题与摘要。
SOURCE_PRIORITY = 380

# 与 keywords.md §10.1 一致：命中即判无关，优先级高于品类信号。
# 只列「同名异域」——即与本司品类共用词汇但完全是另一回事的场景。
# 不列物业/食堂/装修这类，它们本来就过不了品类关，多写一层反而误伤
# （「免疫分析仪维修保养」曾被 维修 误杀）。
EXCLUDE = re.compile(
    r"酶标仪|电泳|洗板机|兽医|兽用|畜牧|生猪|结核|干扰素释放|免疫组化|重组蛋白|"
    r"培养基|缓冲液|核酸提取|PCR|测序",
    re.I,
)
INTENT = re.compile(
    r"招标|采购|遴选|询价|询比|磋商|谈判|竞价|竞采|议价|中标|成交|公示|征集|调研|意向|入围|合同"
)
# 采购标的：真公告必须说明买什么。缺了它就只剩「招标 + 机构名」，那是栏目页。
OBJECT = re.compile(
    r"试剂|耗材|设备|仪器|仪|系统|装置|器械|材料|药品|服务|工程|项目|软件|平台|"
    r"检测|检验|诊断|分析|培训|维保|维修|租赁|配送|供应商|第\s*[0-9一二三四五六七八九十]{1,3}\s*[批次期包标]"
)
# 站点导航词，出现即基本可判定是栏目页
NAV = re.compile(r"报名站点|栏目|列表|首页|更多|下一页|共\s*\d+\s*页")


def is_channel_page(title):
    """栏目/列表页而非单条公告。

    医院站的招采频道首页标题就叫「招标公告-龙岩市第二医院」「招标-四川省儿童医院」，
    会被 INTENT 放行，但页面上没有任何可核实字段，进队列只会浪费核实预算。

    判据是**结构**不是长度：栏目页只有意图词加机构名，说不出买什么；真公告一定有
    采购标的。长度分不开二者——「免疫印迹仪采购公告」才 9 个字却是真公告，
    「川投西昌医院- 招标采购- 招采公告」有 18 个字却是栏目页。
    """
    t = (title or "").strip()
    if NAV.search(t):
        return True
    return not OBJECT.search(t)
# 宽线索档（keywords.md §11 方案 B）：整批检验试剂/耗材招采，目标品类常在清单里而不在标题。
# 医院院内遴选大量是这种形态——「2026年第三批医用试剂耗材遴选公告」——只认精准品类会漏掉绝大部分。
BROAD_LEAD = re.compile(
    r"检验试剂|试剂耗材|医用试剂|医用耗材|体外诊断|检验科|医学检验|检验设备|生化免疫|"
    r"试剂.{0,6}(?:遴选|采购|招标|入围|批|供应商|配送)|(?:遴选|采购|招标).{0,6}试剂",
    re.I,
)


class HospError(Exception):
    pass


# ---------------------------------------------------------------- 域名清单

def load_sites(min_db=1, min_target=0, limit=None):
    """从索引挑白名单域名。按 tg / db 降序，保证额度优先花在高产站上。"""
    if not SITES_INDEX.exists():
        raise HospError(f"找不到 {SITES_INDEX.relative_to(ROOT)}，无法构造白名单")
    with gzip.open(SITES_INDEX, "rt", encoding="utf-8") as handle:
        doc = json.load(handle)
    picked, seen = [], set()
    for rec in doc.get("records", []):
        if rec.get("bad"):
            continue                     # 被抢注/过期/第三方目录，内容不可信
        host = (rec.get("h") or "").strip().lower()
        if not host or host in seen:
            continue
        if rec.get("db", 0) < min_db or rec.get("tg", 0) < min_target:
            continue
        seen.add(host)
        picked.append(rec)
    picked.sort(key=lambda r: (-r.get("tg", 0), -r.get("db", 0)))
    if limit:
        picked = picked[:limit]
    return picked


def batches(hosts, size=SITES_PER_CALL):
    for i in range(0, len(hosts), size):
        yield hosts[i:i + size]


# ---------------------------------------------------------------- 调用

def call(endpoint, api_key, payload, limiter):
    limiter.wait()
    request = Request(
        endpoint, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    last = ""
    for attempt in range(3):
        try:
            body = json.loads(urlopen(request, timeout=60).read().decode("utf-8"))
        except Exception as exc:                      # noqa: BLE001 - 网络异常统一重试
            last = f"{type(exc).__name__}: {str(exc)[:60]}"
            time.sleep(1.5 * (attempt + 1))
            continue
        error = (body.get("ResponseMetadata") or {}).get("Error")
        if error:
            code = error.get("CodeN")
            if code in db.FATAL_CODES:
                raise db.Fatal(f"额度或鉴权失败（{code}）：{error.get('Message')}")
            if code in db.RETRY_CODES:
                last = f"{code}: {error.get('Message')}"
                time.sleep(2.0 * (attempt + 1))
                continue
            return None, f"{code}: {error.get('Message')}"
        return (body.get("Result") or {}).get("WebResults") or [], None
    return None, last or "重试耗尽"


def build_payload(query, hosts, time_range, count):
    return {
        "Query": query,
        "SearchType": "web",
        "Count": int(count),
        "Filter": {
            "NeedUrl": True,
            "NeedContent": False,
            "Sites": "|".join(hosts),
            # AuthInfoLevel 保持 0：设 1 会把非央媒站整体滤掉（见 references/doubao.md）
        },
        "ContentFormats": "markdown",
        "TimeRange": time_range,
        "QueryControl": {"QueryRewrite": True},
        # 不设 Industry：与 Sites 互斥，叠加后结果归零
    }


# ---------------------------------------------------------------- 候选

def _name_corroborated(name, blob):
    """索引里的医院名能否被公告文本印证。

    要求全名出现，或去掉通用后缀后的判别性词（地名/机构特征）全部出现——
    只匹配「中医院」这种通用词会把同类医院互相认错。
    """
    if not name or not blob:
        return False
    if name in blob:
        return True
    core = re.sub(r'[（(].*?[)）]', '', name)
    core = re.sub(r'(附属|人民|中心|医院|医疗|集团|分院|院区|保健院|中医|中西医结合|'
                  r'第[一二三四五六七八九十]+)', ' ', core)
    parts = [p for p in re.split(r'\s+', core) if len(p) >= 2]
    return bool(parts) and all(p in blob for p in parts)


def to_candidate(item, query, host_to_name):
    url = canonical_url(item.get("Url") or "")
    if not url:
        return None
    title = item.get("Title") or ""
    summary = item.get("Summary") or item.get("Snippet") or ""
    content = item.get("Content") or ""
    blob = f"{title} {summary}"
    if EXCLUDE.search(blob) or not INTENT.search(title) or is_channel_page(title):
        return None
    signals = target_category_signals(blob)
    if signals:
        lead = "precise"
    elif BROAD_LEAD.search(blob):
        lead = "broad"                    # 整批试剂耗材招采，目标品类待核实阶段开清单确认
    else:
        return None
    host = urlsplit(url).netloc.lower()
    name = host_to_name.get(host)
    fields = {}
    # 域名反查医院名只在标题/摘要能印证时才写入 source_fields。
    # 拼音缩写高度歧义——sxzyy.cn 既像「山西中医院」也像「绍兴中医院」，索引标的是前者，
    # 实际公告来自后者。写错采购人比留空更糟，留空时管线会填 "null"，由核实阶段读原文确认。
    if name and _name_corroborated(name, blob):
        fields["单位"] = name
    return {
        "title": title,
        "url": url,
        "site_name": item.get("SiteName") or name or host,
        "publish_time": item.get("PublishTime") or "",
        "summary": compact_text(summary, 2000),
        "content": content,
        "auth_info_level": item.get("AuthInfoLevel"),
        "auth_info_des": item.get("AuthInfoDes") or "",
        "rank_score": item.get("RankScore"),
        "source": "hosp",
        "sources": ["hosp"],
        "source_fields": fields,
        "field_evidence": {},
        "found_by_source_query": [{"source": "hosp", "query": query}],
        # 搜索索引给的是标题+摘要，没抓详情页
        "content_access": "public_partial" if summary or content else "metadata_only",
        "date_authoritative": False,      # PublishTime 是网页元数据，不是公告发布日
        "retrieval_verified": False,
        "source_priority": SOURCE_PRIORITY,
        "lead_tier": lead,                # precise=标题即命中品类；broad=整批招采，需开清单核实
    }


def collect(endpoint, api_key, sites, queries, time_range, count, window):
    limiter = db.RateLimiter(QPS)
    host_to_name = {r["h"].lower(): r["n"] for r in sites}
    hosts = [r["h"] for r in sites]
    by_url, order = {}, []
    raw, calls, failures, dropped_filter = 0, 0, [], 0

    for batch_no, batch in enumerate(batches(hosts), 1):
        for query in queries:
            results, err = call(endpoint, api_key,
                                build_payload(query, batch, time_range, count), limiter)
            calls += 1
            if err:
                failures.append({"batch": batch_no, "query": query, "error": err})
                continue
            for item in results:
                raw += 1
                cand = to_candidate(item, query, host_to_name)
                if cand is None:
                    dropped_filter += 1
                    continue
                url = cand["url"]
                if url in by_url:
                    hit = {"source": "hosp", "query": query}
                    if hit not in by_url[url]["found_by_source_query"]:
                        by_url[url]["found_by_source_query"].append(hit)
                    continue
                by_url[url] = cand
                order.append(url)

    candidates = [by_url[u] for u in order]
    kept, date_dropped = db.filter_by_publish_date(candidates, window)
    return kept, raw, calls, failures, dropped_filter, date_dropped


# ---------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser(description="医院官网检索适配器（豆包 Sites 白名单）")
    parser.add_argument("--time-range", default="72h",
                        help="72h / 3d / OneWeek / YYYY-MM-DD..YYYY-MM-DD")
    parser.add_argument("--out-dir", help="候选目录；默认 .tmp/search/<日期>")
    parser.add_argument("--queries", help="逗号分隔的宽词；默认 招标,试剂")
    parser.add_argument("--min-db", type=int, default=1,
                        help="域名最低豆包招采产出数，默认 1（即普查中出过招采页）")
    parser.add_argument("--min-target", type=int, default=0,
                        help="域名最低检验类命中数；设 1 只打高相关站，省一半额度但会漏周期性买家")
    parser.add_argument("--limit-sites", type=int, help="只取前 N 个域名（按 tg/db 降序），用于控成本")
    parser.add_argument("--count", type=int, default=50, help="每次返回数上限，1~50")
    parser.add_argument("--dry-run", action="store_true", help="只算批次与成本，不发请求")
    args = parser.parse_args()

    try:
        queries = ([q.strip() for q in args.queries.split(",") if q.strip()]
                   if args.queries else list(DEFAULT_QUERIES))
        if not queries:
            raise HospError("至少要有一个 Query")
        bad = [q for q in queries if re.search(r"\s", q)]
        if bad:
            raise HospError(f"Query 不得含空格（官方不支持多词搜索）：{bad}")

        sites = load_sites(args.min_db, args.min_target, args.limit_sites)
        if not sites:
            raise HospError(f"没有符合条件的域名（min-db={args.min_db} min-target={args.min_target}）")

        batch_count = (len(sites) + SITES_PER_CALL - 1) // SITES_PER_CALL
        calls = batch_count * len(queries)
        print(f"医院域名 {len(sites)} 个 → {batch_count} 批 × {len(queries)} 词 = {calls} 次调用"
              f"（约 {calls * 0.01:.2f} 元/次运行，{calls * 0.01 * 30:.1f} 元/月）")

        api_time_range = db.api_time_range(args.time_range)
        window = db.local_window(args.time_range, None)
        if args.dry_run:
            print(f"DRY-RUN（未发请求）  TimeRange={api_time_range}  "
                  f"本地窗口={window[0]}~{window[1] if window else '不限'}")
            print(f"首批域名：{[r['h'] for r in sites[:SITES_PER_CALL]][:5]} …")
            return 0

        cfg = db.load_config()
        api_key, key_src = db.load_api_key(cfg)
        endpoint = cfg.get("endpoint") or db.DEFAULT_ENDPOINT
        print(f"Key 来源：{key_src}   TimeRange：{api_time_range}")

        out_dir = Path(args.out_dir) if args.out_dir else ROOT / ".tmp" / "search" / date.today().isoformat()
        out_dir.mkdir(parents=True, exist_ok=True)
        started = time.time()
        candidates, raw, calls_made, failures, dropped, date_dropped = collect(
            endpoint, api_key, sites, queries, api_time_range, args.count, window
        )
        index = write_candidates(candidates, out_dir, date.today().isoformat())

        summary = {
            "schema_version": 1,
            "source": "hosp",
            "run_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "time_range": args.time_range,
            "api_time_range": api_time_range,
            "site_count": len(sites),
            "batch_count": batch_count,
            "query_count": len(queries),
            "queries": queries,
            "api_calls": calls_made,
            "cost_yuan": round(calls_made * 0.01, 2),
            "query_failed": len(failures),
            "raw_result_count": raw,
            "prefilter_excluded": dropped,
            "date_filtered": date_dropped,
            "candidate_count": len(index),
            "lead_tier": {
                "precise": sum(1 for c in candidates if c.get("lead_tier") == "precise"),
                "broad": sum(1 for c in candidates if c.get("lead_tier") == "broad"),
            },
            "failures": failures,
        }
        (out_dir / "search_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"HOSP：{len(sites)} 个医院域名 / {batch_count} 批，{calls_made} 次调用"
              f"（{calls_made * 0.01:.2f} 元），原始 {raw} 条，"
              f"预筛排除 {dropped} 条，窗口外 {date_dropped} 条，"
              f"最终 {len(index)} 条候选，失败 {len(failures)} 项，耗时 {time.time() - started:.1f}s")
        print(f"落盘：{out_dir}")
        return 0 if (index or not failures) else 2

    except db.Fatal as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 3
    except HospError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
