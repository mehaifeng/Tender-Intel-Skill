import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_report  # noqa: E402
from tender_pipeline import prepare  # noqa: E402  仅用于取闸门实际写下的 skip_reason 前缀


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def candidate(candidate_id, title="某医院过敏原试剂采购项目招标公告", **extra):
    row = {
        "candidate_id": candidate_id,
        "title": title,
        "url": "https://example.invalid/{}".format(candidate_id),
        "publish_time": "2026-09-08T10:00:00+08:00",
        "site_name": "知了标讯",
        "source_fields": {"单位": "某医院"},
    }
    row.update(extra)
    return row


def build_run(root, *, queue=(), screened=(), already=(), concluded=(),
              results=None, push=(), summary=None, counts=None):
    """搭一个最小但结构真实的运行目录。"""
    root = Path(root)
    pipeline = root / "pipeline"
    write_jsonl(root / "candidate_index.jsonl",
                list(queue) + list(screened) + list(already) + list(concluded))
    write_jsonl(pipeline / "queue.jsonl", queue)
    write_jsonl(pipeline / "screened_out.jsonl", screened)
    write_jsonl(pipeline / "already_seen.jsonl", already)
    write_jsonl(pipeline / "concluded.jsonl", concluded)
    write_jsonl(pipeline / "semantic_review.jsonl", [])
    if results:
        (pipeline / "results").mkdir(parents=True, exist_ok=True)
        (pipeline / "results" / "batch-0001.json").write_text(
            json.dumps({"results": results}, ensure_ascii=False), encoding="utf-8")
    if push:
        (pipeline / "push_ledger.json").write_text(
            json.dumps({"records": list(push)}, ensure_ascii=False), encoding="utf-8")
    (root / "search_summary.json").write_text(
        json.dumps(summary or {"candidate_count": len(queue)}, ensure_ascii=False), encoding="utf-8")
    (pipeline / "manifest.json").write_text(json.dumps({
        "run_id": root.name, "mode": "daily-push", "state": "PUSHED",
        "counts": counts or {"indexed": len(queue) + len(screened) + len(already) + len(concluded),
                             "clusters": len(queue) + len(screened) + len(already) + len(concluded)},
    }, ensure_ascii=False), encoding="utf-8")
    return root


class ScreenGateAttributionTests(unittest.TestCase):
    """screened_out.jsonl 是五个闸门的混合体，报告只能靠 skip_reason 前缀分回去。

    前缀一旦在 prepare() 或 screen_domain() 里改字，报告会把所有候选静默归到
    「产品域预筛」——漏斗还是满的，但「丢在哪一关」全是错的。这类静默失真正是
    这张报告要消灭的东西，所以把前缀钉在这里。
    """

    def test_each_gate_lands_in_its_own_bucket(self):
        for reason, expected in (
            ("标题命中明确排除模式：中标结果", "title_exclude"),
            ("标题缺少招采/交易意图词", "no_intent"),
            ("纯流程性公告（开标）", "procedural"),
            ("采购主体非医疗机构（血站）", "non_hospital"),
            ("标题、摘要和搜索正文均无目标品类信号", "domain"),
            ("标题命中非本司产品域硬排除词：测序", "domain"),
        ):
            with self.subTest(reason=reason):
                self.assertEqual(run_report.classify_screened(reason), expected)

    def test_prepare_still_writes_the_prefixes_the_report_matches(self):
        source = (ROOT / "scripts" / "tender_pipeline.py").read_text(encoding="utf-8")
        for prefix, _ in run_report.SCREEN_PREFIXES:
            with self.subTest(prefix=prefix):
                self.assertIn(prefix, source)


class FunnelArithmeticTests(unittest.TestCase):
    """漏斗少一条都不行——它存在的全部意义就是「哪一关吃掉了多少」不再是黑盒。"""

    def test_search_chain_lands_exactly_on_indexed(self):
        summary = {
            "raw_result_count": 491,
            "source_candidate_count": 65,
            "intra_source_duplicates": 1,
            "source_summary": {"unique_notice_count": 470, "prefilter_excluded": 405},
        }
        stages = run_report.search_stages(summary, indexed=64)
        self.assertEqual(stages[0]["enter"], 491)
        self.assertEqual(stages[-1]["enter"] - stages[-1]["drop"], 64)
        for before, after in zip(stages, stages[1:]):
            self.assertEqual(before["enter"] - before["drop"], after["enter"])

    def test_unexplained_remainder_gets_its_own_stage(self):
        """摘要没细分的那一段宁可显式记一笔，也不让几百条在漏斗里凭空消失。"""
        stages = run_report.search_stages({"raw_result_count": 1178, "source_candidate_count": 274}, 265)
        self.assertEqual(stages[-1]["label"], "来源侧其它丢弃")
        self.assertEqual(stages[-1]["enter"] - stages[-1]["drop"], 265)

    def test_gate_stages_chain_from_clusters_down_to_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_run(
                Path(tmp) / "run",
                queue=[candidate("C1")],
                screened=[dict(candidate("C2"), skip_reason="标题缺少招采/交易意图词")],
                already=[dict(candidate("C3"), skip_reason="同一公告链接")],
                concluded=[dict(candidate("C4"), skip_reason="标的已有结论（中标公告）")],
            )
            stages = run_report.funnel(run_report.collect(root))
            gates = [s for s in stages if s["key"] in dict(run_report.DROP_STAGES)]
            self.assertEqual(gates[0]["enter"], 4)
            self.assertEqual(gates[-1]["enter"] - gates[-1]["drop"], 1)

    def test_partial_verification_does_not_count_pending_rows_as_passed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_run(
                Path(tmp) / "run",
                queue=[candidate("C1"), candidate("C2"), candidate("C3")],
                results=[{"candidate_id": "C1", "decision": "create", "record": {}},
                         {"candidate_id": "C2", "decision": "exclude", "reason": "无关"}],
            )
            verify = next(s for s in run_report.funnel(run_report.collect(root))
                          if s["key"] == "verify")
            self.assertEqual(verify["enter"], 3)
            self.assertEqual(verify["drop"], 2)  # 一条排除 + 一条仍待核实
            self.assertEqual(verify["enter"] - verify["drop"], 1)
            self.assertIn("待核实 1", verify["note"])

    def test_partial_push_does_not_count_unwritten_rows_as_pushed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_run(
                Path(tmp) / "run",
                queue=[candidate("C1"), candidate("C2")],
                results=[{"candidate_id": "C1", "decision": "create", "record": {}},
                         {"candidate_id": "C2", "decision": "create", "record": {}}],
                push=[{"candidate_id": "C1", "delivery_status": "confirmed",
                       "feishu_record_id": "rec1"}],
            )
            stage = next(s for s in run_report.funnel(run_report.collect(root))
                         if s["key"] == "push")
            self.assertEqual(stage["enter"], 2)
            self.assertEqual(stage["drop"], 1)
            self.assertEqual(stage["enter"] - stage["drop"], 1)
            self.assertIn("待写入 1", stage["note"])


class ReplayDiffTests(unittest.TestCase):
    """diff 就是为了回放同一批候选看改动的代价，所以它必须在重放过的目录上说真话。"""

    def _pair(self, tmp):
        base = build_run(
            Path(tmp) / "base",
            queue=[candidate("C1"), candidate("C2")],
            results=[{"candidate_id": "C1", "decision": "create", "record": {}},
                     {"candidate_id": "C2", "decision": "exclude"}],
            push=[{"candidate_id": "C1", "delivery_status": "confirmed", "feishu_record_id": "rec1"}],
        )
        new = build_run(
            Path(tmp) / "new",
            queue=[candidate("C2")],
            screened=[dict(candidate("C1"), skip_reason="标题、摘要和搜索正文均无目标品类信号")],
            results=[{"candidate_id": "C2", "decision": "exclude"}],
            # 重放前那一轮的回执还在目录里，说 C1 已经推送过。
            push=[{"candidate_id": "C1", "delivery_status": "confirmed", "feishu_record_id": "rec1"}],
        )
        return run_report.collect(base), run_report.collect(new)

    def test_stale_receipt_does_not_mask_a_candidate_that_left_the_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, new = self._pair(tmp)
            self.assertEqual(new["orphan_push"], 1)
            self.assertEqual(run_report.verdicts(new)["C1"]["stage"], "domain")

    def test_a_candidate_that_stops_being_pushed_counts_as_a_regression(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, new = self._pair(tmp)
            buckets = run_report.diff(base, new)
            self.assertEqual([item["id"] for _, item in buckets["regress"]], ["C1"])
            self.assertEqual(buckets["gain"], [])

    def test_identical_runs_report_no_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, _ = self._pair(tmp)
            self.assertEqual(sum(len(v) for v in run_report.diff(base, base).values()), 0)


class UntrustedContentTests(unittest.TestCase):
    """标题与采购人是不可信数据。报告是给人看的网页，转义漏一处就是可执行脚本。"""

    def test_markup_in_a_title_is_escaped_and_javascript_urls_are_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_run(Path(tmp) / "run", queue=[candidate(
                "C1", title="<img src=x onerror=alert(1)>过敏原试剂采购",
                url="javascript:alert(1)")])
            page = run_report.render_run(run_report.collect(root))
            self.assertNotIn("<img src=x", page)
            self.assertIn("&lt;img src=x", page)
            self.assertNotIn("javascript:alert", page)


class DegradedRunTests(unittest.TestCase):
    """跑挂的那一轮最需要报告——凭证故障长得就像「今天没情报」。"""

    def test_credential_failure_is_surfaced_not_rendered_as_a_quiet_empty_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "run"
            root.mkdir(parents=True)
            (root / "search_summary.json").write_text(json.dumps({
                "exit_code": 3, "source_auth_failed": True, "candidate_count": 0,
                "failure_reason": "积分不足",
            }, ensure_ascii=False), encoding="utf-8")
            run = run_report.collect(root)
            headline, detail = run_report.failure_of(run)
            self.assertIn("凭证失败", headline)
            self.assertIn("积分不足", detail)
            self.assertIn("凭证失败", run_report.render_run(run))

    def test_intentional_search_only_run_is_not_reported_as_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "run"
            root.mkdir(parents=True)
            (root / "search_summary.json").write_text(json.dumps({
                "exit_code": 0, "candidate_count": 0, "raw_result_count": 0,
            }), encoding="utf-8")
            self.assertIsNone(run_report.failure_of(run_report.collect(root)))

    def test_a_directory_that_is_not_a_run_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(run_report.ReportError):
                run_report.collect(tmp)


if __name__ == "__main__":
    unittest.main()
