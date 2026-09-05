"""字段质量回归：品类信号、截止时间、医院匹配、省份与地区规范化。

这批用例都对应 2026-08-27 在真实数据上量到的缺陷，别随手放宽。
"""
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from zlbx_search import _extract_deadline  # noqa: E402
from hospital_match import (  # noqa: E402
    geo_is_confirmed,
    geo_is_suspect,
    geo_matches,
    get_default_index,
    is_generic_org_name,
    loose_key,
)
from search_common import target_category_signals  # noqa: E402
from tender_pipeline import canonical_province, normalize_region_location  # noqa: E402


class CategorySignalTests(unittest.TestCase):
    def test_enzyme_plate_reader_is_not_a_positive_signal(self):
        """酶标仪不在两张关键词表里，也在排除词里，不能当命中理由。"""
        self.assertEqual(target_category_signals("某医院全自动酶标仪采购项目公开招标公告"), [])

    def test_instrument_bundle_matches_only_on_a_table_item(self):
        """仪器词一律不算命中；同一个包里出现表内项目才留下。"""
        self.assertEqual(
            target_category_signals("某医院全自动酶免仪、酶标仪及洗板机采购项目"), [])
        self.assertIn(
            "核抗体谱",
            target_category_signals("某医院免疫分析仪及抗核抗体谱检测试剂采购项目"),
        )


class DeadlineExtractionTests(unittest.TestCase):
    def test_heading_form_without_colon(self):
        """国办标准模板是小节标题换行给值。旧规则要求紧跟冒号，整块抽不到。"""
        text = "四、提交投标文件截止时间、开标时间和地点 2026年09月16日 09时00分00秒 （北京时间）"
        self.assertEqual(_extract_deadline(text)[0], "2026-09-16T09:00")

    def test_colon_form_still_works(self):
        self.assertEqual(
            _extract_deadline("投标截止时间：2026年09月16日 09时00分")[0], "2026-09-16T09:00")

    def test_iso_source_format(self):
        text = "提交投标文件截止时间、开标时间和地点 2026-09-18 08:30:00（北京时间）"
        self.assertEqual(_extract_deadline(text)[0], "2026-09-18T08:30")

    def test_correction_notice_with_two_datetimes_yields_nothing(self):
        """更正公告并排写原/现两个时间，抓第一个就是作废的旧时间。填错比留空危险。"""
        for text in (
            "3开标时间、投标文件递交截止时间2026年09月01日11时00分（北京时间）"
            "2026年09月11日11时00分（北京时间）",
            "投标文件递交截止时间：“2026年08月26日9:00” 现更正为：“2026年08月27日9:00”",
        ):
            with self.subTest(text=text[:24]):
                self.assertEqual(_extract_deadline(text)[0], "")

    def test_dead_tender_without_datetime_yields_nothing(self):
        self.assertEqual(
            _extract_deadline("至投标文件递交截止时间止，递交投标文件的供应商不足法定三家")[0], "")


class HospitalMatchTests(unittest.TestCase):
    def setUp(self):
        self.index = get_default_index()

    def test_renamed_county_matches_city_spelling(self):
        """弥勒 2013 年撤县设市；公告写「市」而索引存「县」，精确键对不上。"""
        match = self.index.match(name="弥勒市人民医院")
        self.assertTrue(match["matched"])
        self.assertEqual(match["hospital_name"], "弥勒县人民医院")
        self.assertEqual(match["match_method"], "loose_name")

    def test_loose_key_drops_division_suffix(self):
        self.assertEqual(loose_key("弥勒市人民医院"), loose_key("弥勒县人民医院"))

    def test_mislabelled_record_keeps_name_but_not_geography(self):
        """故城县在河北衡水，索引把它编码到了云南丽江（故城→古城）。
        名称与等级仍可用，地理绝不能回填，否则省份/大区全错、消息发错人。"""
        match = self.index.match(name="故城县中医医院")
        self.assertTrue(match["matched"])
        self.assertFalse(match["geo_trusted"])

    def test_consistent_record_is_geo_trusted(self):
        for name in ("宾县人民医院", "宁夏回族自治区人民医院", "余姚市第二人民医院"):
            with self.subTest(name=name):
                match = self.index.match(name=name)
                self.assertTrue(match["matched"])
                self.assertTrue(match["geo_trusted"])

    def test_geo_suspect_needs_a_real_contradiction(self):
        self.assertFalse(geo_is_suspect({"n": "宾县人民医院", "p": "黑龙江省",
                                         "c": "哈尔滨市", "d": "宾县"}))
        self.assertTrue(geo_is_suspect({"n": "故城县中医医院", "p": "云南省",
                                        "c": "丽江", "d": "古城"}))

    def test_province_full_form_is_a_locality_too(self):
        """「山东省南山医院」被编码到四川内江。字符类漏了「省」就抓不到这一类。"""
        self.assertTrue(geo_is_suspect({"n": "山东省南山医院", "p": "四川省",
                                        "c": "内江市", "d": "市中区"}))

    def test_geo_confirmed_is_stricter_than_not_suspect(self):
        """名字里没有地名的记录：既不可疑，也不算自洽——只是无从判断。

        消歧押的是正面自洽，不能押在「没被标记」上，否则一条无地名的脏记录
        会白捡一次胜出。
        """
        nameless = {"n": "协和专科医院", "p": "内蒙古", "c": "乌海市", "d": "海南区"}
        self.assertFalse(geo_is_suspect(nameless))
        self.assertFalse(geo_is_confirmed(nameless))
        # 「宾县」的地名只有「宾」一个字，够不着正则的 {2,4} 下限，同样属于无从判断
        short = {"n": "宾县人民医院", "p": "黑龙江省", "c": "哈尔滨市", "d": "宾县"}
        self.assertFalse(geo_is_suspect(short))
        self.assertFalse(geo_is_confirmed(short))
        self.assertTrue(geo_is_confirmed({"n": "故城县中医院", "p": "河北省",
                                          "c": "衡水市", "d": "故城县"}))

    def test_same_name_conflict_resolved_by_self_consistency(self):
        """索引里「临湘市人民医院」既挂在湖南岳阳临湘市，也挂在云南临沧临翔区
        （临湘被编码成同音的临翔）。后者地理与自身名字矛盾，剔掉它就唯一了，
        无需调用方给任何提示。"""
        match = self.index.match(name="临湘市人民医院")
        self.assertTrue(match["matched"])
        self.assertTrue(match["geo_disambiguated"])
        self.assertEqual(match["province"], "湖南省")
        self.assertTrue(match["geo_trusted"])

    def test_hint_resolved_match_must_not_backfill_geography(self):
        """「山东中医药大学附属眼科医院」的脏副本挂在四川内江，且名字不以行政区划
        打头，geo_is_suspect 看不见它——只能靠调用方提示裁决。

        但靠提示选出来的记录不得回填地理：那是循环论证，好的情况只是复述提示，
        坏的情况（提示本身就错）会把错省份洗成确信字段、让消息发错大区。
        名称与等级仍然可用，那才是这次匹配的增量。
        """
        without_hint = self.index.match(name="山东中医药大学附属眼科医院")
        self.assertFalse(without_hint["matched"])
        self.assertTrue(without_hint["ambiguous"])

        right = self.index.match(name="山东中医药大学附属眼科医院", province="山东省")
        self.assertTrue(right["matched"])
        self.assertEqual(right["hospital_level"], "三级甲等")
        self.assertFalse(right["geo_trusted"])

        # 提示给错时也一样收敛，但同样禁止回填——不会把四川写进结果
        wrong = self.index.match(name="山东中医药大学附属眼科医院", province="四川省")
        self.assertTrue(wrong["matched"])
        self.assertFalse(wrong["geo_trusted"])

    def test_disambiguation_leaves_unambiguous_matches_alone(self):
        """没有同名冲突的匹配不该被这套逻辑碰到。"""
        for name in ("宾县人民医院", "宁夏回族自治区人民医院", "余姚市第二人民医院"):
            with self.subTest(name=name):
                match = self.index.match(name=name)
                self.assertTrue(match["matched"])
                self.assertFalse(match["geo_disambiguated"])
                self.assertTrue(match["geo_trusted"])


class GenericNameHijackTests(unittest.TestCase):
    """通用机构名不得劫持匹配。

    岳阳县血防医院（湖南）的别名就叫「第三人民医院」，于是
    「新疆维吾尔自治区第三人民医院」的三条公告整批被它匹走，
    连省份提示都挡不住——当时子串命中走的是豁免地理校验的 explicit 通道。
    """

    def test_generic_names_are_recognised(self):
        for key in ("第三人民医院", "人民医院", "中心医院", "第一人民医院", "妇幼保健院", "医院"):
            with self.subTest(key=key):
                self.assertTrue(is_generic_org_name(key))

    def test_place_qualified_names_are_not_generic(self):
        """不能退回长度阈值：漳州市医院 5 字、宾县人民医院 6 字，都是具体医院。"""
        for key in ("宾县人民医院", "漳州市医院", "余姚市第二人民医院", "佛山市顺德区第一人民医院"):
            with self.subTest(key=key):
                self.assertFalse(is_generic_org_name(key))

    def test_generic_suffix_does_not_match_a_different_hospital(self):
        index = get_default_index()
        for hints in ({}, {"province": "新疆"}):
            with self.subTest(hints=hints):
                match = index.match(name="新疆维吾尔自治区第三人民医院", **hints)
                self.assertNotEqual(match.get("hospital_name"), "岳阳县血防医院")

    def test_generic_suffix_in_free_text_is_also_blocked(self):
        """prepare 阶段是拿标题当 text 匹的，这条通道同样会被通用名劫持。"""
        index = get_default_index()
        match = index.match(
            name="",
            text="新疆维吾尔自治区第三人民医院检验科全自动化学发光免疫分析仪采购项目公开招标公告",
        )
        self.assertNotEqual(match.get("hospital_name"), "岳阳县血防医院")


class GeoHintTests(unittest.TestCase):
    def test_hint_may_match_any_administrative_level(self):
        """调用方给的层级不可靠：来源的「所属省/市」常是「宾县」「平和县」这类地名。
        按层级对号入座会把真匹配误杀。"""
        record = {"n": "宾县人民医院", "p": "黑龙江省", "c": "哈尔滨市", "d": "宾县"}
        self.assertTrue(geo_matches(record, province="宾县"))
        self.assertTrue(geo_matches(record, province="黑龙江"))

    def test_cross_province_hint_still_rejected(self):
        """放宽层级不等于放弃把关——跨省的错配仍要拦住。"""
        record = {"n": "宁波北仑大港医院", "p": "浙江省", "c": "宁波市", "d": "北仑区"}
        self.assertFalse(geo_matches(record, province="天津", city="滨海新区"))


class RegionFieldTests(unittest.TestCase):
    """飞书侧的分发依赖这两个字段：省份必须无后缀，地区必须带省级全称。"""

    def test_province_has_no_administrative_suffix(self):
        for raw, expected in (
            ("新疆维吾尔自治区", "新疆"), ("青海省", "青海"), ("北京市", "北京"),
            ("广东省", "广东"), ("广西壮族自治区", "广西"), ("内蒙古自治区", "内蒙古"),
            ("宁夏回族自治区", "宁夏"), ("新疆生产建设兵团", "新疆"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(canonical_province(raw), expected)

    def test_region_always_carries_the_province(self):
        for location, province, expected in (
            ("呼和浩特市", "内蒙古", "内蒙古自治区呼和浩特市"),
            ("朝阳区", "北京", "北京市朝阳区"),
            ("凤阳县", "安徽", "安徽省凤阳县"),
            ("乌鲁木齐市", "新疆", "新疆维吾尔自治区乌鲁木齐市"),
            ("新疆维吾尔自治区乌鲁木齐市", "新疆", "新疆维吾尔自治区乌鲁木齐市"),
        ):
            with self.subTest(location=location):
                self.assertEqual(normalize_region_location(location, province), expected)

    def test_bare_locality_without_known_province_is_dropped(self):
        """省份不明时宁可置空，也不能留一个孤立地名让人猜该发给谁。"""
        self.assertEqual(normalize_region_location("某地", "null"), "null")


if __name__ == "__main__":
    unittest.main()


class SourceFieldBindingTests(unittest.TestCase):
    """接口结构化字段直接绑定后，地理一致性没有模型把关，必须由管线自己守住。"""

    def _run(self, source_fields, title="某医院试剂采购公告"):
        from tender_pipeline import canonicalize_create
        candidate = {
            "candidate_id": "C1", "title": title, "site_name": "知了标讯",
            "url": "https://example.gov.cn/a", "publish_time": "2026-09-04",
            "date_authoritative": True, "retrieval_verified": True,
            "source_fields": source_fields,
            "search_evidence": {"summary": "摘要", "matched_keywords": [], "departments": []},
        }
        row = {"candidate_id": "C1", "decision": "create", "record": {},
               "evidence": {"source_verified": True,
                            "checked_at": "2026-09-05T20:30:00+08:00",
                            "field_evidence": {}}}
        record, _ = canonicalize_create(row, candidate)
        return record, row

    def test_structured_fields_are_bound_without_model_input(self):
        record, _ = self._run({
            "项目编号": "ABC-1", "单位": "云浮市妇幼保健院", "所属省/市": "广东",
            "地区": "云浮市", "预算": "1700000", "采购方式": "公开招标",
        })
        self.assertEqual(record["项目编号"], "ABC-1")
        self.assertEqual(record["预算"], "1700000")
        self.assertEqual(record["采购方式"], "公开招标")
        self.assertEqual(record["所属省/市"], "广东")
        self.assertEqual(record["地区"], "广东省云浮市")
        self.assertEqual(record["所属大区"], "华南大区")

    def test_buyer_province_conflicting_with_api_province_blocks_geo_binding(self):
        """实测：单位=浙江宁波的医院，接口 province 却是广东深圳。填错省份会发错大区。"""
        record, row = self._run({
            "单位": "浙江省宁波市宁海县城关医院", "所属省/市": "广东",
            "地区": "深圳市南山区", "采购方式": "邀请招标",
        })
        self.assertNotEqual(record["所属省/市"], "广东")
        self.assertNotIn("深圳", record["地区"])
        self.assertEqual(record["采购方式"], "邀请招标")
        self.assertTrue(any(a["field"] == "接口地理" for a in row["pipeline_adjustments"]))

    def test_model_override_needs_field_evidence(self):
        from tender_pipeline import canonicalize_create
        candidate = {
            "candidate_id": "C1", "title": "某医院试剂采购公告", "site_name": "知了标讯",
            "url": "https://example.gov.cn/a", "publish_time": "2026-09-04",
            "retrieval_verified": True, "source_fields": {"预算": "100"},
            "search_evidence": {"summary": "摘要", "matched_keywords": [], "departments": []},
        }
        with_evidence = {"candidate_id": "C1", "decision": "create",
                         "record": {"预算": "985000"},
                         "evidence": {"source_verified": True,
                                      "checked_at": "2026-09-05T20:30:00+08:00",
                                      "field_evidence": {"预算": "预算金额：98.5万元"}}}
        record, _ = canonicalize_create(with_evidence, candidate)
        self.assertEqual(record["预算"], "985000")

        without_evidence = {"candidate_id": "C1", "decision": "create",
                            "record": {"预算": "985000"},
                            "evidence": {"source_verified": True,
                                         "checked_at": "2026-09-05T20:30:00+08:00",
                                         "field_evidence": {}}}
        record, _ = canonicalize_create(without_evidence, candidate)
        self.assertEqual(record["预算"], "100")
