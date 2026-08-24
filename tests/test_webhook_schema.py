import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from send_webhook import FIELDS, validate_payload  # noqa: E402
from doubao_search import dedup_candidates  # noqa: E402
from tender_pipeline import (  # noqa: E402
    extract_departments,
    matched_query_keywords,
    normalize_region_location,
)


class WebhookSchemaTests(unittest.TestCase):
    def _payload(self):
        payload = {field: "null" for field in FIELDS}
        payload["标题"] = "某医院过敏原试剂公开招标公告"
        payload["链接"] = "https://www.ccgp.gov.cn/test.htm"
        payload["所属省/市"] = "安徽"
        payload["地区"] = "安徽省凤阳县"
        payload["科室"] = "医学检验科"
        payload["命中关键词"] = "过敏原"
        return payload

    def test_region_location_contains_full_province_name(self):
        self.assertEqual(normalize_region_location("凤阳县", "安徽"), "安徽省凤阳县")
        self.assertEqual(normalize_region_location("北京市朝阳区", "北京"), "北京市朝阳区")
        self.assertEqual(normalize_region_location("朝阳区", "北京"), "北京市朝阳区")

    def test_flat_fifteen_field_payload_is_valid(self):
        payload = self._payload()
        self.assertEqual(len(payload), 15)
        self.assertEqual(validate_payload(payload), [])

    def test_bare_local_place_is_rejected(self):
        payload = self._payload()
        payload["地区"] = "凤阳县"
        self.assertTrue(any("地区必须" in error for error in validate_payload(payload)))

    def test_department_and_keywords_come_from_retrieved_content(self):
        text = "项目使用科室：医学检验科\n拟采购过敏原检测试剂。"
        candidate = {
            "found_by_source_query": [
                {"source": "doubao", "query_number": 1, "query": "过敏原检测 试剂 招标公告"},
                {"source": "ccgp", "query": "过敏原"},
            ]
        }
        self.assertEqual(extract_departments(text), ["医学检验科"])
        self.assertEqual(matched_query_keywords(candidate, text), ["过敏原检测", "过敏原"])

    def test_doubao_candidate_keeps_actual_query_text(self):
        candidates = dedup_candidates([{
            "num": 1,
            "query": "过敏原检测 试剂 招标公告",
            "results": [{"Url": "https://example.test/1", "Title": "过敏原检测采购"}],
        }])
        self.assertEqual(candidates[0]["found_by_source_query"], [{
            "source": "doubao",
            "query_number": 1,
            "query": "过敏原检测 试剂 招标公告",
        }])


if __name__ == "__main__":
    unittest.main()
