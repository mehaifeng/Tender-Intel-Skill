#!/usr/bin/env python3
"""只读取飞书 XLSX 台账的身份字段，不执行公式，不修改原文件。Python 标准库。"""
from __future__ import annotations

import argparse
import hashlib
import json
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

from tender_identity import publish_date, text, remember_aliases, IdentityIndex
from tender_identity import url_key, fingerprint
from tender_ledger import ledger_lock, read_ledger, save_ledger, now_iso, LedgerError

ROOT = Path(__file__).resolve().parents[1]
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
DATE_FIELDS = {"发布时间", "推送时间", "插入表格的时间"}
IDENTITY_FIELDS = ("标题", "项目编号", "单位", "医院全名", "所属省/市", "发布时间", "链接")


def xlsx_rows(path):
    with zipfile.ZipFile(path) as archive:
        strings = []
        if "xl/sharedStrings.xml" in archive.namelist():
            tree = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            strings = ["".join(t.text or "" for t in si.findall(".//m:t", NS)) for si in tree]
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        props = workbook.find("m:workbookPr", NS)
        epoch = datetime(1904, 1, 1) if props is not None and props.get("date1904") in {"1", "true"} else datetime(1899, 12, 30)
        rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))}
        matched = False
        for sheet in workbook.findall("m:sheets/m:sheet", NS):
            target = rels[sheet.get("{" + NS["r"] + "}id")]
            target = target.lstrip("/") if target.startswith("/") else posixpath.normpath("xl/" + target)
            tree = ET.fromstring(archive.read(target))
            # 不相信第三方导出器的 dimension；逐个读取真实 cell 节点。
            data = []
            for row in tree.findall("m:sheetData/m:row", NS):
                values = {}
                for cell in row.findall("m:c", NS):
                    col = "".join(c for c in cell.get("r", "") if c.isalpha())
                    typ = cell.get("t")
                    if typ == "inlineStr":
                        value = "".join(t.text or "" for t in cell.findall(".//m:t", NS))
                    else:
                        value = cell.findtext("m:v", "", NS)
                        if typ == "s":
                            value = strings[int(value)] if value else ""
                    values[col] = value
                data.append((int(row.get("r", "0")), values))
            if not data:
                continue
            headers = data[0][1]
            if not {"编号", "标题", "链接", "发布时间"}.issubset(set(headers.values())):
                continue
            matched = True
            for row_number, values in data[1:]:
                row = {name: values.get(col, "") for col, name in headers.items()}
                if not any(row.values()):
                    continue
                for name in DATE_FIELDS:
                    value = row.get(name, "")
                    if value and value.replace(".", "", 1).isdigit():
                        row[name] = (epoch + timedelta(days=float(value))).isoformat()
                yield sheet.get("name"), row_number, row
        if not matched:
            raise LedgerError("未找到含编号、标题、链接、发布时间的飞书台账工作表")


def export_records(path):
    result = []
    for sheet, row_number, row in xlsx_rows(path):
        if not text(row.get("标题")) or not text(row.get("编号")):
            raise LedgerError(f"{sheet} 第 {row_number} 行缺少标题或编号，未导入")
        published = publish_date(row.get("发布时间"))
        if not published:
            raise LedgerError(f"{sheet} 第 {row_number} 行发布时间无效，未导入")
        record = {k: text(row.get(k)) or "null" for k in IDENTITY_FIELDS}
        record["发布时间"] = published
        if not url_key(record["链接"]):
            # 导出表有把完整标题存进链接列的旧行。它不是 URL，但可能明确写出采购人。
            raw_link = record["链接"]
            record["_untrusted_link"] = raw_link
            record["链接"] = "null"
            if record["单位"] == "null":
                from hospital_match import get_default_index
                match = get_default_index().match(text=raw_link)
                name = match.get("hospital_name", "")
                if match.get("matched") and name and fingerprint(name) in fingerprint(raw_link):
                    record["单位"] = name
                    record["_buyer_evidence"] = "台账链接列的标题明确包含医院全名：" + name
                elif text(raw_link):
                    # 仅取标题开头明确写出的完整机构称谓，不补地理或等级。
                    prefix = re.match(r"^(.{2,40}?(?:医院|卫生院|保健院))", raw_link)
                    if prefix:
                        record["单位"] = prefix[1]
                        record["_buyer_evidence"] = "台账链接列标题开头的采购机构：" + prefix[1]
        record.update({
            "_feishu_id": text(row["编号"]), "_pushed": True,
            "_source": "feishu_export", "_feishu_delivery_flag": text(row.get("是否已推送")),
            "_feishu_duplicate_of": text(row.get("重复记录")),
            "_import_row": row_number, "_import_sheet": sheet,
        })
        remember_aliases(record)
        result.append(record)
    if not result:
        raise LedgerError("工作表没有可导入的公告，未修改台账")
    return result


def import_ledger(xlsx_path, seen_path, apply=False):
    source = export_records(xlsx_path)
    digest = hashlib.sha256(Path(xlsx_path).read_bytes()).hexdigest()
    with ledger_lock(seen_path):
        data = read_ledger(seen_path) if Path(seen_path).exists() else {"records": []}
        by_id = {r.get("_feishu_id"): r for r in data["records"] if r.get("_feishu_id")}
        inserted = updated = 0
        for incoming in source:
            current = by_id.get(incoming["_feishu_id"])
            if current is None:
                current = dict(incoming)
                data["records"].append(current)
                by_id[current["_feishu_id"]] = current
                inserted += 1
            else:
                remember_aliases(current, incoming)
                current.update(incoming | {
                    "_identity_urls": current["_identity_urls"],
                    "_identity_ids": current["_identity_ids"],
                })
                updated += 1
        index = IdentityIndex(r for r in data["records"] if r.get("_pushed") is True)
        covered = sum(index.find(r)[0] is not None for r in source)
        report = {"source_file": Path(xlsx_path).name, "source_sha256": digest,
                  "source_rows": len(source), "sales_delivery_checked": sum(r["_feishu_delivery_flag"] == "1" for r in source),
                  "inserted": inserted, "updated": updated, "ledger_rows": len(data["records"]),
                  "covered": covered, "applied": apply}
        if covered != len(source):
            raise LedgerError("导入后的身份覆盖验证失败，未写入")
        if apply:
            data["schema_version"] = 4
            data["retention"] = "permanent"
            data.setdefault("imports", {})[digest] = {"file": Path(xlsx_path).name, "rows": len(source), "imported_at": now_iso()}
            save_ledger(seen_path, data)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xlsx", required=True)
    parser.add_argument("--seen", default=str(ROOT / "data/seen.json"))
    parser.add_argument("--apply", action="store_true", help="写入台账；省略时只分析")
    args = parser.parse_args()
    try:
        print(json.dumps(import_ledger(args.xlsx, args.seen, args.apply), ensure_ascii=False, indent=2))
        return 0
    except (LedgerError, ValueError, OSError, zipfile.BadZipFile) as exc:
        print(f"导入失败：{exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
