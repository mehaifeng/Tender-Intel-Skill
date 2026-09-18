#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""台账反馈统计：近 N 天窗口 + 全表累计，落成 JSON 供运行报告渲染。

    python scripts/feedback_stats.py --run-dir <检索目录> [--days 7] [--weeks 8]

运行报告讲的是「这一轮」——候选进来多少、每关拦下多少、推了几条。它讲不了最要紧的
那件事：推出去的东西到底有没有用。那个判断只存在于飞书台账的「信息是否有效」与
「无效原因(信息无效时填写)」两列上，由销售手工填，不看表就看不见。这一步把它变成
每轮运行的常规动作。

与 `run_report.py` 的分工：运行报告只读本地、不发请求；这一步**要发一次飞书请求**，
把结果落成 `<检索目录>/feedback_stats.json`，再由运行报告读它渲染成一节。

三条口径，都是拿真实台账核过的：

- **只统计 `标讯来源=AI收集` 的行。** 台账里有大量「员工录入」行（行 395–518 的
  124 条里占 42 条），那不是管线的产出，混进分母会把录入的问题算到管线头上。
  表里还有个游离的 `true` 值，同样不算。
- **窗口按 `推送时间`，空值回退 `插入表格的时间`。** 实测 432 条 AI 行里只有 370 条
  有推送时间，缺的 62 条全部是 2026-08-01 的（那时表格工作流还没填这一列）；
  插入时间是 432/432。
- **无效率的分母是「已反馈」，不是「推送」。** 反馈有滞后，用推送当分母会把滞后
  误报成质量。覆盖率单独显示，它低就说明窗口太新、无效率不可信。

**累计不落本地累加文件**：每次从台账重算。台账本身就是全量历史，重算永远和它一致、
不会漂移，也符合本项目「本地不保存状态」的一贯做法；代价只是每轮多读一次表。
拉不到台账就报错退出，**不按空台账出一个「无效率 0%」**——那比没有统计更糟。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

from feishu_client import FeishuClient, FeishuError, cell_text, date_text

ROOT = Path(__file__).resolve().parent.parent
STATS_NAME = "feedback_stats.json"
SCHEMA_VERSION = 1

# 只拉统计要用的列。**不走 tender_ledger.fetch_ledger()**：那份快照的契约是
# 「判重用的最小切片」，塞进反馈列会把两个用途搅在一起。
STATS_FIELDS = ["标题", "单位", "所属大区", "标讯来源", "信息是否有效",
                "无效原因(信息无效时填写)", "推送时间", "插入表格的时间", "发布时间"]

AI_SOURCE = "AI收集"
VALID = "有效"
INVALID = "无效/已过期"
DEFAULT_DAYS = 7
DEFAULT_WEEKS = 8
TOP_REASONS = 3


class StatsError(Exception):
    pass


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def push_day(fields):
    """这条记录算哪天推的。推送时间为空时退回插入时间——见模块开头。"""
    return date_text(fields.get("推送时间")) or date_text(fields.get("插入表格的时间"))


def read_rows(items):
    """接口返回项 -> 统计用的扁平行。非 AI 收集一律丢掉。"""
    rows = []
    for item in items:
        fields = item.get("fields") or {}
        if cell_text(fields.get("标讯来源")) != AI_SOURCE:
            continue
        rows.append({
            "标题": cell_text(fields.get("标题")),
            "单位": cell_text(fields.get("单位")),
            "大区": cell_text(fields.get("所属大区")),
            "反馈": cell_text(fields.get("信息是否有效")),
            "原因": cell_text(fields.get("无效原因(信息无效时填写)")),
            "推送日": push_day(fields),
            "发布日": date_text(fields.get("发布时间")),
        })
    return rows


def as_date(value):
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def ratio(part, whole):
    """没有分母时返回 None 而不是 0——「一条都没反馈」和「反馈了但都对」不是一回事。"""
    return round(part / whole, 4) if whole else None


def tally(rows):
    reviewed = [r for r in rows if r["反馈"] in (VALID, INVALID)]
    invalid = [r for r in reviewed if r["反馈"] == INVALID]
    return {
        "pushed": len(rows),
        "reviewed": len(reviewed),
        "invalid": len(invalid),
        "valid": len(reviewed) - len(invalid),
        "coverage": ratio(len(reviewed), len(rows)),
        "invalid_rate": ratio(len(invalid), len(reviewed)),
    }, invalid


def top_reasons(invalid):
    """出现次数最多的前几条，同次数按更新鲜的排前面。

    原因是自由文本，多数只出现一次；带条数一起显示，读者自己看得出是共性还是孤例。
    """
    counts, latest, example = Counter(), {}, {}
    for row in invalid:
        reason = row["原因"].strip()
        if not reason:
            continue
        counts[reason] += 1
        example.setdefault(reason, row)
        if row["推送日"] > latest.get(reason, ""):
            latest[reason] = row["推送日"]
    # 次数降序，同次数按推送日降序——一个 sorted 就够，不必排两遍。
    ranked = sorted(counts, key=lambda r: (counts[r], latest.get(r, "")), reverse=True)
    return [{
        "原因": reason,
        "条数": counts[reason],
        "单位": example[reason]["单位"],
        "最后一条": latest.get(reason, ""),
    } for reason in ranked[:TOP_REASONS]]


def week_start(day):
    return day - timedelta(days=day.weekday())


def weekly(rows, today, weeks):
    """近 N 个自然周的无效率，按 ISO 周（周一起）切。"""
    current = week_start(today)
    buckets = {}
    for row in rows:
        day = as_date(row["推送日"])
        if day is None or day > today:
            continue
        start = week_start(day)
        if start < current - timedelta(weeks=weeks - 1):
            continue
        buckets.setdefault(start.isoformat(), []).append(row)
    out = []
    for offset in range(weeks - 1, -1, -1):
        start = (current - timedelta(weeks=offset)).isoformat()
        stats, _ = tally(buckets.get(start, []))
        out.append({"week_start": start, **stats})
    return out


def compute(rows, today, days=DEFAULT_DAYS, weeks=DEFAULT_WEEKS):
    start = today - timedelta(days=days - 1)
    window = []
    for row in rows:
        day = as_date(row["推送日"])
        if day is not None and start <= day <= today:
            window.append(row)
    window_stats, window_invalid = tally(window)
    cumulative, _ = tally(rows)
    undated = sum(1 for r in rows if as_date(r["推送日"]) is None)
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "feishu_api",
        "generated_at": now_iso(),
        "today": today.isoformat(),
        "days": days,
        "weeks": weeks,
        "scope": AI_SOURCE,
        "rows_total": len(rows),
        # 两个日期都空的行进得了累计、进不了窗口，单独披露而不是悄悄漏掉。
        "undated": undated,
        "window": {"start": start.isoformat(), "end": today.isoformat(),
                   **window_stats, "reasons": top_reasons(window_invalid)},
        "cumulative": cumulative,
        "weekly": weekly(rows, today, weeks),
    }


def generate(run_dir=None, days=DEFAULT_DAYS, weeks=DEFAULT_WEEKS, today=None,
             client=None):
    """拉台账、算统计、写 JSON。返回 (路径, 统计)。"""
    client = client or FeishuClient()
    try:
        items = client.search_records(field_names=STATS_FIELDS)
    except FeishuError as exc:
        raise StatsError(
            f"拉取飞书台账失败，不出统计（禁止按空台账算出一个假的 0%）：{exc}") from exc
    stats = compute(read_rows(items), today or date.today(), days, weeks)
    if run_dir is None:
        return None, stats
    path = Path(run_dir) / STATS_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    return path, stats


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="台账反馈统计：近 N 天窗口 + 全表累计（会发一次飞书请求）")
    parser.add_argument("--run-dir", required=True, help="检索目录；统计写进这里")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help=f"窗口天数，默认 {DEFAULT_DAYS}")
    parser.add_argument("--weeks", type=int, default=DEFAULT_WEEKS,
                        help=f"周趋势的周数，默认 {DEFAULT_WEEKS}")
    args = parser.parse_args()

    try:
        path, stats = generate(args.run_dir, args.days, args.weeks)
    except (StatsError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    window, cumulative = stats["window"], stats["cumulative"]
    print(json.dumps({
        "stats": str(path),
        "window": {k: window[k] for k in ("start", "end", "pushed", "reviewed",
                                          "invalid", "coverage", "invalid_rate")},
        "cumulative": cumulative,
        "undated": stats["undated"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
