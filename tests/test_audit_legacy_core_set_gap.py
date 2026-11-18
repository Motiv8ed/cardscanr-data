"""Read-only legacy core gap algorithm fixtures; no network."""
import collections
import unittest
from tools.audit_legacy_core_set_gap import missing_set_analysis


class LegacyCoreSetTests(unittest.TestCase):
    def test_archived_core_can_resolve_only_missing_references(self):
        refs={
            "ko":collections.Counter({"ko-set1":120,"ko-set2":80,"missing":1}),
            "zh-tw":collections.Counter({"tw-set1":75}),
            "ja":collections.Counter({"ja-set":40}),
        }
        result=missing_set_analysis(
            refs,current_sets={"ja-set","tw-set1"},
            legacy_core_sets={"ko-set1","ko-set2"})
        self.assertEqual(result["missing_rows_before_legacy_core"],201)
        self.assertEqual(result["missing_rows_after_legacy_core"],1)
        self.assertEqual(result["sets_recoverable_from_legacy_core"],2)
        self.assertEqual(result["sets_requiring_another_source"],["missing"])
        self.assertEqual(result["by_pack_after"]["ja"]["rows"],0)

    def test_old_core_does_not_overwrite_current_set_identity(self):
        # A conflicting legacy ID is still considered present in current
        # metadata; actual conflict comparison is a separate explicit gate.
        result=missing_set_analysis(
            {"en":collections.Counter({"shared":3})},
            current_sets={"shared"},legacy_core_sets={"shared"})
        self.assertEqual(result["missing_rows_before_legacy_core"],0)
        self.assertEqual(result["sets_recoverable_from_legacy_core"],0)
        self.assertEqual(result["missing_rows_after_legacy_core"],0)

    def test_unrecoverable_rows_are_reported_not_published(self):
        result=missing_set_analysis(
            {"id":collections.Counter({"unknown":23})},
            current_sets=set(),legacy_core_sets=set())
        self.assertEqual(result["sets_requiring_another_source"],["unknown"])
        self.assertEqual(result["by_pack_after"]["id"]["rows"],23)


if __name__=="__main__":
    unittest.main()
