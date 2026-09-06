import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from tender_identity import identity, duplicate_reason, IdentityIndex, remember_aliases
from tender_ledger import (ledger_lock, save_ledger, read_ledger, resolve_delivery,
                           resolve_review, LedgerError)
from search_common import canonical_url, write_candidates, merge_source_dirs
from tender_pipeline import (cluster_candidates, prepare, record_push, canonicalize_create,
                             resolve_review_item, PipelineError)
from send_webhook import FIELDS, send_once, SendError, sha256_bytes
import zlbx_search


def row(title="甲医院过敏原试剂采购公告", buyer="甲医院", day="2026-09-04", url="https://example.org/a", **kwargs):
    return {"标题": title, "单位": buyer, "发布时间": day, "链接": url, **kwargs}


class IdentityContractTests(unittest.TestCase):
    def same(self, a, b):
        return bool(duplicate_reason(identity(a), identity(b)))

    def test_repost_and_query_order(self):
        self.assertTrue(self.same(row(url="http://x.org/detail?b=2&id=9"),
                                  row(url="https://x.org/detail?id=9&b=2&utm_source=other")))

    def test_fragment_routes_are_part_of_identity(self):
        a, b = "https://x.org/#/detail?uuid=a", "https://x.org/#/detail?uuid=b"
        self.assertNotEqual(canonical_url(a), canonical_url(b))
        self.assertFalse(self.same(row(url=a, buyer="甲医院"), row(url=b, buyer="乙医院")))

    def test_generic_query_parameters_are_not_tracking(self):
        self.assertNotEqual(canonical_url("https://x.org/view?t=1"), canonical_url("https://x.org/view?t=2"))

    def test_different_buyers_with_same_title_survive_both_stages(self):
        with tempfile.TemporaryDirectory() as tmp:
            candidates = [{"title": "医用耗材公开遴选公告", "url": f"https://x.org/{i}",
                           "publish_time": "2026-09-04", "source_fields": {"单位": buyer}}
                          for i, buyer in enumerate(("甲医院", "乙医院"))]
            index = write_candidates(candidates, tmp, "2026-09-04")
            self.assertEqual(len(cluster_candidates(index)), 2)
            self.assertEqual(len(merge_source_dirs([tmp])), 2)

    def test_same_project_different_stage_round_and_package_survive(self):
        a = row(项目编号="P-2026-01")
        for title in ("甲医院过敏原试剂采购更正公告", "甲医院过敏原试剂采购公告(第二次)",
                      "甲医院过敏原试剂采购公告(A包)", "甲医院过敏原试剂采购结果公告"):
            self.assertFalse(self.same(a, row(title=title, url="https://x.org/other", 项目编号="P-2026-01")), title)

    def test_different_project_numbers_do_not_override_identical_template_title(self):
        # 明确项目号冲突是独立项目，不能只因同院同标题同日而合并。
        self.assertFalse(self.same(row(项目编号="P1"), row(url="https://x.org/b", 项目编号="P2")))

    def test_title_changes_with_same_project_scope_are_reposts(self):
        self.assertTrue(self.same(row(项目编号="P-2026"),
                                  row(title="甲医院检验科过敏原试剂公开招标公告", url="https://x.org/b", 项目编号="P-2026")))

    def test_new_correction_on_another_day_is_new_notice(self):
        self.assertFalse(self.same(row(title="甲医院过敏原采购更正公告"),
                                   row(title="甲医院过敏原采购更正公告", day="2026-09-05", url="https://x.org/new")))

    def test_unknown_buyer_does_not_merge_generic_templates(self):
        self.assertFalse(self.same(row(title="医用耗材公开遴选公告", buyer=""),
                                   row(title="医用耗材公开遴选公告", buyer="乙医院", url="https://x.org/b")))

    def test_unknown_buyer_repost_is_held_for_review(self):
        known = IdentityIndex([row(title="医用耗材公开遴选公告", buyer="", _pushed=True)])
        candidate = row(title="医用耗材公开遴选公告", buyer="乙医院", url="https://x.org/new")
        self.assertIsNone(known.find(candidate)[0])
        self.assertIsNotNone(known.possible(candidate)[0])

    def test_known_different_buyers_do_not_require_review(self):
        known = IdentityIndex([row(title="医用耗材公开遴选公告", buyer="甲医院")])
        self.assertIsNone(known.possible(row(title="医用耗材公开遴选公告", buyer="乙医院"))[0])

    def test_old_missing_buyer_with_specific_title_is_supported(self):
        title = "柳州市柳铁中心医院试剂耗材一批采购项目市场调查公告"
        self.assertTrue(self.same(row(title=title, buyer=""),
                                  row(title="【调查公告】" + title, buyer="柳州市柳铁中心医院", url="https://x.org/b")))

    def test_explicit_hospital_aliases(self):
        from tender_identity import buyer_key
        # 该医院的两个名称在随包医院索引中有确定性别名关系。
        a, b = "黑龙江省神经精神病医院", "黑龙江省第三医院"
        self.assertEqual(buyer_key(a), buyer_key(b))

    def test_same_title_later_reissue_is_new(self):
        self.assertFalse(self.same(row(), row(day="2027-09-04", url="https://x.org/new")))

    def test_bid_id_survives_adapter_and_merge(self):
        candidate = zlbx_search.build_candidate(
            {"bid_id": 123, "title": "甲医院过敏原试剂采购公告", "url": "https://www.zhiliaobiaoxun.com/content/123/b1"},
            {"source": "过敏原试剂", "source_url": "https://x.org/a"}, {(1, "过敏")})
        with tempfile.TemporaryDirectory() as tmp:
            write_candidates([candidate], tmp, "2026-09-04")
            merged = merge_source_dirs([tmp])[0]
            self.assertEqual(str(merged["bid_id"]), "123")
            self.assertIn("zlbx:123", identity(merged).ids)
            self.assertTrue(merged["alternate_sources"])

    def test_same_url_different_stage_keeps_bodies_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = write_candidates([
                {"title": "甲医院过敏原试剂采购公告", "url": "https://x.org/a", "content": "原文"},
                {"title": "甲医院过敏原试剂采购更正公告", "url": "https://x.org/a", "content": "更正"},
            ], tmp, "2026-09-04")
            self.assertEqual(len({r["candidate_id"] for r in index}), 2)
            self.assertEqual(len(merge_source_dirs([tmp])), 2)

    def test_seen_before_details_saves_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            seen = Path(tmp) / "seen.json"
            save_ledger(seen, {"records": [row(_pushed=True)]})
            item = {"bid_id": 9, "title": "甲医院过敏原试剂采购公告", "caller_name": "甲医院",
                    "pub_time": "2026-09-04", "url": "https://x.org/mirror", "sm_names": ["过敏原试剂"]}
            with patch.object(zlbx_search, "collect_listings", return_value={9: item}), \
                 patch.object(zlbx_search, "fetch_detail") as detail:
                candidates, stats = zlbx_search.collect(None, ["过敏"], datetime(2026,9,3), datetime(2026,9,5), 8, 50, 60, seen)
            detail.assert_not_called()
            self.assertEqual(candidates, [])
            self.assertEqual(stats["already_seen_before_detail_count"], 1)


class Response:
    status = 200
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self): return b'{"code":0}'


class DeliveryContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.seen = self.root / "seen.json"
        save_ledger(self.seen, {"records": []})
        self.calls = 0

    def setup_run(self, name="run", url="https://example.org/a"):
        run = self.root / name
        candidate = {"title": "甲医院过敏原试剂采购公告", "url": url, "publish_time": "2026-09-04",
                     "content": "采购过敏原试剂", "source_fields": {"单位": "甲医院"}, "bid_id": "123"}
        index = write_candidates([candidate], run, "2026-09-04")
        pipeline, manifest = prepare(run, self.seen, 10, "daily-push")
        cid = index[0]["candidate_id"]
        payload = {k: "null" for k in FIELDS}
        payload.update(row(url=url))
        payload_path = pipeline / "payloads/push" / (cid + ".json")
        payload_path.parent.mkdir(parents=True)
        body = json.dumps(payload, ensure_ascii=False).encode()
        payload_path.write_bytes(body)
        manifest.update({"state": "VALIDATED", "payload_dir": str(pipeline / "payloads"),
                         "decision_counts": {"create": 1},
                         "payloads": [{"flow": "push", "candidate_id": cid,
                                       "path": str(payload_path), "sha256": sha256_bytes(body)}]})
        (pipeline / "manifest.json").write_text(json.dumps(manifest))
        return manifest, cid, payload_path, body

    def transport(self, *args, **kwargs):
        self.calls += 1
        return Response()

    def send(self, run):
        return send_once(*run, "https://unused.invalid", transport=self.transport)

    def test_repeat_before_record_push_makes_one_post(self):
        run = self.setup_run()
        first, second = self.send(run), self.send(run)
        self.assertTrue(first["sent"])
        self.assertTrue(second["already_seen"])
        self.assertEqual(self.calls, 1)
        self.assertEqual(len(read_ledger(self.seen)["records"]), 1)

    def test_two_prepared_runs_cannot_both_send(self):
        a, b = self.setup_run("a"), self.setup_run("b", "https://x.org/mirror")
        self.send(a)
        second = self.send(b)
        self.assertEqual(self.calls, 1)
        self.assertTrue(second["already_seen"])
        state = record_push(self.root / "b", second["receipt"])
        self.assertEqual(state["state"], "PUSHED")
        self.assertEqual(state["push_counts"]["skipped"], 1)

    def test_successful_send_record_push_is_idempotent(self):
        run = self.setup_run()
        result = self.send(run)
        record_push(self.root / "run", result["receipt"])
        repeat = record_push(self.root / "run", result["receipt"])
        self.assertTrue(repeat["idempotent"])
        self.assertEqual(len(read_ledger(self.seen)["records"]), 1)

    def test_timeout_does_not_auto_retry(self):
        run = self.setup_run()
        with self.assertRaises(SendError):
            send_once(*run, "https://unused.invalid", transport=lambda *a, **k: (_ for _ in ()).throw(TimeoutError()))
        with self.assertRaises(SendError):
            self.send(run)
        self.assertEqual(self.calls, 0)
        self.assertEqual(len(read_ledger(self.seen)["records"]), 0)

    def test_confirmed_not_delivered_can_be_retried(self):
        run = self.setup_run()
        with self.assertRaises(SendError):
            send_once(*run, "https://unused.invalid", transport=lambda *a, **k: (_ for _ in ()).throw(TimeoutError()))
        attempt = next(iter(read_ledger(self.seen)["deliveries"]))
        resolve_delivery(self.seen, attempt, "not-delivered", "测试：已核对飞书中无此记录")
        self.assertTrue(self.send(run)["sent"])
        self.assertEqual(self.calls, 1)

    def test_crash_after_receipt_recovers_without_second_post(self):
        run = self.setup_run()
        real_save = save_ledger
        writes = []
        def fail_second(path, data):
            writes.append(1)
            if len(writes) == 2:
                raise OSError("simulated disk failure after receipt")
            real_save(path, data)
        with patch("send_webhook.save_ledger", side_effect=fail_second), self.assertRaises(OSError):
            self.send(run)
        self.assertTrue(self.send(run)["already_seen"])
        self.assertEqual(self.calls, 1)

    def test_another_process_cannot_hold_the_same_ledger_lock(self):
        code = "from tender_ledger import ledger_lock; import sys\nwith ledger_lock(sys.argv[1]): print('ACQUIRED')"
        with ledger_lock(self.seen):
            result = subprocess.run([sys.executable, "-c", code, str(self.seen)],
                                    env={"PYTHONPATH": str(ROOT / "scripts")}, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("ACQUIRED", result.stdout)

    def test_old_history_is_not_pruned_when_new_record_is_confirmed(self):
        save_ledger(self.seen, {"records": [row(title="乙医院过敏原试剂采购公告", buyer="乙医院",
                                              url="https://x.org/old", day="2020-01-01", _pushed=True)]})
        self.send(self.setup_run())
        self.assertEqual(len(read_ledger(self.seen)["records"]), 2)

    def test_possible_duplicate_is_not_sent(self):
        run = self.setup_run()
        save_ledger(self.seen, {"records": [row(title="甲医院过敏原试剂采购公告", buyer="",
                                              url="https://x.org/old", _pushed=True)]})
        # 短标题且缺采购人，不足以确定跨链接是同一医院，应阻止直接发送。
        with self.assertRaises(SendError):
            self.send(run)
        self.assertEqual(self.calls, 0)

    def test_review_resolution_unblocks_the_send_gate(self):
        run = self.setup_run()
        save_ledger(self.seen, {"records": [row(title="甲医院过敏原试剂采购公告", buyer="",
                                              url="https://x.org/old", _pushed=True)]})
        with self.assertRaises(SendError):
            self.send(run)
        resolve_review(self.seen, row(), "new", "测试：已核对飞书中无此条")
        self.assertTrue(self.send(run)["sent"])
        self.assertEqual(self.calls, 1)


class ReviewResolutionTests(unittest.TestCase):
    """疑似重复必须有出口：核对飞书后能放行，也能永久判定为重复。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.seen = self.root / "seen.json"
        # 旧台账没存采购人，候选换了来源链接：两边都不足以判定，只能留待人工核对。
        save_ledger(self.seen, {"records": [row(buyer="", _pushed=True)]})
        self.run = self.root / "run"
        index = write_candidates([{
            "title": "甲医院过敏原试剂采购公告", "url": "https://x.org/new",
            "publish_time": "2026-09-04", "content": "采购过敏原试剂",
            "source_fields": {"单位": "甲医院"}, "bid_id": "123",
        }], self.run, "2026-09-04")
        self.cid = index[0]["candidate_id"]

    def counts(self, force=False):
        return prepare(self.run, self.seen, 10, "daily-push", force)[1]["counts"]

    def test_suspect_is_held_out_of_the_queue(self):
        counts = self.counts()
        self.assertEqual(counts["dedup_review"], 1)
        self.assertEqual(counts["queued"], 0)

    def test_confirmed_new_notice_reaches_the_queue(self):
        self.counts()
        result = resolve_review_item(self.run, self.cid, "new", "测试：已核对飞书中无此条")
        self.assertIn("prepare", result["next_action"])
        counts = self.counts(force=True)
        self.assertEqual(counts["queued"], 1)
        self.assertEqual(counts["dedup_review"], 0)

    def test_confirmed_duplicate_stops_recurring(self):
        self.counts()
        resolve_review_item(self.run, self.cid, "duplicate", "测试：飞书已有该记录")
        counts = self.counts(force=True)
        self.assertEqual(counts["already_seen"], 1)
        self.assertEqual(counts["dedup_review"], 0)
        self.assertEqual(counts["queued"], 0)

    def test_resolution_requires_a_stated_basis(self):
        self.counts()
        for outcome, note in (("new", "   "), ("duplicate", ""), ("既非", "测试")):
            with self.assertRaises(LedgerError):
                resolve_review_item(self.run, self.cid, outcome, note)
        self.assertEqual(self.counts(force=True)["dedup_review"], 1)

    def test_unknown_candidate_is_rejected(self):
        self.counts()
        with self.assertRaises(PipelineError):
            resolve_review_item(self.run, "no-such-candidate", "new", "测试")

    def test_strong_duplicate_cannot_be_declared_new(self):
        save_ledger(self.seen, {"records": [row(_pushed=True)]})
        with self.assertRaises(LedgerError):
            resolve_review(self.seen, row(), "new", "测试：不应放行强身份重复")


if __name__ == "__main__":
    unittest.main()
