import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from search_common import (  # noqa: E402
    canonical_url,
    excluded_domain_term,
    screen_domain,
    merge_source_dirs,
    non_hospital_buyer,
    plap_notice_id,
    signal_tier,
    target_category_signals,
    write_candidates,
)
from tender_pipeline import (  # noqa: E402
    historical_identity_keys,
    canonicalize_create,
    historical_identity_keys,
    procedural_notice,
)


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

    def test_exclude_terms_are_recognised(self):
        """排除词表本身认得出这些写法。

        注意这只测词表，不测优先级：优先级由 screen_domain 定，且只在标题域
        生效（见 MixedBundleScreeningTests）。
        """
        for text in ("酶标仪采购", "兽用自身抗体检测试剂", "新冠核酸PCR检测试剂",
                     "全自动电泳仪", "免疫组化二抗"):
            self.assertTrue(excluded_domain_term(text), text)


class BuyerAndNoticeGateTests(unittest.TestCase):
    """两道零误杀闸门，判据是 2026-09-04《招标信息跟踪档案》115 条销售反馈的回测。"""

    def test_non_hospital_buyers_are_excluded(self):
        """血站/疾控/药检所/体检中心 在回测里 0/11 有效，成因是检验场景本就不同。"""
        for buyer, title in (
            ("漯河市中心血站", "漯河市中心血站全自动酶免仪设备采购项目"),
            ("河南省红十字血液中心", "献血者血液标本筛查酶联免疫诊断试剂盒项目"),
            ("哈尔滨市疾病预防控制中心", "实验室配套试剂及检测试剂耗材采购招标公告"),
            ("广东省药品检验所", "广东省药品检验所竞价公告"),
            ("北京市体检中心", "北京市体检中心医用体外诊断试剂购置项目(二)公开招标公告"),
            # 代理机构挂名时，主体仍从标题里认出来。
            ("江西众信源实业有限公司", "关于抚州市中心血站采购全自动酶免分析仪项目的公开招标公告"),
        ):
            self.assertTrue(non_hospital_buyer(buyer, title), title)

    def test_hospital_departments_named_like_those_buyers_survive(self):
        """「XX医院体检中心」「医院输血科」是医院的科室，不是独立主体，不得误杀。"""
        for buyer, title in (
            ("上海市第六人民医院体检中心", "上海市第六人民医院体检中心检验试剂采购"),
            ("某县人民医院", "某县人民医院与市疾病预防控制中心共建实验室设备采购"),
            ("某市医共体牵头医院", "某市医共体检验试剂集中采购项目"),
            ("某妇幼保健院", "某妇幼保健院过敏原特异性IgE抗体检测试剂采购"),
        ):
            self.assertEqual(non_hospital_buyer(buyer, title), "", title)

    def test_procedural_notices_are_excluded(self):
        """只通报开标/评标环节的公告，可行动信息都在原招标公告里。"""
        for title in (
            "南昌大学第二附属医院免疫印迹成像仪开标时间通知公告",
            "某院自身抗体检测试剂采购开标通知",
            "某项目开标记录",
            "某院过敏原检测试剂项目评标结果",
            "某院检验试剂采购资格预审结果",
        ):
            self.assertTrue(procedural_notice(title), title)

    def test_correction_notices_beat_the_procedural_gate(self):
        """更正/变更公告改的是在售标的的截止时间或参数，仍然可行动（SKILL.md）。"""
        for title in (
            "某院自身抗体检测试剂采购项目更正公告（开标时间变更）",
            "某院过敏原试剂采购更正公告 开标地点通知",
            "某院检验试剂采购项目变更公告：开标时间调整",
        ):
            self.assertEqual(procedural_notice(title), "", title)


class SignalTierTests(unittest.TestCase):
    """分层只用于调整核实力度，绝不决定去留——宽片段捞回的印迹仪标已经应标过。"""

    def test_broad_fragment_only_candidates_are_tiered_broad(self):
        for text in (
            "许昌市中心医院医学检验中心生化室耗材试剂项目竞争性谈判公告 类风湿因子",
            "某院免疫印迹成像仪采购",
            "北京市某中心医用体外诊断试剂购置项目 25羟基维生素D",
            "12项细胞因子检测试剂盒（多重微球流式免疫荧光发光法）",
        ):
            self.assertEqual(signal_tier(target_category_signals(text)), "broad", text)

    def test_core_nouns_and_project_codes_are_tiered_core(self):
        for text in (
            "过敏原特异性IgE抗体检测试剂盒采购",
            "自身抗体检测试剂及配套服务采购",
            "抗环瓜氨酸肽抗体（CCP）检测试剂采购",   # 类风湿谱的核心项目代号
            "抗PLA2R抗体检测试剂（膜性肾病）",
        ):
            self.assertEqual(signal_tier(target_category_signals(text)), "core", text)

    def test_broad_and_core_together_are_core(self):
        text = "某院过敏原特异性IgE及类风湿因子检测试剂采购"
        self.assertEqual(signal_tier(target_category_signals(text)), "core")

    def test_no_signal_is_none(self):
        self.assertEqual(signal_tier(target_category_signals("办公家具采购")), "none")


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


class HistoricalIdentityKeyTests(unittest.TestCase):
    def test_blank_link_yields_no_url_key(self):
        """空链接被 normalize_url 归一成 "/"，那是伪身份键，不能进 known_keys。

        2026-09-04 导入飞书台账时踩到：73 条记录的链接是聚合站的 tag/搜索列表页，
        降级为「仅标题+日期」后传了空链接，第一条占住 ("url", "/")，其余 72 条
        全被判成重复丢弃。
        """
        keys = historical_identity_keys("某某医院过敏原检测试剂采购公告", "", "2026-09-03")
        self.assertNotIn("url", {kind for kind, *_ in keys})
        self.assertIn(
            ("title_date", "某某医院过敏原检测试剂采购公告", "2026-09-03"), keys
        )

    def test_two_blank_link_records_stay_distinct(self):
        a = historical_identity_keys("甲医院过敏原试剂采购公告", "", "2026-09-03")
        b = historical_identity_keys("乙医院自身抗体试剂采购公告", "", "2026-09-03")
        self.assertFalse(a & b)


class MixedBundleScreeningTests(unittest.TestCase):
    """混合包回归：硬排除只在标题域决定去留，正文域只打标记。

    判据是 2026-09-03~09-04 两天窗口的实跑。当时正文域也硬排除，睿销漏 5 条、
    CCGP 漏 1 条真候选，同期实际推送只有 2 条——漏的比发的多。下面每条都是那次
    实跑里被误杀的原文片段。
    """

    # (标的, 正文片段, 期望命中的正文排除词)
    REAL_MISSES = (
        (
            "长沙市医健建设发展有限公司检验科、健康管理中心、医疗美容科、超声医学科等科室设备采购项目招标公告",
            "13 全自动自身抗体检测系统 否 否 1 180000 180000"
            " 19 实时荧光定量PCR分析仪 否 否 2 190000 380000",
            "PCR",
        ),
        (
            "阿拉善盟中心医院实验室设备配置项目招标公告",
            "临床检验设备全自动过敏源分析仪（化学发光）1(台)详见采购文件300,000.00"
            " 临床检验设备全自动（HPV）PCR分析仪1(台)",
            "PCR",
        ),
        (
            "仙居县人民医院2026年9月政府采购意向",
            "全自动核酸检测系统,数字PCR,"
            "全自动化化学发光免疫分析仪(自身免疫检测+过敏原专用)",
            "核酸",
        ),
        (
            "2026年甘肃省妇幼保健院(甘肃省中心医院)医疗设备及相关服务第十九批采购项目招标公告",
            "全自动体外过敏原筛查系统及其配套试剂(二次),梯度pcr,内腔镜手术监测系统(二次)",
            "pcr",
        ),
        (
            "国家康复辅具研究中心附属康复医院2026年医用耗材试剂遴选公告",
            "白介素6(IL-6)测定试剂盒(化学发光免疫分析法),"
            "人乳头瘤病毒核酸分型检测试剂盒(PCR-荧光探针法)",
            "核酸",
        ),
        (
            "彭州市人民医院2026年第二十次医用耗材临时采购遴选项目（第二次挂网）",
            "猫毛皮屑变应原皮肤点刺液,悬铃木花粉变应原皮肤点刺液,dmem培养基,胰蛋白胨",
            "培养基",
        ),
    )

    def test_body_exclude_term_does_not_drop_real_candidates(self):
        """混合包里本司品类只占一两行时，正文里的 PCR/核酸/培养基 不得带走整条公告。"""
        for title, body, term in self.REAL_MISSES:
            screen = screen_domain(title, body)
            self.assertTrue(screen["keep"], title)
            self.assertTrue(screen["signals"], title)
            self.assertEqual(screen["body_exclude_term"].lower(), term.lower(), title)

    def test_product_list_alone_can_carry_the_signal(self):
        """正文写「详见附件/下载」时，清单只在来源自带的标的字段里。

        原型是国家康复辅具研究中心附属康复医院的耗材试剂遴选：正文只有一个「下载」，
        `白介素6(IL-6)测定试剂盒` 只出现在睿销 product 字段里。该字段以 product_list
        随候选落盘，统一层把它并进正文域，否则候选会以「无目标品类信号」二次丢失。
        """
        body = "拟采购医用耗材试剂，其主要用途和要求如下：下载"
        title = "国家康复辅具研究中心附属康复医院2026年医用耗材试剂遴选公告"
        self.assertFalse(screen_domain(title, body)["keep"])
        product_list = ("白介素6(IL-6)测定试剂盒(化学发光免疫分析法),"
                        "人乳头瘤病毒核酸分型检测试剂盒(PCR-荧光探针法)")
        screen = screen_domain(title, "\n".join((body, product_list)))
        self.assertTrue(screen["keep"])
        self.assertEqual(screen["signals"], ["细胞因子"])

    def test_title_scope_exclusion_still_drops(self):
        """标题就说明了不是本司产品域的，照旧丢——这一层没有放宽。"""
        for title in ("酶标仪采购", "兽用自身抗体检测试剂", "全自动电泳仪",
                      "免疫组化二抗采购", "新冠核酸PCR检测试剂采购",
                      "结核分枝杆菌特异性细胞因子检测试剂盒配送服务项目招标公告",
                      "江苏省畜牧总站鸡节本增效养殖技术协同推广计划项目试剂耗材采购"):
            screen = screen_domain(title, "")
            self.assertFalse(screen["keep"], title)
            self.assertIn("硬排除词", screen["reason"], title)

    def test_body_exclude_without_target_signal_still_drops(self):
        """正文有排除词但没有目标品类信号的，仍被「无目标品类信号」丢掉。

        这说明正文域硬排除与那一条冗余——它唯一独立生效的场合就是上面的误杀。
        """
        screen = screen_domain(
            "泉州市疾病预防控制中心呼吸道传染病监测试剂（微检科）采购项目公开招标招标公告",
            "呼吸道多病原核酸检测试剂盒 TaqMan探针法",
        )
        self.assertFalse(screen["keep"])
        self.assertEqual(screen["reason"], "标题、摘要和搜索正文均无目标品类信号")

    def test_clean_candidate_carries_no_body_exclude_term(self):
        """没有混合包成分的候选，标记为空，核实阶段不必额外盘问。"""
        screen = screen_domain(
            "检验科抗核抗体IgG及相关试剂（国产）采购项目二次公开招标公告",
            "标项名称:抗核抗体IgG及相关试剂（国产） 预算金额（元）:100000",
        )
        self.assertTrue(screen["keep"])
        self.assertEqual(screen["body_exclude_term"], "")


if __name__ == "__main__":
    unittest.main()
