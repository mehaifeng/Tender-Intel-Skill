import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from send_webhook import FIELDS, validate_payload  # noqa: E402
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

    def test_flat_sixteen_field_payload_is_valid(self):
        payload = self._payload()
        self.assertEqual(len(payload), 16)
        self.assertEqual(list(payload)[:2], ["标题", "项目编号"])
        self.assertEqual(validate_payload(payload), [])

    def test_bare_local_place_is_rejected(self):
        payload = self._payload()
        payload["地区"] = "凤阳县"
        self.assertTrue(any("地区必须" in error for error in validate_payload(payload)))

    def test_department_and_keywords_come_from_retrieved_content(self):
        text = "项目使用科室：医学检验科\n拟采购过敏原检测试剂。"
        candidate = {
            "found_by_source_query": [
                {"source": "zlbx", "query_number": 1, "query": "过敏原检测 试剂 招标公告"},
                {"source": "zlbx", "query": "过敏原"},
            ]
        }
        self.assertEqual(extract_departments(text), ["医学检验科"])
        # 「过敏原」被更具体的「过敏原检测」包含，不再重复列一遍。
        self.assertEqual(matched_query_keywords(candidate, text), ["过敏原检测"])


class MatchedKeywordTests(unittest.TestCase):
    """命中关键词要能向业务方解释「这条为什么会被检索到」，且不允许为空。"""

    def test_attachment_only_hit_still_names_the_term(self):
        # 知了的 fulltext 覆盖附件：`PLA2R` 把它捞了回来，但可见文本里只有中文全称，
        # 按检索词回找的结果是空。真实样本：山西省心血管病医院医用耗材采购。
        products = "医用耗材、抗磷脂酶A2受体抗体IgG测定试剂、非水合性导管"
        values = matched_query_keywords({"found_by_source_query": []}, products, products)
        self.assertTrue(values, "品类信号命中就不该为空")
        self.assertIn("抗磷脂酶A2受体抗体IgG", values)

    def test_product_list_entry_beats_the_widened_query_fragment(self):
        # 真实样本：云浮市妇幼保健院委托检验服务，旧实现给的是「过敏、自身免疫」。
        products = "临床检验、过敏原检测、病理诊断、自身免疫性疾病检测"
        candidate = {"found_by_source_query": [
            {"query": "过敏"}, {"query": "自身免疫"},
        ]}
        self.assertEqual(
            matched_query_keywords(candidate, products, products),
            ["过敏原检测", "自身免疫性疾病检测"],
        )

    def test_buyer_name_and_procurement_words_stay_out(self):
        text = "宁海县城关医院过敏原检测试剂采购项目"
        self.assertEqual(matched_query_keywords({}, text), ["过敏原"])

    def test_instrument_is_not_trimmed_down_to_the_assay(self):
        text = "全自动免疫印迹仪"
        self.assertEqual(matched_query_keywords({}, text), ["全自动免疫印迹仪"])

    def test_broad_fragments_rank_last(self):
        text = "细胞因子检测、抗磷脂酶A2受体抗体测定试剂盒"
        values = matched_query_keywords({}, text, text)
        self.assertEqual(values[0], "抗磷脂酶A2受体抗体")
        self.assertEqual(values[-1], "细胞因子检测")

    def test_null_keyword_payload_is_rejected(self):
        payload = WebhookSchemaTests()._payload()
        payload["命中关键词"] = "null"
        self.assertTrue(any("命中关键词" in error for error in validate_payload(payload)))


if __name__ == "__main__":
    unittest.main()
