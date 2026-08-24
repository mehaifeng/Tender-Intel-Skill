import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from tender_search import ADAPTERS, DEFAULT_SOURCES, parse_sources  # noqa: E402


class TenderSearchDefaultsTests(unittest.TestCase):
    def test_every_registered_adapter_is_enabled_by_default(self):
        self.assertEqual(parse_sources(DEFAULT_SOURCES), list(ADAPTERS))


if __name__ == "__main__":
    unittest.main()
