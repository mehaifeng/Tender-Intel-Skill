#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IVD Bid Radar 检索层：运行知了标讯适配器并生成统一候选目录。"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

from search_common import merge_source_dirs, write_candidates


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

# 凭证类失败必须与“今天没有新公告”区分开：适配器用 3 表示 API Key 缺失、被拒或积分不足。
AUTH_ERROR_EXIT_CODE = 3


def build_command(args, source_dir):
    command = [
        sys.executable, str(SCRIPTS / "zlbx_search.py"),
        "--time-range", args.time_range,
        "--out-dir", str(source_dir),
        "--batch-size", str(args.batch_size),
        "--page-size", str(args.page_size),
        "--max-details", str(args.max_details),
        "--delay", str(args.delay),
    ]
    if args.queries:
        command.extend(["--queries", args.queries])
    if getattr(args, "seen", None):
        command.extend(["--seen", args.seen])
    if args.dry_run:
        command.append("--dry-run")
    return command


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="IVD Bid Radar 检索层（知了标讯）")
    parser.add_argument("--time-range", default="72h", help="72h / 3d / YYYY-MM-DD..YYYY-MM-DD")
    parser.add_argument("--out-dir", help="统一候选目录；默认 .tmp/search/<日期>")
    parser.add_argument("--queries", help="逗号分隔 Query；默认读 references/keywords.md")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--page-size", type=int, default=50)
    parser.add_argument("--max-details", type=int, default=60)
    parser.add_argument("--delay", type=float, default=0.25)
    parser.add_argument("--seen", default=str(ROOT / "data/seen.json"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    out_dir = Path(args.out_dir) if args.out_dir else ROOT / ".tmp" / "search" / date.today().isoformat()
    source_dir = out_dir / ".sources" / "zlbx"
    source_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        build_command(args, source_dir),
        cwd=ROOT, env=env, text=True, encoding="utf-8", errors="replace", capture_output=True,
    )
    if completed.stdout.strip():
        print(completed.stdout.rstrip())
    if completed.stderr.strip():
        print(completed.stderr.rstrip(), file=sys.stderr)

    if completed.returncode == AUTH_ERROR_EXIT_CODE:
        # 无人值守时这类失败最危险：它看起来像“今天没情报”，实际是 Key 掉了或积分用光。
        print(
            "警告：知了标讯凭证失败（API Key 缺失、被拒或积分不足），本次未产出任何候选；"
            "需要修复凭证后重跑，详见上面的错误输出",
            file=sys.stderr,
        )

    summary_path = source_dir / "search_summary.json"
    source_summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else None

    if args.dry_run:
        return 0 if completed.returncode == 0 else 2
    if not (source_dir / "candidate_index.jsonl").exists():
        print("错误：检索来源失败，未生成候选目录", file=sys.stderr)
        return 2

    merged = merge_source_dirs([source_dir])
    index = write_candidates(merged, out_dir, date.today().isoformat())
    source_candidate_count = int((source_summary or {}).get("candidate_count") or 0)
    summary = {
        "schema_version": 3,
        "run_date": date.today().isoformat(),
        "time_range": args.time_range,
        "source": "zlbx",
        "exit_code": completed.returncode,
        "source_auth_failed": completed.returncode == AUTH_ERROR_EXIT_CODE,
        "raw_result_count": int((source_summary or {}).get("raw_result_count") or 0),
        "request_count": int((source_summary or {}).get("request_count") or 0),
        "cost_units": (source_summary or {}).get("cost_units"),
        "source_candidate_count": source_candidate_count,
        "intra_source_duplicates": max(0, source_candidate_count - len(index)),
        "candidate_count": len(index),
        "already_seen_before_detail_count": int((source_summary or {}).get("already_seen_before_detail_count") or 0),
        "source_summary": source_summary,
    }
    (out_dir / "search_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"统一候选：来源内候选 {source_candidate_count} 条，去重 "
        f"{summary['intra_source_duplicates']} 条，最终 {len(index)} 条"
    )
    print(f"统一目录：{out_dir}")
    return 0 if completed.returncode == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
