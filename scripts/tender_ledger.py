"""长期台账：唯一真相源是飞书多维表格，本地不再保存去重库。

每次检索开头拉一份台账快照放进本次运行目录，供列表阶段预筛复用；发送前再拉一次
最新的重新查重。快照是运行产物，删掉重跑即可，不是资产，也不参与跨运行的记忆。
"""
from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from feishu_client import FeishuClient, FeishuError, cell_text

SNAPSHOT_NAME = "ledger_snapshot.json"
SCHEMA_VERSION = 4
# 只拉判重要用的字段：内容用于正文语义比对，标讯来源用于区分 AI 收集与人工录入。
LEDGER_FIELDS = ["编号", "标题", "项目编号", "单位", "医院全名", "所属省/市",
                 "地区", "发布时间", "链接", "内容", "标讯来源"]


class LedgerError(Exception):
    pass


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


@contextmanager
def ledger_lock(path):
    """OS 锁会随进程退出释放，锁文件本身不表示占用，不需删除旧锁文件。"""
    path = Path(str(Path(path).resolve()) + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            if path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise LedgerError("另一进程正在更新台账或发送，请稍后重试") from exc
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise LedgerError("另一进程正在更新台账或发送，请稍后重试") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def to_record(item):
    """一行多维表格 -> 判重记录。业务字段一律不带进来，只留身份与正文。"""
    fields = item.get("fields") or {}
    record = {name: cell_text(fields.get(name)) for name in
              ("标题", "项目编号", "单位", "医院全名", "所属省/市", "地区", "发布时间", "链接", "内容")}
    record["_feishu_id"] = cell_text(fields.get("编号"))
    record["_record_id"] = item.get("record_id", "")
    record["_feishu_source"] = cell_text(fields.get("标讯来源"))
    record["_pushed"] = True
    return record


def fetch_ledger(client=None):
    """全量拉取飞书台账。失败必须抛错，绝不允许按空台账继续。"""
    client = client or FeishuClient()
    try:
        items = client.search_records(field_names=LEDGER_FIELDS)
    except FeishuError as exc:
        raise LedgerError(f"拉取飞书台账失败，禁止按空台账继续：{exc}") from exc
    records = [to_record(item) for item in items]
    blank = [r["_record_id"] for r in records if not r["标题"].strip() or not r["链接"].strip()]
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "feishu_api",
        "app_token": client.credentials["app_token"],
        "table_id": client.credentials["table_id"],
        "fetched_at": now_iso(),
        "row_count": len(records),
        "rows_missing_identity": blank,
        "records": records,
    }


def snapshot_path(run_dir):
    return Path(run_dir) / SNAPSHOT_NAME


def save_snapshot(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=1)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_snapshot(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LedgerError(f"台账快照不可读，禁止按空台账继续：{path}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        raise LedgerError(f"台账快照必须含 records 数组：{path}")
    if data.get("source") != "feishu_api":
        raise LedgerError(f"台账快照不是飞书接口拉取的结果：{path}")
    return data


def refresh_snapshot(run_dir, client=None):
    """拉一份最新台账写进运行目录，返回 (路径, 快照)。"""
    path = snapshot_path(run_dir)
    data = fetch_ledger(client)
    save_snapshot(path, data)
    return path, data


def append_record(path, record):
    """成功写入飞书之后把新行并进本次运行的快照，供同批后续候选查重。"""
    path = Path(path)
    with ledger_lock(path):
        data = read_snapshot(path)
        if not any(r.get("_record_id") and r["_record_id"] == record.get("_record_id")
                   for r in data["records"]):
            data["records"].append(record)
            data["row_count"] = len(data["records"])
            save_snapshot(path, data)
    return record


def main():
    import argparse
    parser = argparse.ArgumentParser(description="拉取飞书台账快照或查看其概况")
    parser.add_argument("command", choices=["fetch", "show"])
    parser.add_argument("--run-dir", help="快照写入/读取的运行目录")
    parser.add_argument("--out", help="fetch 时直接指定快照路径")
    args = parser.parse_args()
    try:
        if args.command == "fetch":
            if not args.run_dir and not args.out:
                raise LedgerError("fetch 需要 --run-dir 或 --out")
            path = Path(args.out) if args.out else snapshot_path(args.run_dir)
            data = fetch_ledger()
            save_snapshot(path, data)
        else:
            if not args.run_dir:
                raise LedgerError("show 需要 --run-dir")
            path = snapshot_path(args.run_dir)
            data = read_snapshot(path)
        print(json.dumps({"snapshot": str(path), "fetched_at": data["fetched_at"],
                          "row_count": data["row_count"],
                          "rows_missing_identity": data.get("rows_missing_identity", [])},
                         ensure_ascii=False, indent=2))
        return 0
    except (LedgerError, FeishuError) as exc:
        print(f"错误：{exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
