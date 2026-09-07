# -*- coding: utf-8 -*-
"""离线替身：按真实表结构模拟飞书多维表格接口，测试不碰网络也不碰生产表。

字段类型与选项取自生产表 tbl8tFKaOFHW7Cau 的实际结构，写入形态一旦走偏就会被测试抓到。
"""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

TEXT, SINGLE_SELECT, DATETIME, CHECKBOX, URL_FIELD, AUTO_NUMBER = 1, 3, 5, 7, 15, 1005

SCHEMA = {
    "编号": {"type": AUTO_NUMBER, "options": []},
    "标题": {"type": TEXT, "options": []},
    "项目编号": {"type": TEXT, "options": []},
    "单位": {"type": TEXT, "options": []},
    "地区": {"type": TEXT, "options": []},
    "所属省/市": {"type": TEXT, "options": []},
    "所属大区": {"type": SINGLE_SELECT,
               "options": ["东北一区", "东北二区", "华北一区", "华北二区", "华东大区",
                           "华南大区", "东南大区", "西南大区", "西北大区", "华中大区",
                           "北京直管区"]},
    "发布时间": {"type": TEXT, "options": []},
    "截止时间": {"type": TEXT, "options": []},
    "预算": {"type": TEXT, "options": []},
    "采购方式": {"type": SINGLE_SELECT,
               "options": ["公开招标", "询价", "竞争性磋商", "竞争性谈判", "单一来源",
                           "中标公告", "其他"]},
    "科室名称": {"type": TEXT, "options": []},
    "关键词命中": {"type": TEXT, "options": []},
    "内容": {"type": TEXT, "options": []},
    "链接": {"type": URL_FIELD, "options": []},
    "医院全名": {"type": TEXT, "options": []},
    "医院等级": {"type": TEXT, "options": []},
    "标讯来源": {"type": SINGLE_SELECT, "options": ["AI收集", "大区员工录入", "总部员工录入"]},
    "标讯状态": {"type": SINGLE_SELECT, "options": ["新推送", "已跟踪", "已关闭"]},
    "是否已推送": {"type": CHECKBOX, "options": []},
    "插入表格的时间": {"type": DATETIME, "options": []},
    "推送时间": {"type": DATETIME, "options": []},
}

CREDENTIALS = {"app_id": "cli_test", "app_secret": "secret",
               "app_token": "basTEST", "table_id": "tblTEST"}


def ledger_row(标题="甲医院过敏原试剂采购公告", 链接="https://example.org/a",
               单位="甲医院", 发布时间="2026-09-04", 编号="ZB-000001", **extra):
    """按接口返回形态构造一行：文本字段是富文本数组，链接是对象。"""
    fields = {"标题": [{"text": 标题, "type": "text"}],
              "链接": {"link": 链接, "text": 链接, "type": "url"},
              "单位": [{"text": 单位, "type": "text"}],
              "发布时间": [{"text": 发布时间, "type": "text"}],
              "编号": 编号}
    for name, value in extra.items():
        fields[name] = [{"text": str(value), "type": "text"}] if value else None
    return {"record_id": "rec" + 编号, "fields": {k: v for k, v in fields.items() if v is not None}}


class _Response:
    status = 200

    def __init__(self, payload):
        self._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


class FakeFeishu:
    """可当作 FeishuClient(transport=...) 直接传入的假传输层。"""

    def __init__(self, rows=(), schema=None):
        self.rows = [dict(row) for row in rows]
        self.schema = schema or SCHEMA
        self.created = []
        self.searches = 0
        self.create_error = None       # 设成异常实例即模拟写入结果未知
        self.create_lands_anyway = False  # 报错但行其实落库了

    def __call__(self, request, timeout=None):
        url = request.full_url
        path = urlsplit(url).path
        body = json.loads(request.data.decode("utf-8")) if request.data else {}
        if path.endswith("/tenant_access_token/internal"):
            return _Response({"code": 0, "tenant_access_token": "t-test", "expire": 7200})
        if path.endswith("/fields"):
            return _Response({"code": 0, "data": {"items": [
                {"field_name": name, "field_id": "fld" + name, "type": meta["type"],
                 "property": {"options": [{"name": o} for o in meta["options"]]}}
                for name, meta in self.schema.items()]}})
        if path.endswith("/records/search"):
            self.searches += 1
            rows = self.rows
            conditions = (body.get("filter") or {}).get("conditions") or []
            for condition in conditions:
                wanted = set(condition.get("value") or [])
                rows = [r for r in rows
                        if self._text(r["fields"].get(condition["field_name"])) in wanted]
            return _Response({"code": 0, "data": {"items": rows, "has_more": False}})
        if path.endswith("/records"):
            if self.create_error is not None:
                if self.create_lands_anyway:
                    self._insert(body["fields"])
                raise self.create_error
            record = self._insert(body["fields"])
            return _Response({"code": 0, "data": {"record": record}})
        raise AssertionError("替身没有实现的接口：" + path)

    def _insert(self, fields):
        number = f"ZB-TEST-{len(self.rows) + 1:04d}"
        record_id = f"recTEST{len(self.rows) + 1}"
        self.rows.append({"record_id": record_id, "fields": dict(fields, 编号=number)})
        self.created.append(dict(fields))
        # 实测：新增记录的响应里不含自动编号，要再查一次才拿得到。发送器不能依赖它。
        return {"record_id": record_id, "fields": dict(fields)}

    @staticmethod
    def _text(value):
        if isinstance(value, list):
            return "".join(str(item.get("text", "")) for item in value)
        if isinstance(value, dict):
            return str(value.get("link") or value.get("text") or "")
        return "" if value is None else str(value)


def client(rows=(), transport=None):
    from feishu_client import FeishuClient
    fake = transport or FakeFeishu(rows)
    return FeishuClient(credentials=dict(CREDENTIALS), transport=fake), fake
