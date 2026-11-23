"""Offline regression coverage for Workbench collector-total identity mapping."""
import unittest
from tools.private_live_ko_tw_missing_crosswalk import key


class CollectorTotalAliasTests(unittest.TestCase):
    def setUp(self):
        self.base = dict(language="ko", set_id="sv1", native_card_name="테스트",
                         printed_collector_number="001")

    def test_workbench_total_is_accepted_without_denominator(self):
        row = dict(self.base, printed_collector_total="100")
        self.assertEqual(key(row), ("ko", "sv1", "테스트", "001", "100"))

    def test_matching_both_fields_is_accepted(self):
        row = dict(self.base, printed_denominator="100", printed_collector_total="100")
        self.assertEqual(key(row), ("ko", "sv1", "테스트", "001", "100"))

    def test_conflicting_totals_are_quarantined(self):
        row = dict(self.base, printed_denominator="101", printed_collector_total="100")
        self.assertIsNone(key(row))

    def test_absent_total_remains_unmatched(self):
        self.assertIsNone(key(self.base))

    def test_nonstring_alias_remains_unmatched(self):
        self.assertIsNone(key(dict(self.base, printed_collector_total=100)))


if __name__ == "__main__":
    unittest.main()
