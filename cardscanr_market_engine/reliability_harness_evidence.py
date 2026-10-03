"""Build reliability-card evidence from authoritative job-result fields only.

Never hydrates global post_sold_capture_last as primary evidence.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .capture_evidence_correlation import correlate_capture_evidence, not_run_capture_block
from .live_navigation_attempt import lookup_search_submission_event
from .pipeline_phase_diagnostics import PARSE_COMPLETE, extract_pipeline_phases
from .providers.post_sold_capture import POST_SOLD_CAPTURE_READY
from .reliability_harness_classification import (
    classify_reliability_card_verdict,
    is_structured_challenge,
)


def _diag(result: dict[str, Any]) -> dict[str, Any]:
    pd = result.get("providerDiagnostics") or {}
    if isinstance(pd, dict) and isinstance(pd.get("diagnostics"), dict):
        return pd.get("diagnostics") or {}
    return pd if isinstance(pd, dict) else {}


def build_parse_evidence(result: dict[str, Any], diag: dict[str, Any] | None = None) -> dict[str, Any]:
    diag = diag if isinstance(diag, dict) else _diag(result)
    phases = extract_pipeline_phases({**diag, **{k: result.get(k) for k in ("parsePhase", "postSoldCapturePhase", "x11SoldStateVerified")}})
    parse_phase = phases.get("parsePhase") or result.get("parsePhase") or diag.get("parsePhase")
    # When production completed a write/update with accepted comps, parse ran even if
    # aggregate previously dropped PARSE_COMPLETE — recover from pricing stats.
    pd = result.get("providerDiagnostics") if isinstance(result.get("providerDiagnostics"), dict) else {}
    stats = diag.get("pricingStats") or pd.get("pricingStats") or result.get("pricingStats") or {}
    if not isinstance(stats, dict):
        stats = {}
    fetched = (
        stats.get("fetched")
        or stats.get("fetchedCount")
        or diag.get("resultCount")
        or diag.get("dedupedResultCount")
        or result.get("fetchedCount")
    )
    accepted = (
        stats.get("accepted")
        or stats.get("acceptedCount")
        or stats.get("includedCount")
        or result.get("includedCount")
        or diag.get("cleanExactCompCount")
    )
    rejected = (
        stats.get("rejected")
        or stats.get("rejectedCount")
        or result.get("rejectedCount")
    )
    if parse_phase is None and (
        result.get("status") == "completed"
        or result.get("ownedDailyOutcome") in {"UPDATED_FROM_EBAY", "UNCHANGED_FROM_EBAY", "CHECKED_NO_NEW_EXACT_EVIDENCE"}
        or accepted is not None
    ):
        parse_phase = PARSE_COMPLETE
    if parse_phase is None and not result.get("x11SoldStateVerified") and not phases.get("postSoldCapturePhase"):
        return {
            "phase": "NOT_RUN",
            "fetched": None,
            "accepted": None,
            "rejected": None,
            "rejectionReasons": None,
            "acceptedPrices": None,
            "median": None,
            "confidence": None,
            "basis": None,
            "currency": result.get("currency") or "AUD",
            "sampleCount": None,
        }
    return {
        "phase": parse_phase,
        "fetched": fetched,
        "accepted": accepted,
        "rejected": rejected,
        "rejectionReasons": stats.get("rejectionReasons") or diag.get("rejectionReasons"),
        "acceptedPrices": stats.get("acceptedPrices") or stats.get("accepted_prices") or result.get("acceptedPrices"),
        "median": stats.get("median") or result.get("recommendedPrice"),
        "confidence": stats.get("confidence") or result.get("confidence"),
        "basis": stats.get("priceBasis") or result.get("priceBasis") or diag.get("final_price_basis"),
        "currency": result.get("currency") or diag.get("currency") or "AUD",
        "sampleCount": stats.get("sampleCount") or result.get("sampleCount") or accepted,
    }


def build_write_evidence(
    result: dict[str, Any],
    *,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> dict[str, Any]:
    before = before if isinstance(before, dict) else {}
    after = after if isinstance(after, dict) else {}
    prior = before.get("price")
    if prior is None:
        prior = before.get("current_market_price")
    resulting = after.get("price")
    if resulting is None:
        resulting = after.get("current_market_price")
    if resulting is None:
        resulting = result.get("recommendedPrice")
    return {
        "outcome": result.get("ownedDailyOutcome") or result.get("outcomeClass"),
        "snapshotId": result.get("snapshotId") or result.get("priceSnapshotId") or after.get("latest_snapshot_id"),
        "priorPrice": prior,
        "resultingPrice": resulting,
        "provider": after.get("provider") or result.get("provider"),
        "displayPriceSource": (
            after.get("displayPriceSource")
            or after.get("display_price_source")
            or result.get("displayPriceSource")
        ),
        "refreshStatus": after.get("refreshStatus") or after.get("refresh_status") or result.get("refreshStatus"),
        "verifiedLocalAfter": after.get("verifiedLocal"),
        "sourceClassAfter": after.get("sourceClass"),
        # Source-aware: never label reference freshness as verified eBay success.
        **_freshness_fields(result, after),
    }


def _freshness_fields(result: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    verified_local = bool(after.get("verifiedLocal"))
    source = str(after.get("sourceClass") or "").upper()
    outcome = str(result.get("ownedDailyOutcome") or "").upper()
    display = str(
        result.get("displayPriceSource") or after.get("displayPriceSource") or after.get("display_price_source") or ""
    ).lower()
    is_verified_success = verified_local or source in {
        "VERIFIED_LOCAL",
        "EBAY_VERIFIED_LOCAL",
    } or outcome in {"UPDATED_FROM_EBAY", "UNCHANGED_FROM_EBAY"} or display.startswith("ebay")
    stamp = (
        after.get("freshness")
        or after.get("last_updated_at")
        or result.get("verifiedSuccessFreshness")
        or result.get("lastUpdatedAt")
    )
    ref_stamp = (
        after.get("referenceUpdatedAt")
        or after.get("freshness")
        or after.get("last_updated_at")
        or result.get("referenceUpdatedAt")
    )
    return {
        "verifiedSuccessFreshness": stamp if is_verified_success else None,
        "referenceUpdatedAt": None if is_verified_success else ref_stamp,
    }


def build_capture_evidence(
    result: dict[str, Any],
    *,
    job_id: str | None = None,
    attempt_id: str | None = None,
    price_key_id: str | None = None,
    fingerprint: str | None = None,
) -> dict[str, Any]:
    diag = _diag(result)
    phases = extract_pipeline_phases({**diag, **{k: result.get(k) for k in ("postSoldCapturePhase", "parsePhase", "x11SoldStateVerified")}})
    # Prefer current-job compact capture metadata on the result/diagnostics.
    current = (
        result.get("currentJobCapture")
        or diag.get("currentJobCapture")
        or phases.get("persistedCaptureArtifact")
        or diag.get("persistedCaptureArtifact")
    )
    if isinstance(current, dict) and (current.get("htmlPath") or current.get("sha256") or current.get("htmlSha256")):
        # Normalize into correlate_capture_evidence shape.
        result_for_corr = {
            **result,
            "postSoldCapturePhase": result.get("postSoldCapturePhase") or phases.get("postSoldCapturePhase") or POST_SOLD_CAPTURE_READY,
            "x11SoldStateVerified": True if (result.get("x11SoldStateVerified") or phases.get("x11SoldStateVerified")) else result.get("x11SoldStateVerified"),
            "postSoldCapture": {
                "targetId": current.get("targetId") or (result.get("postSoldCapture") or {}).get("targetId") if isinstance(result.get("postSoldCapture"), dict) else current.get("targetId"),
                "diagnostics": {
                    "html_path": current.get("htmlPath") or current.get("html_path"),
                    "html_sha256": current.get("sha256") or current.get("htmlSha256") or current.get("html_sha256"),
                    "job_id": current.get("jobId") or current.get("job_id") or job_id,
                    "attempt_id": current.get("attemptId") or current.get("attempt_id") or attempt_id,
                    "price_key_id": current.get("priceKeyId") or current.get("price_key_id") or price_key_id,
                    "fingerprint": current.get("fingerprint") or fingerprint,
                    "capture_origin": current.get("captureOrigin") or current.get("capture_origin") or "LIVE_BROWSER_CAPTURE",
                },
            },
            "persistedCaptureArtifact": current,
        }
        corr = correlate_capture_evidence(
            result=result_for_corr,
            diag=diag,
            expected_job_id=job_id,
            expected_attempt_id=attempt_id,
            expected_price_key_id=price_key_id,
            expected_fingerprint=fingerprint,
            allow_global_last_capture=False,
        )
    else:
        corr = correlate_capture_evidence(
            result=result,
            diag=diag,
            expected_job_id=job_id,
            expected_attempt_id=attempt_id,
            expected_price_key_id=price_key_id,
            expected_fingerprint=fingerprint,
            allow_global_last_capture=False,
        )

    if corr.status == "NOT_RUN":
        block = not_run_capture_block()
        block["details"] = corr.details
        block["jobId"] = job_id
        block["attemptId"] = attempt_id
        block["priceKeyId"] = price_key_id
        block["fingerprint"] = fingerprint
        return block

    html_path = corr.artifact_path
    sha = corr.sha256
    sha_ok = False
    utf8_ok = False
    bytes_len = None
    chars_len = None
    if html_path and Path(str(html_path)).is_file() and "post_sold_capture_last" not in str(html_path).replace("\\", "/"):
        raw = Path(str(html_path)).read_bytes()
        bytes_len = len(raw)
        try:
            text = raw.decode("utf-8")
            utf8_ok = True
            chars_len = len(text)
            recomputed = hashlib.sha256(raw).hexdigest()
            sha_ok = bool(sha) and recomputed == str(sha)
            if not sha:
                sha = recomputed
        except UnicodeDecodeError:
            utf8_ok = False

    status = "SUCCESS" if corr.status == "ATTACHED" and (phases.get("postSoldCapturePhase") == POST_SOLD_CAPTURE_READY or corr.artifact_path) else corr.status
    # Propagate current-job failure class/detail (never leave null when capture failed).
    post = result.get("postSoldCapture") if isinstance(result.get("postSoldCapture"), dict) else {}
    post_diag = post.get("diagnostics") if isinstance(post.get("diagnostics"), dict) else {}
    failure_class = (
        result.get("failureClass")
        or diag.get("failureClass")
        or post.get("failureClass")
        or post_diag.get("failureClass")
    )
    failure_detail = (
        result.get("failureDetail")
        or diag.get("failureDetail")
        or post.get("failureDetail")
        or post_diag.get("failureDetail")
        or post_diag.get("error")
    )
    phase_val = corr.phase or phases.get("postSoldCapturePhase") or result.get("postSoldCapturePhase")
    if status != "SUCCESS" and not failure_class:
        cap_proc = diag.get("captureProcess") if isinstance(diag.get("captureProcess"), dict) else {}
        nested_cap = diag.get("capture") if isinstance(diag.get("capture"), dict) else {}
        failure_class = (
            cap_proc.get("status")
            or nested_cap.get("failureClass")
            or result.get("captureFailureClass")
        )
        if not failure_detail:
            failure_detail = nested_cap.get("failureDetail") or post_diag.get("error")
    return {
        "status": status,
        "phase": phase_val,
        "failureClass": failure_class if status != "SUCCESS" else None,
        "failureDetail": failure_detail if status != "SUCCESS" else None,
        "targetId": corr.target_id,
        "artifactPath": html_path if corr.correlated else None,
        "sha256": sha if corr.correlated else None,
        "shaVerified": sha_ok,
        "utf8Verified": utf8_ok,
        "htmlBytes": bytes_len,
        "htmlChars": chars_len,
        "captureOrigin": corr.capture_origin,
        "correlated": corr.correlated,
        "rejectionReasons": corr.rejection_reasons,
        "globalLastCaptureAuthoritative": False,
        "jobId": job_id,
        "attemptId": attempt_id,
        "priceKeyId": price_key_id,
        "fingerprint": fingerprint,
    }


def classify_card_from_job_result(
    result: dict[str, Any],
    *,
    attempt_id: str,
    price_key_id: str | None = None,
    job_id: str | None = None,
    fingerprint: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    preflight_failed: bool = False,
) -> dict[str, Any]:
    """Authoritative harness classification for one card/job."""
    diag = _diag(result)
    phases = extract_pipeline_phases({**diag, **{k: result.get(k) for k in ("postSoldCapturePhase", "parsePhase", "x11SoldStateVerified")}})
    # Merge recovered phase fields onto a working result view.
    view = dict(result)
    if phases.get("postSoldCapturePhase") and not view.get("postSoldCapturePhase"):
        view["postSoldCapturePhase"] = phases["postSoldCapturePhase"]
    if phases.get("parsePhase") and not view.get("parsePhase"):
        view["parsePhase"] = phases["parsePhase"]
    if phases.get("x11SoldStateVerified"):
        view["x11SoldStateVerified"] = True

    event_lookup = lookup_search_submission_event(
        attempt_id,
        expected_price_key_id=price_key_id,
    )
    search_started = bool(event_lookup.get("found") and event_lookup.get("valid"))
    # Consistency: completed write without event is an error, not invented consumption.
    consistency_error = None
    if (
        not search_started
        and str(view.get("status") or "") == "completed"
        and str(view.get("ownedDailyOutcome") or "") in {"UPDATED_FROM_EBAY", "UNCHANGED_FROM_EBAY"}
        and bool(view.get("x11SoldStateVerified") or phases.get("x11SoldStateVerified"))
    ):
        consistency_error = "COMPLETED_WRITE_WITHOUT_SEARCH_SUBMISSION_EVENT"

    challenge = is_structured_challenge(view, diag)
    verdict = classify_reliability_card_verdict(
        view,
        diag=diag,
        search_submission_started=search_started,
        preflight_failed=preflight_failed,
    )
    if consistency_error and not search_started:
        verdict = "HARNESS_CONSISTENCY_ERROR"

    capture = build_capture_evidence(
        view,
        job_id=job_id,
        attempt_id=attempt_id,
        price_key_id=price_key_id,
        fingerprint=fingerprint,
    )
    parse = build_parse_evidence(view, diag)
    write = build_write_evidence(view, before=before, after=after)
    return {
        "searchSubmitted": search_started,
        "searchSubmissionEvent": event_lookup.get("event"),
        "eventLookup": event_lookup,
        "x11SoldStateVerified": bool(view.get("x11SoldStateVerified") or phases.get("x11SoldStateVerified")),
        "postSoldCapturePhase": view.get("postSoldCapturePhase") or phases.get("postSoldCapturePhase"),
        "parsePhase": view.get("parsePhase") or phases.get("parsePhase") or parse.get("phase"),
        "capture": capture,
        "parse": parse,
        "write": write,
        "challenge": challenge,
        "cardVerdict": verdict,
        "consistencyError": consistency_error,
    }


__all__ = [
    "build_capture_evidence",
    "build_parse_evidence",
    "build_write_evidence",
    "classify_card_from_job_result",
]
