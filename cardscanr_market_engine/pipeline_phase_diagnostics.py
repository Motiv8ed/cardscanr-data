#!/usr/bin/env python3
"""Authoritative pipeline-phase diagnostics for harness/reporting (no secrets)."""
from __future__ import annotations

from typing import Any

from .providers.errors import sanitize_provider_diagnostics
from .providers.post_sold_capture import (
    POST_SOLD_CAPTURE_FAILED,
    POST_SOLD_CAPTURE_PENDING,
    POST_SOLD_CAPTURE_READY,
)

PARSE_COMPLETE = "PARSE_COMPLETE"
PARSE_FAILED = "PARSE_FAILED"
PARSE_PENDING = "PARSE_PENDING"


def _operational_meta_from_attempts(meta: dict[str, Any]) -> dict[str, Any]:
    """Recover phase fields from queryAttempts[*].raw_metadata when aggregate dropped them."""
    attempts = meta.get("queryAttempts")
    if not isinstance(attempts, list):
        return {}
    for item in reversed(attempts):
        if not isinstance(item, dict):
            continue
        nested = item.get("raw_metadata") or item.get("rawMetadata") or item.get("diagnostics")
        if isinstance(nested, dict) and (
            nested.get("postSoldCapturePhase")
            or nested.get("x11SoldStateVerified")
            or nested.get("parsePhase")
        ):
            return nested
    return {}


def extract_pipeline_phases(raw_metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Pull capture/parse/finalize phases from provider raw_metadata / stageTimings."""
    meta = raw_metadata if isinstance(raw_metadata, dict) else {}
    recovered = _operational_meta_from_attempts(meta)
    stage = meta.get("stageTimings") or recovered.get("stageTimings")
    # StageTimings.snapshot() is a flat dict (fields themselves), not {"fields": ...}.
    stage_flat = stage if isinstance(stage, dict) else {}
    stage_nested_fields = stage_flat.get("fields") if isinstance(stage_flat.get("fields"), dict) else {}
    desktop_nav = meta.get("desktopNav") or recovered.get("desktopNav") or stage_flat.get("desktopNav")
    if not isinstance(desktop_nav, dict):
        desktop_nav = {}

    def _first(*values: Any) -> Any:
        for value in values:
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        return None

    capture_phase = _first(
        meta.get("postSoldCapturePhase"),
        recovered.get("postSoldCapturePhase"),
        stage_flat.get("postSoldCapturePhase"),
        stage_nested_fields.get("postSoldCapturePhase"),
    )
    parse_phase = _first(
        meta.get("parsePhase"),
        recovered.get("parsePhase"),
        stage_flat.get("parsePhase"),
        stage_nested_fields.get("parsePhase"),
    )
    finalize_terminal = _first(
        meta.get("finalizeTerminal"),
        recovered.get("finalizeTerminal"),
        stage_flat.get("finalizeTerminal"),
        stage_nested_fields.get("finalizeTerminal"),
    )
    sold_verified = bool(
        meta.get("x11SoldStateVerified")
        or recovered.get("x11SoldStateVerified")
        or stage_flat.get("x11SoldStateVerified")
        or stage_nested_fields.get("x11SoldStateVerified")
        or desktop_nav.get("SOLD_STATE_VERIFIED")
        or (isinstance(meta.get("soldState"), dict) and meta["soldState"].get("SOLD_STATE_VERIFIED"))
        or (isinstance(recovered.get("soldState"), dict) and recovered["soldState"].get("SOLD_STATE_VERIFIED"))
    )
    persisted = (
        meta.get("persistedCaptureArtifact")
        or meta.get("currentJobCapture")
        or recovered.get("persistedCaptureArtifact")
        or stage_flat.get("persistedCaptureArtifact")
        or stage_nested_fields.get("persistedCaptureArtifact")
    )
    post_sold_capture = (
        meta.get("postSoldCapture")
        or recovered.get("postSoldCapture")
        or stage_flat.get("postSoldCapture")
        or stage_nested_fields.get("postSoldCapture")
    )
    return {
        "postSoldCapturePhase": capture_phase,
        "parsePhase": parse_phase,
        "finalizeTerminal": finalize_terminal,
        "x11SoldStateVerified": sold_verified,
        "persistedCaptureArtifact": persisted if isinstance(persisted, dict) else None,
        "postSoldCapture": post_sold_capture if isinstance(post_sold_capture, dict) else None,
        "desktopNav": desktop_nav or None,
        "navMode": meta.get("navMode") or recovered.get("navMode"),
        "stageTimings": stage_flat or None,
    }


def build_provider_diagnostics_for_result(
    provider_result: Any | None = None,
    *,
    raw_metadata: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Sanitize provider diagnostics for job result / harness consumption."""
    meta: dict[str, Any] = {}
    if raw_metadata and isinstance(raw_metadata, dict):
        meta.update(raw_metadata)
    elif provider_result is not None:
        raw = getattr(provider_result, "raw_metadata", None)
        if isinstance(raw, dict):
            meta.update(raw)
    phases = extract_pipeline_phases(meta)
    # Ensure phase keys are always present at diagnostics top-level (authoritative).
    diagnostics = {
        **{k: v for k, v in meta.items() if k not in {"cookies", "authorization", "localStorage"}},
        **{k: v for k, v in phases.items() if v is not None},
    }
    if extra:
        diagnostics.update(extra)
    return sanitize_provider_diagnostics(
        {
            "providerErrorCode": None,
            "retryable": False,
            "diagnostics": diagnostics,
        }
    )


def capture_phase_acknowledged(capture_phase: str | None) -> bool:
    return str(capture_phase or "").strip() == POST_SOLD_CAPTURE_READY


def capture_phase_failed(capture_phase: str | None) -> bool:
    text = str(capture_phase or "").strip()
    return text in {POST_SOLD_CAPTURE_FAILED, POST_SOLD_CAPTURE_PENDING} or text.endswith("_FAILED")


__all__ = [
    "PARSE_COMPLETE",
    "PARSE_FAILED",
    "PARSE_PENDING",
    "build_provider_diagnostics_for_result",
    "capture_phase_acknowledged",
    "capture_phase_failed",
    "extract_pipeline_phases",
]
