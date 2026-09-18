# -*- coding: utf-8 -*-
"""台账反馈统计：窗口口径、来源过滤、分周聚合，以及运行报告对它的渲染。

口径依据见 scripts/feedback_stats.py 的模块说明：只算 AI 收集、窗口按推送时间
（空值回退插入时间）、无效率的分母是已反馈而不是推送。
"""

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import fake_feishu  # noqa: E402
import run_report  # noqa: E402
from feedback_stats import (AI_SOURCE, INVALID, VALID, StatsError, compute,  # noqa: E402
                            generate, read_rows)
from test_run_report import build_run, candidate  # noqa: E402

TODAY = date(2026, 9, 18)


def api_row(validity="", reason="", pushed="2026-09-18", inserted="", source=AI_SOURCE,
            unit="甲医院", title="甲医院过敏原试剂采购公告", region="华东大区"):
    """按接口返回形态造一行：文本字段是富文本数组，空值整个字段不出现。

    `pushed` 默认落在窗口内——绝大多数用例关心的是统计口径，不是窗口边界；
    要测无日期或出窗的行，显式传空或传别的日期。
    """
    fields = {}
    for name, value in (("标题", title), ("单位", unit), ("所属大区", region),
                        ("标讯来源", source), ("信息是否有效", validity),
                        ("无效原因(信息无效时填写)", reason), ("推送时间", pushed),
                        ("插入表格的时间", inserted)):
        if value:
            fields[name] = [{"text": str(value), "type": "text"}]
    return {"record_id": "rec" + str(abs(hash(title + pushed + validity))), "fields": fields}


def read(*specs):
    return read_rows([api_row(**spec) for spec in specs])


class ReadRowsTests(unittest.TestCase):
    def test_only_ai_sourced_rows_are_counted(self):
        # 「员工录入」不是管线的产出，表里那个游离的 true 同样不算。
        self.assertEqual(read({"source": "总部员工录入"}, {"source": "大区员工录入"},
                               {"source": "true"}), [])
        self.assertEqual(len(read({}, {"source": AI_SOURCE})), 2)

    def test_push_time_wins_over_insert_time(self):
        # 插入时间可能晚于推送（工作流回填），窗口要按真正推出去的那天算。
        row = read({"pushed": "2026-09-10", "inserted": "2026-09-16"})[0]
        self.assertEqual(row["推送日"], "2026-09-10")

    def test_push_day_falls_back_to_insert_time(self):
        # 实测 432 条 AI 行里有 62 条没有推送时间（都是 2026-08-01 那批），插入时间满覆盖。
        row = read({"pushed": "", "inserted": "2026-09-16"})[0]
        self.assertEqual(row["推送日"], "2026-09-16")

    def test_row_without_any_date_reads_as_undated(self):
        self.assertEqual(read({"pushed": "", "inserted": ""})[0]["推送日"], "")


class TallyTests(unittest.TestCase):
    def test_invalid_rate_uses_reviewed_not_pushed(self):
        # 推 4 条、只反馈 2 条、其中 1 条无效：无效率 50%（不是 25%），覆盖率 50%。
        window = compute(read({"validity": INVALID}, {"validity": VALID}, {}, {}),
                         TODAY)["window"]
        self.assertEqual((window["pushed"], window["reviewed"], window["invalid"]),
                         (4, 2, 1))
        self.assertEqual(window["coverage"], 0.5)
        self.assertEqual(window["invalid_rate"], 0.5)

    def test_unreviewed_rows_enter_neither_side(self):
        stats = compute(read({"validity": ""}, {"validity": VALID}), TODAY)
        self.assertEqual(stats["window"]["reviewed"], 1)
        self.assertEqual(stats["window"]["invalid"], 0)
        self.assertEqual(stats["cumulative"]["invalid_rate"], 0.0)

    def test_rate_is_none_when_nothing_was_reviewed(self):
        # 「一条都没反馈」不能显示成 0%——那会被读成「都对」。
        # 覆盖率不同：它有分母（推了 2 条），0% 是真实读数。
        stats = compute(read({}, {}), TODAY)["window"]
        self.assertIsNone(stats["invalid_rate"])
        self.assertEqual(stats["coverage"], 0.0)


class WindowTests(unittest.TestCase):
    def test_window_is_the_last_seven_days_inclusive(self):
        stats = compute(read({"pushed": "2026-09-12", "validity": VALID},
                             {"pushed": "2026-09-11", "validity": VALID},
                             {"pushed": "2026-09-18", "validity": VALID}), TODAY)
        self.assertEqual(stats["window"]["pushed"], 2)
        self.assertEqual((stats["window"]["start"], stats["window"]["end"]),
                         ("2026-09-12", "2026-09-18"))

    def test_window_length_follows_the_flag(self):
        rows_ = read({"pushed": "2026-09-13", "validity": VALID})
        self.assertEqual(compute(rows_, TODAY, days=7)["window"]["pushed"], 1)
        self.assertEqual(compute(rows_, TODAY, days=2)["window"]["pushed"], 0)

    def test_rows_outside_the_window_still_count_cumulatively(self):
        stats = compute(read({"pushed": "2026-08-01", "validity": INVALID},
                             {"pushed": "2026-09-18", "validity": VALID}), TODAY)
        self.assertEqual(stats["window"]["pushed"], 1)
        self.assertEqual(stats["cumulative"]["pushed"], 2)
        self.assertEqual(stats["cumulative"]["invalid"], 1)

    def test_undated_rows_are_disclosed_not_silently_dropped(self):
        stats = compute(read({"pushed": "", "inserted": ""},
                             {"pushed": "2026-09-18"}), TODAY)
        self.assertEqual(stats["undated"], 1)
        self.assertEqual(stats["cumulative"]["pushed"], 2)
        self.assertEqual(stats["window"]["pushed"], 1)


class ReasonTests(unittest.TestCase):
    def test_reasons_rank_by_count_then_recency(self):
        stats = compute(read(
            {"validity": INVALID, "reason": "公司无相关产品",
             "pushed": "2026-09-14", "unit": "A院"},
            {"validity": INVALID, "reason": "公司无相关产品",
             "pushed": "2026-09-16", "unit": "B院"},
            {"validity": INVALID, "reason": "存量续签",
             "pushed": "2026-09-17", "unit": "C院"},
        ), TODAY)
        reasons = stats["window"]["reasons"]
        self.assertEqual(reasons[0]["原因"], "公司无相关产品")
        self.assertEqual(reasons[0]["条数"], 2)
        self.assertEqual(reasons[0]["最后一条"], "2026-09-16")
        self.assertEqual(reasons[1]["原因"], "存量续签")

    def test_reasons_come_only_from_invalid_rows(self):
        stats = compute(read({"validity": VALID, "reason": "不该出现"}), TODAY)
        self.assertEqual(stats["window"]["reasons"], [])

    def test_blank_reasons_are_not_listed(self):
        stats = compute(read({"validity": INVALID, "reason": ""}), TODAY)
        self.assertEqual(stats["window"]["reasons"], [])


class WeeklyTests(unittest.TestCase):
    def test_weeks_are_monday_based_and_empty_weeks_are_kept(self):
        stats = compute(read({"pushed": "2026-09-16", "validity": VALID},
                             {"pushed": "2026-09-09", "validity": INVALID}),
                        TODAY, days=7, weeks=3)
        weeks = stats["weekly"]
        self.assertEqual([w["week_start"] for w in weeks],
                         ["2026-08-31", "2026-09-07", "2026-09-14"])
        # 空周照常出现：跳过去会让「这周没推」看起来像「这周没问题」。
        self.assertEqual(weeks[0]["pushed"], 0)
        self.assertEqual((weeks[1]["reviewed"], weeks[1]["invalid"]), (1, 1))
        self.assertEqual(weeks[2]["reviewed"], 1)

    def test_future_rows_cannot_land_in_a_week_bucket(self):
        stats = compute(read({"pushed": "2026-10-01", "validity": VALID}),
                        TODAY, weeks=3)
        self.assertEqual(sum(w["pushed"] for w in stats["weekly"]), 0)


class GenerateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_writes_json_into_the_run_dir(self):
        client, _ = fake_feishu.client([api_row(validity=VALID, pushed="2026-09-18")])
        path, stats = generate(self.root, client=client, today=TODAY)
        self.assertEqual(path, self.root / "feedback_stats.json")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["window"]["pushed"], 1)
        self.assertEqual(stats["cumulative"]["reviewed"], 1)

    def test_ledger_failure_raises_and_writes_nothing(self):
        # 拉了台账却失败时，宁可没有统计，也不要出一个假的「无效率 0%」。
        def denied(request, timeout=None):
            return fake_feishu._Response({"code": 99991672, "msg": "permission denied"})
        client, _ = fake_feishu.client(transport=denied)
        with self.assertRaises(StatsError):
            generate(self.root, client=client, today=TODAY)
        self.assertFalse((self.root / "feedback_stats.json").exists())


class ReportSectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "run"

    def render(self):
        return run_report.render_run(run_report.collect(self.root))

    def test_section_renders_when_the_stats_file_exists(self):
        build_run(self.root, queue=[candidate("C1")])
        (self.root / "feedback_stats.json").write_text(json.dumps({
            "days": 7, "scope": "AI收集", "undated": 0,
            "window": {"start": "2026-09-12", "end": "2026-09-18", "pushed": 52,
                       "reviewed": 43, "invalid": 14, "valid": 29,
                       "coverage": 0.8269, "invalid_rate": 0.3256,
                       "reasons": [{"原因": "公司无相关产品", "条数": 2,
                                    "单位": "某医院", "最后一条": "2026-09-16"}]},
            "cumulative": {"pushed": 432, "reviewed": 210, "invalid": 112,
                           "valid": 98, "coverage": 0.4861, "invalid_rate": 0.5333},
            "weekly": [{"week_start": "2026-09-14", "pushed": 34, "reviewed": 34,
                        "invalid": 13, "valid": 21, "invalid_rate": 0.3824}],
        }, ensure_ascii=False), encoding="utf-8")
        html = self.render()
        self.assertIn("台账反馈", html)
        self.assertIn("32.6%", html)          # 窗口无效率
        self.assertIn("82.7%", html)          # 覆盖率
        self.assertIn("公司无相关产品", html)   # 无效原因
        self.assertIn("09-14", html)          # 周趋势

    def test_missing_stats_render_a_hint_instead_of_failing(self):
        build_run(self.root, queue=[candidate("C1")])
        html = self.render()
        self.assertIn("feedback_stats.py", html)
        self.assertIn("本轮没有生成反馈统计", html)

    def test_zero_reviewed_shows_a_dash_not_zero_percent(self):
        build_run(self.root, queue=[candidate("C1")])
        (self.root / "feedback_stats.json").write_text(json.dumps({
            "days": 7, "scope": "AI收集",
            "window": {"start": "2026-09-12", "end": "2026-09-18", "pushed": 3,
                       "reviewed": 0, "invalid": 0, "valid": 0,
                       "coverage": None, "invalid_rate": None, "reasons": []},
            "cumulative": {"pushed": 3, "reviewed": 0, "invalid": 0, "valid": 0,
                           "coverage": None, "invalid_rate": None},
            "weekly": [],
        }, ensure_ascii=False), encoding="utf-8")
        html = self.render()
        self.assertIn("—", html)
        self.assertNotIn(">None<", html)


if __name__ == "__main__":
    unittest.main()
