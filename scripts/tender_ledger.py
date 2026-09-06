"""长期已入账台账与发送占位。同一台账的所有写入共用进程锁。"""
from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from tender_identity import IdentityIndex, remember_aliases


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


def read_ledger(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LedgerError(f"台账不可读，禁止按空台账继续：{path}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        raise LedgerError("台账必须含 records 数组")
    if not isinstance(data.get("deliveries", {}), dict):
        raise LedgerError("台账 deliveries 必须是对象")
    if not isinstance(data.get("review_overrides", []), list):
        raise LedgerError("台账 review_overrides 必须是数组")
    return data


def save_ledger(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def confirmed_index(data):
    return IdentityIndex(r for r in data["records"] if r.get("_pushed") is True)


def approved_index(data):
    """人工核对后判定「不是重复」的公告；prepare 与发送门禁据此放行。"""
    return IdentityIndex(data.get("review_overrides", []))


def add_confirmed(data, payload, candidate=None, confirmed_at=None):
    candidate = candidate or {}
    record = {k: v for k, v in payload.items() if k != "内容（检索的摘要）"}
    remember_aliases(record, candidate)
    existing, _ = confirmed_index(data).find(record)
    if existing is not None:
        remember_aliases(existing, record)
        return existing
    record.update({
        "_pushed": True,
        "_candidate_id": candidate.get("candidate_id", ""),
        "_first_seen": confirmed_at or now_iso(),
        "_last_seen": confirmed_at or now_iso(),
        "_found_by_query": candidate.get("found_by_query", []),
    })
    data["records"].append(record)
    return record


def remember_confirmed(path, payload, candidate=None, confirmed_at=None):
    with ledger_lock(path):
        data = read_ledger(path)
        result = add_confirmed(data, payload, candidate, confirmed_at)
        save_ledger(path, data)
        return result


def resolve_delivery(path, attempt_id, outcome, note):
    """仅用于人工核对飞书后的登记，不发送请求。"""
    if outcome not in {"delivered", "not-delivered"} or not note.strip():
        raise LedgerError("必须给出核对结论和说明")
    with ledger_lock(path):
        data = read_ledger(path)
        attempt = data.get("deliveries", {}).get(attempt_id)
        if not attempt or attempt["status"] != "pending":
            raise LedgerError("找不到待核对的发送尝试")
        if outcome == "delivered":
            add_confirmed(data, attempt["record"])
        attempt.update({"status": "confirmed" if outcome == "delivered" else "not_delivered",
                        "resolved_at": now_iso(), "resolution_note": note})
        save_ledger(path, data)
        return {"attempt_id": attempt_id, "status": attempt["status"]}


def resolve_review(path, record, outcome, note):
    """仅用于人工核对飞书后给「疑似重复」定性，不发送请求。"""
    if outcome not in {"duplicate", "new"} or not note.strip():
        raise LedgerError("必须给出核对结论和说明")
    with ledger_lock(path):
        data = read_ledger(path)
        confirmed = confirmed_index(data)
        existing, reason = confirmed.find(record)
        if outcome == "duplicate":
            # 已有强身份命中说明台账早已认得它，此时无需再补别名。
            if existing is None:
                existing, reason = confirmed.possible(record)
                if existing is None:
                    raise LedgerError("台账中已无对应的疑似记录，无需登记")
                # 把候选的链接与来源 ID 并入旧记录：下次是 find() 强命中，不再进待核对。
                remember_aliases(existing, record)
            save_ledger(path, data)
            return {"outcome": "duplicate", "matched_title": existing.get("标题"), "reason": reason}
        if existing is not None:
            raise LedgerError(f"该公告已按强身份认定为已入账（{reason}），不能登记为新公告")
        overrides = data.setdefault("review_overrides", [])
        approved, _ = IdentityIndex(overrides).find(record)
        if approved is None:
            approved = {k: v for k, v in record.items() if k != "内容（检索的摘要）"}
            remember_aliases(approved)
            approved["_review"] = {"outcome": "new", "note": note, "resolved_at": now_iso()}
            overrides.append(approved)
        save_ledger(path, data)
        return {"outcome": "new", "标题": approved.get("标题"), "链接": approved.get("链接")}


def main():
    import argparse
    parser = argparse.ArgumentParser(description="查看或核对未知发送结果")
    parser.add_argument("command", choices=["pending", "resolve-delivery"])
    parser.add_argument("--seen", default=str(Path(__file__).resolve().parents[1] / "data/seen.json"))
    parser.add_argument("--attempt-id")
    parser.add_argument("--outcome", choices=["delivered", "not-delivered"])
    parser.add_argument("--note", default="")
    args = parser.parse_args()
    try:
        if args.command == "pending":
            result = [{"attempt_id": a["attempt_id"], "started_at": a["started_at"],
                       "标题": a["record"].get("标题"), "链接": a["record"].get("链接")}
                      for a in read_ledger(args.seen).get("deliveries", {}).values() if a["status"] == "pending"]
        else:
            result = resolve_delivery(args.seen, args.attempt_id, args.outcome, args.note)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except LedgerError as exc:
        print(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
