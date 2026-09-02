import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from tender_search import ADAPTERS, DEFAULT_SOURCES, parse_sources  # noqa: E402


# 2026-09-02 起默认来源不再等于全部已注册适配器：doubao 被 jrbx 取代，
# 但适配器保留注册，`--sources doubao,ccgp,plap` 可一键回滚。
DEREGISTERED_FROM_DEFAULTS = {"doubao"}


class TenderSearchDefaultsTests(unittest.TestCase):
    def test_default_sources_are_all_registered_adapters(self):
        for source in parse_sources(DEFAULT_SOURCES):
            self.assertIn(source, ADAPTERS)

    def test_defaults_cover_every_adapter_except_the_deregistered_ones(self):
        self.assertEqual(
            set(parse_sources(DEFAULT_SOURCES)),
            set(ADAPTERS) - DEREGISTERED_FROM_DEFAULTS,
        )

    def test_deregistered_adapters_remain_runnable_for_rollback(self):
        for source in DEREGISTERED_FROM_DEFAULTS:
            self.assertIn(source, ADAPTERS)
            self.assertEqual(parse_sources(source), [source])

    def test_unknown_source_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_sources("jrbx,not-a-source")


if __name__ == "__main__":
    unittest.main()
