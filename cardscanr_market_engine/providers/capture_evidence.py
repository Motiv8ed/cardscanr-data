"""Immutable capture evidence model for R6 offline decision (no IO)."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Sequence

PROTOCOL_VERSION = "R6-P0-v1"
MAX_HTML_BYTES = 5 * 1024 * 1024
MAX_VISIBLE_TEXT_BYTES = 1 * 1024 * 1024
MAX_INERT_NODES = 1000
DEFAULT_CAPTURE_DEADLINE_SECONDS = 15.0
DEFAULT_MAX_SKEW_SECONDS = 5.0

_AU_HOST = re.compile(r"^https://(www\.)?ebay\.com\.au/", re.I)


class ObservationOrigin(str, Enum):
    CURRENT_CAPTURE = "CURRENT_CAPTURE"
    PRIOR_NAVIGATION = "PRIOR_NAVIGATION"
    RAW_SOURCE = "RAW_SOURCE"
    APPROX_VISIBLE_HTML = "APPROX_VISIBLE_HTML"
    UNKNOWN = "UNKNOWN"


class CaptureRepresentation(str, Enum):
    RENDERED_DOM = "rendered_dom"
    CLIPBOARD_TEXT = "clipboard_text"
    UIA_SPARSE = "uia_sparse"
    HTML_SOURCE = "html_source"
    UNKNOWN = "unknown"


class Tristate(str, Enum):
    TRUE = "TRUE"
    FALSE = "FALSE"
    UNKNOWN = "UNKNOWN"


class Confidence(str, Enum):
    QUALIFIED_CURRENT = "QUALIFIED_CURRENT"
    DIAGNOSTIC_ONLY = "DIAGNOSTIC_ONLY"
    UNSUPPORTED = "UNSUPPORTED"
    UNKNOWN = "UNKNOWN"


class ValidationError(ValueError):
    """Raised when snapshot or binding validation fails."""


@dataclass(frozen=True, slots=True)
class ObservedValue:
    value: Any
    origin: ObservationOrigin
    representation: CaptureRepresentation
    evidence_id: str
    observed_at: datetime
    assurance: Confidence
    unknown_reason: str | None = None

    def qualified_true(self) -> bool:
        return self.assurance == Confidence.QUALIFIED_CURRENT and self.origin in {
            ObservationOrigin.CURRENT_CAPTURE,
        }


@dataclass(frozen=True, slots=True)
class EvidenceFact:
    kind: str
    state: Tristate
    evidence_id: str
    scoped_region: str | None = None
    source_origin: ObservationOrigin = ObservationOrigin.UNKNOWN
    assurance: Confidence = Confidence.UNKNOWN


@dataclass(frozen=True, slots=True)
class CaptureItemReference:
    node_id: str
    item_url: str | None
    raw_text: str


@dataclass(frozen=True, slots=True)
class PayloadBinding:
    html_sha256: str | None
    html_byte_length: int
    visible_text_sha256: str | None
    visible_text_byte_length: int
    artifact_root: str
    manifest_binding_sha256: str | None


@dataclass(frozen=True, slots=True)
class PageSnapshot:
    protocol_version: str
    capture_id: str
    job_id: str
    price_key_id: str
    attempt_id: str
    query_fingerprint: str
    requested_market: str
    requested_currency: str
    worker_id: str
    lease_id: str
    lease_expiry: datetime
    browser_session_id: str
    target_id: str
    document_id: str
    navigation_generation: int
    started_at: datetime
    completed_at: datetime
    monotonic_duration_ms: int
    capture_deadline: datetime
    navigation_generation_before: int
    navigation_generation_after: int
    expected_target_url: ObservedValue
    actual_final_url: ObservedValue
    actual_title: ObservedValue
    http_status: ObservedValue | None
    visible_text: ObservedValue | None
    raw_html: ObservedValue | None
    facts: tuple[EvidenceFact, ...]
    inert_item_references: tuple[CaptureItemReference, ...]
    body_complete: ObservedValue | None
    parser_ready: ObservedValue | None
    zero_results: ObservedValue | None
    sold_state: ObservedValue | None
    payload_binding: PayloadBinding
    adapter_id: str
    adapter_capabilities: tuple[str, ...]
    unsupported_observations: tuple[str, ...]

    def content_hash(self) -> str:
        parts = (
            self.capture_id,
            self.job_id,
            self.attempt_id,
            self.query_fingerprint,
            self.payload_binding.html_sha256 or "",
            self.payload_binding.visible_text_sha256 or "",
            str(self.navigation_generation),
        )
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse_aware(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def validate_protocol_version(version: str) -> None:
    if str(version or "").strip() != PROTOCOL_VERSION:
        raise ValidationError(f"incompatible_protocol_version:{version!r}")


def validate_au_aud(snapshot: PageSnapshot) -> None:
    if str(snapshot.requested_market or "").upper() != "AU":
        raise ValidationError("requested_market_not_au")
    if str(snapshot.requested_currency or "").upper() != "AUD":
        raise ValidationError("requested_currency_not_aud")
    url = snapshot.actual_final_url.value
    if url and isinstance(url, str) and not _AU_HOST.match(url.strip()):
        raise ValidationError("final_url_not_au_marketplace")


def validate_binding_identity(snapshot: PageSnapshot, *, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    if not (snapshot.job_id and snapshot.attempt_id and snapshot.price_key_id):
        raise ValidationError("missing_job_attempt_or_price_key")
    if not (snapshot.worker_id and snapshot.lease_id):
        raise ValidationError("missing_worker_or_lease")
    lease_expiry = _parse_aware(snapshot.lease_expiry)
    if lease_expiry <= now:
        raise ValidationError("lease_expired")
    if snapshot.navigation_generation_before != snapshot.navigation_generation_after:
        raise ValidationError("navigation_generation_changed_mid_capture")
    if snapshot.navigation_generation != snapshot.navigation_generation_after:
        raise ValidationError("navigation_generation_mismatch")
    deadline = _parse_aware(snapshot.capture_deadline)
    completed = _parse_aware(snapshot.completed_at)
    if completed > deadline:
        raise ValidationError("capture_completed_after_deadline")
    started = _parse_aware(snapshot.started_at)
    if (completed - started).total_seconds() > DEFAULT_CAPTURE_DEADLINE_SECONDS + DEFAULT_MAX_SKEW_SECONDS:
        raise ValidationError("capture_duration_exceeds_bounds")


def validate_payload_sizes(snapshot: PageSnapshot) -> None:
    binding = snapshot.payload_binding
    if binding.html_byte_length > MAX_HTML_BYTES:
        raise ValidationError("html_payload_too_large")
    if binding.visible_text_byte_length > MAX_VISIBLE_TEXT_BYTES:
        raise ValidationError("visible_text_payload_too_large")
    if len(snapshot.inert_item_references) > MAX_INERT_NODES:
        raise ValidationError("too_many_inert_item_references")


def validate_page_snapshot(snapshot: PageSnapshot, *, now: datetime | None = None) -> None:
    validate_protocol_version(snapshot.protocol_version)
    validate_au_aud(snapshot)
    validate_binding_identity(snapshot, now=now)
    validate_payload_sizes(snapshot)


def binding_valid_current(snapshot: PageSnapshot, *, now: datetime | None = None) -> bool:
    try:
        validate_page_snapshot(snapshot, now=now)
    except ValidationError:
        return False
    for field in (snapshot.expected_target_url, snapshot.actual_final_url, snapshot.actual_title):
        if field.origin not in {ObservationOrigin.CURRENT_CAPTURE}:
            return False
        if field.assurance not in {Confidence.QUALIFIED_CURRENT}:
            return False
    if snapshot.http_status is not None:
        status = snapshot.http_status.value
        if snapshot.http_status.origin == ObservationOrigin.RAW_SOURCE and status is not None:
            return False
    return True


def fact_state(snapshot: PageSnapshot, kind: str) -> Tristate:
    for fact in snapshot.facts:
        if fact.kind == kind:
            if fact.assurance == Confidence.UNSUPPORTED:
                return Tristate.UNKNOWN
            if fact.source_origin == ObservationOrigin.RAW_SOURCE and fact.state == Tristate.TRUE:
                return Tristate.UNKNOWN
            return fact.state
    return Tristate.FALSE


def derive_policy_signals(snapshot: PageSnapshot, *, now: datetime | None = None) -> dict[str, bool]:
    """Map qualified snapshot facts to the 12-dimension policy row."""
    binding = binding_valid_current(snapshot, now=now)

    def qualified_fact(kind: str) -> bool:
        st = fact_state(snapshot, kind)
        return st == Tristate.TRUE

    complete_sold = qualified_fact("complete_current_sold_document")
    zero_results = qualified_fact("verified_current_zero_results")
    visible = snapshot.visible_text
    if visible is not None and visible.representation == CaptureRepresentation.HTML_SOURCE:
        if visible.assurance != Confidence.QUALIFIED_CURRENT:
            complete_sold = False
    if visible is not None and visible.origin == ObservationOrigin.RAW_SOURCE:
        complete_sold = False
    incomplete = qualified_fact("capture_incomplete")
    if visible is None or visible.assurance == Confidence.UNSUPPORTED:
        incomplete = True
    if fact_state(snapshot, "auth_observed") == Tristate.UNKNOWN:
        auth = False
    else:
        auth = qualified_fact("auth_observed")
        if snapshot.visible_text and snapshot.visible_text.representation == CaptureRepresentation.CLIPBOARD_TEXT:
            if snapshot.visible_text.value in (None, "") and auth:
                auth = qualified_fact("auth_observed") and bool(
                    snapshot.actual_title.qualified_true() or snapshot.actual_final_url.qualified_true()
                )
    return {
        "auth_observed": auth,
        "active_challenge_observed": qualified_fact("active_challenge_observed"),
        "sorry_observed": qualified_fact("sorry_observed"),
        "capture_incomplete": incomplete,
        "optional_banner_present": qualified_fact("optional_banner_present"),
        "prior_state_present": bool(snapshot.facts and any(f.kind == "prior_navigation_context" for f in snapshot.facts)),
        "sold_looking_markers": qualified_fact("sold_looking_markers"),
        "hidden_script_css_comment_tokens": qualified_fact("hidden_script_css_comment_tokens"),
        "binding_valid_current": binding,
        "access_403_observed": qualified_fact("access_403_observed"),
        "complete_current_sold_document": complete_sold,
        "verified_current_zero_results": zero_results,
    }


def observed_from_mapping(data: Mapping[str, Any], *, field: str) -> ObservedValue:
    block = data.get(field) or {}
    if not isinstance(block, dict):
        block = {"value": block}
    origin = ObservationOrigin(str(block.get("origin") or ObservationOrigin.UNKNOWN.value))
    rep = CaptureRepresentation(str(block.get("representation") or CaptureRepresentation.UNKNOWN.value))
    assurance = Confidence(str(block.get("assurance") or Confidence.UNKNOWN.value))
    obs_at = _parse_aware(block.get("observed_at") or datetime.now(timezone.utc).isoformat())
    return ObservedValue(
        value=block.get("value"),
        origin=origin,
        representation=rep,
        evidence_id=str(block.get("evidence_id") or f"{field}:unbound"),
        observed_at=obs_at,
        assurance=assurance,
        unknown_reason=block.get("unknown_reason"),
    )


def facts_from_mapping(rows: Sequence[Mapping[str, Any]]) -> tuple[EvidenceFact, ...]:
    out: list[EvidenceFact] = []
    for row in rows:
        out.append(
            EvidenceFact(
                kind=str(row.get("kind") or ""),
                state=Tristate(str(row.get("state") or Tristate.FALSE.value)),
                evidence_id=str(row.get("evidence_id") or ""),
                scoped_region=row.get("scoped_region"),
                source_origin=ObservationOrigin(
                    str(row.get("source_origin") or ObservationOrigin.UNKNOWN.value)
                ),
                assurance=Confidence(str(row.get("assurance") or Confidence.UNKNOWN.value)),
            )
        )
    return tuple(out)


def page_snapshot_from_mapping(data: Mapping[str, Any]) -> PageSnapshot:
    validate_protocol_version(str(data.get("protocol_version") or ""))
    binding_raw = data.get("payload_binding") or {}
    if not isinstance(binding_raw, dict):
        binding_raw = {}
    payload_binding = PayloadBinding(
        html_sha256=binding_raw.get("html_sha256"),
        html_byte_length=int(binding_raw.get("html_byte_length") or 0),
        visible_text_sha256=binding_raw.get("visible_text_sha256"),
        visible_text_byte_length=int(binding_raw.get("visible_text_byte_length") or 0),
        artifact_root=str(binding_raw.get("artifact_root") or ""),
        manifest_binding_sha256=binding_raw.get("manifest_binding_sha256"),
    )
    items_raw = data.get("inert_item_references") or ()
    items: list[CaptureItemReference] = []
    if isinstance(items_raw, Sequence):
        for row in items_raw:
            if isinstance(row, Mapping):
                items.append(
                    CaptureItemReference(
                        node_id=str(row.get("node_id") or ""),
                        item_url=row.get("item_url"),
                        raw_text=str(row.get("raw_text") or ""),
                    )
                )
    facts_raw = data.get("facts") or ()
    facts = facts_from_mapping(facts_raw if isinstance(facts_raw, Sequence) else ())
    return PageSnapshot(
        protocol_version=str(data.get("protocol_version")),
        capture_id=str(data.get("capture_id") or ""),
        job_id=str(data.get("job_id") or ""),
        price_key_id=str(data.get("price_key_id") or ""),
        attempt_id=str(data.get("attempt_id") or ""),
        query_fingerprint=str(data.get("query_fingerprint") or ""),
        requested_market=str(data.get("requested_market") or "AU"),
        requested_currency=str(data.get("requested_currency") or "AUD"),
        worker_id=str(data.get("worker_id") or ""),
        lease_id=str(data.get("lease_id") or ""),
        lease_expiry=_parse_aware(data.get("lease_expiry") or datetime.now(timezone.utc).isoformat()),
        browser_session_id=str(data.get("browser_session_id") or ""),
        target_id=str(data.get("target_id") or ""),
        document_id=str(data.get("document_id") or ""),
        navigation_generation=int(data.get("navigation_generation") or 0),
        started_at=_parse_aware(data.get("started_at") or datetime.now(timezone.utc).isoformat()),
        completed_at=_parse_aware(data.get("completed_at") or datetime.now(timezone.utc).isoformat()),
        monotonic_duration_ms=int(data.get("monotonic_duration_ms") or 0),
        capture_deadline=_parse_aware(data.get("capture_deadline") or datetime.now(timezone.utc).isoformat()),
        navigation_generation_before=int(data.get("navigation_generation_before") or 0),
        navigation_generation_after=int(data.get("navigation_generation_after") or 0),
        expected_target_url=observed_from_mapping(data, field="expected_target_url"),
        actual_final_url=observed_from_mapping(data, field="actual_final_url"),
        actual_title=observed_from_mapping(data, field="actual_title"),
        http_status=observed_from_mapping(data, field="http_status") if data.get("http_status") else None,
        visible_text=observed_from_mapping(data, field="visible_text") if data.get("visible_text") else None,
        raw_html=observed_from_mapping(data, field="raw_html") if data.get("raw_html") else None,
        facts=facts,
        inert_item_references=tuple(items),
        body_complete=observed_from_mapping(data, field="body_complete") if data.get("body_complete") else None,
        parser_ready=observed_from_mapping(data, field="parser_ready") if data.get("parser_ready") else None,
        zero_results=observed_from_mapping(data, field="zero_results") if data.get("zero_results") else None,
        sold_state=observed_from_mapping(data, field="sold_state") if data.get("sold_state") else None,
        payload_binding=payload_binding,
        adapter_id=str(data.get("adapter_id") or "unknown"),
        adapter_capabilities=tuple(str(x) for x in (data.get("adapter_capabilities") or ())),
        unsupported_observations=tuple(str(x) for x in (data.get("unsupported_observations") or ())),
    )
