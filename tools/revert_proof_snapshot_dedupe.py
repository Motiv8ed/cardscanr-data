"""Revert-proof harness: force always-insert snapshot policy and run gate tests."""
from __future__ import annotations

import sys
import unittest
from unittest import mock

import cardscanr_market_engine.bulk.reference_refresh as reference_refresh
from tests import test_bulk_reference_snapshot_dedupe as dedupe_tests

REVERT_PROOF_METHODS = (
    "test_utc_midnight_boundary_skip_then_checkpoint",
    "test_skip_path_still_upserts_cache_reference_timestamp",
    "test_hourly_verified_rechecks_keep_one_daily_checkpoint",
    "test_pending_verification_rechecks_do_not_flood_snapshots",
    "test_offset_timezone_same_utc_day_skips",
    "test_verified_au_source_survives_projection_limited_cache_load",
)


def _suite() -> unittest.TestSuite:
    loader = unittest.TestLoader()
    suites: list[unittest.TestSuite] = []
    for name in REVERT_PROOF_METHODS:
        if name.startswith("test_verified_au"):
            suites.append(loader.loadTestsFromName(name, dedupe_tests.VerifiedSourcePreservationTests))
        else:
            suites.append(loader.loadTestsFromName(name, dedupe_tests.ReferenceSnapshotDedupeTests))
    return unittest.TestSuite(suites)


def main() -> int:
    always_insert = mock.patch.object(
        reference_refresh,
        "should_record_reference_snapshot",
        return_value=True,
    )
    suite = _suite()
    with always_insert:
        result = unittest.TextTestRunner(verbosity=0).run(suite)
    if result.wasSuccessful():
        print("REVERT_PROOF_FAIL: tests passed with dedupe disabled (expected failures)", file=sys.stderr)
        return 1
    print(
        f"REVERT_PROOF_OK: {len(result.failures) + len(result.errors)} test(s) failed with dedupe disabled",
    )
    for test, trace in result.failures + result.errors:
        first_line = trace.strip().splitlines()[-1]
        print(f"  {test.id()}: {first_line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())