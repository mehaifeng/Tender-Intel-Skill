#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""飞书多维表格开放接口客户端：读台账、写新记录、按链接回查。

只用标准库。读操作可重试；**写操作绝不自动重试**——多维表格没有唯一约束，
重试一次结果未知的插入就等于制造重复行。写入结果未知时由调用方回查确认。
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

BASE = "https://open.feishu.cn/open-apis"
ROOT = Path(__file__).resolve().parent.parent
APP_CONFIG = ROOT / "config" / "feishu_app.json"
TIMEOUT = 30
# 多维表格按租户时区渲染日期字段，这张表在 Asia/Shanghai。
TENANT_TZ = timezone(timedelta(hours=8))
PAGE_SIZE = 500
READ_RETRIES = 2

# 多维表格字段类型号，见开放平台「字段编辑指南」。
TEXT, NUMBER, SINGLE_SELECT, MULTI_SELECT, DATETIME, CHECKBOX = 1, 2, 3, 4, 5, 7
URL_FIELD, AUTO_NUMBER, CREATED_TIME, MODIFIED_TIME = 15, 1005, 1001, 1002


class FeishuError(Exception):
    pass


class FeishuWriteResultUnknown(FeishuError):
    """写请求遇到传输故障，服务端可能已经落库，调用方必须回查且不得重试。"""


def load_credentials(config_path=None):
    """环境变量优先，其次受保护的本地配置。缺任何一项都直接失败，不猜。"""
    keys = ("app_id", "app_secret", "app_token", "table_id")
    values = {key: (os.environ.get("FEISHU_" + key.upper()) or "").strip() for key in keys}
    sources = {key: "环境变量FEISHU_" + key.upper() for key, value in values.items() if value}
    path = Path(config_path) if config_path else APP_CONFIG
    if not all(values.values()) and path.exists():
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise FeishuError(f"飞书应用配置不可读：{path}") from exc
        for key in keys:
            value = str(config.get(key) or "").strip()
            if not values[key] and value and not value.startswith("填入"):
                values[key] = value
                sources[key] = str(path)
    missing = [key for key in keys if not values[key]]
    if missing:
        raise FeishuError(
            "缺少飞书应用凭据：" + "、".join(missing)
            + f"；设置环境变量 FEISHU_APP_ID 等，或填写 {path}"
        )
    values["_sources"] = sources
    return values


class FeishuClient:
    def __init__(self, credentials=None, transport=None, config_path=None):
        self.credentials = credentials or load_credentials(config_path)
        self.transport = transport or urlopen
        self._token = ""
        self._token_expires = 0.0
        self._fields = None
        self.request_count = 0

    # ---- 传输 ----

    def _call(self, method, path, body=None, params=None, retries=0,
              uncertain_on_transport=False):
        url = BASE + path + ("?" + urlencode(params) if params else "")
        headers = {"Content-Type": "application/json; charset=utf-8",
                   "Authorization": "Bearer " + self.token()}
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        for attempt in range(retries + 1):
            request = Request(url, data=data, method=method, headers=headers)
            try:
                self.request_count += 1
                with self.transport(request, timeout=TIMEOUT) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                error = f"飞书接口 {path} 调用失败：{exc}"
                if uncertain_on_transport:
                    raise FeishuWriteResultUnknown(error) from exc
                raise FeishuError(error) from exc
            if payload.get("code") == 0:
                return payload.get("data") or {}
            # 业务错误码不重试：重试改变不了权限、参数或配额问题。
            raise FeishuError(
                f"飞书接口 {path} 返回 code={payload.get('code')}：{payload.get('msg')}")
        raise FeishuError(f"飞书接口 {path} 调用失败")

    def token(self):
        if self._token and time.time() < self._token_expires:
            return self._token
        request = Request(
            BASE + "/auth/v3/tenant_access_token/internal", method="POST",
            data=json.dumps({"app_id": self.credentials["app_id"],
                             "app_secret": self.credentials["app_secret"]}).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"})
        try:
            self.request_count += 1
            with self.transport(request, timeout=TIMEOUT) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            raise FeishuError(f"获取 tenant_access_token 失败：{exc}") from exc
        if payload.get("code") != 0 or not payload.get("tenant_access_token"):
            raise FeishuError(
                f"获取 tenant_access_token 被拒：code={payload.get('code')} {payload.get('msg')}")
        self._token = payload["tenant_access_token"]
        self._token_expires = time.time() + max(60, int(payload.get("expire") or 7200) - 300)
        return self._token

    # ---- 表结构 ----

    @property
    def table_path(self):
        return (f"/bitable/v1/apps/{quote(self.credentials['app_token'])}"
                f"/tables/{quote(self.credentials['table_id'])}")

    def fields(self):
        """字段名 -> {type, field_id, options}。写入形态据此决定，不写死表结构。"""
        if self._fields is None:
            data = self._call("GET", self.table_path + "/fields",
                              params={"page_size": 100}, retries=READ_RETRIES)
            self._fields = {
                item["field_name"]: {
                    "type": item.get("type"),
                    "field_id": item.get("field_id"),
                    "options": [o.get("name") for o in
                                ((item.get("property") or {}).get("options") or [])],
                }
                for item in data.get("items", [])
            }
        return self._fields

    # ---- 读 ----

    def search_records(self, field_names=None, filter_=None, page_size=PAGE_SIZE):
        body = {"automatic_fields": True}
        if field_names:
            body["field_names"] = list(field_names)
        if filter_:
            body["filter"] = filter_
        items, page_token = [], None
        while True:
            params = {"page_size": page_size}
            if page_token:
                params["page_token"] = page_token
            data = self._call("POST", self.table_path + "/records/search", body=body,
                              params=params, retries=READ_RETRIES)
            items.extend(data.get("items", []))
            if not data.get("has_more"):
                return items
            page_token = data.get("page_token")
            if not page_token:
                return items

    def find_by_url(self, url, field_names=None):
        """按链接精确回查。写入结果未知时用它判断这一行到底落没落。"""
        url = str(url or "").strip()
        if not url:
            return []
        return self.search_records(
            field_names=field_names,
            filter_={"conjunction": "and",
                     "conditions": [{"field_name": "链接", "operator": "is", "value": [url]}]},
        )

    # ---- 写 ----

    def create_record(self, fields):
        """新增一行。调用方必须把未知结果当作「可能已写入」，禁止自动重试。"""
        data = self._call(
            "POST", self.table_path + "/records", body={"fields": fields}, retries=0,
            uncertain_on_transport=True,
        )
        return data.get("record") or {}


# ---- 单元格值的读写形态 ----

def cell_text(value):
    """把多维表格返回的各种单元格形态压成纯文本。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else ""
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "".join(cell_text(item) for item in value)
    if isinstance(value, dict):
        for key in ("text", "link", "name", "full_name", "en_name"):
            if value.get(key):
                return cell_text(value[key])
        return ""
    return str(value)


def epoch_ms(value):
    """`2026-09-04` / `2026-09-04T09:00` -> 毫秒时间戳；已是时间戳的原样返回。

    多维表格的日期字段收发的都是毫秒时间戳，按租户时区（Asia/Shanghai）渲染。
    载荷里的日期是本地日历日，所以按 +08 解释，否则会整体前移一天。

    空值返回 None（这个字段不写），但认不出的写法一律抛错：`cell_value` 里
    None 的语义是"不写"，认不出就返回 None 会让日期整列静默落空。
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    match = re.fullmatch(r"(20\d{2})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?", text)
    if not match:
        raise FeishuError(
            f"日期字段值无法解析：{text!r}；只认 YYYY-MM-DD 或 YYYY-MM-DD[T ]HH:MM[:SS]")
    year, month, day, hour, minute, second = match.groups()
    try:
        stamp = datetime(int(year), int(month), int(day),
                         int(hour or 0), int(minute or 0), int(second or 0), tzinfo=TENANT_TZ)
    except ValueError as exc:
        raise FeishuError(f"日期字段值不是合法日期：{text!r}（{exc}）") from exc
    return int(stamp.timestamp() * 1000)


def date_text(value, with_time=False):
    """日期字段读出来是毫秒时间戳，按租户时区还原成 `YYYY-MM-DD`。

    判重、身份比对都按日历日算，拿到毫秒数会让 `publish_date` 解析失败、日期门
    静默失效，所以读台账时统一在这里还原。
    """
    text = cell_text(value).strip()
    if not text.isdigit():
        return text
    stamp = datetime.fromtimestamp(int(text) / 1000, TENANT_TZ)
    return stamp.strftime("%Y-%m-%dT%H:%M" if with_time else "%Y-%m-%d")


def cell_value(field_type, value):
    """按字段类型生成写入值；返回 None 表示这个字段不写。"""
    if value is None:
        return None
    if field_type == URL_FIELD:
        url = str(value).strip()
        return {"link": url, "text": url} if url else None
    if field_type == CHECKBOX:
        return bool(value)
    if field_type == DATETIME:
        return epoch_ms(value)
    if field_type == NUMBER:
        return float(value)
    text = str(value).strip()
    return text or None
