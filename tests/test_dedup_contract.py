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
sys.path.insert(0, str(ROOT / "tests"))
from tender_identity import identity, duplicate_reason, IdentityIndex, remember_aliases
from tender_ledger import (ledger_lock, fetch_ledger, save_snapshot, snapshot_path,
                           LedgerError)
from search_common import canonical_url, write_candidates, merge_source_dirs
from tender_pipeline import (authorize_unattended, cluster_candidates, prepare, record_push,
                             canonicalize_create, resolve_semantic, PipelineError)
from send_record import FIELDS, send_once, SendError, sha256_bytes, build_fields
from feishu_client import FeishuError, cell_value, date_text, epoch_ms
import fake_feishu
from fake_feishu import FakeFeishu, ledger_row
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
        item = {"bid_id": 9, "title": "甲医院过敏原试剂采购公告", "caller_name": "甲医院",
                "pub_time": "2026-09-04", "url": "https://x.org/mirror", "sm_names": ["过敏原试剂"]}
        with patch.object(zlbx_search, "collect_listings", return_value={9: item}), \
             patch.object(zlbx_search, "fetch_detail") as detail:
            candidates, stats = zlbx_search.collect(
                None, ["过敏"], datetime(2026, 9, 3), datetime(2026, 9, 5), 8, 50, 60,
                ledger_records=[row(_pushed=True)])
        detail.assert_not_called()
        self.assertEqual(candidates, [])
        self.assertEqual(stats["already_seen_before_detail_count"], 1)


class FeishuLedgerTests(unittest.TestCase):
    """接口返回的富文本与 URL 对象必须还原成判重用得上的纯文本。"""

    def test_rows_are_normalized_for_identity(self):
        client, _ = fake_feishu.client([ledger_row()])
        ledger = fetch_ledger(client)
        record = ledger["records"][0]
        self.assertEqual(record["标题"], "甲医院过敏原试剂采购公告")
        self.assertEqual(record["链接"], "https://example.org/a")
        self.assertEqual(record["_feishu_id"], "ZB-000001")
        self.assertTrue(record["_pushed"])
        self.assertEqual(ledger["row_count"], 1)
        self.assertNotIn("app_token", ledger)
        self.assertNotIn("table_id", ledger)

    def test_fetch_failure_never_degrades_to_an_empty_ledger(self):
        real = FakeFeishu()

        def broken(request, timeout=None):
            if request.full_url.split("?")[0].endswith("/records/search"):
                raise TimeoutError()
            return real(request, timeout)

        client, _ = fake_feishu.client(transport=broken)
        with self.assertRaises(LedgerError):
            fetch_ledger(client)


class PayloadToTableTests(unittest.TestCase):
    """16 字段载荷到多维表格列的映射；空值不再写成字符串 null。"""

    def payload(self, **overrides):
        payload = {field: "null" for field in FIELDS}
        payload.update({"标题": "甲医院过敏原试剂采购公告", "链接": "https://example.org/a",
                        "单位": "甲医院", "命中关键词": "过敏原", "所属省/市": "安徽",
                        "地区": "安徽省亳州市", "所属大区": "华中大区", "采购方式": "公开招标",
                        "科室": "检验科", "内容（检索的摘要）": "采购过敏原试剂"})
        payload.update(overrides)
        return payload

    def fields(self, **overrides):
        return build_fields(self.payload(**overrides), fake_feishu.SCHEMA)

    def test_null_values_are_omitted_not_written(self):
        fields, _ = self.fields()
        self.assertNotIn("项目编号", fields)
        self.assertNotIn("医院等级", fields)
        self.assertNotIn("null", list(fields.values()))

    def test_payload_names_are_mapped_to_table_columns(self):
        fields, _ = self.fields()
        self.assertEqual(fields["科室名称"], "检验科")
        self.assertEqual(fields["关键词命中"], "过敏原")
        self.assertEqual(fields["内容"], "采购过敏原试剂")
        self.assertNotIn("科室", fields)

    def test_url_field_takes_its_own_shape(self):
        fields, _ = self.fields()
        self.assertEqual(fields["链接"], {"link": "https://example.org/a",
                                          "text": "https://example.org/a"})

    def test_source_and_status_are_filled_by_the_sender(self):
        fields, _ = self.fields()
        self.assertEqual(fields["标讯来源"], "AI收集")
        self.assertEqual(fields["标讯状态"], "新插入")

    def test_push_state_columns_are_left_to_the_table_workflow(self):
        fields, dropped = self.fields()
        for name in ("是否已推送", "插入表格的时间", "推送时间"):
            self.assertNotIn(name, fields)
        self.assertEqual(dropped, [])

    def test_unknown_single_select_option_is_dropped_with_a_trace(self):
        fields, dropped = self.fields(采购方式="比选")
        self.assertNotIn("采购方式", fields)
        self.assertTrue(any(d["字段"] == "采购方式" and d["值"] == "比选" for d in dropped))


class DateColumnTests(unittest.TestCase):
    """日期列按租户时区（+08）在毫秒与日历日之间来回，认不出的写法必须报错。"""

    def test_calendar_day_is_read_back_as_the_same_day(self):
        for text in ("2026-09-04", "2026-09-04T09:00", "2026-09-04 09:00:30"):
            with self.subTest(text=text):
                self.assertEqual(date_text(epoch_ms(text)), "2026-09-04")

    def test_午夜前后不跨日(self):
        # 按 UTC 解释的话 00:00 会退回前一天、23:59 会跳到后一天。
        self.assertEqual(date_text(epoch_ms("2026-09-04T00:00")), "2026-09-04")
        self.assertEqual(date_text(epoch_ms("2026-09-04T23:59")), "2026-09-04")

    def test_timestamps_and_blanks_pass_through(self):
        self.assertEqual(epoch_ms(1700000000000), 1700000000000)
        self.assertEqual(epoch_ms("1700000000000"), 1700000000000)
        for blank in (None, "", "   "):
            self.assertIsNone(epoch_ms(blank))

    def test_unparsable_date_raises_instead_of_silently_skipping(self):
        # cell_value 里 None 的语义是"这个字段不写"，认不出就返回 None 会让
        # 日期整列静默落空，所以这里必须抛。
        for bad in ("2026/09/04", "2026年9月4日", "昨天", "2026-09", "2026-09-04 09"):
            with self.subTest(bad=bad):
                with self.assertRaises(FeishuError):
                    cell_value(fake_feishu.DATETIME, bad)

    def test_impossible_calendar_date_raises(self):
        with self.assertRaises(FeishuError):
            epoch_ms("2026-13-45")


class DeliveryContractTests(unittest.TestCase):
    """发送门禁：台账在飞书，每次发送前重新拉取并重新查重。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fake = FakeFeishu()
        self.client = fake_feishu.client(transport=self.fake)[0]

    def setup_run(self, name="run", url="https://example.org/a", ledger_rows=()):
        run = self.root / name
        candidate = {"title": "甲医院过敏原试剂采购公告", "url": url, "publish_time": "2026-09-04",
                     "content": "采购过敏原试剂", "source_fields": {"单位": "甲医院"}, "bid_id": "123"}
        index = write_candidates([candidate], run, "2026-09-04")
        snapshot_client = fake_feishu.client(list(ledger_rows))[0]
        save_snapshot(snapshot_path(run), fetch_ledger(snapshot_client))
        pipeline, manifest = prepare(run, 10, "daily-push")
        cid = index[0]["candidate_id"]
        payload = {k: "null" for k in FIELDS}
        payload.update(row(url=url))
        payload["命中关键词"] = "过敏原检测"  # 该字段不接受 null，载荷校验会拦下
        payload_path = pipeline / "payloads/push" / (cid + ".json")
        payload_path.parent.mkdir(parents=True)
        body = json.dumps(payload, ensure_ascii=False).encode()
        payload_path.write_bytes(body)
        manifest.update({"state": "VALIDATED", "payload_dir": str(pipeline / "payloads"),
                         "decision_counts": {"create": 1},
                         "payloads": [{"flow": "push", "candidate_id": cid,
                                       "path": str(payload_path), "sha256": sha256_bytes(body)}]})
        (pipeline / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False),
                                                encoding="utf-8")
        return manifest, cid, payload_path, body

    def send(self, run):
        return send_once(*run, client=self.client)

    def test_repeat_before_record_push_writes_one_row(self):
        run = self.setup_run()
        first, second = self.send(run), self.send(run)
        self.assertTrue(first["sent"])
        self.assertTrue(second["already_seen"])
        self.assertEqual(len(self.fake.created), 1)

    def test_success_hinges_on_record_id_not_on_the_auto_number(self):
        # 新增记录的响应不含自动编号；成功判定只能看 record_id，回执照样能登记。
        run = self.setup_run()
        result = self.send(run)
        self.assertTrue(result["feishu_record_id"])
        self.assertEqual(result["feishu_id"], "")
        state = record_push(self.root / "run", result["receipt"])
        self.assertEqual(state["push_counts"]["confirmed"], 1)

    def test_two_prepared_runs_cannot_both_write(self):
        a = self.setup_run("a")
        b = self.setup_run("b", "https://x.org/mirror")
        self.send(a)
        second = self.send(b)
        self.assertEqual(len(self.fake.created), 1)
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
        self.assertEqual(len(self.fake.created), 1)

    def test_row_added_by_someone_else_between_prepare_and_send_blocks_the_write(self):
        run = self.setup_run()
        # prepare 之后别人手工加了同一条：发送前重新拉取必须看见它。
        self.fake.rows.append(ledger_row(链接="https://example.org/a", 编号="ZB-MANUAL"))
        result = self.send(run)
        self.assertTrue(result["already_seen"])
        self.assertEqual(len(self.fake.created), 0)

    def test_unknown_write_result_is_verified_by_lookup_not_by_retrying(self):
        run = self.setup_run()
        self.fake.create_error = TimeoutError()
        self.fake.create_lands_anyway = True
        result = self.send(run)
        # 回查确认行已落库：算成功，且没有第二次写入。
        self.assertTrue(result["sent"])
        self.assertEqual(len(self.fake.created), 1)

    def test_unknown_write_result_that_did_not_land_stops_without_retrying(self):
        run = self.setup_run()
        self.fake.create_error = TimeoutError()
        with self.assertRaises(SendError):
            self.send(run)
        self.assertEqual(len(self.fake.created), 0)
        # 行确实没落库，下一次发送才允许真正写入，且只写一行。
        self.fake.create_error = None
        self.assertTrue(self.send(run)["sent"])
        self.assertEqual(len(self.fake.created), 1)

    def test_known_business_rejection_is_not_misreported_as_unknown(self):
        run = self.setup_run()
        real = self.fake

        def rejected(request, timeout=None):
            if request.full_url.split("?")[0].endswith("/records"):
                return fake_feishu._Response({"code": 1254064, "msg": "字段校验失败"})
            return real(request, timeout)

        self.client = fake_feishu.client(transport=rejected)[0]
        with self.assertRaisesRegex(FeishuError, "字段校验失败"):
            self.send(run)
        # 只发生发送前的台账读取；确定性业务错误不做“结果未知”URL 回查。
        self.assertEqual(real.searches, 1)

    def test_unattended_authorization_only_lifts_a_report_only_run(self):
        """定时跑先按离线建了队列时的补救路径；已 PUSHED 的运行不许再改门禁位。"""
        run = self.setup_run()
        manifest = run[0]
        pipeline = Path(manifest["payload_dir"]).parent
        manifest_path = pipeline / "manifest.json"

        def rewrite(**changes):
            manifest_path.write_text(json.dumps({**manifest, **changes}, ensure_ascii=False),
                                     encoding="utf-8")

        rewrite(mode="report-only", live_push_allowed=False)
        lifted = authorize_unattended(self.root / "run")
        self.assertEqual(lifted["mode"], "daily-push")
        self.assertTrue(lifted["live_push_allowed"])
        self.assertEqual(lifted["mode_authorization"]["previous_mode"], "report-only")

        rewrite(mode="report-only", live_push_allowed=False, state="PUSHED")
        with self.assertRaisesRegex(PipelineError, "已PUSHED"):
            authorize_unattended(self.root / "run")

    def test_another_process_cannot_hold_the_same_snapshot_lock(self):
        run = self.setup_run()
        path = Path(run[0]["ledger_snapshot"])
        code = ("from tender_ledger import ledger_lock; import sys\n"
                "with ledger_lock(sys.argv[1]): print('ACQUIRED')")
        with ledger_lock(path):
            # 子进程的报错在简中 Windows 上是 GBK；显式指定解码，别按 locale 猜。
            result = subprocess.run([sys.executable, "-c", code, str(path)],
                                    env={"PYTHONPATH": str(ROOT / "scripts")},
                                    capture_output=True, text=True,
                                    encoding="utf-8", errors="replace")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("ACQUIRED", result.stdout)

    def test_semantic_suspect_is_not_sent(self):
        # 台账缺采购人、链接也不同：字符级相似但证据不足，发送门禁必须扣下。
        old = ledger_row(链接="https://x.org/old", 单位="", 编号="ZB-OLD")
        run = self.setup_run(ledger_rows=[old])
        self.fake.rows.append(old)
        with self.assertRaises(SendError):
            self.send(run)
        self.assertEqual(len(self.fake.created), 0)


class SemanticReviewTests(unittest.TestCase):
    """语义判定出口：模型定案后才放行或判重，结论落在本次运行里。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.run = self.root / "run"
        index = write_candidates([{
            "title": "甲医院过敏原试剂采购公告", "url": "https://x.org/new",
            "publish_time": "2026-09-04", "content": "采购过敏原试剂",
            "source_fields": {"单位": "甲医院"}, "bid_id": "123",
        }], self.run, "2026-09-04")
        self.cid = index[0]["candidate_id"]
        # 台账没存采购人，候选换了来源链接：两边都不足以判定，只能交给语义比对。
        client, _ = fake_feishu.client([ledger_row(链接="https://x.org/old", 单位="",
                                                   编号="ZB-OLD")])
        save_snapshot(snapshot_path(self.run), fetch_ledger(client))

    def counts(self, force=False):
        return prepare(self.run, 10, "daily-push", force)[1]["counts"]

    def pair_id(self):
        text = (self.run / "pipeline" / "semantic_review.jsonl").read_text(encoding="utf-8")
        return json.loads(text.splitlines()[0])["台账候选"][0]["pair_id"]

    def decide(self, same, note="测试依据"):
        path = self.root / "decisions.json"
        path.write_text(json.dumps([{"pair_id": self.pair_id(), "same": same, "note": note}],
                                   ensure_ascii=False), encoding="utf-8")
        return resolve_semantic(self.run, path)

    def test_suspect_is_held_out_of_the_queue(self):
        counts = self.counts()
        self.assertEqual(counts["semantic_review"], 1)
        self.assertEqual(counts["queued"], 0)

    def test_model_says_different_and_the_notice_reaches_the_queue(self):
        self.counts()
        next_action = self.decide(False)["next_action"]
        self.assertIn("prepare", next_action)
        self.assertIn("--mode daily-push", next_action)
        counts = self.counts(force=True)
        self.assertEqual(counts["queued"], 1)
        self.assertEqual(counts["semantic_review"], 0)

    def test_model_says_same_and_the_notice_stops_recurring(self):
        self.counts()
        self.decide(True)
        counts = self.counts(force=True)
        self.assertEqual(counts["already_seen"], 1)
        self.assertEqual(counts["semantic_review"], 0)
        self.assertEqual(counts["queued"], 0)

    def test_decisions_require_a_stated_basis_and_a_boolean(self):
        self.counts()
        path = self.root / "bad.json"
        for value in ({"pair_id": self.pair_id(), "same": True, "note": "  "},
                      {"pair_id": self.pair_id(), "note": "有依据"},
                      {"pair_id": "不存在~ZB-OLD", "same": True, "note": "有依据"}):
            path.write_text(json.dumps([value], ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(PipelineError):
                resolve_semantic(self.run, path)
        self.assertEqual(self.counts(force=True)["semantic_review"], 1)


if __name__ == "__main__":
    unittest.main()
