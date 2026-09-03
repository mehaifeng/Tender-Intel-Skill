import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from ccgp_search import (  # noqa: E402
    CCGPError,
    collect,
    parse_detail_page,
    parse_query_list,
    parse_search_page,
)


SEARCH_HTML = """
<html><head><title>采购公告搜索_中国政府采购网</title></head><body>
<p>关键字：过敏原 全文检索 共找到 <span>1</span> 条内容</p>
<script>Pager({size: 1, current: 0});</script>
<ul class="vT-srch-result-list-bid">
  <li>
    <a href="http://www.ccgp.gov.cn/cggg/dfgg/gkzb/202608/t20260821_27184986.htm">
      某医院<font color="red">过敏原</font>试剂公开招标公告
    </a>
    <p>项目概况：过敏原试剂采购。</p>
    <span>2026.08.21 19:47:44 | 采购人：某医院 | 代理机构：某代理<br>
      <strong>公开招标公告</strong> | 新疆 |
    </span>
  </li>
</ul>
</body></html>
"""


DETAIL_HTML = """
<html><head>
<meta name="ArticleTitle" content="某医院过敏原试剂公开招标公告">
</head><body>
<table>
<tr><td>采购单位</td><td>某医院</td></tr>
<tr><td>行政区域</td><td>新疆维吾尔自治区</td><td>公告时间</td><td>2026年08月21日 19:47</td></tr>
<tr><td>预算金额</td><td>￥20.000000万元（人民币）</td></tr>
</table>
<p>项目编号：ABC-2026-01</p>
<p>采购方式：公开招标</p>
<p>使用科室：医学检验科</p>
<p>提交投标文件截止时间：2026年09月10日 11:00（北京时间）</p>
<p class="fjxx">附件信息：</p>
<a href="https://files.example.gov/spec.docx">采购文件.docx</a>
</body></html>
"""


class CCGPQueryListTests(unittest.TestCase):
    """清单从 references/keywords.md 解析，与睿销共用同一份。

    2026-09-03 实测：睿销与 CCGP 都是「长复合词命中骤降、短片段是超集」
    （`免疫印迹仪` 33/0 vs `免疫印迹` 81/3；`类风湿关节炎` 5/0 vs `类风湿` 62/1），
    所以清单按最短片段放宽，去留交给筛选层与核实阶段的模型。
    """

    def test_every_query_is_a_single_word(self):
        # CCGP 全文检索对多词组合会收窄为同时匹配，解析器强制校验无空格。
        queries = parse_query_list()
        self.assertGreaterEqual(len(queries), 80)
        self.assertEqual(len(queries), len(set(queries)))
        for query in queries:
            self.assertNotIn(" ", query)
            self.assertNotIn("`", query)

    def test_both_keyword_tables_are_covered(self):
        queries = parse_query_list()
        for term in ("过敏", "变态反应", "变应", "IgE", "特异性IgG", "sIgG", "不耐受",
                     "混合筛查", "印迹", "自免", "自身免疫", "自身抗体", "核抗体", "ANA",
                     "狼疮", "干燥综合", "硬皮", "胞浆抗体", "ANCA", "血管炎", "肌炎",
                     "膜性肾病", "PBC", "胆汁性", "心磷脂", "风湿", "类风关", "糖尿病抗体",
                     "胃肠疾病", "亚类", "大疱", "羟基维生素", "细胞因子", "白介素",
                     "肿瘤坏死因子"):
            self.assertIn(term, queries)

    def test_project_codes_are_searched(self):
        # 代号在库内确实有命中（dsDNA 6、SS-A 6、Scl 14、aCL 3、RA33 3、PLA2R 3、
        # C1q 7、GBM 8、CCP 15），一律进检索。
        queries = parse_query_list()
        for code in ("dsDNA", "SS-A", "SS-B", "Ro52", "CENP-B", "Scl", "Jo-1", "C1q",
                     "GBM", "MPO", "PR3", "MDA5", "SRP", "PL-12", "AMA-M2", "gp210",
                     "sp100", "SLA/LP", "aCL", "GP1", "CCP", "RA33", "ZnT8", "PLA2R",
                     "Dsg", "BP180", "IgG4", "IL-", "TNF", "IFN"):
            self.assertIn(code, queries)

    def test_prefix_merges_replace_the_per_code_rows(self):
        """前缀合并：一条覆盖整族，既放宽召回又省请求。"""
        queries = parse_query_list()
        for merged in ("IL-1β", "IL-2R", "IL-12p70", "IL-33", "IFN-α", "IFN-γ",
                       "TNF-α", "Scl-70", "PM-Scl", "Dsg1", "Dsg2",
                       "免疫印迹仪", "蛋白印迹仪"):
            self.assertNotIn(merged, queries)

    def test_shortest_unambiguous_form_wins(self):
        """长写法是短写法的子集，一律取短的（实测数见 keywords.md）。"""
        queries = parse_query_list()
        for short, dropped in (("风湿", "类风湿"), ("风湿", "类风湿因子"),
                               ("羟基维生素", "25羟基"), ("IgE", "sIgE"),
                               ("特异性IgG", "食物特异性IgG"), ("亚类", "IgG亚类"),
                               ("不耐受", "食物不耐受"), ("变应", "变应原"),
                               ("狼疮", "红斑狼疮"), ("印迹", "免疫印迹"),
                               ("胰岛细胞", "胰岛细胞抗体"), ("胃壁", "胃壁细胞"),
                               ("内因子", "内因子抗体"), ("脱羧酶", "谷氨酸脱羧酶"),
                               ("自免", "自免肝")):
            self.assertIn(short, queries)
            self.assertNotIn(dropped, queries)

    def test_over_broad_forms_are_rejected(self):
        """放宽有下限：这些实测会淹没结果，不进清单。

        `硬化` 8389（动脉硬化、硬化路面）、`胰岛` 425（胰岛素药品）、`磷脂` 104
        （磷脂酶、卵磷脂）、`SS-` 278；`His` 361／`Sm` 97／`RF` 97／`IF` 24 这类
        两三字母代号命中的基本是英文碎片。
        """
        queries = parse_query_list()
        for noisy in ("硬化", "胰岛", "磷脂", "SS-", "His", "Sm", "RF", "IF",
                      "PCA", "ICA", "IAA", "AGA", "Nuc", "P0", "IL"):
            self.assertNotIn(noisy, queries)


class CCGPParserTests(unittest.TestCase):
    def test_search_page(self):
        rows, total, pages = parse_search_page(SEARCH_HTML)
        self.assertEqual((total, pages, len(rows)), (1, 1, 1))
        self.assertEqual(rows[0]["publish_time"], "2026-08-21 19:47:44")
        self.assertEqual(rows[0]["buyer"], "某医院")
        self.assertEqual(rows[0]["notice_type"], "公开招标公告")
        self.assertTrue(rows[0]["url"].startswith("https://www.ccgp.gov.cn/"))

    def test_detail_page_extracts_fields_and_attachment(self):
        detail = parse_detail_page(
            DETAIL_HTML,
            "https://www.ccgp.gov.cn/cggg/dfgg/gkzb/202608/t20260821_27184986.htm",
            {"notice_type": "公开招标公告"},
        )
        self.assertEqual(detail["source_fields"]["单位"], "某医院")
        self.assertEqual(detail["source_fields"]["地区"], "新疆维吾尔自治区")
        self.assertEqual(detail["source_fields"]["所属省/市"], "新疆")
        self.assertEqual(detail["source_fields"]["科室"], "医学检验科")
        self.assertEqual(detail["source_fields"]["发布时间"], "2026-08-21")
        self.assertEqual(detail["source_fields"]["项目编号"], "ABC-2026-01")
        self.assertEqual(detail["source_fields"]["截止时间"], "2026-09-10T11:00")
        self.assertEqual(detail["source_fields"]["预算"], "200000")
        self.assertEqual(detail["attachments"][0]["name"], "采购文件.docx")
        self.assertIn("提交投标文件截止时间", detail["content"])

    def test_detail_page_preserves_locality_after_full_province_name(self):
        detail = parse_detail_page(
            DETAIL_HTML.replace("新疆维吾尔自治区", "安徽省滁州市凤阳县"),
            "https://www.ccgp.gov.cn/cggg/dfgg/gkzb/202608/t20260821_27184986.htm",
            {"notice_type": "公开招标公告"},
        )
        self.assertEqual(detail["source_fields"]["所属省/市"], "安徽")
        self.assertEqual(detail["source_fields"]["地区"], "安徽省滁州市凤阳县")

    def test_rate_limit_stops_new_searches_but_keeps_collected_candidate(self):
        class FakeClient:
            def get(self, url, referer=None):
                if "kw=%E8%BF%87%E6%95%8F%E5%8E%9F" in url:
                    return SEARCH_HTML
                if "bxsearch" in url:
                    raise CCGPError("中国政府采购网返回访问频繁页；本轮停止，不连续重试")
                return DETAIL_HTML

        candidates, failures, raw_count = collect(
            FakeClient(), ["过敏原", "自身抗体"],
            __import__("datetime").date(2026, 8, 20),
            __import__("datetime").date(2026, 8, 23),
        )
        self.assertEqual(raw_count, 1)
        self.assertEqual(len(candidates), 1)
        self.assertTrue(candidates[0]["retrieval_verified"])
        self.assertEqual(len(failures), 1)


if __name__ == "__main__":
    unittest.main()
