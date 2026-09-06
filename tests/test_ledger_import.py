import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from import_feishu_ledger import import_ledger, export_records
from tender_ledger import read_ledger, save_ledger
from tender_identity import IdentityIndex


def workbook(path, rows):
    headers = ["编号", "标题", "单位", "发布时间", "链接", "是否已推送"]
    matrix = [headers] + rows
    cells = []
    for n, row in enumerate(matrix, 1):
        cells.append('<row r="%d">%s</row>' % (n, ''.join(
            '<c r="%s%d" t="inlineStr"><is><t>%s</t></is></c>' % (chr(65+i), n, escape(str(v)))
            for i, v in enumerate(row))))
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                   '<sheets><sheet name="台账" r:id="rId4"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships><Relationship Id="rId4" Target="/xl/worksheets/sheet2.xml"/></Relationships>')
        z.writestr("xl/worksheets/sheet2.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                   '<dimension ref="A1"/><sheetData>' + ''.join(cells) + '</sheetData></worksheet>')


class LedgerImportTests(unittest.TestCase):
    def test_full_export_including_unchecked_rows_and_old_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, seen = Path(tmp)/"source.xlsx", Path(tmp)/"seen.json"
            workbook(source, [["F1", "甲医院过敏原试剂采购公告", "甲医院", "2020-01-01", "https://x.org/old", "1"],
                              ["F2", "乙医院过敏原试剂采购公告", "乙医院", "2026-09-04", "https://x.org/new", ""]])
            report = import_ledger(source, seen, True)
            self.assertEqual(report["source_rows"], 2)
            self.assertEqual(report["sales_delivery_checked"], 1)
            self.assertEqual(report["covered"], 2)
            again = import_ledger(source, seen, True)
            self.assertEqual(again["inserted"], 0)
            self.assertEqual(again["ledger_rows"], 2)
            self.assertEqual(read_ledger(seen)["retention"], "permanent")

    def test_import_preserves_old_link_when_same_feishu_row_changes_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, seen = Path(tmp)/"source.xlsx", Path(tmp)/"seen.json"
            old = {"_feishu_id": "F1", "标题": "甲医院过敏原试剂采购公告", "单位": "甲医院",
                   "发布时间": "2026-09-04", "链接": "https://x.org/old", "_pushed": True}
            save_ledger(seen, {"records": [old]})
            workbook(source, [["F1", old["标题"], "甲医院", "2026-09-04", "https://x.org/new", "1"]])
            import_ledger(source, seen, True)
            data = read_ledger(seen)
            self.assertIn("https://x.org/old", data["records"][0]["_identity_urls"])
            self.assertIsNotNone(IdentityIndex(data["records"]).find(old)[0])

    def test_dry_run_does_not_create_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, seen = Path(tmp)/"source.xlsx", Path(tmp)/"seen.json"
            workbook(source, [["F1", "甲医院过敏原试剂采购公告", "甲医院", "2026-09-04", "https://x.org/new", ""]])
            import_ledger(source, seen)
            self.assertFalse(seen.exists())

    def test_misplaced_title_is_not_used_as_a_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/"source.xlsx"
            workbook(source, [["F1", "医学检验中心试剂配送供应商遴选项目遴选公告", "", "2026-08-31",
                               "岳阳市中心医院医学检验中心试剂配送供应商遴选项目遴选公告", "1"]])
            record = export_records(source)[0]
            self.assertEqual(record["链接"], "null")
            self.assertEqual(record["单位"], "岳阳市中心医院")


if __name__ == "__main__":
    unittest.main()
