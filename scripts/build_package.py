#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打开箱即用分发包。

**包内含明文凭据**（知了 API Key 与飞书 Webhook 地址），因此只落在 `dist/`，
该目录已在 .gitignore 里。不要把生成的包提交仓库或对外分发。
用 `--no-secrets` 打不含凭据的版本，解压后需自行填 `config/*.json`。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import zipfile
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
PACKAGE_NAME = "ivd-bid-radar"

# 运行必需。列成清单而不是整目录拷贝，避免把 .tmp、_scratch、旧包一起带走。
FILES = [
    "SKILL.md",
    "README.md",
    "AGENT_HANDOFF.md",
    ".gitignore",
    "agents/openai.yaml",
    "references/schema.md",
    "references/verification.md",
    "references/keywords.md",
    "references/zlbx.md",
    "references/dedup.md",
    "scripts/zlbx_search.py",
    "scripts/tender_search.py",
    "scripts/tender_pipeline.py",
    "scripts/search_common.py",
    "scripts/hospital_match.py",
    "scripts/send_record.py",
    "scripts/send_record.ps1",
    "scripts/tender_identity.py",
    "scripts/tender_ledger.py",
    "scripts/feishu_client.py",
    "scripts/dedup_match.py",
    "config/zlbx.example.json",
    "config/feishu_app.example.json",
    # 医院索引：没有它跑不出医院全名与等级。台账不再随包分发，运行时从飞书拉。
    "data/hospitals.min.json.gz",
    "data/query_stats.json",
    # 每词命中数：带上它，首次运行就是热态 27 次调用而不是冷启动的 50 次。
    "data/query_hits.json",
]
# 部署后自检用，不参与运行。
TEST_FILES = [
    "tests/test_zlbx_search.py",
    "tests/test_search_common.py",
    "tests/test_tender_search.py",
    "tests/test_field_quality.py",
    "tests/test_notice_stage.py",
    "tests/test_webhook_schema.py",
    "tests/test_dedup_contract.py",
    "tests/test_dedup_match.py",
    "tests/fake_feishu.py",
]
SECRET_FILES = ["config/zlbx.json", "config/feishu_app.json"]

QUICKSTART = """# 快速开始

开箱即用包，凭据已内置。Python 3.9+ 标准库，无需安装第三方包。

## 1. 放到技能目录

解压后整个 `ivd-bid-radar/` 目录放进你的技能目录，例如 `~/.hermes/skills/`。

**凭据已在包内**（`config/zlbx.json` 知了 API Key、`config/feishu_app.json` 飞书自建
应用凭据与目标多维表格），不需要再配环境变量。两个文件权限应为 `0600`；Windows 或部分
解压工具不保留权限位，在 macOS/Linux 上解压后确认一次：

    chmod 600 config/zlbx.json config/feishu_app.json

**这个包含明文凭据，不要提交版本库、不要转发。**

## 2. 自检

    python3 -m unittest discover -s tests        # 全部测试应通过
    python3 scripts/tender_search.py --dry-run   # 不发请求、不读凭据，校验清单与参数

## 3. 跑一次完整流程

    python3 scripts/tender_search.py
    python3 scripts/tender_pipeline.py prepare --search-dir .tmp/search/<日期> --batch-size 10
    python3 scripts/tender_pipeline.py next-batch --run-dir .tmp/search/<日期>

之后按 `SKILL.md` 走核实、提交批次、DryRun、推送、登记回执。
默认窗口 72 小时；`--time-range 24h` 或 `YYYY-MM-DD..YYYY-MM-DD` 可改。

长期台账就是飞书多维表格本身，本地不再保存去重库，升级时也没有台账要保留或合并。
每次检索开头拉一份快照写进运行目录（`ledger_snapshot.json`），发送前再拉一次最新的
重新查重。拉不到台账时整轮停下，绝不按空台账继续。
判不了是否重复的候选扣在 pipeline/semantic_review.jsonl，模型判定后用
tender_pipeline.py resolve-semantic 登记结论，不要绕过。
写入结果未知时不会自动重发；下一轮拉取台账会自然消解，手工重发才会造成重复行。

## 4. 花多少钱

按调用次数计费。热态一轮 72h 窗约 **66 积分**（列表约 27 + 每条通过预筛的候选 1 次
详情），每天跑一次约 **¥132/月**。

`data/query_hits.json` 已随包带上，所以**第一次运行就是热态**（列表约 27 次）；
删掉它会退回冷启动，列表约 50 次。

## 5. 出问题先看这两处

- **退出码 3** = API Key 缺失、被拒或积分不足。这类失败长得像「今天没情报」，
  务必当凭证故障报警，不要当空结果放过。
- `.tmp/search/<日期>/search_summary.json` 里的 `source_auth_failed`、
  `raw_result_count`、`cost_units` 是判断「是没数据还是没跑成」的第一手依据。

接口的实测行为与坑（分页不稳定、必须传 fulltext、pub_time 早一天等）见
`references/zlbx.md`；改检索层前务必先读。
"""


def build(out_dir, include_secrets, include_tests):
    if out_dir.exists():
        shutil.rmtree(out_dir)
    names = FILES + (TEST_FILES if include_tests else [])
    if include_secrets:
        names += SECRET_FILES
    missing = [name for name in names if not (ROOT / name).exists()]
    if missing:
        raise SystemExit(f"缺少文件，无法打包：{missing}")

    for name in names:
        target = out_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
        if name in SECRET_FILES:
            target.chmod(0o600)
    (out_dir / "快速开始.md").write_text(QUICKSTART, encoding="utf-8")
    if not include_secrets:
        guide = (out_dir / "快速开始.md").read_text(encoding="utf-8")
        guide = guide.replace("开箱即用包，凭据已内置。", "本包不含凭据，需自行配置 config/*.json。")
        start = guide.index("**凭据已在包内**")
        end = guide.index("## 2. 自检", start)
        guide = guide[:start] + "按 config/*.example.json 配置本机凭据，然后运行自检。\n\n" + guide[end:]
        (out_dir / "快速开始.md").write_text(guide, encoding="utf-8")
    return names + ["快速开始.md"]


def verify(package_dir, include_tests):
    """在包内跑自检：--dry-run 不读凭据也不发请求；测试全绿才算这个包是活的。"""
    checks = []
    completed = subprocess.run(
        [sys.executable, "scripts/tender_search.py", "--dry-run"],
        cwd=package_dir, capture_output=True, text=True,
        # 子进程按 UTF-8 吐中文；不写死就走 locale（简中 Windows 是 cp936），
        # 解码线程会炸掉，stdout 变成 None，自检结论看不出真因。
        encoding="utf-8", errors="replace",
    )
    ok = completed.returncode == 0 and '"query_count"' in completed.stdout
    checks.append(("tender_search --dry-run", ok, completed.stderr.strip()[:200]))

    if include_tests:
        completed = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
            cwd=package_dir, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        tail = completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else ""
        checks.append(("unittest discover", completed.returncode == 0, tail))
    return checks


def main():
    parser = argparse.ArgumentParser(description="打开箱即用分发包")
    parser.add_argument("--no-secrets", action="store_true", help="不带凭据")
    parser.add_argument("--no-tests", action="store_true", help="不带自检用例")
    parser.add_argument("--out", help="输出目录；默认 dist/")
    parser.add_argument("--zip-only", action="store_true",
                        help="只留压缩包；自检仍在包目录里跑，跑完把目录删掉")
    args = parser.parse_args()

    include_secrets = not args.no_secrets
    include_tests = not args.no_tests
    dist = Path(args.out) if args.out else DIST
    stamp = date.today().strftime("%Y%m%d")
    suffix = "" if include_secrets else "-nosecrets"
    package_dir = dist / f"{PACKAGE_NAME}{suffix}"

    names = build(package_dir, include_secrets, include_tests)
    checks = verify(package_dir, include_tests)
    # 自检是在包目录里跑的，会留下 __pycache__。必须在压缩前清掉，
    # 否则字节码进包，换个 Python 小版本解压出来就是一堆无用文件。
    for cache in package_dir.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)

    archive = dist / f"{PACKAGE_NAME}-{stamp}{suffix}.zip"
    if archive.exists():
        archive.unlink()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as handle:
        for path in sorted(package_dir.rglob("*")):
            if path.is_file():
                handle.write(path, path.relative_to(package_dir.parent))

    # 自检必须先在真实目录里跑完，删目录只是最后一步：没跑过自检的包不该发出去。
    if args.zip_only:
        shutil.rmtree(package_dir, ignore_errors=True)
        print("包目录：已删除（--zip-only）")
    else:
        print(f"包目录：{package_dir}")
    print(f"压缩包：{archive}（{archive.stat().st_size / 1024:.0f} KB，{len(names)} 个文件）")
    print(f"含凭据：{'是' if include_secrets else '否'}　含自检用例：{'是' if include_tests else '否'}")
    for label, ok, detail in checks:
        print(f"  自检 {label}: {'通过' if ok else '失败 ' + detail}")
    return 0 if all(ok for _, ok, _ in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
