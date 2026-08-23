import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from ccgp_search import CCGPError, collect, parse_detail_page, parse_search_page  # noqa: E402


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
<p>提交投标文件截止时间：2026年09月10日 11:00（北京时间）</p>
<p class="fjxx">附件信息：</p>
<a href="https://files.example.gov/spec.docx">采购文件.docx</a>
</body></html>
"""


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
        self.assertEqual(detail["source_fields"]["所属省/市"], "新疆")
        self.assertEqual(detail["source_fields"]["发布时间"], "2026-08-21")
        self.assertEqual(detail["source_fields"]["项目编号"], "ABC-2026-01")
        self.assertEqual(detail["source_fields"]["截止时间"], "2026-09-10T11:00")
        self.assertEqual(detail["source_fields"]["预算"], "200000")
        self.assertEqual(detail["attachments"][0]["name"], "采购文件.docx")
        self.assertIn("提交投标文件截止时间", detail["content"])

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
