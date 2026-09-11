#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IVD Bid Radar 运行报告：把一轮运行已经落盘的东西渲染成一页可读的 HTML。

    python scripts/run_report.py --run-dir <检索目录>              # 本轮漏斗报告
    python scripts/run_report.py --compare <基线目录> <当前目录>    # 两轮归宿 diff

只读取本地运行数据：不发任何请求、不改管线状态、不碰飞书；唯一写入是报告 HTML。
所以在任何时刻、对任何一轮（哪怕跑挂了）都可以反复生成。

diff 的用法是回归：改了关键词、闸门或阈值之后，在两个隔离的代码副本中用同一批候选
和同一份台账快照执行 `prepare --force`（零检索开销），再比较两份运行目录，逐条看清
这次改动捞回了什么、又误杀了什么。

标题、采购人、摘要都是不可信数据，一律转义后输出；链接只放行 http/https。
"""

from __future__ import annotations

import argparse
import html
import json
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# prepare() 里闸门的实际先后顺序。报告的漏斗必须跟它一致，否则「丢在哪一关」是错的。
DROP_STAGES = [
    ("already_seen", "台账已入账"),
    ("semantic", "扣住待语义判定"),
    ("title_exclude", "标题硬排除"),
    ("no_intent", "无招采意图"),
    ("procedural", "纯流程性公告"),
    ("concluded", "标的已有结论"),
    ("domain", "产品域预筛"),
    ("non_hospital", "采购主体非医疗机构"),
]
STAGE_LABELS = dict(DROP_STAGES)
STAGE_LABELS.update({
    "cluster_merged": "同源聚类合并",
    "queued": "已入队待核实",
    "create": "核实通过",
    "exclude": "核实排除",
    "manual": "转人工",
    "pushed": "推送成功",
    "push_skipped": "推送时已入账跳过",
})

# screened_out.jsonl 是五个闸门的混合体，只能按 skip_reason 的前缀分回去。
# 前缀出自 tender_pipeline.prepare() 与 search_common.screen_domain()。
SCREEN_PREFIXES = [
    ("标题命中明确排除模式：", "title_exclude"),
    ("标题缺少招采/交易意图词", "no_intent"),
    ("纯流程性公告（", "procedural"),
    ("采购主体非医疗机构（", "non_hospital"),
]

KEPT_STAGES = {"queued", "create", "pushed"}
REVIEW_STAGES = {"manual", "semantic"}
DECISION_STAGES = {"queued", "create", "exclude", "manual", "pushed", "push_skipped"}


class ReportError(Exception):
    pass


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportError("无法读取 JSON {}：{}".format(path, exc)) from exc


def load_jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ReportError("无法读取 JSONL {}：{}".format(path, exc)) from exc
    for number, line in enumerate(lines, 1):
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ReportError("JSONL 无效 {}:{}：{}".format(path, number, exc)) from exc
    return rows


def resolve_dirs(run_dir):
    run_dir = Path(run_dir).resolve()
    if run_dir.name == "pipeline":
        return run_dir.parent, run_dir
    return run_dir, run_dir / "pipeline"


def classify_screened(reason):
    for prefix, key in SCREEN_PREFIXES:
        if reason.startswith(prefix):
            return key
    return "domain"


def text(value):
    value = "" if value is None or value == "null" else str(value)
    return value.strip()


def day(value):
    """发布时间在不同来源里有 ISO 时间戳也有纯日期，报告里一律只留日历日。"""
    value = text(value)
    return value[:10] if len(value) >= 10 and value[4] == "-" else value


def badges_for(row):
    """候选身上已经写好的标记，直接当徽章用——哪条是弱信号、哪条是混合包一眼可见。"""
    evidence = row.get("search_evidence") or {}
    out = []
    tier = evidence.get("signal_tier")
    if tier == "broad":
        out.append(("warn", "宽片段", "只命中宽片段，回测有效率 21%"))
    elif tier == "core":
        out.append(("ok", "核心词", "命中核心名词或项目代号"))
    if evidence.get("signal_only_in_attachment"):
        out.append(("info", "信号只在附件", "正文回找不到品类词属正常"))
    if evidence.get("aggregate_notice"):
        out.append(("warn", "汇总页", "多家单位合成，结构化字段一律不绑定"))
    if evidence.get("body_exclude_term"):
        out.append(("info", "混合包·正文", "正文同时出现：" + text(evidence["body_exclude_term"])))
    if evidence.get("title_exclude_term"):
        out.append(("info", "混合包·标题", "标题并列标的：" + text(evidence["title_exclude_term"])))
    access = evidence.get("content_access") or row.get("content_access")
    # unknown 只是「这条没走到取详情」，不是正文缺失，别当成告警刷在每一行被丢的候选上。
    if access in ("public_partial", "metadata_only"):
        out.append(("warn", str(access),
                    text(evidence.get("content_access_reason") or row.get("content_access_reason"))))
    signals = evidence.get("target_category_signals") or []
    if signals:
        out.append(("plain", "信号 " + "/".join(signals[:4]), ""))
    return out


def view(row, stage_key, reason=""):
    """把几种不同形状的行归一成报告要用的那几列。"""
    if "候选" in row:  # semantic_review.jsonl 是 dedup_match.review_row 的形状
        inner = row["候选"]
        pairs = row.get("台账候选") or []
        detail = "；".join(
            "vs《{}》标题相似 {:.2f} 正文相似 {:.2f}".format(
                text(p.get("标题"))[:40], p.get("标题相似度") or 0.0, p.get("正文相似度") or 0.0)
            for p in pairs[:3]
        )
        return {
            "id": row.get("candidate_id", ""),
            "title": text(inner.get("标题")),
            "buyer": text(inner.get("采购人")),
            "publish": day(inner.get("发布时间")),
            "url": "",
            "stage": stage_key,
            "reason": text(row.get("reason")),
            "detail": detail,
            "badges": [],
        }
    fields = row.get("source_fields") or (row.get("search_evidence") or {}).get("source_fields") or {}
    detail = ""
    if row.get("match_layer"):
        detail = "命中层：{}".format(text(row.get("match_layer")))
        if row.get("matched_title"):
            detail += "；台账《{}》".format(text(row["matched_title"])[:50])
    members = row.get("cluster_members") or []
    if len(members) > 1:
        detail = (detail + "；" if detail else "") + "聚类合并 {} 条".format(len(members))
    return {
        "id": row.get("candidate_id", ""),
        "title": text(row.get("title") or row.get("标题")),
        "buyer": text(fields.get("单位") or row.get("site_name")),
        "publish": day(row.get("publish_time") or fields.get("发布时间")),
        "url": text(row.get("url")),
        "stage": stage_key,
        "reason": reason or text(row.get("skip_reason")),
        "detail": detail,
        "badges": badges_for(row),
    }


def collect(run_dir):
    """一轮运行的全部可读状态。缺件不报错——跑挂的轮次同样要能出报告。"""
    search_dir, pipeline_dir = resolve_dirs(run_dir)
    manifest_path = pipeline_dir / "manifest.json"
    summary_path = search_dir / "search_summary.json"
    if not manifest_path.exists() and not summary_path.exists():
        raise ReportError(
            "{} 下既没有 pipeline/manifest.json 也没有 search_summary.json，"
            "不像一个运行目录".format(search_dir)
        )
    manifest = load_json(manifest_path) if manifest_path.exists() else {}
    summary = load_json(summary_path) if summary_path.exists() else {}

    indexed = load_jsonl(search_dir / "candidate_index.jsonl")
    queue = load_jsonl(pipeline_dir / "queue.jsonl")

    buckets = {key: [] for key, _ in DROP_STAGES}
    buckets["already_seen"] = [view(r, "already_seen") for r in load_jsonl(pipeline_dir / "already_seen.jsonl")]
    buckets["semantic"] = [view(r, "semantic") for r in load_jsonl(pipeline_dir / "semantic_review.jsonl")]
    buckets["concluded"] = [view(r, "concluded") for r in load_jsonl(pipeline_dir / "concluded.jsonl")]
    for row in load_jsonl(pipeline_dir / "screened_out.jsonl"):
        key = classify_screened(text(row.get("skip_reason")))
        buckets[key].append(view(row, key))

    # 核实结论：results/ 里一批一个文件，join 回队列行才有标题和徽章。
    queue_by_id = {r.get("candidate_id"): r for r in queue}
    decisions = {}
    results_dir = pipeline_dir / "results"
    for path in sorted(results_dir.glob("*.json")) if results_dir.exists() else []:
        data = load_json(path)
        for row in (data.get("results") if isinstance(data, dict) else data) or []:
            decisions[row.get("candidate_id")] = row

    # 卡片要显示的是真正写进飞书的那 16 个字段，不是模型交回的增量 record。
    payloads = {}
    push_dir = pipeline_dir / "payloads" / "push"
    if push_dir.exists():
        for path in sorted(push_dir.glob("*.json")):
            payloads[path.stem] = load_json(path)

    outcome = {"create": [], "exclude": [], "manual": [], "queued": []}
    for candidate_id, source in queue_by_id.items():
        result = decisions.get(candidate_id)
        stage = result["decision"] if result else "queued"
        item = view(source, stage, reason=text((result or {}).get("reason")))
        item["record"] = payloads.get(candidate_id) or (result or {}).get("record") or {}
        item["payload_written"] = candidate_id in payloads
        item["field_evidence"] = ((result or {}).get("evidence") or {}).get("field_evidence") or {}
        item["hospital"] = source.get("hospital_suggestion") or {}
        item["summary"] = text((source.get("search_evidence") or {}).get("summary"))
        outcome.setdefault(stage, []).append(item)

    push_rows = []
    orphan_push = 0
    push_ledger = pipeline_dir / "push_ledger.json"
    if push_ledger.exists():
        for row in (load_json(push_ledger).get("records") or []):
            candidate_id = row.get("candidate_id")
            if candidate_id not in queue_by_id:
                # `prepare --force` 重放之后，上一次的回执还躺在目录里，但它说的那条候选
                # 这轮可能根本没进队列。照它记「已推送」会把重放的结果盖掉——diff 就白做了。
                orphan_push += 1
                continue
            skipped = row.get("delivery_status") == "already_seen"
            item = view(queue_by_id.get(candidate_id) or {}, "push_skipped" if skipped else "pushed")
            item["id"] = item["id"] or candidate_id
            item["detail"] = "飞书 record_id {}".format(text(row.get("feishu_record_id")) or "—")
            item["reason"] = "已在台账，零写入跳过" if skipped else "写入确认"
            push_rows.append(item)

    return {
        "search_dir": search_dir,
        "pipeline_dir": pipeline_dir,
        "manifest": manifest,
        "summary": summary,
        "counts": manifest.get("counts") or {},
        "indexed_rows": indexed,
        "queue": queue,
        "buckets": buckets,
        "outcome": outcome,
        "push_rows": push_rows,
        "orphan_push": orphan_push,
    }


def failure_of(run):
    """凭证故障与台账拉取失败长得都像「今天没情报」，报告必须把它顶到最上面。"""
    summary, manifest = run["summary"], run["manifest"]
    if summary.get("source_auth_failed"):
        return ("检索来源凭证失败（API Key 缺失、被拒或积分不足）",
                text(summary.get("failure_reason")) or "本次结果不可信，修复凭证后重新检索")
    if summary.get("ledger_fetch_failed"):
        return ("飞书台账拉取失败", text(summary.get("failure_reason")) or "无法判重，本目录不得排队")
    if summary.get("exit_code"):
        return ("检索来源以退出码 {} 结束".format(summary["exit_code"]),
                text(summary.get("failure_reason")) or "本次结果不完整")
    failures = summary.get("failures") or []
    if failures:
        return ("检索有 {} 条 query 失败".format(len(failures)),
                json.dumps(failures, ensure_ascii=False)[:400])
    if run.get("orphan_push"):
        return ("队列被重建过：{} 条回执对不上当前队列".format(run["orphan_push"]),
                "本目录跑过 prepare --force，push_ledger.json 里那几条是上一次排队的遗留，"
                "已从漏斗与 diff 里排除")
    return None


def search_stages(summary, indexed):
    """检索层的三段。计数散在顶层摘要与来源摘要两处，且随 schema 版本挪过位置。

    这里只认一件事：这条链必须正好落在 indexed 上。对不齐的差额宁可显式记一段
    「来源侧其它丢弃」，也不让漏斗悄悄少掉几百条——那正是这张报告要消灭的黑盒。
    """
    source = summary.get("source_summary") or {}

    def pick(key):
        for holder in (summary, source):
            value = holder.get(key)
            if isinstance(value, int):
                return value
        return None

    stages = []
    cursor = pick("raw_result_count")
    if cursor is None:
        return stages
    for label, floor, note in (
        ("跨 query 去重", pick("unique_notice_count"), "同一公告被多条 query 捞到"),
        ("来源侧预筛", pick("source_candidate_count") or source.get("candidate_count"),
         "适配器只看标题与标的物清单"),
        ("统一层去重", indexed if summary.get("intra_source_duplicates") is not None else None,
         "跨来源合并同一条公告"),
    ):
        if floor is None or floor > cursor:
            continue
        stages.append({"key": "search", "label": label, "enter": cursor,
                       "drop": cursor - floor, "note": note, "rows": []})
        cursor = floor
    if cursor > indexed:
        stages.append({"key": "search", "label": "来源侧其它丢弃", "enter": cursor,
                       "drop": cursor - indexed, "note": "摘要未细分这一段", "rows": []})
    return stages


def funnel(run):
    """自上而下的漏斗：每一段记录进入、丢弃、留下。顺序与 prepare() 的闸门一致。"""
    summary, counts, buckets = run["summary"], run["counts"], run["buckets"]
    indexed = counts.get("indexed", len(run["indexed_rows"]))
    stages = search_stages(summary, indexed)

    clusters = counts.get("clusters", indexed)
    stages.append({"key": "cluster_merged", "label": "同源聚类合并", "enter": indexed,
                   "drop": max(0, indexed - clusters),
                   "note": "同一公告的多个来源并成一条", "rows": []})

    entering = clusters
    for key, label in DROP_STAGES:
        rows = buckets.get(key) or []
        stages.append({"key": key, "label": label, "enter": entering,
                       "drop": len(rows), "note": "", "rows": rows})
        entering -= len(rows)

    outcome = run["outcome"]
    pending = outcome["queued"]
    processed = len(outcome["create"]) + len(outcome["exclude"]) + len(outcome["manual"])
    if pending or processed:
        # 半途报告也要守住算术：待核实项不能被误算成已经通过模型核实。
        held_or_rejected = len(outcome["exclude"]) + len(outcome["manual"]) + len(pending)
        note = "排除 {} · 转人工 {} · 待核实 {}".format(
            len(outcome["exclude"]), len(outcome["manual"]), len(pending))
        stages.append({"key": "verify", "label": "模型核实", "enter": entering,
                       "drop": held_or_rejected, "drop_label": "未完成" if pending else "未通过",
                       "note": note,
                       "rows": outcome["exclude"] + outcome["manual"] + pending})
        entering = len(outcome["create"])
    push_counts = run["manifest"].get("push_counts") or {}
    push_expected = run["manifest"].get("mode") == "daily-push" and entering > 0
    if run["push_rows"] or push_counts or push_expected:
        confirmed = push_counts.get(
            "confirmed", sum(1 for r in run["push_rows"] if r["stage"] == "pushed"))
        skipped = push_counts.get(
            "skipped", sum(1 for r in run["push_rows"] if r["stage"] == "push_skipped"))
        pending = max(0, entering - confirmed - skipped)
        recorded_ids = {r["id"] for r in run["push_rows"]}
        pending_rows = [r for r in outcome["create"] if r["id"] not in recorded_ids]
        note = "确认 {} · 已入账跳过 {} · 待写入 {}".format(confirmed, skipped, pending)
        stages.append({"key": "push", "label": "推送飞书", "enter": entering,
                       "drop": skipped + pending,
                       "drop_label": "未写入/跳过" if pending and skipped
                       else ("待写入" if pending else "跳过"),
                       "note": note, "rows": run["push_rows"] + pending_rows})
    return stages


def verdicts(run):
    """candidate_id → 这一轮它最后落在哪。diff 全靠这张表。"""
    table = {}
    for key, _ in DROP_STAGES:
        for item in run["buckets"].get(key) or []:
            table[item["id"]] = item
    for items in run["outcome"].values():
        for item in items:
            table[item["id"]] = item
    for item in run["push_rows"]:
        table[item["id"]] = item
    # 被聚类吞掉的成员在任何输出文件里都没有自己的行，只能从候选索引兜回来。
    for row in run["indexed_rows"]:
        table.setdefault(row.get("candidate_id"), {
            "id": row.get("candidate_id", ""),
            "title": text(row.get("title")),
            "buyer": text((row.get("source_fields") or {}).get("单位") or row.get("site_name")),
            "publish": day(row.get("publish_time")),
            "url": text(row.get("url")),
            "stage": "cluster_merged",
            "reason": "并入同源聚类的代表行",
            "detail": "",
            "badges": [],
        })
    return table


def stage_kind(key):
    if key in KEPT_STAGES:
        return "kept"
    if key in REVIEW_STAGES:
        return "review"
    return "dropped"


def diff(base, current):
    """只列归宿变了的。误杀排在最前——那是改动真正的代价。"""
    left, right = verdicts(base), verdicts(current)
    buckets = {"regress": [], "gain": [], "decision": [], "stage": [], "gone": [], "new": []}
    for candidate_id in sorted(set(left) | set(right)):
        a, b = left.get(candidate_id), right.get(candidate_id)
        if a is None:
            buckets["new"].append((None, b))
            continue
        if b is None:
            buckets["gone"].append((a, None))
            continue
        if a["stage"] == b["stage"]:
            continue
        kind_a, kind_b = stage_kind(a["stage"]), stage_kind(b["stage"])
        if kind_a == "kept" and kind_b != "kept":
            buckets["regress"].append((a, b))
        elif kind_a != "kept" and kind_b == "kept":
            buckets["gain"].append((a, b))
        elif a["stage"] in DECISION_STAGES and b["stage"] in DECISION_STAGES:
            buckets["decision"].append((a, b))
        else:
            buckets["stage"].append((a, b))
    return buckets


# ---------------------------------------------------------------- 渲染

CSS = """
:root {
  color-scheme: light dark;
  --bg: #f6f7f9; --panel: #fff; --ink: #14171c; --muted: #6b7280; --line: #e3e6ea;
  --keep: #2f6fed; --drop: #d94a4a; --hold: #d99b2f; --ok: #1f9d63;
  --chip: #eef1f5; --hover: #f0f3f7;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14171c; --panel: #1b1f26; --ink: #e8eaee; --muted: #98a0ad; --line: #2a3038;
    --keep: #5b8dfb; --drop: #ef6b6b; --hold: #e0ae52; --ok: #3fbb83;
    --chip: #252b34; --hover: #222831;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink);
  font: 14px/1.6 -apple-system, "Segoe UI", "Microsoft YaHei", system-ui, sans-serif; }
.wrap { max-width: 1120px; margin: 0 auto; padding: 28px 20px 80px; }
a { color: var(--keep); }
h1 { font-size: 20px; margin: 0 0 4px; letter-spacing: .2px; }
h2 { font-size: 15px; margin: 32px 0 12px; color: var(--muted);
  text-transform: uppercase; letter-spacing: .08em; font-weight: 600; }
.hero { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 20px 22px; }
.sub { color: var(--muted); font-size: 13px; }
.kpis { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 16px; }
.kpi { background: var(--chip); border-radius: 9px; padding: 9px 14px; min-width: 96px; }
.kpi b { display: block; font-size: 20px; line-height: 1.2; font-variant-numeric: tabular-nums; }
.kpi span { color: var(--muted); font-size: 12px; }
.kpi.hot b { color: var(--ok); }
.kpi.cold b { color: var(--drop); }
.notes { margin-top: 12px; color: var(--muted); font-size: 12px; }
.banner { margin-top: 16px; border-radius: 10px; padding: 12px 16px;
  background: color-mix(in srgb, var(--drop) 14%, transparent);
  border: 1px solid color-mix(in srgb, var(--drop) 45%, transparent); }
.banner b { color: var(--drop); }
.banner p { margin: 4px 0 0; font-size: 13px; color: var(--muted); word-break: break-all; }
.filter { width: 100%; margin: 20px 0 0; padding: 10px 14px; border-radius: 9px;
  border: 1px solid var(--line); background: var(--panel); color: var(--ink); font: inherit; }
.stage { background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  margin-bottom: 6px; overflow: hidden; }
.stage > summary { display: grid; grid-template-columns: 190px 1fr 190px; gap: 14px;
  align-items: center; padding: 11px 16px; cursor: pointer; list-style: none; }
.stage > summary::-webkit-details-marker { display: none; }
.stage:hover { border-color: color-mix(in srgb, var(--keep) 40%, var(--line)); }
.stage.quiet > summary { cursor: default; opacity: .62; }
.name { font-weight: 600; }
.name em { display: block; font-style: normal; font-weight: 400; font-size: 12px; color: var(--muted); }
.bar { display: flex; height: 16px; border-radius: 4px; background: var(--chip); overflow: hidden; }
.bar i { background: var(--keep); }
.bar b { background: var(--drop); }
.bar.hold b { background: var(--hold); }
.nums { text-align: right; font-variant-numeric: tabular-nums; font-size: 13px; color: var(--muted); }
.nums s { text-decoration: none; color: var(--drop); font-weight: 600; }
.nums u { text-decoration: none; color: var(--ink); font-weight: 600; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
thead th { text-align: left; font-weight: 600; color: var(--muted); font-size: 12px;
  padding: 8px 16px; border-top: 1px solid var(--line); background: var(--chip); }
tbody td { padding: 10px 16px; border-top: 1px solid var(--line); vertical-align: top; }
tbody tr:hover { background: var(--hover); }
td.t { width: 46%; }
thead th:nth-child(2), tbody td:nth-child(2) { width: 16%; }
thead th:nth-child(3), tbody td:nth-child(3) { width: 10%; white-space: nowrap; }
td.t a, td.t span.plain { font-weight: 500; }
.why { color: var(--drop); font-size: 12.5px; }
.why.keep { color: var(--ok); }
.detail { color: var(--muted); font-size: 12px; margin-top: 2px; }
.tags { margin-top: 5px; display: flex; flex-wrap: wrap; gap: 5px; }
.tag { font-size: 11px; padding: 1px 7px; border-radius: 20px; background: var(--chip); color: var(--muted); }
.tag.warn { background: color-mix(in srgb, var(--hold) 22%, transparent); color: var(--hold); }
.tag.ok { background: color-mix(in srgb, var(--ok) 18%, transparent); color: var(--ok); }
.tag.info { background: color-mix(in srgb, var(--keep) 16%, transparent); color: var(--keep); }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  padding: 14px 16px; margin-bottom: 8px; }
.card > .head { font-weight: 600; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
  gap: 6px 18px; margin-top: 10px; font-size: 12.5px; }
.grid div { border-top: 1px dashed var(--line); padding-top: 4px; }
.grid k { display: block; color: var(--muted); font-size: 11px; }
.empty { color: var(--muted); padding: 14px 16px; font-size: 13px; }
footer { margin-top: 40px; color: var(--muted); font-size: 12px; }
"""

JS = """
const box = document.getElementById('filter');
if (box) box.addEventListener('input', () => {
  const q = box.value.trim().toLowerCase();
  document.querySelectorAll('[data-search]').forEach(el => {
    el.hidden = q !== '' && !el.dataset.search.includes(q);
  });
  if (q !== '') document.querySelectorAll('details').forEach(d => d.open = true);
});
"""


def esc(value):
    return html.escape(text(value))


def href(url):
    url = text(url)
    return url if url.startswith("http://") or url.startswith("https://") else ""


def pct(part, whole):
    return 0.0 if not whole else max(0.0, min(100.0, part * 100.0 / whole))


def row_html(item, keep=False):
    link = href(item.get("url"))
    title = esc(item.get("title")) or "（无标题）"
    head = '<a href="{}" target="_blank" rel="noreferrer noopener">{}</a>'.format(esc(link), title) \
        if link else '<span class="plain">{}</span>'.format(title)
    tags = "".join(
        '<span class="tag {}" title="{}">{}</span>'.format(kind, esc(tip), esc(label))
        for kind, label, tip in item.get("badges") or []
    )
    detail = '<div class="detail">{}</div>'.format(esc(item["detail"])) if item.get("detail") else ""
    reason = '<div class="why{}">{}</div>'.format(
        " keep" if keep else "", esc(item["reason"])) if item.get("reason") else ""
    needle = esc(" ".join((item.get("title", ""), item.get("buyer", ""),
                           item.get("reason", ""), item.get("id", "")))).lower()
    return (
        '<tr data-search="{needle}"><td class="t">{head}{detail}{tags}</td>'
        '<td>{buyer}</td><td>{publish}</td><td>{reason}</td></tr>'
    ).format(needle=needle, head=head, detail=detail,
             tags='<div class="tags">{}</div>'.format(tags) if tags else "",
             buyer=esc(item.get("buyer")) or "—", publish=esc(item.get("publish")) or "—",
             reason=reason or "—")


def table_html(rows, keep=False, columns=("候选", "采购人", "发布", "理由")):
    if not rows:
        return '<div class="empty">这一关这轮没拦下任何东西。</div>'
    head = "".join("<th>{}</th>".format(esc(c)) for c in columns)
    body = "".join(row_html(item, keep) for item in rows)
    return "<table><thead><tr>{}</tr></thead><tbody>{}</tbody></table>".format(head, body)


def stage_html(stage, base):
    enter, drop = stage["enter"], stage["drop"]
    out = enter - drop
    kept_pct, drop_pct = pct(out, base), pct(drop, base)
    drop_label = stage.get("drop_label") or ("待判" if stage["key"] == "semantic" else "丢")
    hold = " hold" if drop_label != "丢" else ""
    note = '<em>{}</em>'.format(esc(stage["note"])) if stage["note"] else ""
    summary = (
        '<summary><span class="name">{label}{note}</span>'
        '<span class="bar{hold}"><i style="width:{kept:.4f}%"></i><b style="width:{dropped:.4f}%"></b></span>'
        '<span class="nums">进 {enter} · <s>{drop_label} {drop}</s> · <u>出 {out}</u></span></summary>'
    ).format(label=esc(stage["label"]), note=note, hold=hold, kept=kept_pct,
             dropped=drop_pct, enter=enter, drop=drop, drop_label=esc(drop_label), out=out)
    if not stage["rows"]:
        # 明细拿不到的两段（来源侧预筛只有计数）与零丢弃的关卡都不给展开箭头。
        return '<div class="stage quiet">{}</div>'.format(summary)
    keep = stage["key"] == "push"
    return "<details class=\"stage\">{}{}</details>".format(summary, table_html(stage["rows"], keep))


def card_html(item):
    record = item.get("record") or {}
    evidence = item.get("field_evidence") or {}
    hospital = item.get("hospital") or {}
    cells = []
    for key in ("单位", "医院全名", "医院等级", "所属省/市", "地区", "所属大区",
                "项目编号", "发布时间", "截止时间", "预算", "采购方式", "科室", "命中关键词"):
        value = record.get(key)
        if not text(value) and key in ("医院全名", "医院等级"):
            value = hospital.get(key) or hospital.get({"医院全名": "name", "医院等级": "level"}[key])
        if not text(value):
            # 空字段不是没意义——16 字段契约里它就是空单元格，但堆在卡片里只是噪声。
            continue
        tip = evidence.get(key) or ""
        cells.append('<div title="{}"><k>{}</k>{}</div>'.format(esc(tip), esc(key), esc(text(value))))
    link = href(item.get("url"))
    head = '<a href="{}" target="_blank" rel="noreferrer noopener">{}</a>'.format(
        esc(link), esc(item.get("title"))) if link else esc(item.get("title"))
    tags = "".join('<span class="tag {}" title="{}">{}</span>'.format(kind, esc(tip), esc(label))
                   for kind, label, tip in item.get("badges") or [])
    summary = item.get("summary")
    needle = esc(" ".join((item.get("title", ""), item.get("buyer", ""), item.get("id", "")))).lower()
    return (
        '<div class="card" data-search="{needle}"><div class="head">{head}</div>'
        '{tags}{summary}<div class="grid">{cells}</div></div>'
    ).format(needle=needle, head=head,
             tags='<div class="tags">{}</div>'.format(tags) if tags else "",
             summary='<div class="detail">{}</div>'.format(esc(summary[:400])) if summary else "",
             cells="".join(cells) or '<div><k>记录</k>尚未导出推送载荷</div>')


def page(title, body):
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>{title}</title><style>{css}</style></head><body><div class=\"wrap\">{body}</div>"
        "<script>{js}</script></body></html>"
    ).format(title=esc(title), css=CSS, body=body, js=JS)


def render_run(run):
    manifest, summary, counts = run["manifest"], run["summary"], run["counts"]
    run_id = text(manifest.get("run_id")) or run["search_dir"].name
    stages = funnel(run)
    base = max([s["enter"] for s in stages] or [1]) or 1
    pushed = (manifest.get("push_counts") or {}).get(
        "confirmed", sum(1 for r in run["push_rows"] if r["stage"] == "pushed"))
    source = summary.get("source_summary") or {}
    kpis = [
        ("候选入库", counts.get("indexed", len(run["indexed_rows"])), ""),
        ("入队核实", counts.get("queued", len(run["queue"])), ""),
        ("核实通过", len(run["outcome"]["create"]), ""),
        ("推送成功", pushed, "hot" if pushed else "cold"),
        ("台账已入账", counts.get("already_seen", 0), ""),
        ("宽片段候选", counts.get("queued_broad_signal_only", 0), ""),
        ("接口请求", summary.get("request_count", source.get("request_count", "—")), ""),
    ]
    # 检索层里既不是丢弃、也不进漏斗的那几笔账：花了多少、二次机会捞回多少、
    # 有多少在取详情之前就被台账挡掉（省下的正是每条要钱的详情调用）。
    notes = [
        ("耗用", summary.get("cost_units", source.get("cost_units"))),
        ("取详情", source.get("detail_fetched")),
        ("详情前已入账跳过", summary.get("already_seen_before_detail_count")),
        ("二次机会复核", "{} 打开 / {} 留下".format(summary["reopened_count"], summary.get("reopened_kept_count", 0))
         if summary.get("reopened_count") else None),
        ("信号只在附件", counts.get("queued_signal_only_in_attachment")),
        ("回源", source.get("origin_lookups")),
    ]
    parts = [
        '<div class="hero"><h1>{}</h1><div class="sub">{}</div>'.format(
            esc(run_id),
            " · ".join(filter(None, (
                "模式 " + esc(manifest.get("mode") or "未 prepare"),
                "状态 " + esc(manifest.get("state") or "SEARCHED"),
                "窗口 " + esc(summary.get("time_range")),
                "检索于 " + esc(summary.get("run_at")),
                "台账 {} 行".format(manifest["ledger_row_count"]) if manifest.get("ledger_row_count") else "",
            )))),
        '<div class="kpis">{}</div>{}</div>'.format(
            "".join('<div class="kpi {}"><b>{}</b><span>{}</span></div>'.format(cls, esc(value), esc(label))
                    for label, value, cls in kpis),
            '<div class="notes">{}</div>'.format(" · ".join(
                "{} {}".format(esc(label), esc(value))
                for label, value in notes if value not in (None, "", 0))) if any(
                    value not in (None, "", 0) for _, value in notes) else ""),
    ]
    problem = failure_of(run)
    if problem:
        parts.append('<div class="banner"><b>{}</b><p>{}</p></div>'.format(esc(problem[0]), esc(problem[1])))
    parts.append('<input class="filter" id="filter" placeholder="过滤：标题、采购人、理由、候选 ID…">')
    parts.append("<h2>漏斗 · 每一关拦下了什么</h2>")
    parts.extend(stage_html(stage, base) for stage in stages)

    created = run["outcome"]["create"]
    parts.append("<h2>本轮产出 · {} 条</h2>".format(len(created)))
    parts.append("".join(card_html(item) for item in created)
                 or '<div class="empty">本轮没有核实通过的候选。</div>')
    parts.append("<footer>生成于 {} · 数据全部来自 {} · 不改管线状态</footer>".format(
        esc(datetime.now().astimezone().isoformat(timespec="seconds")), esc(str(run["search_dir"]))))
    return page("运行报告 " + run_id, "".join(parts))


DIFF_SECTIONS = [
    ("regress", "新误杀 · 基线留下、这次丢了", "改动的代价。逐条确认是不是真该丢。"),
    ("gain", "新捞回 · 基线丢了、这次留下", "改动的收益。逐条确认是不是真该留。"),
    ("decision", "核实判定变化", "同样进了队列，模型给的结论变了。"),
    ("stage", "拦截关卡变化", "两轮都没留下，但被不同的闸门拦下。"),
    ("new", "只在当前轮出现", "检索窗口或来源不同造成，通常不是改动引起的。"),
    ("gone", "只在基线轮出现", "同上。"),
]


def diff_row_html(before, after):
    item = after or before
    link = href(item.get("url"))
    title = esc(item.get("title")) or "（无标题）"
    head = '<a href="{}" target="_blank" rel="noreferrer noopener">{}</a>'.format(esc(link), title) \
        if link else '<span class="plain">{}</span>'.format(title)
    def cell(row):
        if row is None:
            return "—"
        label = STAGE_LABELS.get(row["stage"], row["stage"])
        return '{}<div class="detail">{}</div>'.format(esc(label), esc((row.get("reason") or "")[:160]))
    needle = esc(" ".join((item.get("title", ""), item.get("buyer", ""), item.get("id", "")))).lower()
    return ('<tr data-search="{}"><td class="t">{}<div class="detail">{}</div></td>'
            '<td>{}</td><td>{}</td></tr>').format(
        needle, head, esc(item.get("buyer")) or "—", cell(before), cell(after))


def render_diff(base, current, buckets):
    changed = sum(len(buckets[key]) for key, _, _ in DIFF_SECTIONS)
    base_id = text(base["manifest"].get("run_id")) or base["search_dir"].name
    current_id = text(current["manifest"].get("run_id")) or current["search_dir"].name
    kpis = [
        ("归宿变化", changed, "cold" if buckets["regress"] else ""),
        ("新误杀", len(buckets["regress"]), "cold" if buckets["regress"] else ""),
        ("新捞回", len(buckets["gain"]), "hot" if buckets["gain"] else ""),
        ("判定变化", len(buckets["decision"]), ""),
        ("关卡变化", len(buckets["stage"]), ""),
    ]
    parts = [
        '<div class="hero"><h1>归宿 diff</h1><div class="sub">基线 {} → 当前 {}</div>'
        '<div class="kpis">{}</div></div>'.format(
            esc(base_id), esc(current_id),
            "".join('<div class="kpi {}"><b>{}</b><span>{}</span></div>'.format(cls, value, esc(label))
                    for label, value, cls in kpis)),
        '<input class="filter" id="filter" placeholder="过滤：标题、采购人、候选 ID…">',
    ]
    if not changed:
        parts.append('<div class="empty">两轮的归宿完全一致，这次改动没有改变任何一条候选的去向。</div>')
    for key, title, note in DIFF_SECTIONS:
        rows = buckets[key]
        if not rows:
            continue
        parts.append("<h2>{} · {} 条</h2>".format(esc(title), len(rows)))
        parts.append('<div class="sub" style="margin-bottom:8px">{}</div>'.format(esc(note)))
        parts.append(
            '<div class="stage"><table><thead><tr><th>候选</th><th>基线归宿</th><th>当前归宿</th>'
            '</tr></thead><tbody>{}</tbody></table></div>'.format(
                "".join(diff_row_html(a, b) for a, b in rows)))
    parts.append("<footer>生成于 {} · 基线 {} · 当前 {}</footer>".format(
        esc(datetime.now().astimezone().isoformat(timespec="seconds")),
        esc(str(base["search_dir"])), esc(str(current["search_dir"]))))
    return page("归宿 diff " + base_id + " → " + current_id, "".join(parts))


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="IVD Bid Radar 运行报告（只读取运行数据，输出单文件 HTML）")
    parser.add_argument("--run-dir", help="检索目录或其 pipeline 子目录")
    parser.add_argument("--compare", nargs=2, metavar=("基线目录", "当前目录"),
                        help="两轮归宿 diff；两边都用同一批原始候选时最有意义")
    parser.add_argument("--out", help="输出 HTML 路径；默认写进运行目录")
    parser.add_argument("--open", dest="open_browser", action="store_true", help="生成后直接打开")
    args = parser.parse_args()
    if bool(args.run_dir) == bool(args.compare):
        parser.error("--run-dir 与 --compare 必须且只能给一个")

    try:
        if args.compare:
            base, current = collect(args.compare[0]), collect(args.compare[1])
            buckets = diff(base, current)
            out = Path(args.out) if args.out else current["search_dir"] / "report-diff.html"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(render_diff(base, current, buckets), encoding="utf-8")
            result = {"report": str(out), "kind": "diff",
                      "changed": {key: len(buckets[key]) for key, _, _ in DIFF_SECTIONS}}
        else:
            run = collect(args.run_dir)
            out = Path(args.out) if args.out else run["search_dir"] / "report.html"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(render_run(run), encoding="utf-8")
            result = {"report": str(out), "kind": "run",
                      "run_id": text(run["manifest"].get("run_id")) or run["search_dir"].name,
                      "counts": run["counts"] or {"indexed": len(run["indexed_rows"])}}
    except (ReportError, OSError) as exc:
        print("错误：{}".format(exc), file=sys.stderr)
        return 2
    if args.open_browser:
        webbrowser.open(out.resolve().as_uri())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
