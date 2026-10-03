"""Correlate post-Sold capture artifacts to the current job/attempt only.

Global ``post_sold_capture_last/last_capture.*`` files are developer diagnostics
only — never authoritative reliability evidence and never a production parser
fallback.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LAST_CAPTURE_DIR = ROOT / "reports" / "artifacts" / "post_sold_capture_last"
CAPTURE_ORIGIN_LIVE = "LIVE_BROWSER_CAPTURE"
CAPTURE_ORIGIN_LOCAL_FIXTURE = "LOCAL_FIXTURE"
CAPTURE_ORIGIN_UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class CaptureEvidenceCorrelation:
    status: str  # ATTACHED | NOT_RUN | REJECTED_MISMATCH | FAILED_NO_ARTIFACT
    phase: str | None
    artifact_path: str | None
    sha256: str | None
    target_id: str | None
    capture_origin: str | None
    correlated: bool
    rejection_reasons: list[str]
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def capture_origin_from_env_or_meta(meta: dict[str, Any] | None = None) -> str:
    meta = meta if isinstance(meta, dict) else {}
    env = str(os.environ.get("CARDSCANR_CAPTURE_ORIGIN") or "").strip().upper()
    if env in {CAPTURE_ORIGIN_LIVE, CAPTURE_ORIGIN_LOCAL_FIXTURE}:
        return env
    tagged = str(meta.get("capture_origin") or meta.get("captureOrigin") or "").strip().upper()
    if tagged in {CAPTURE_ORIGIN_LIVE, CAPTURE_ORIGIN_LOCAL_FIXTURE}:
        return tagged
    return CAPTURE_ORIGIN_UNKNOWN


def _norm(value: Any) -> str:
    return str(value or "").strip()


def correlate_capture_evidence(
    *,
    result: dict[str, Any] | None,
    diag: dict[str, Any] | None = None,
    expected_job_id: str | None = None,
    expected_attempt_id: str | None = None,
    expected_price_key_id: str | None = None,
    expected_fingerprint: str | None = None,
    allow_global_last_capture: bool = False,
) -> CaptureEvidenceCorrelation:
    """Attach capture evidence only when positively correlated to the current job.

    Never reads ``post_sold_capture_last`` unless ``allow_global_last_capture`` is
    explicitly True (developer diagnostics only — not for reliability reports).
    """
    result = result if isinstance(result, dict) else {}
    diag = diag if isinstance(diag, dict) else {}
    phase = result.get("postSoldCapturePhase") or diag.get("postSoldCapturePhase")
    probe = result.get("postSoldCapture") or diag.get("postSoldCapture") or {}
    if not isinstance(probe, dict):
        probe = {}
    pdiag = probe.get("diagnostics") if isinstance(probe.get("diagnostics"), dict) else {}

    # Pre-submit / skip / never reached capture.
    if not phase and not probe.get("targetId") and not result.get("x11SoldStateVerified"):
        status_hint = str(result.get("status") or result.get("ownedDailyOutcome") or "")
        return CaptureEvidenceCorrelation(
            status="NOT_RUN",
            phase=None,
            artifact_path=None,
            sha256=None,
            target_id=None,
            capture_origin=None,
            correlated=True,
            rejection_reasons=[],
            details={
                "reason": "capture_phase_absent",
                "jobStatus": status_hint,
                "globalLastCaptureAuthoritative": False,
                "allowGlobalLastCapture": bool(allow_global_last_capture),
            },
        )

    # Prefer paths returned by the CURRENT provider/process result only.
    persisted = result.get("persistedCaptureArtifact")
    persisted_path = persisted.get("htmlPath") if isinstance(persisted, dict) else None
    html_path = (
        pdiag.get("html_path")
        or pdiag.get("htmlPath")
        or probe.get("html_path")
        or probe.get("htmlPath")
        or persisted_path
    )
    sha = pdiag.get("html_sha256") or pdiag.get("htmlSha256") or probe.get("html_sha256")
    meta_corr = {
        "job_id": pdiag.get("job_id") or pdiag.get("jobId") or probe.get("jobId"),
        "attempt_id": pdiag.get("attempt_id") or pdiag.get("attemptId") or probe.get("attemptId"),
        "price_key_id": pdiag.get("price_key_id") or pdiag.get("priceKeyId") or probe.get("priceKeyId"),
        "fingerprint": pdiag.get("fingerprint") or probe.get("fingerprint"),
        "capture_origin": pdiag.get("capture_origin") or probe.get("captureOrigin"),
    }
    origin = capture_origin_from_env_or_meta(meta_corr)

    # Explicitly refuse global last_capture for evidence-grade reports.
    if html_path and "post_sold_capture_last" in str(html_path).replace("\\", "/"):
        # Only accept if correlation metadata matches AND caller allowed — still label diagnostic.
        if not allow_global_last_capture:
            html_path = None
            sha = None

    reject: list[str] = []
    if expected_job_id and meta_corr.get("job_id") and _norm(meta_corr.get("job_id")) != _norm(expected_job_id):
        reject.append("jobId_mismatch")
    if expected_attempt_id and meta_corr.get("attempt_id") and _norm(meta_corr.get("attempt_id")) != _norm(expected_attempt_id):
        reject.append("attemptId_mismatch")
    if expected_price_key_id and meta_corr.get("price_key_id") and _norm(meta_corr.get("price_key_id")) != _norm(expected_price_key_id):
        reject.append("priceKeyId_mismatch")
    if expected_fingerprint and meta_corr.get("fingerprint") and _norm(meta_corr.get("fingerprint")) != _norm(expected_fingerprint):
        reject.append("fingerprint_mismatch")
    if origin == CAPTURE_ORIGIN_LOCAL_FIXTURE and expected_attempt_id:
        # Local fixture must never appear as live card evidence.
        reject.append("local_fixture_not_live_evidence")

    if reject:
        return CaptureEvidenceCorrelation(
            status="REJECTED_MISMATCH",
            phase=str(phase) if phase else None,
            artifact_path=None,
            sha256=None,
            target_id=None,
            capture_origin=origin,
            correlated=False,
            rejection_reasons=reject,
            details={"meta": meta_corr, "globalLastCaptureAuthoritative": False},
        )

    if phase and str(phase).endswith("_FAILED") and not html_path:
        return CaptureEvidenceCorrelation(
            status="FAILED_NO_ARTIFACT",
            phase=str(phase),
            artifact_path=None,
            sha256=None,
            target_id=probe.get("targetId"),
            capture_origin=origin,
            correlated=True,
            rejection_reasons=[],
            details={
                "failureClass": probe.get("failureClass"),
                "globalLastCaptureAuthoritative": False,
            },
        )

    if not html_path and not probe.get("targetId") and not phase:
        return CaptureEvidenceCorrelation(
            status="NOT_RUN",
            phase=None,
            artifact_path=None,
            sha256=None,
            target_id=None,
            capture_origin=None,
            correlated=True,
            rejection_reasons=[],
            details={"globalLastCaptureAuthoritative": False},
        )

    return CaptureEvidenceCorrelation(
        status="ATTACHED" if html_path or probe.get("targetId") else "NOT_RUN",
        phase=str(phase) if phase else None,
        artifact_path=str(html_path) if html_path else None,
        sha256=str(sha) if sha else None,
        target_id=probe.get("targetId"),
        capture_origin=origin if (html_path or probe.get("targetId")) else None,
        correlated=True,
        rejection_reasons=[],
        details={
            "meta": meta_corr,
            "globalLastCaptureAuthoritative": False,
            "lastCaptureDir": str(LAST_CAPTURE_DIR),
            "lastCaptureRole": "developer_diagnostic_non_authoritative",
        },
    )


def not_run_capture_block() -> dict[str, Any]:
    return {
        "status": "NOT_RUN",
        "phase": None,
        "failureClass": None,
        "failureDetail": None,
        "targetId": None,
        "targetUrl": None,
        "targetTitle": None,
        "captureMethod": None,
        "elapsedMs": None,
        "exitCode": None,
        "orphanCount": None,
        "htmlChars": None,
        "htmlBytes": None,
        "bodyLength": None,
        "artifactPath": None,
        "sha256": None,
        "shaVerified": False,
        "utf8Verified": False,
        "protocolBytesApprox": None,
        "workerStatus": None,
        "captureOrigin": None,
        "correlated": True,
        "globalLastCaptureAuthoritative": False,
    }


__all__ = [
    "CAPTURE_ORIGIN_LIVE",
    "CAPTURE_ORIGIN_LOCAL_FIXTURE",
    "CAPTURE_ORIGIN_UNKNOWN",
    "CaptureEvidenceCorrelation",
    "LAST_CAPTURE_DIR",
    "capture_origin_from_env_or_meta",
    "correlate_capture_evidence",
    "not_run_capture_block",
]
