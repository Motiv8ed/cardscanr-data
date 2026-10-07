#!/usr/bin/env python3
"""OFFLINE auditable reconciliation of TEST_CONTAMINATION control-plane incidents.

Does NOT contact eBay. Does NOT delete incident records.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.control_plane_incidents import (  # noqa: E402
    invalidate_control_plane_incident,
    list_active_incidents,
)
from cardscanr_market_engine.ebay_browser_work_gate import (  # noqa: E402
    evaluate_ebay_browser_work_gate,
)

AU_TEST_CONTAMINATION_IDS = [
    "cpi_00d49391f11a45b9",
    "cpi_5710b9a3ee164552",
    "cpi_880b92b80d594031",
    "cpi_cbab6c35809e40f8",
]

# Matching tests/test_marketplace_ops_state.py frozen now + exact message.
CA_TEST_CONTAMINATION_RECORDED_AT = "2026-08-16T12:00:00Z"
CA_TEST_CONTAMINATION_MESSAGE = "verification challenge"

REASON_AU = (
    "TEST_CONTAMINATION: written by tests/test_job_runner_phase_result_paths.py "
    "(JobRunnerPhasePathTests with now_func=2026-10-01T12:00:00Z) via "
    "maybe_record_failure_cooldown reflecting gate denial "
    "'EBAY_CHALLENGE_REQUIRED: eBay browser work deferred' into the production "
    "ledger because CONTROL_PLANE_INCIDENTS_PATH was unset while ops/avail were "
    "isolated to tempfile. Empty evidence; identical frozen timestamp across "
    "four rows; no live eBay observation. Ledger updatedAtUtc after fifth probe."
)

REASON_CA = (
    "TEST_CONTAMINATION: written by tests/test_marketplace_ops_state.py "
    "(test_maybe_record_ignores_no_comps_and_existing_cooldown) with "
    "now=2026-08-16T12:00:00Z message='verification challenge' isolating only "
    "ops path; register_incident defaulted to production CONTROL_PLANE_INCIDENTS "
    "path. Empty evidence; frozen fixture timestamp."
)


def main() -> int:
    now = datetime.now(timezone.utc)
    out_dir = ROOT / "reports" / "artifacts" / "control_plane_reconciliation"
    out_dir.mkdir(parents=True, exist_ok=True)

    before_active_au = list_active_incidents(market="AU")
    before_active_all = list_active_incidents()
    actions: list[dict] = []

    for iid in AU_TEST_CONTAMINATION_IDS:
        result = invalidate_control_plane_incident(
            iid,
            invalidation_reason=REASON_AU,
            evidence={
                "provenanceClassification": "TEST_CONTAMINATION",
                "reconciliationSource": "CARDSCANR-EBAY-CONTROL-PLANE-RECONCILIATION",
                "sourceTest": "tests/test_job_runner_phase_result_paths.py",
                "frozenTimestamp": "2026-10-01T12:00:00Z",
                "messageFingerprint": "EBAY_CHALLENGE_REQUIRED: eBay browser work deferred",
                "isolationDefect": "CONTROL_PLANE_INCIDENTS_PATH_unset_while_ops_avail_isolated",
            },
            now=now,
        )
        actions.append({"incidentId": iid, "market": "AU", **result})

    for row in before_active_all:
        if str(row.get("market") or "").upper() != "CA":
            continue
        if str(row.get("recordedAt") or "") != CA_TEST_CONTAMINATION_RECORDED_AT:
            continue
        if str(row.get("message") or "") != CA_TEST_CONTAMINATION_MESSAGE:
            continue
        if str(row.get("status") or "") != "active":
            continue
        iid = str(row.get("incidentId") or "")
        result = invalidate_control_plane_incident(
            iid,
            invalidation_reason=REASON_CA,
            evidence={
                "provenanceClassification": "TEST_CONTAMINATION",
                "reconciliationSource": "CARDSCANR-EBAY-CONTROL-PLANE-RECONCILIATION",
                "sourceTest": "tests/test_marketplace_ops_state.py",
                "frozenTimestamp": CA_TEST_CONTAMINATION_RECORDED_AT,
                "messageFingerprint": CA_TEST_CONTAMINATION_MESSAGE,
                "isolationDefect": "ops_path_isolated_incidents_defaulted_to_production",
            },
            now=now,
        )
        actions.append({"incidentId": iid, "market": "CA", **result})

    after_active_au = list_active_incidents(market="AU")
    after_active_all = list_active_incidents()
    gate = evaluate_ebay_browser_work_gate(market="AU", now=now, for_probe=False)

    payload = {
        "taskId": "CARDSCANR-EBAY-CONTROL-PLANE-RECONCILIATION",
        "reconciledAtUtc": now.isoformat().replace("+00:00", "Z"),
        "beforeActiveAuCount": len(before_active_au),
        "afterActiveAuCount": len(after_active_au),
        "beforeActiveAllCount": len(before_active_all),
        "afterActiveAllCount": len(after_active_all),
        "recordsDeleted": 0,
        "actions": actions,
        "afterActiveAuIds": [r.get("incidentId") for r in after_active_au],
        "gateAfter": gate.to_dict(),
    }
    (out_dir / "RECONCILIATION_ACTIONS.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(payload, indent=2))
    return 0 if len(after_active_au) == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
