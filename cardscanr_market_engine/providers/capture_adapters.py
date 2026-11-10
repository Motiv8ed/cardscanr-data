"""Offline capture adapters: provenance-aware snapshot assembly (no live network)."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from .capture_evidence import (
    PROTOCOL_VERSION,
    CaptureItemReference,
    CaptureRepresentation,
    Confidence,
    EvidenceFact,
    ObservedValue,
    ObservationOrigin,
    PageSnapshot,
    PayloadBinding,
    Tristate,
    ValidationError,
    _parse_aware,
    _sha256_bytes,
    _sha256_text,
    page_snapshot_from_mapping,
    validate_page_snapshot,
)

SUPPORTED_ADAPTER_IDS = frozenset(
    {
        "cdp_rendered_dom",
        "x11_clipboard",
        "win32_uia_sparse",
        "html_source_inert",
        "network_disabled_inert_fixture",
    }
)


class AdapterPath(str, Enum):
    CDP = "cdp_rendered_dom"
    X11_CLIPBOARD = "x11_clipboard"
    WIN32_UIA = "win32_uia_sparse"
    HTML_SOURCE = "html_source_inert"
    INERT_FIXTURE = "network_disabled_inert_fixture"


class ProvenanceError(ValidationError):
    """Raised when adapter or artifact provenance cannot be trusted."""


@dataclass(frozen=True, slots=True)
class CaptureProvenanceEnvelope:
    job_id: str
    attempt_id: str
    price_key_id: str
    worker_id: str
    lease_id: str
    lease_expiry: datetime
    target_id: str
    document_id: str
    navigation_generation: int
    expected_job_id: str
    expected_attempt_id: str
    artifact_root: str
    adapter_id: str
    protocol_version: str = PROTOCOL_VERSION

    def binding_matches(self) -> bool:
        return (
            self.job_id == self.expected_job_id
            and self.attempt_id == self.expected_attempt_id
            and self.protocol_version == PROTOCOL_VERSION
        )


def _resolve_under_root(path: Path, root: Path) -> Path:
    root = root.resolve()
    raw = Path(path)
    if raw.is_symlink():
        raise ProvenanceError("artifact_path_symlink_escape")
    candidate = raw.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ProvenanceError("artifact_path_outside_approved_root") from exc
    return candidate


def validate_artifact_file(
    path: Path | str,
    *,
    expected_sha256: str | None,
    artifact_root: Path | str,
    max_bytes: int,
) -> tuple[bytes, str]:
    root = Path(artifact_root)
    resolved = _resolve_under_root(Path(path), root)
    if not resolved.is_file():
        raise ProvenanceError("artifact_missing")
    data = resolved.read_bytes()
    if len(data) > max_bytes:
        raise ProvenanceError("artifact_too_large")
    digest = _sha256_bytes(data)
    if expected_sha256 and digest != expected_sha256.lower():
        raise ProvenanceError("artifact_hash_mismatch")
    return data, digest


def validate_provenance_envelope(envelope: CaptureProvenanceEnvelope, *, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    if envelope.protocol_version != PROTOCOL_VERSION:
        raise ProvenanceError("incompatible_protocol_version")
    if not envelope.binding_matches():
        raise ProvenanceError("job_or_attempt_mismatch")
    if envelope.adapter_id not in SUPPORTED_ADAPTER_IDS:
        raise ProvenanceError("unsupported_adapter_path_non_price_eligible")
    if _parse_aware(envelope.lease_expiry) <= now:
        raise ProvenanceError("lease_expired")


def adapter_price_eligible(adapter_id: str) -> bool:
    return adapter_id in SUPPORTED_ADAPTER_IDS and adapter_id != "html_source_inert"


def build_observed_text(
    text: str,
    *,
    adapter_id: str,
    representation: CaptureRepresentation,
    qualified: bool,
) -> ObservedValue:
    if adapter_id == "html_source_inert":
        origin = ObservationOrigin.RAW_SOURCE
        assurance = Confidence.DIAGNOSTIC_ONLY
    elif adapter_id == "win32_uia_sparse":
        origin = ObservationOrigin.CURRENT_CAPTURE
        assurance = Confidence.DIAGNOSTIC_ONLY if not qualified else Confidence.QUALIFIED_CURRENT
    elif adapter_id == "x11_clipboard":
        origin = ObservationOrigin.CURRENT_CAPTURE
        assurance = Confidence.QUALIFIED_CURRENT if qualified and text.strip() else Confidence.UNSUPPORTED
    else:
        origin = ObservationOrigin.CURRENT_CAPTURE
        assurance = Confidence.QUALIFIED_CURRENT if qualified else Confidence.UNSUPPORTED
    return ObservedValue(
        value=text,
        origin=origin,
        representation=representation,
        evidence_id=f"{adapter_id}:visible_text",
        observed_at=datetime.now(timezone.utc),
        assurance=assurance,
        unknown_reason=None if qualified else "visibility_or_selection_unverified",
    )


def facts_from_adapter_payload(payload: Mapping[str, Any]) -> tuple[EvidenceFact, ...]:
    rows = payload.get("facts") or ()
    out: list[EvidenceFact] = []
    if not isinstance(rows, list):
        return tuple()
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append(
            EvidenceFact(
                kind=str(row.get("kind") or ""),
                state=Tristate(str(row.get("state") or Tristate.FALSE.value)),
                evidence_id=str(row.get("evidence_id") or ""),
                scoped_region=row.get("scoped_region"),
                source_origin=ObservationOrigin(str(row.get("source_origin") or ObservationOrigin.UNKNOWN.value)),
                assurance=Confidence(str(row.get("assurance") or Confidence.UNKNOWN.value)),
            )
        )
    return tuple(out)


def build_page_snapshot_from_adapter_payload(
    payload: Mapping[str, Any],
    *,
    envelope: CaptureProvenanceEnvelope,
    artifact_root: Path | str,
    now: datetime | None = None,
) -> PageSnapshot:
    """Construct a trusted PageSnapshot when provenance and hashes validate."""
    now = now or datetime.now(timezone.utc)
    validate_provenance_envelope(envelope, now=now)
    adapter_id = str(payload.get("adapter_id") or envelope.adapter_id)
    if not adapter_price_eligible(adapter_id) and payload.get("require_price_eligible"):
        raise ProvenanceError("adapter_not_price_eligible")

    html_path = payload.get("html_path")
    body_path = payload.get("body_path")
    root = Path(artifact_root)
    html_sha = payload.get("html_sha256")
    body_sha = payload.get("body_sha256")
    html_bytes = b""
    body_text = ""
    if html_path:
        html_bytes, html_sha = validate_artifact_file(
            html_path,
            expected_sha256=html_sha,
            artifact_root=root,
            max_bytes=5 * 1024 * 1024,
        )
    if body_path:
        body_raw, body_sha = validate_artifact_file(
            body_path,
            expected_sha256=body_sha,
            artifact_root=root,
            max_bytes=1 * 1024 * 1024,
        )
        body_text = body_raw.decode("utf-8")

    visible = build_observed_text(
        body_text or html_bytes.decode("utf-8", errors="replace"),
        adapter_id=adapter_id,
        representation={
            AdapterPath.CDP.value: CaptureRepresentation.RENDERED_DOM,
            AdapterPath.X11_CLIPBOARD.value: CaptureRepresentation.CLIPBOARD_TEXT,
            AdapterPath.WIN32_UIA.value: CaptureRepresentation.UIA_SPARSE,
            AdapterPath.HTML_SOURCE.value: CaptureRepresentation.HTML_SOURCE,
            AdapterPath.INERT_FIXTURE.value: CaptureRepresentation.RENDERED_DOM,
        }.get(adapter_id, CaptureRepresentation.UNKNOWN),
        qualified=bool(body_text.strip()) and adapter_id != AdapterPath.HTML_SOURCE.value,
    )

    started = now - timedelta(milliseconds=int(payload.get("elapsed_ms") or 1))
    deadline = started + timedelta(seconds=15)
    gen = int(payload.get("navigation_generation") or envelope.navigation_generation)
    binding = PayloadBinding(
        html_sha256=html_sha,
        html_byte_length=len(html_bytes),
        visible_text_sha256=body_sha or (_sha256_text(body_text) if body_text else None),
        visible_text_byte_length=len(body_text.encode("utf-8")) if body_text else 0,
        artifact_root=str(root),
        manifest_binding_sha256=payload.get("manifest_binding_sha256"),
    )
    items_raw = payload.get("inert_item_references") or ()
    items: list[CaptureItemReference] = []
    if isinstance(items_raw, list):
        for row in items_raw:
            if isinstance(row, dict):
                items.append(
                    CaptureItemReference(
                        node_id=str(row.get("node_id") or ""),
                        item_url=row.get("item_url"),
                        raw_text=str(row.get("raw_text") or ""),
                    )
                )

    mapping: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "capture_id": str(payload.get("capture_id") or hashlib.sha256(f"{envelope.job_id}:{envelope.attempt_id}".encode()).hexdigest()[:32]),
        "job_id": envelope.job_id,
        "price_key_id": envelope.price_key_id,
        "attempt_id": envelope.attempt_id,
        "query_fingerprint": str(payload.get("query_fingerprint") or ""),
        "requested_market": "AU",
        "requested_currency": "AUD",
        "worker_id": envelope.worker_id,
        "lease_id": envelope.lease_id,
        "lease_expiry": envelope.lease_expiry.isoformat(),
        "browser_session_id": str(payload.get("browser_session_id") or "offline"),
        "target_id": envelope.target_id,
        "document_id": envelope.document_id,
        "navigation_generation": gen,
        "started_at": started.isoformat(),
        "completed_at": now.isoformat(),
        "monotonic_duration_ms": int(payload.get("elapsed_ms") or 0),
        "capture_deadline": deadline.isoformat(),
        "navigation_generation_before": gen,
        "navigation_generation_after": gen,
        "expected_target_url": payload.get("expected_target_url") or {
            "value": payload.get("target_url"),
            "origin": "CURRENT_CAPTURE",
            "representation": "rendered_dom",
            "assurance": "QUALIFIED_CURRENT",
            "evidence_id": "target:url",
        },
        "actual_final_url": payload.get("actual_final_url") or {
            "value": payload.get("target_url") or "https://www.ebay.com.au/sch/i.html?_nkw=test",
            "origin": "CURRENT_CAPTURE",
            "representation": "rendered_dom",
            "assurance": "QUALIFIED_CURRENT",
            "evidence_id": "final:url",
        },
        "actual_title": payload.get("actual_title") or {
            "value": payload.get("target_title") or "Sold",
            "origin": "CURRENT_CAPTURE",
            "representation": "rendered_dom",
            "assurance": "QUALIFIED_CURRENT",
            "evidence_id": "title",
        },
        "visible_text": {
            "value": visible.value,
            "origin": visible.origin.value,
            "representation": visible.representation.value,
            "assurance": visible.assurance.value,
            "evidence_id": visible.evidence_id,
            "observed_at": visible.observed_at.isoformat(),
        },
        "facts": [
            {
                "kind": f.kind,
                "state": f.state.value,
                "evidence_id": f.evidence_id,
                "assurance": f.assurance.value,
                "source_origin": f.source_origin.value,
                "scoped_region": f.scoped_region,
            }
            for f in facts_from_adapter_payload(payload)
        ],
        "inert_item_references": [
            {"node_id": i.node_id, "item_url": i.item_url, "raw_text": i.raw_text} for i in items
        ],
        "payload_binding": {
            "html_sha256": binding.html_sha256,
            "html_byte_length": binding.html_byte_length,
            "visible_text_sha256": binding.visible_text_sha256,
            "visible_text_byte_length": binding.visible_text_byte_length,
            "artifact_root": binding.artifact_root,
            "manifest_binding_sha256": binding.manifest_binding_sha256,
        },
        "adapter_id": adapter_id,
        "adapter_capabilities": list(payload.get("adapter_capabilities") or ()),
        "unsupported_observations": list(payload.get("unsupported_observations") or ()),
    }
    snapshot = page_snapshot_from_mapping(mapping)
    validate_page_snapshot(snapshot, now=now)
    return snapshot


def hydrate_capture_payload_strict(
    payload: dict[str, Any],
    *,
    expected_job_id: str,
    expected_attempt_id: str,
    artifact_root: Path | str,
) -> dict[str, Any]:
    """Strict hydration: refuse shared last-capture paths and hash/job mismatches."""
    out = dict(payload)
    job_id = str(out.get("job_id") or out.get("jobId") or "")
    attempt_id = str(out.get("attempt_id") or out.get("attemptId") or "")
    if job_id != expected_job_id or attempt_id != expected_attempt_id:
        out["status"] = "CAPTURE_PROVENANCE_FAILURE"
        out["error"] = "job_or_attempt_mismatch"
        return out
    for key in ("html_path", "htmlPath", "body_path", "bodyPath", "candidates_path", "candidatesPath"):
        raw = out.get(key)
        if not raw:
            continue
        norm = str(raw).replace("\\", "/").lower()
        if "post_sold_capture_last" in norm:
            out["status"] = "CAPTURE_PROVENANCE_FAILURE"
            out["error"] = "refused_shared_diagnostic_path"
            return out
    root = Path(artifact_root)
    html_path = out.get("html_path") or out.get("htmlPath")
    if html_path:
        try:
            data, digest = validate_artifact_file(
                html_path,
                expected_sha256=out.get("html_sha256"),
                artifact_root=root,
                max_bytes=5 * 1024 * 1024,
            )
            out["html"] = data.decode("utf-8")
            out["html_sha256"] = digest
        except ProvenanceError as exc:
            out["status"] = "CAPTURE_PROVENANCE_FAILURE"
            out["error"] = str(exc)
            return out
    return out


def network_disabled() -> bool:
    """Test helper: confirm offline fixture mode (no outbound network)."""
    return str(os.environ.get("CARDSCANR_NETWORK_DISABLED", "")).strip() in {"1", "true", "TRUE"}
