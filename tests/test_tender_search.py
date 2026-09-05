import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from tender_search import AUTH_ERROR_EXIT_CODE, build_command  # noqa: E402


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

    def test_dry_run_needs_no_credentials(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "tender_search.py"), "--dry-run"],
            cwd=ROOT, capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("zlbx", result.stdout)


if __name__ == "__main__":
    unittest.main()
