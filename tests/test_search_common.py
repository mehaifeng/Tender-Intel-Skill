import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from search_common import (  # noqa: E402
    canonical_url,
    excluded_domain_term,
    merge_source_dirs,
    plap_notice_id,
    target_category_signals,
    write_candidates,
)
from tender_pipeline import canonicalize_create, historical_identity_keys  # noqa: E402


class ProductDomainScreenTests(unittest.TestCase):
    """目标词与排除词都以业务方《过敏》《自免》两张表为准，三个来源共用一份。"""

    def test_keyword_table_spectra_are_all_recognised(self):
        """两张表的每个谱系都必须能被认出来。

        检索侧一行一条 query，筛选侧靠这一层——整批招采公告的目标品类往往只出现在
        正文的清单行里，认不出来就等于这些标从来没被检索过。
        """
        cases = {
            "过敏/IgE": "过敏原特异性IgE检测试剂盒采购",
            "食物不耐受/sIgG": "食物不耐受检测试剂（食物特异性IgG）",
            "印迹": "免疫印迹仪及配套试剂采购",
            "自身抗体/自身免疫": "自身抗体检测试剂招标",
            "核抗体谱": "抗Ro52抗体、抗SS-A抗体、抗dsDNA抗体检测试剂",
            # 检索词放宽到最短片段后，筛选层必须认同一批片段，否则宽词白捞。

            "血管炎/ANCA": "抗中性粒细胞胞浆抗体（ANCA）、髓过氧化物酶抗体",
            "肌炎谱": "肌炎抗体谱（Mi-2、MDA5、PL-12）检测试剂",
            "自免肝/PBC": "自免肝抗体谱AMA-M2、gp210、sp100检测试剂",
            "抗磷脂谱": "抗心磷脂抗体、抗β2-GP1抗体检测试剂",
            "类风湿谱": "类风湿因子及抗CCP抗体检测试剂",
            "糖尿病自身抗体": "谷氨酸脱羧酶抗体、胰岛细胞抗体、抗ZnT8抗体检测",
            "膜性肾病/PLA2R": "抗PLA2R抗体检测试剂（膜性肾病）",
            "大疱性皮肤病": "大疱性皮肤病抗体谱Dsg1、Dsg2、BP180、BP230",
            "胃肠疾病抗体": "胃肠疾病抗体检测（胃壁细胞抗体、内因子抗体）",
            "IgG亚类/IgG4": "血清IgG4测定试剂盒",
            "25羟基维生素D": "25羟基维生素D检测试剂采购",
            "细胞因子": "细胞因子12项检测试剂（IL-6、TNF-α、IFN-γ）",
        }
        for signal, text in cases.items():
            self.assertIn(signal, target_category_signals(text), text)

    def test_screening_accepts_every_broadened_query_form(self):
        """检索词按最短片段放宽后，筛选层必须认同一批片段，否则宽词捞回来就白捞。

        这条把两侧钉在一起：清单里每个放宽过的写法，都要能在筛选层命中。
        """
        for query, text in (
            ("变应", "变应原筛查试剂采购"),
            ("不耐受", "乳糖不耐受检测试剂"),
            ("印迹", "蛋白印迹法检测试剂"),
            ("核抗体", "核抗体谱检测试剂盒"),
            ("狼疮", "狼疮性肾炎抗体检测"),
            ("干燥综合", "干燥综合征抗体谱"),
            ("硬皮", "硬皮病相关抗体检测"),
            ("胞浆抗体", "中性粒细胞胞浆抗体测定"),
            ("胆汁性", "原发性胆汁性肝硬化抗体"),
            ("风湿", "风湿免疫科检验试剂采购"),
            ("脱羧酶", "谷氨酸脱羧酶抗体测定"),
            ("胃壁", "胃壁细胞抗体检测"),
            ("内因子", "内因子抗体检测"),
            ("麦胶", "麦胶蛋白抗体检测"),
            ("大疱", "大疱性类天疱疮抗体"),
            ("羟基维生素", "25-羟基维生素D3检测试剂"),
            ("Scl", "抗Scl-70抗体、抗PM-Scl抗体"),
            ("Dsg", "抗Dsg1、抗Dsg2抗体"),
            ("GP1", "抗β2-GP1抗体检测"),
            ("IL-", "IL-12p70、IL-33检测试剂"),
            ("TNF", "TNF-α检测试剂"),
            ("IFN", "IFN-γ检测试剂"),
        ):
            self.assertTrue(target_category_signals(text), "%s -> %s" % (query, text))

    def test_words_outside_the_two_tables_are_not_signals(self):
        """方法学词、仪器词、甲状腺线都不在两张表里，一律不算命中。"""
        for text in ("酶联免疫检测试剂采购", "全自动酶免仪及配套试剂",
                     "全自动免疫荧光图像分析系统", "全自动化学发光免疫分析仪",
                     "甲状腺球蛋白及Anti-TPO检测试剂", "微流控芯片采购"):
            self.assertEqual(target_category_signals(text), [], text)

    def test_latin_codes_fire_when_glued_to_chinese(self):
        """代号边界不能用 \b：中文也是 \w，`\bANCA\b` 在「抗ANCA抗体」里两侧都不成立。

        中文公告正文里代号几乎总是紧贴汉字，用 \b 包住等于这些代号从来没生效过。
        """
        for text in ("抗ANCA抗体检测试剂", "ANCA检测试剂盒", "抗PLA2R抗体测定",
                     "抗dsDNA抗体检测", "抗Ro52抗体", "含IgG4检测项目"):
            self.assertTrue(target_category_signals(text), text)
        # 但仍不得命中被更长英文单词包住的同名片段。
        self.assertEqual(target_category_signals("ANALYZER PANAMA SMART"), [])

    def test_same_name_other_domain_stays_out(self):
        """表里有一批词的字面在别的科室也成立，必须靠语境收窄挡住。"""
        for text in ("病毒性心肌炎标志物检测试剂",      # 肌炎 vs 心肌炎
                     "多发性硬化症治疗药品采购",        # 硬化症 vs 系统性硬化
                     "患者自控镇痛泵（PCA泵）采购",      # PCA 抗体 vs 镇痛泵
                     "颈内动脉ICA介入导管采购",          # ICA 抗体 vs 颈内动脉
                     "维生素D3注射液采购",              # 25羟基维生素D vs 药品
                     "糖尿病足护理服务采购",            # 糖尿病自身抗体 vs 糖尿病服务
                     "糖蛋白激素类药品采购"):           # β2-GP1 vs 裸「糖蛋白」
            self.assertEqual(target_category_signals(text), [], text)

    def test_exclude_terms_beat_target_terms(self):
        """排除词优先于目标词：同时含目标词也排除。"""
        for text in ("酶标仪采购", "兽用自身抗体检测试剂", "新冠核酸PCR检测试剂",
                     "全自动电泳仪", "免疫组化二抗"):
            self.assertTrue(excluded_domain_term(text), text)


class SearchMergeTests(unittest.TestCase):
    def test_ccgp_http_is_canonicalized_to_https(self):
        self.assertEqual(
            canonical_url("http://www.ccgp.gov.cn/a.htm?utm_source=x"),
            "https://www.ccgp.gov.cn/a.htm",
        )

    def test_plap_url_drops_routing_parameters_and_keeps_notice_id(self):
        raw = (
            "https://www.plap.mil.cn/freecms/site/juncai/ggxx/info/2026/"
            "8a1d04009fd98fe401a03138aab456cf.html?noticeType=001024&channel=abc"
        )
        normalized = canonical_url(raw)
        self.assertNotIn("noticeType", normalized)
        self.assertNotIn("channel", normalized)
        self.assertEqual(plap_notice_id(normalized), "8a1d04009fd98fe401a03138aab456cf")

    def test_cross_source_duplicate_prefers_ccgp_and_keeps_jrbx_attribution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jrbx = root / "jrbx"
            ccgp = root / "ccgp"
            write_candidates([{
                "title": "某医院过敏原试剂公开招标公告",
                "site_name": "转载站",
                "url": "https://mirror.example/tender/1",
                "publish_time": "2026-08-21",
                "summary": "项目编号：ABC-2026-01",
                "content": "项目编号：ABC-2026-01 采购方式：公开招标",
                "found_by_query": [1, 3],
                "found_by_source_query": [{"source": "jrbx", "query_number": 1}],
                "source": "jrbx",
                "sources": ["jrbx"],
            }], jrbx, "2026-08-23")
            write_candidates([{
                "title": "某医院过敏原试剂公开招标公告",
                "site_name": "中国政府采购网",
                "url": "http://www.ccgp.gov.cn/cggg/dfgg/gkzb/202608/t20260821_27184986.htm",
                "publish_time": "2026-08-21 19:47:44",
                "summary": "官方摘要",
                "content": "官方完整正文",
                "source_fields": {"项目编号": "ABC-2026-01", "公告类型": "公开招标公告"},
                "found_by_source_query": [{"source": "ccgp", "query": "过敏原"}],
                "source": "ccgp",
                "sources": ["ccgp"],
                "date_authoritative": True,
                "retrieval_verified": True,
            }], ccgp, "2026-08-23")

            merged = merge_source_dirs([jrbx, ccgp])
            self.assertEqual(len(merged), 1)
            self.assertEqual(merged[0]["source"], "ccgp")
            self.assertEqual(merged[0]["content"], "官方完整正文")
            self.assertEqual(merged[0]["found_by_query"], [1, 3])
            self.assertEqual(set(merged[0]["sources"]), {"ccgp", "jrbx"})
            self.assertEqual(len(merged[0]["alternate_sources"]), 1)

    def test_same_project_different_notice_stage_is_not_merged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first"
            second = root / "second"
            common = {"site_name": "中国政府采购网", "publish_time": "2026-08-21", "source": "ccgp"}
            write_candidates([common | {
                "title": "某项目公开招标公告", "url": "https://example.test/a",
                "content": "项目编号：ABC-1", "source_fields": {"项目编号": "ABC-1", "公告类型": "公开招标公告"},
            }], first, "2026-08-23")
            write_candidates([common | {
                "title": "某项目中标公告", "url": "https://example.test/b",
                "content": "项目编号：ABC-1", "source_fields": {"项目编号": "ABC-1", "公告类型": "中标公告"},
            }], second, "2026-08-23")
            self.assertEqual(len(merge_source_dirs([first, second])), 2)

    def test_plap_official_candidate_wins_over_jrbx_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jrbx = root / "jrbx"
            plap = root / "plap"
            title = "某医院自身抗体试剂采购公告"
            write_candidates([{
                "title": title, "url": "https://mirror.example/1", "source": "jrbx",
                "summary": "转载摘要", "content": "转载正文", "retrieval_verified": False,
            }], jrbx, "2026-08-25")
            write_candidates([{
                "title": title,
                "url": "https://www.plap.mil.cn/freecms/site/juncai/ggxx/info/2026/8a1d04009fd98fe401a03138aab456cf.html",
                "source": "plap", "sources": ["plap"], "source_priority": 400,
                "summary": "官方摘要", "content": "官方公开正文",
                "retrieval_verified": True, "content_access": "public_partial",
            }], plap, "2026-08-25")
            merged = merge_source_dirs([jrbx, plap])
            self.assertEqual(len(merged), 1)
            self.assertEqual(merged[0]["source"], "plap")
            self.assertEqual(merged[0]["content_access"], "public_partial")

    def test_seen_identity_requires_same_date_for_title_only_match(self):
        first = historical_identity_keys("某医院试剂采购公告", "https://a.test/1", "2026-08-21")
        mirror = historical_identity_keys("某医院试剂采购公告", "https://b.test/2", "2026-08-21")
        next_year = historical_identity_keys("某医院试剂采购公告", "https://b.test/3", "2027-08-21")
        self.assertTrue(first & mirror)
        self.assertFalse(first & next_year)

    def test_verified_ccgp_fields_prefill_record_and_authoritative_date_wins(self):
        candidate = {
            "title": "某医院过敏原试剂公开招标公告",
            "site_name": "中国政府采购网",
            "url": "https://www.ccgp.gov.cn/cggg/dfgg/gkzb/202608/t20260821_1.htm",
            "publish_time": "2026-08-21 19:47:44",
            "date_authoritative": True,
            "retrieval_verified": True,
            "source_fields": {
                "项目编号": "WTYZSZC25-203-04",
                "单位": "某医院", "地区": "新疆维吾尔自治区", "所属省/市": "新疆",
                "截止时间": "2026-09-10T11:00", "预算": "200000",
                "采购方式": "公开招标", "科室": "医学检验科",
            },
            "field_evidence": {
                "项目编号": "项目编号：WTYZSZC25-203-04",
                "单位": "采购单位：某医院", "地区": "行政区域：新疆维吾尔自治区",
                "所属省/市": "行政区域：新疆维吾尔自治区",
                "截止时间": "提交投标文件截止时间：2026年09月10日 11:00",
                "预算": "预算金额：20万元", "采购方式": "采购方式：公开招标",
                "科室": "使用科室：医学检验科",
            },
            "search_evidence": {
                "summary": "官方检索摘要", "matched_keywords": ["过敏原"],
                "departments": ["医学检验科"],
            },
        }
        row = {
            "record": {"发布时间": "2026-08-20"},
            "evidence": {
                "source_verified": True,
                "checked_at": "2026-08-23T23:00:00+08:00",
                "field_evidence": {},
            },
        }
        record, _ = canonicalize_create(row, candidate)
        self.assertEqual(record["发布时间"], "2026-08-21")
        self.assertEqual(record["项目编号"], "WTYZSZC25-203-04")
        self.assertEqual(record["单位"], "某医院")
        self.assertEqual(record["地区"], "新疆维吾尔自治区")
        self.assertEqual(record["所属省/市"], "新疆")
        self.assertEqual(record["截止时间"], "2026-09-10T11:00")
        self.assertEqual(record["预算"], "200000")
        self.assertEqual(record["采购方式"], "公开招标")
        self.assertEqual(record["科室"], "医学检验科")
        self.assertEqual(record["命中关键词"], "过敏原")


if __name__ == "__main__":
    unittest.main()
