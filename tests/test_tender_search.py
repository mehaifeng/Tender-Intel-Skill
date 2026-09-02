import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from tender_search import ADAPTERS, DEFAULT_SOURCES, parse_sources  # noqa: E402


class TenderSearchDefaultsTests(unittest.TestCase):
    def test_default_sources_are_all_registered_adapters(self):
        self.assertEqual(set(parse_sources(DEFAULT_SOURCES)), set(ADAPTERS))

    def test_every_registered_adapter_is_runnable_alone(self):
        for source in ADAPTERS:
            self.assertEqual(parse_sources(source), [source])

    def test_unknown_source_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_sources("jrbx,not-a-source")


if __name__ == "__main__":
    unittest.main()
