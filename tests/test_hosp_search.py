import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from hosp_search import (  # noqa: E402
    SITES_PER_CALL,
    batches,
    is_channel_page,
    load_sites,
    to_candidate,
)


def item(title, url="https://www.example-hosp.cn/news/1.html", summary=""):
    return {"Title": title, "Url": url, "Summary": summary,
            "PublishTime": "2026-08-25T10:00:00+08:00", "SiteName": "某医院"}


class ChannelPageTests(unittest.TestCase):
    def test_bare_channel_titles_are_rejected(self):
        for t in ["招标公告-龙岩市第二医院", "招标-四川省儿童医院",
                  "川投西昌医院- 招标采购- 招采公告", "招标采购-本地招标报名站点"]:
            self.assertTrue(is_channel_page(t), t)

    def test_real_announcements_are_kept(self):
        for t in ["绍兴市中医院细胞因子检测试剂采购项目重招中标结果公告",
                  "2026年医用耗材第三批遴选公告",
                  "关于全自动化学发光免疫分析仪2台院内询价采购项目的公示"]:
            self.assertFalse(is_channel_page(t), t)

    def test_short_titles_are_not_rejected_for_being_short(self):
        """长度分不开栏目页和真公告——「免疫印迹仪采购公告」才 9 个字却是真公告，
        「川投西昌医院- 招标采购- 招采公告」有 18 个字却是栏目页。判据必须是结构。"""
        for t in ["免疫印迹仪采购公告", "某某市中医院试剂采购招标公告", "全自动电泳仪采购公告"]:
            self.assertFalse(is_channel_page(t), t)


class CandidateTests(unittest.TestCase):
    def test_precise_category_hit(self):
        c = to_candidate(item("某院细胞因子检测试剂采购项目中标结果公告"), "试剂", {})
        self.assertIsNotNone(c)
        self.assertEqual(c["lead_tier"], "precise")

    def test_broad_lead_kept_for_batch_reagent_tender(self):
        """医院院内遴选多是「医用耗材第三批遴选」这种，目标品类在清单里不在标题。"""
        c = to_candidate(item("2026年医用耗材第三批遴选公告"), "招标", {})
        self.assertIsNotNone(c)
        self.assertEqual(c["lead_tier"], "broad")

    def test_excluded_domain_wins_over_category_signal(self):
        """同名异域场景优先级高于品类信号：兽用/结核/免疫组化即使含免疫词也排除。"""
        for t in ["兽医实验室免疫检测试剂采购公告",
                  "γ-干扰素释放试验试剂采购项目招标公告",
                  "免疫组化二抗试剂采购公告"]:
            self.assertIsNone(to_candidate(item(t), "试剂", {}), t)

    def test_company_excluded_products_are_dropped(self):
        for t in ["全自动酶标仪采购项目招标公告", "全自动电泳仪及配套耗材采购公开招标公告"]:
            self.assertIsNone(to_candidate(item(t), "招标", {}), t)

    def test_no_intent_word_is_dropped(self):
        self.assertIsNone(to_candidate(item("检验科简介与设备一览"), "试剂", {}))

    def test_hospital_name_written_only_when_corroborated(self):
        c = to_candidate(item("中日友好医院检验试剂采购项目招标公告",
                              url="https://www.zryhyy.com.cn/a/1.html"),
                         "试剂", {"www.zryhyy.com.cn": "中日友好医院"})
        self.assertEqual(c["source_fields"]["单位"], "中日友好医院")

    def test_unverified_domain_name_is_not_written_as_unit(self):
        """拼音缩写高度歧义：sxzyy.cn 索引标「山西省中西医结合医院」，
        实际公告来自绍兴市中医院。写错采购人比留空更糟。"""
        c = to_candidate(item("绍兴市中医院细胞因子检测试剂采购项目重招中标结果公告",
                              url="https://www.sxzyy.cn/a/1.html"),
                         "试剂", {"www.sxzyy.cn": "山西省中西医结合医院"})
        self.assertIsNotNone(c)
        self.assertNotIn("单位", c["source_fields"])

    def test_generic_suffix_alone_does_not_corroborate(self):
        """只匹配「中医院」这种通用词会把同类医院互相认错。"""
        c = to_candidate(item("某某市中医院试剂采购招标公告",
                              url="https://www.x.cn/a/1.html"),
                         "试剂", {"www.x.cn": "杭州市中医院"})
        self.assertNotIn("单位", c["source_fields"])

    def test_index_metadata_is_not_claimed_authoritative(self):
        """只有搜索索引的标题与摘要，没抓详情页——不得声明日期权威或已核实。"""
        c = to_candidate(item("某院检验试剂采购项目招标公告"), "试剂", {})
        self.assertFalse(c["date_authoritative"])
        self.assertFalse(c["retrieval_verified"])
        self.assertEqual(c["source"], "hosp")


class SiteSelectionTests(unittest.TestCase):
    def test_batches_respect_api_limit(self):
        hosts = [f"h{i}.cn" for i in range(45)]
        got = list(batches(hosts))
        self.assertEqual([len(b) for b in got], [20, 20, 5])
        self.assertLessEqual(max(len(b) for b in got), SITES_PER_CALL)

    def test_bad_domains_never_enter_whitelist(self):
        """被抢注/过期/第三方目录的域名必须排除——那些页面内容不可信。"""
        picked = load_sites(min_db=0, min_target=0)
        self.assertTrue(picked)
        self.assertFalse([r for r in picked if r.get("bad")])

    def test_selection_ignores_http_reachability(self):
        """s 与 db 是独立信号：HTTP 抓不到但豆包有产出的域名必须保留。"""
        picked = load_sites(min_db=1, min_target=0)
        self.assertTrue([r for r in picked if r.get("s") == 5],
                        "应保留 HTTP 不可达但豆包有招采产出的域名")


if __name__ == "__main__":
    unittest.main()
