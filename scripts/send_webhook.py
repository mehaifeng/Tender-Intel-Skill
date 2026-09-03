#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验并发送IVD Bid Radar固定16字段Webhook载荷。"""

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


FIELDS = [
    "标题", "项目编号", "单位", "地区", "所属省/市", "所属大区", "发布时间", "截止时间",
    "预算", "采购方式", "科室", "命中关键词", "内容（检索的摘要）", "链接",
    "医院全名", "医院等级",
]
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
WEBHOOK_CONFIG = ROOT / "config" / "webhook.json"


class SendError(Exception):
    pass


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SendError(f"找不到文件：{path}") from exc
    except json.JSONDecodeError as exc:
        raise SendError(f"JSON无效：{path}: {exc}") from exc


def load_webhook_url(explicit=None):
    """按命令行（仅DryRun）>环境变量>本地受保护配置读取Webhook。"""
    if explicit:
        return explicit.strip(), "命令行"
    for name in ("FEISHU_WEBHOOK_URL", "FEISHU_CREATE_WEBHOOK_URL"):
        value = os.environ.get(name)
        if value:
            return value.strip(), f"环境变量{name}"
    if WEBHOOK_CONFIG.exists():
        cfg = load_json(WEBHOOK_CONFIG)
        value = (cfg.get("webhook_url") or "").strip()
        if value and not value.startswith("填入"):
            return value, str(WEBHOOK_CONFIG.relative_to(ROOT))
    return "", ""


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
    province = payload.get("所属省/市")
    if province != "null" and province not in PROVINCE_LEVEL_DIVISIONS:
        errors.append("所属省/市必须是省级行政区或直辖市简称，例如北京、河北、上海、新疆")
    location = payload.get("地区")
    if location != "null":
        expected_prefix = PROVINCE_FULL_NAMES.get(province)
        if not expected_prefix or not location.startswith(expected_prefix):
            errors.append("地区必须以所属省份、自治区或直辖市全称开头，例如安徽省凤阳县、北京市朝阳区")
    return errors


def validate_manifest(manifest_path, payload_path, payload_sha256):
    manifest_path = Path(manifest_path).resolve()
    manifest = load_json(manifest_path)
    declared = Path(manifest.get("pipeline_dir", "")).resolve() / "manifest.json"
    if manifest_path != declared:
        raise SendError("ManifestPath与manifest声明的pipeline_dir不一致")
    if not (
        manifest.get("mode") == "daily-push"
        and manifest.get("live_push_allowed") is True
        and manifest.get("state") == "VALIDATED"
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


def main():
    parser = argparse.ArgumentParser(description="发送固定16字段IVD Bid Radar Webhook载荷")
    parser.add_argument("--payload", required=True)
    parser.add_argument("--manifest")
    parser.add_argument("--webhook-url", help="仅DryRun可显式传入；Live使用环境变量或config/webhook.json")
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

        webhook_url, webhook_source = load_webhook_url(args.webhook_url)

        if args.dry_run:
            print(json.dumps({
                "valid": True,
                "sent": False,
                "field_count": len(payload),
                "bytes": len(body),
                "webhook_configured": bool(webhook_url),
                "webhook_source": webhook_source or "未配置",
                "payload": payload,
            }, ensure_ascii=False, indent=2))
            return 0

        if args.webhook_url:
            raise SendError("Live模式不接受命令行Webhook URL，必须使用环境变量或受保护的本地配置")
        if not args.manifest:
            raise SendError("Live模式必须提供--manifest")
        manifest, candidate_id = validate_manifest(args.manifest, payload_path, payload_sha256)
        if not webhook_url:
            raise SendError("未配置Webhook；请设置环境变量或config/webhook.json")

        request = Request(
            webhook_url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urlopen(request, timeout=30) as response:
                status = response.status
                response_body = response.read().decode("utf-8")
        except HTTPError as exc:
            raise SendError(f"Webhook HTTP错误：{exc.code}") from exc
        except URLError as exc:
            raise SendError(f"Webhook请求失败：{exc.reason}") from exc
        if status != 200:
            raise SendError(f"Webhook HTTP状态不是200：{status}")
        try:
            response_json = json.loads(response_body)
        except json.JSONDecodeError as exc:
            raise SendError("Webhook响应不是JSON") from exc
        if response_json.get("code") != 0:
            raise SendError(f"Webhook未确认成功：code={response_json.get('code')}")

        receipt_path = Path(manifest["pipeline_dir"]) / "receipts" / f"push-{candidate_id}.json"
        atomic_write_json(receipt_path, {
            "schema_version": 2,
            "flow": "push",
            "candidate_id": candidate_id,
            "payload_path": str(payload_path),
            "payload_sha256": payload_sha256,
            "http_status": 200,
            "feishu_code": 0,
            "confirmed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        })
        print(json.dumps({
            "sent": True,
            "http_status": 200,
            "feishu_code": 0,
            "receipt": str(receipt_path),
        }, ensure_ascii=False, indent=2))
        return 0
    except SendError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
