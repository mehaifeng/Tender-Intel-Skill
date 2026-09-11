#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验固定16字段载荷，并用飞书开放接口把它写成多维表格的一行。

去重的唯一真相源是飞书台账本身：每次发送前重新拉一遍，用同一套漏斗再查一次重，
确认无重复才写入。写入结果未知时**不重试**，改为按链接回查确认行到底落没落；
仍不确定就停下来，由下一轮拉取台账自然消解，绝不盲目再写一次。
"""

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from dedup_match import LedgerMatcher
from feishu_client import FeishuClient, FeishuError, FeishuWriteResultUnknown, cell_value
from tender_identity import remember_aliases
from tender_ledger import LedgerError, fetch_ledger, ledger_lock, save_snapshot, to_record


FIELDS = [
    "标题", "项目编号", "单位", "地区", "所属省/市", "所属大区", "发布时间", "截止时间",
    "预算", "采购方式", "科室", "命中关键词", "内容（检索的摘要）", "链接",
    "医院全名", "医院等级",
]
# 载荷字段 -> 多维表格字段。其余字段两边同名。
FIELD_ALIASES = {"科室": "科室名称", "命中关键词": "关键词命中", "内容（检索的摘要）": "内容"}
# 表里区分来源与初始状态的两列，接口写入时没人填，发送器补上。
# 「是否已推送」和两个时间戳归表格自身的工作流，发送器不碰。
CONSTANT_FIELDS = {"标讯来源": "AI收集", "标讯状态": "新插入"}
PROVINCE_LEVEL_DIVISIONS = {
    "北京", "天津", "上海", "重庆", "河北", "山西", "辽宁", "吉林", "黑龙江",
    "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南",
    "广东", "广西", "海南", "四川", "贵州", "云南", "西藏", "陕西", "甘肃",
    "青海", "宁夏", "新疆", "内蒙古",
}
PROVINCE_FULL_NAMES = {
    "北京": "北京市", "天津": "天津市", "上海": "上海市", "重庆": "重庆市",
    "河北": "河北省", "山西": "山西省", "辽宁": "辽宁省", "吉林": "吉林省",
    "黑龙江": "黑龙江省", "江苏": "江苏省", "浙江": "浙江省", "安徽": "安徽省",
    "福建": "福建省", "江西": "江西省", "山东": "山东省", "河南": "河南省",
    "湖北": "湖北省", "湖南": "湖南省", "广东": "广东省", "广西": "广西壮族自治区",
    "海南": "海南省", "四川": "四川省", "贵州": "贵州省", "云南": "云南省",
    "西藏": "西藏自治区", "陕西": "陕西省", "甘肃": "甘肃省", "青海": "青海省",
    "宁夏": "宁夏回族自治区", "新疆": "新疆维吾尔自治区", "内蒙古": "内蒙古自治区",
}

ROOT = Path(__file__).resolve().parent.parent


class SendError(Exception):
    pass


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SendError(f"找不到文件：{path}") from exc
    except json.JSONDecodeError as exc:
        raise SendError(f"JSON无效：{path}: {exc}") from exc


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def is_within(path, directory):
    try:
        Path(path).resolve().relative_to(Path(directory).resolve())
        return True
    except ValueError:
        return False


def atomic_write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def validate_payload(payload):
    errors = []
    if not isinstance(payload, dict):
        return ["载荷必须是单条对象，不能是数组"]
    if list(payload) != FIELDS:
        missing = [field for field in FIELDS if field not in payload]
        extra = [field for field in payload if field not in FIELDS]
        if missing:
            errors.append(f"缺少字段：{missing}")
        if extra:
            errors.append(f"多余字段：{extra}")
        if not missing and not extra:
            errors.append("字段顺序与固定16字段不一致")
    for field, value in payload.items():
        if not isinstance(value, str) or value == "":
            errors.append(f"{field}必须是非空字符串；缺失填null")
    if payload.get("标题") == "null":
        errors.append("标题必填，不接受null")
    if payload.get("命中关键词") == "null":
        # 交给业务方的这条消息必须能解释「为什么会检索到它」，说不出命中词就别发。
        errors.append("命中关键词必填，不接受null")
    if payload.get("链接") == "null":
        # 链接是台账里唯一 100% 覆盖的身份字段，缺了就无法回查、无法查重。
        errors.append("链接必填，不接受null")
    province = payload.get("所属省/市")
    if province != "null" and province not in PROVINCE_LEVEL_DIVISIONS:
        errors.append("所属省/市必须是省级行政区或直辖市简称，例如北京、河北、上海、新疆")
    location = payload.get("地区")
    if location != "null":
        expected_prefix = PROVINCE_FULL_NAMES.get(province)
        if not expected_prefix or not location.startswith(expected_prefix):
            errors.append("地区必须以所属省份、自治区或直辖市全称开头，例如安徽省凤阳县、北京市朝阳区")
    return errors


def build_fields(payload, schema):
    """16字段载荷 -> 多维表格写入体。空值直接不传，不再往表里写字符串 null。"""
    fields, dropped = {}, []
    values = {FIELD_ALIASES.get(k, k): v for k, v in payload.items()}
    values.update(CONSTANT_FIELDS)
    for name, raw in values.items():
        meta = schema.get(name)
        if meta is None:
            dropped.append({"字段": name, "值": raw, "原因": "表中没有这个字段"})
            continue
        if isinstance(raw, str) and raw.strip() in {"", "null"}:
            continue  # 空值不传，让单元格保持真正的空
        if meta["options"] and str(raw) not in meta["options"]:
            # 单选字段写未登记的选项会被丢掉，与其静默丢，不如留痕。
            dropped.append({"字段": name, "值": raw, "原因": "不在该单选字段的选项里"})
            continue
        value = cell_value(meta["type"], raw)
        if value is not None:
            fields[name] = value
    return fields, dropped


def validate_manifest(manifest_path, payload_path, payload_sha256):
    manifest_path = Path(manifest_path).resolve()
    manifest = load_json(manifest_path)
    declared = Path(manifest.get("pipeline_dir", "")).resolve() / "manifest.json"
    if manifest_path != declared:
        raise SendError("ManifestPath与manifest声明的pipeline_dir不一致")
    if not (
        manifest.get("mode") == "daily-push"
        and manifest.get("live_push_allowed") is True
        and manifest.get("state") in {"VALIDATED", "PUSHED"}
    ):
        raise SendError("manifest必须为daily-push、live_push_allowed=true且state=VALIDATED")
    payload_path = Path(payload_path).resolve()
    required = Path(manifest.get("payload_dir", "")).resolve() / "push"
    if not is_within(payload_path, required):
        raise SendError("载荷必须位于本次运行的pipeline/payloads/push目录")
    candidate_id = payload_path.stem
    matches = [
        row for row in manifest.get("payloads", [])
        if row.get("flow") == "push" and row.get("candidate_id") == candidate_id
    ]
    if len(matches) != 1:
        raise SendError("manifest中没有唯一匹配的已验证载荷")
    declared_path = Path(matches[0].get("path", "")).resolve()
    if declared_path != payload_path or matches[0].get("sha256") != payload_sha256:
        raise SendError("载荷路径或哈希与manifest不一致")
    return manifest, candidate_id


def send_once(manifest, candidate_id, payload_path, body, client=None):
    """拉最新台账、再查一次重、写入、登记。未知结果按链接回查，绝不自动重发。"""
    from tender_pipeline import find_queue_candidate, load_semantic_decisions
    payload_path = Path(payload_path).resolve()
    payload = json.loads(body.decode("utf-8"))
    pipeline_dir = Path(manifest["pipeline_dir"])
    candidate = find_queue_candidate(pipeline_dir, candidate_id)
    if candidate is None:
        raise SendError("本次队列没有该候选，禁止丢失公告身份后发送")
    record = dict(payload)
    remember_aliases(record, candidate)
    record["candidate_id"] = candidate_id
    snapshot_path = Path(manifest["ledger_snapshot"])
    receipt_path = pipeline_dir / "receipts" / f"push-{candidate_id}.json"
    sha = sha256_bytes(body)
    client = client or FeishuClient()

    with ledger_lock(snapshot_path):
        # 发送前必须看最新台账：本地快照可能落后于别人刚加的行。
        ledger = fetch_ledger(client)
        save_snapshot(snapshot_path, ledger)
        matcher = LedgerMatcher(ledger["records"], load_semantic_decisions(pipeline_dir))
        match = matcher.check(record)
        if match.verdict == "duplicate":
            if not receipt_path.exists():
                atomic_write_json(receipt_path, {
                    "schema_version": 4, "flow": "push", "candidate_id": candidate_id,
                    "payload_path": str(payload_path), "payload_sha256": sha,
                    "delivery_status": "already_seen", "reason": match.reason,
                    "match_layer": match.layer,
                    "matched_feishu_id": (match.matched or {}).get("_feishu_id"),
                    "checked_at": now_iso(),
                })
            return {"sent": False, "already_seen": True, "reason": match.reason,
                    "match_layer": match.layer, "receipt": str(receipt_path)}
        if match.verdict == "semantic":
            raise SendError(
                "与台账存在需要语义判定的相似公告，尚未发送："
                + "、".join(p.ledger.get("_feishu_id", "") for p in match.pairs)
                + "；先用 tender_pipeline.py resolve-semantic 登记结论"
            )

        fields, dropped = build_fields(payload, client.fields())
        try:
            created = client.create_record(fields)
        except FeishuWriteResultUnknown as exc:
            # 结果未知：行可能已经写进去了。按链接回查，回查不出来也不重试。
            recovered = None
            try:
                found = client.find_by_url(payload["链接"])
                recovered = found[0] if found else None
            except FeishuError:
                recovered = None
            if recovered is None:
                raise SendError(
                    f"写入结果未知且回查未找到该链接，已停止且未重试：{exc}；"
                    "下一轮拉取台账会自然消解，不要手工重发"
                ) from exc
            created = recovered

        record_id = created.get("record_id", "")
        if not record_id:
            raise SendError("飞书未返回 record_id，无法确认写入结果")
        stored = to_record({"record_id": record_id, "fields": created.get("fields") or fields})
        # create 返回体可能不带自动编号；至少保证身份字段可用于同批后续查重。
        stored["标题"] = stored["标题"] or payload["标题"]
        stored["链接"] = stored["链接"] or payload["链接"]
        receipt = {
            "schema_version": 4, "flow": "push", "candidate_id": candidate_id,
            "payload_path": str(payload_path), "payload_sha256": sha,
            "http_status": 200, "feishu_code": 0,
            "feishu_record_id": record_id, "feishu_id": stored.get("_feishu_id", ""),
            "dropped_fields": dropped, "written_field_count": len(fields),
            "confirmed_at": now_iso(),
        }
        atomic_write_json(receipt_path, receipt)
        # 保持同一把锁直到本地快照也包含新行，避免另一进程在写入成功与快照更新之间
        # 抢到锁、读到旧快照后再次写入同一公告。
        if not any(r.get("_record_id") and r["_record_id"] == stored.get("_record_id")
                   for r in ledger["records"]):
            ledger["records"].append(stored)
            ledger["row_count"] = len(ledger["records"])
            save_snapshot(snapshot_path, ledger)
    return {"sent": True, "feishu_record_id": record_id, "feishu_id": stored.get("_feishu_id", ""),
            "dropped_fields": dropped, "receipt": str(receipt_path)}


def main():
    # DryRun 会把整条载荷打回控制台。Windows 控制台默认 GBK，公告正文里的零宽连接符
    # （U+200D）这类字符直接抛 UnicodeEncodeError，把 SKILL 规定的推送前离线校验卡死。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="校验并用飞书接口写入固定16字段记录")
    parser.add_argument("--payload", required=True)
    parser.add_argument("--manifest")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--live", action="store_true")
    args = parser.parse_args()

    try:
        payload_path = Path(args.payload).resolve()
        payload = load_json(payload_path)
        errors = validate_payload(payload)
        if errors:
            raise SendError("载荷校验失败：\n- " + "\n- ".join(errors))
        body = payload_path.read_bytes()
        payload_sha256 = sha256_bytes(body)

        if args.dry_run:
            configured = True
            detail = {}
            try:
                client = FeishuClient()
                schema = client.fields()
            except FeishuError as exc:
                configured = False
                detail = {"feishu_error": str(exc)}
            else:
                # 组字段时的报错（比如日期写法不认）不揉进 feishu_error，那会
                # 把一条格式错误报成"没配好飞书"。让它冒到外层，dry-run 直接失败。
                fields, dropped = build_fields(payload, schema)
                detail = {"table_fields": len(fields), "dropped_fields": dropped,
                          "omitted_null_fields": sorted(
                              k for k, v in payload.items() if v == "null")}
            print(json.dumps({
                "valid": True, "sent": False, "field_count": len(payload), "bytes": len(body),
                "feishu_configured": configured, **detail, "payload": payload,
            }, ensure_ascii=False, indent=2))
            return 0

        if not args.manifest:
            raise SendError("Live模式必须提供--manifest")
        manifest, candidate_id = validate_manifest(args.manifest, payload_path, payload_sha256)
        result = send_once(manifest, candidate_id, payload_path, body)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (SendError, LedgerError, FeishuError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
