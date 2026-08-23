#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tender Intel 可插拔检索层：运行来源适配器并生成统一候选目录。"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

from search_common import merge_source_dirs, write_candidates


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def _doubao_command(args, source_dir):
    command = [
        sys.executable, str(SCRIPTS / "doubao_search.py"),
        "--time-range", args.time_range,
        "--out-dir", str(source_dir),
    ]
    if args.doubao_queries:
        command.extend(["--queries", args.doubao_queries])
    if args.no_stats:
        command.append("--no-stats")
    if args.dry_run:
        command.append("--dry-run")
    return command


def _ccgp_command(args, source_dir):
    command = [
        sys.executable, str(SCRIPTS / "ccgp_search.py"),
        "--time-range", args.time_range,
        "--out-dir", str(source_dir),
        "--delay", str(args.ccgp_delay),
        "--max-pages-per-query", str(args.ccgp_max_pages),
    ]
    if args.ccgp_queries:
        command.extend(["--queries", args.ccgp_queries])
    if args.dry_run:
        command.append("--dry-run")
    return command


# 新来源只需实现同一候选目录契约并在这里注册命令构造器。
ADAPTERS = {
    "doubao": _doubao_command,
    "ccgp": _ccgp_command,
}


def parse_sources(value):
    names = []
    for name in str(value or "").split(","):
        name = name.strip().lower()
        if name and name not in names:
            names.append(name)
    unknown = [name for name in names if name not in ADAPTERS]
    if unknown:
        raise ValueError(f"未知检索来源：{unknown}；可用来源：{sorted(ADAPTERS)}")
    if not names:
        raise ValueError("至少启用一个检索来源")
    return names


def load_summary(source_dir):
    path = Path(source_dir) / "search_summary.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(description="Tender Intel 可插拔多来源检索层")
    parser.add_argument("--sources", default="doubao,ccgp", help="逗号分隔；默认 doubao,ccgp")
    parser.add_argument("--time-range", default="72h", help="72h / 3d / YYYY-MM-DD..YYYY-MM-DD")
    parser.add_argument("--out-dir", help="统一候选目录；默认 .tmp/search/<日期>")
    parser.add_argument("--doubao-queries", help="传给豆包适配器的编号表达式")
    parser.add_argument("--ccgp-queries", help="传给 CCGP 的逗号分隔单词 Query")
    parser.add_argument("--ccgp-delay", type=float, default=2.0)
    parser.add_argument("--ccgp-max-pages", type=int, default=100)
    parser.add_argument("--no-stats", action="store_true", help="不更新豆包 query_stats")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        sources = parse_sources(args.sources)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir) if args.out_dir else ROOT / ".tmp" / "search" / date.today().isoformat()
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    source_root = out_dir / ".sources"
    runs = []
    usable_dirs = []
    for source in sources:
        source_dir = source_root / f"{source}-{stamp}"
        source_dir.mkdir(parents=True, exist_ok=True)
        command = ADAPTERS[source](args, source_dir)
        print(f"运行检索来源：{source}", flush=True)
        completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
        if completed.stdout.strip():
            print(completed.stdout.rstrip())
        if completed.stderr.strip():
            print(completed.stderr.rstrip(), file=sys.stderr)
        summary = load_summary(source_dir)
        usable = (source_dir / "candidate_index.jsonl").exists()
        if usable:
            usable_dirs.append(source_dir)
        runs.append({
            "source": source,
            "exit_code": completed.returncode,
            "usable": usable,
            "source_dir": str(source_dir),
            "summary": summary,
        })

    if args.dry_run:
        return 0 if all(run["exit_code"] == 0 for run in runs) else 2
    if not usable_dirs:
        print("错误：所有检索来源都失败，未生成候选目录", file=sys.stderr)
        return 2

    merged = merge_source_dirs(usable_dirs)
    index = write_candidates(merged, out_dir, date.today().isoformat())
    source_candidate_count = sum(
        int((run.get("summary") or {}).get("candidate_count") or 0) for run in runs
    )
    summary = {
        "schema_version": 2,
        "run_date": date.today().isoformat(),
        "time_range": args.time_range,
        "sources": runs,
        "source_count": len(runs),
        "source_succeeded": sum(1 for run in runs if run["exit_code"] == 0),
        "source_failed": sum(1 for run in runs if run["exit_code"] != 0),
        "raw_result_count": sum(
            int((run.get("summary") or {}).get("raw_result_count") or 0) for run in runs
        ),
        "source_candidate_count": source_candidate_count,
        "cross_source_duplicates": max(0, source_candidate_count - len(index)),
        "candidate_count": len(index),
    }
    (out_dir / "search_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"统一候选：来源内候选 {source_candidate_count} 条，跨来源去重 "
        f"{summary['cross_source_duplicates']} 条，最终 {len(index)} 条"
    )
    print(f"统一目录：{out_dir}")
    return 0 if any(run["exit_code"] == 0 for run in runs) else 2


if __name__ == "__main__":
    raise SystemExit(main())
