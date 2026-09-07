import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import tender_search  # noqa: E402
from tender_search import AUTH_ERROR_EXIT_CODE, build_command  # noqa: E402
from tender_pipeline import prepare, PipelineError  # noqa: E402
from tender_ledger import save_ledger  # noqa: E402


class TenderSearchEntryTests(unittest.TestCase):
    def test_command_passes_through_search_parameters(self):
        class Args:
            time_range = "72h"
            queries = None
            batch_size = 8
            page_size = 50
            max_details = 60
            delay = 0.25
            dry_run = False

        command = build_command(Args(), Path("/tmp/out"))
        self.assertIn("zlbx_search.py", command[1])
        self.assertIn("--time-range", command)
        self.assertIn("--max-details", command)
        self.assertNotIn("--dry-run", command)

    def test_auth_failure_uses_its_own_exit_code(self):
        """凭证故障必须与「今天没有新公告」区分开，否则无人值守会静默失效。"""
        self.assertEqual(AUTH_ERROR_EXIT_CODE, 3)

    def _run_with_failed_source(self, out_dir, code=AUTH_ERROR_EXIT_CODE):
        class Completed:
            returncode = code
            stdout = ""
            stderr = "错误：知了标讯拒绝了这个 API Key"

        argv = ["tender_search.py", "--out-dir", str(out_dir), "--time-range", "72h"]
        with patch.object(tender_search.subprocess, "run", return_value=Completed()), \
             patch.object(sys, "argv", argv):
            return tender_search.main()

    def test_auth_failure_never_reuses_the_same_day_candidates(self):
        """同一天早先跑成功过：失败后若复用旧目录，会把昨天的情报当今天的再报一遍。"""
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "run"
            stale = out_dir / ".sources" / "zlbx"
            stale.mkdir(parents=True)
            (stale / "candidate_index.jsonl").write_text(
                json.dumps({"candidate_id": "C1", "title": "旧候选"}) + "\n", encoding="utf-8")

            self.assertEqual(self._run_with_failed_source(out_dir), AUTH_ERROR_EXIT_CODE)
            summary = json.loads((out_dir / "search_summary.json").read_text(encoding="utf-8"))
            self.assertTrue(summary["source_auth_failed"])
            self.assertEqual(summary["candidate_count"], 0)
            # 旧候选没有被合并成本次结果
            self.assertFalse((out_dir / "candidate_index.jsonl").exists())

    def test_non_auth_failure_still_writes_a_summary_but_keeps_exit_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "run"
            self.assertEqual(self._run_with_failed_source(out_dir, code=2), 2)
            summary = json.loads((out_dir / "search_summary.json").read_text(encoding="utf-8"))
            self.assertFalse(summary["source_auth_failed"])
            self.assertTrue(summary["failure_reason"])

    def test_prepare_refuses_a_failed_search_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"
            run.mkdir()
            (run / "candidate_index.jsonl").write_text("", encoding="utf-8")
            (run / "search_summary.json").write_text(json.dumps({
                "source": "zlbx", "exit_code": 3, "source_auth_failed": True,
                "candidate_count": 0,
            }), encoding="utf-8")
            seen = Path(tmp) / "seen.json"
            save_ledger(seen, {"records": []})
            with self.assertRaises(PipelineError) as caught:
                prepare(run, seen, 10, "daily-push")
            self.assertIn("凭证", str(caught.exception))

    def test_dry_run_needs_no_credentials(self):
        # 只摘掉凭证，其余环境照抄：清空 env 在 Windows 上会连 SYSTEMROOT 一起丢。
        env = {k: v for k, v in os.environ.items() if k != "ZLBX_API_KEY"}
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "tender_search.py"), "--dry-run"],
            cwd=ROOT, capture_output=True, text=True,
            # 子进程按 UTF-8 输出中文；不写死就走 locale（简中 Windows 是 cp936），
            # 解码线程会炸掉，stdout 变成 None。
            encoding="utf-8", errors="replace", env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("zlbx", result.stdout)


if __name__ == "__main__":
    unittest.main()
