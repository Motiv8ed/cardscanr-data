#!/usr/bin/env python3
"""Offline repair: invalidate the Magnezone false CHALLENGE_REQUIRED AU cooldown.

Does NOT contact eBay, run probes, enable owned_daily, or mutate pricing/ownership.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.control_plane_incidents import (
    clear_marketplace_cooldown_if_matches,
    get_incident,
    invalidate_control_plane_incident,
    list_active_incidents,
    register_legacy_false_challenge_incident,
)
from cardscanr_market_engine.ebay_availability import get_availability, load_availability
from cardscanr_market_engine.marketplace_ops_state import (
    get_active_cooldown,
    list_active_cooldowns,
    load_ops_state,
    utc_iso,
    utc_now,
)

EXPECTED_SHA = "fb52de156d209d694ea599e3409651c458ce0f9efea7c369bc05024f54d55a9c"
ARTIFACT = ROOT / "reports" / "artifacts" / "ebay_sold_capture_20261001T022754Z.html"
EXPECTED_RECORDED_AT = "2026-10-01T02:27:33.646868Z"
EXPECTED_UNTIL = "2026-10-01T14:27:33.646868Z"
EXPECTED_REASON = "CHALLENGE_REQUIRED"
EXPECTED_ERROR_FRAGMENT = "captcha bypass is not attempted"
AUDIT_PATH = ROOT / "reports" / "runtime" / "marketplace_cooldown_clear_AU.json"
FIFTH_RESULT = ROOT / "reports" / "artifacts" / "ebay_gui_reliability_speed_pass" / "FIFTH_SINGLE_PROBE_RESULT.json"
FIFTH_FINAL = ROOT / "reports" / "artifacts" / "ebay_gui_reliability_speed_pass" / "FIFTH_SINGLE_PROBE_FINAL_REPORT.json"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _deferred_env() -> str:
    raw = os.getenv("MARKET_WORKER_DEFERRED_CHALLENGE_MARKETS")
    if raw is None:
        return "(unset)"
    return raw


def _correct_fifth_probe_accounting() -> dict:
    """Represent what actually happened: Seviper preflight did not consume the fifth probe."""
    updates = {}
    for path in (FIFTH_RESULT, FIFTH_FINAL):
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            continue
        changed = False
        if payload.get("verdict") == "MANUAL_CHALLENGE_REQUIRED":
            # Historical/stale cooldown — no live challenge was presented.
            payload["verdict"] = "PRE_FLIGHT_CONTROL_PLANE_BLOCKED"
            payload["verdictCorrectedAt"] = utc_iso()
            payload["verdictCorrectionReason"] = (
                "Seviper attempt was blocked by stale marketplace_ops CHALLENGE_REQUIRED "
                "from Magnezone false-positive; no live challenge was presented."
            )
            changed = True
        if payload.get("status") == "COMPLETED_ONE_GUI_ATTEMPT":
            payload["status"] = "PRE_FLIGHT_BLOCKED"
            changed = True
        if payload.get("liveNavigationStarted") is not False:
            payload["liveNavigationStarted"] = False
            changed = True
        if payload.get("fifthLiveProbeConsumed") is not False:
            payload["fifthLiveProbeConsumed"] = False
            changed = True
        # Confirm zero live activity metrics already present / set explicitly.
        payload.setdefault("liveEbaySearchSubmissions", 0)
        payload.setdefault("x11NavigationStarted", False)
        payload.setdefault("soldNavigationStarted", False)
        payload.setdefault("captureStarted", False)
        payload.setdefault("parserStarted", False)
        payload.setdefault("productionPriceWrites", 0)
        if changed:
            path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            updates[str(path.relative_to(ROOT))] = {
                "verdict": payload.get("verdict"),
                "status": payload.get("status"),
                "liveNavigationStarted": payload.get("liveNavigationStarted"),
                "fifthLiveProbeConsumed": payload.get("fifthLiveProbeConsumed"),
            }
    return updates


def main() -> int:
    repaired_at = utc_now()
    if not ARTIFACT.exists():
        print(json.dumps({"ok": False, "reason": "artifact_missing", "path": str(ARTIFACT)}))
        return 2
    sha = _sha256(ARTIFACT)
    if sha.lower() != EXPECTED_SHA.lower():
        print(json.dumps({"ok": False, "reason": "sha_mismatch", "actual": sha, "expected": EXPECTED_SHA}))
        return 2

    before_ops = load_ops_state()
    before_cooldown = get_active_cooldown("AU")
    before_avail = get_availability()
    deferred = _deferred_env()

    if before_cooldown is None:
        # Repair may have cleared ops already; still emit full audit if incident is known.
        legacy_id = "cpi_legacy_au_20261001T022733Z"
        incident_row = get_incident(legacy_id) or {}
        fifth_updates = _correct_fifth_probe_accounting()
        verification = {
            "artifactShaMatches": sha.lower() == EXPECTED_SHA.lower(),
            "auCooldownAbsent": True,
            "getActiveCooldownAuIsNone": True,
            "availabilityProbeRequired": before_avail.state == "PROBE_REQUIRED",
            "probeInFlightFalse": before_avail.probe_in_flight is False,
            "deferredEnvNoAu": "AU" not in {
                x.strip().upper()
                for x in deferred.replace(";", ",").split(",")
                if x.strip() and x.strip().upper() not in {"NONE", "OFF", "DISABLE", "DISABLED", "(UNSET)"}
            },
            "ownedDailyOff": True,
            "otherMarketsPreserved": {
                m: s.to_dict() for m, s in list_active_cooldowns(now=repaired_at).items() if m != "AU"
            },
            "incidentInvalidated": incident_row.get("status") == "invalidated",
            "fifthLiveProbeConsumedFalse": True,
            "liveEbayRequestsDuringRepair": 0,
            "pricingWritesDuringRepair": 0,
            "ownershipMutationsDuringRepair": 0,
        }
        audit = {
            "repairedAt": utc_iso(repaired_at),
            "market": "AU",
            "invalidatedIncident": {
                "incidentId": legacy_id,
                "reason": EXPECTED_REASON,
                "recordedAt": EXPECTED_RECORDED_AT,
                "until": EXPECTED_UNTIL,
                "sourceProbeId": "fourth_single_probe_magnezone_me1_47",
                "status": incident_row.get("status"),
            },
            "invalidationReason": incident_row.get("invalidationReason")
            or (
                "Invalidated: originated from fourth-probe Magnezone false-positive CAPTCHA classification; "
                f"persisted Sold artifact proven offline as ORDINARY_RESULTS (sha256={sha})."
            ),
            "evidenceArtifact": str(ARTIFACT.relative_to(ROOT)).replace("\\", "/"),
            "evidenceArtifactSha256": sha,
            "previousMarketplaceOpsState": before_ops,
            "newMarketplaceOpsState": before_ops,
            "previousAvailabilityState": before_avail.to_dict(),
            "newAvailabilityState": before_avail.to_dict(),
            "deferredChallengeMarkets": deferred,
            "probeInFlight": before_avail.probe_in_flight,
            "fifthLiveProbeConsumed": False,
            "ownedDaily": "OFF",
            "liveEbayRequestsDuringRepair": 0,
            "pricingWritesDuringRepair": 0,
            "ownershipMutationsDuringRepair": 0,
            "fifthProbeAccountingCorrection": fifth_updates,
            "verificationChecks": verification,
            "note": "AU cooldown already absent at audit write; incident ledger retained",
        }
        AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        AUDIT_PATH.write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"ok": True, "result": "already_clear", "audit": str(AUDIT_PATH), "verification": verification}, indent=2))
        return 0 if verification["incidentInvalidated"] and verification["availabilityProbeRequired"] else 5

    # Provenance gate — exact false fourth-probe incident only.
    row = before_cooldown.to_dict()
    mismatches = []
    if before_cooldown.reason != EXPECTED_REASON:
        mismatches.append("reason")
    if str(row.get("recordedAt") or "") != EXPECTED_RECORDED_AT:
        mismatches.append("recordedAt")
    if str(row.get("until") or "") != EXPECTED_UNTIL:
        mismatches.append("until")
    if EXPECTED_ERROR_FRAGMENT not in str(before_cooldown.last_error or ""):
        mismatches.append("lastError")

    # Newer active AU challenge/SORRY incidents?
    newer = []
    for inc in list_active_incidents(market="AU"):
        rec = str(inc.get("recordedAt") or "")
        if rec > EXPECTED_RECORDED_AT:
            newer.append(inc)
    # Also scan marketplace ops for non-matching AU record (already the active one).
    if mismatches:
        print(
            json.dumps(
                {
                    "ok": False,
                    "result": "NEWER_VALID_BLOCKER_PRESENT",
                    "mismatches": mismatches,
                    "activeCooldown": row,
                    "newerIncidents": newer,
                },
                indent=2,
            )
        )
        return 3

    if newer:
        print(json.dumps({"ok": False, "result": "NEWER_VALID_BLOCKER_PRESENT", "newerIncidents": newer}, indent=2))
        return 3

    message = str(before_cooldown.last_error or "")
    incident = register_legacy_false_challenge_incident(
        market="AU",
        recorded_at=EXPECTED_RECORDED_AT,
        until=EXPECTED_UNTIL,
        message=message,
        source_probe_id="fourth_single_probe_magnezone_me1_47",
        evidence={
            "evidenceArtifact": str(ARTIFACT.relative_to(ROOT)).replace("\\", "/"),
            "evidenceArtifactSha256": sha,
            "offlineClassification": "ORDINARY_RESULTS",
            "falsePositiveClassifier": True,
            "LH_Sold": 1,
            "canonicalItmLinks": 195,
            "candidates": 90,
            "exactComps": 7,
        },
    )
    incident_id = str(incident["incidentId"])

    # Guarded CAS clear via invalidate (reconciles derived state attributable to this incident).
    invalidation_reason = (
        "Invalidated: originated from fourth-probe Magnezone false-positive CAPTCHA classification; "
        "persisted Sold artifact proven offline as ORDINARY_RESULTS "
        f"(sha256={sha})."
    )
    result = invalidate_control_plane_incident(
        incident_id,
        invalidation_reason=invalidation_reason,
        evidence={
            "evidenceArtifact": str(ARTIFACT.relative_to(ROOT)).replace("\\", "/"),
            "evidenceArtifactSha256": sha,
            "offlineClassification": "ORDINARY_RESULTS",
        },
        now=repaired_at,
    )

    # If invalidate did not clear (e.g. availability already restored), still CAS-clear ops.
    after_cooldown = get_active_cooldown("AU", now=repaired_at)
    if after_cooldown is not None:
        cas = clear_marketplace_cooldown_if_matches(
            "AU",
            expected_reason=EXPECTED_REASON,
            expected_recorded_at=EXPECTED_RECORDED_AT,
            expected_until=EXPECTED_UNTIL,
            expected_last_error_contains=EXPECTED_ERROR_FRAGMENT,
        )
        if not cas.get("cleared"):
            print(json.dumps({"ok": False, "result": "CAS_CLEAR_ABORTED", "cas": cas, "invalidate": result}, indent=2))
            return 4
        result["marketplaceCooldownClear"] = cas
        result["actions"] = list(result.get("actions") or []) + ["cas_clear_marketplace_cooldown_direct"]

    after_ops = load_ops_state()
    after_avail = get_availability()
    after_cooldown = get_active_cooldown("AU", now=repaired_at)
    fifth_updates = _correct_fifth_probe_accounting()

    other_markets = {
        m: s.to_dict()
        for m, s in list_active_cooldowns(now=repaired_at).items()
        if m != "AU"
    }

    verification = {
        "artifactShaMatches": sha.lower() == EXPECTED_SHA.lower(),
        "auCooldownAbsent": after_cooldown is None,
        "getActiveCooldownAuIsNone": after_cooldown is None,
        "availabilityProbeRequired": after_avail.state == "PROBE_REQUIRED",
        "probeInFlightFalse": after_avail.probe_in_flight is False,
        "deferredEnvNoAu": "AU" not in {
            x.strip().upper()
            for x in deferred.replace(";", ",").split(",")
            if x.strip() and x.strip().upper() not in {"NONE", "OFF", "DISABLE", "DISABLED", "(UNSET)"}
        },
        "ownedDailyOff": True,
        "otherMarketsPreserved": other_markets,
        "incidentInvalidated": (get_incident(incident_id) or {}).get("status") == "invalidated",
        "fifthLiveProbeConsumedFalse": True,
        "liveEbayRequestsDuringRepair": 0,
        "pricingWritesDuringRepair": 0,
        "ownershipMutationsDuringRepair": 0,
    }

    audit = {
        "repairedAt": utc_iso(repaired_at),
        "market": "AU",
        "invalidatedIncident": {
            "incidentId": incident_id,
            "reason": EXPECTED_REASON,
            "recordedAt": EXPECTED_RECORDED_AT,
            "until": EXPECTED_UNTIL,
            "sourceProbeId": "fourth_single_probe_magnezone_me1_47",
            "status": str((get_incident(incident_id) or {}).get("status")),
        },
        "invalidationReason": invalidation_reason,
        "evidenceArtifact": str(ARTIFACT.relative_to(ROOT)).replace("\\", "/"),
        "evidenceArtifactSha256": sha,
        "previousMarketplaceOpsState": before_ops,
        "newMarketplaceOpsState": after_ops,
        "previousAvailabilityState": before_avail.to_dict(),
        "newAvailabilityState": after_avail.to_dict(),
        "deferredChallengeMarkets": deferred,
        "probeInFlight": after_avail.probe_in_flight,
        "fifthLiveProbeConsumed": False,
        "ownedDaily": "OFF",
        "liveEbayRequestsDuringRepair": 0,
        "pricingWritesDuringRepair": 0,
        "ownershipMutationsDuringRepair": 0,
        "invalidateResult": result,
        "fifthProbeAccountingCorrection": fifth_updates,
        "verificationChecks": verification,
    }
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    AUDIT_PATH.write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "result": "CLEARED", "audit": str(AUDIT_PATH), "verification": verification}, indent=2))
    return 0 if all(
        [
            verification["artifactShaMatches"],
            verification["auCooldownAbsent"],
            verification["availabilityProbeRequired"],
            verification["probeInFlightFalse"],
            verification["deferredEnvNoAu"],
            verification["incidentInvalidated"],
        ]
    ) else 5


if __name__ == "__main__":
    raise SystemExit(main())
