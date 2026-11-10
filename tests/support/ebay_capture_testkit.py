"""Shared builders for R6 offline capture tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from cardscanr_market_engine.providers.capture_evidence import (
    PROTOCOL_VERSION,
    CaptureItemReference,
    Confidence,
    EvidenceFact,
    ObservedValue,
    ObservationOrigin,
    PageSnapshot,
    PayloadBinding,
    Tristate,
    page_snapshot_from_mapping,
)


def _obs(value: Any, *, representation: str = "rendered_dom") -> dict[str, Any]:
    return {
        "value": value,
        "origin": ObservationOrigin.CURRENT_CAPTURE.value,
        "representation": representation,
        "assurance": Confidence.QUALIFIED_CURRENT.value,
        "evidence_id": "test:obs",
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def minimal_valid_snapshot(**overrides: Any) -> PageSnapshot:
    now = datetime.now(timezone.utc)
    base: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "capture_id": "cap-test-1",
        "job_id": "job-1",
        "price_key_id": "pk-1",
        "attempt_id": "attempt-1",
        "query_fingerprint": "a" * 64,
        "requested_market": "AU",
        "requested_currency": "AUD",
        "worker_id": "worker-1",
        "lease_id": "lease-1",
        "lease_expiry": (now + timedelta(hours=1)).isoformat(),
        "browser_session_id": "sess-1",
        "target_id": "target-1",
        "document_id": "doc-1",
        "navigation_generation": 3,
        "started_at": (now - timedelta(seconds=2)).isoformat(),
        "completed_at": now.isoformat(),
        "monotonic_duration_ms": 1200,
        "capture_deadline": (now + timedelta(seconds=10)).isoformat(),
        "navigation_generation_before": 3,
        "navigation_generation_after": 3,
        "expected_target_url": _obs("https://www.ebay.com.au/sch/i.html?_nkw=test"),
        "actual_final_url": _obs("https://www.ebay.com.au/sch/i.html?_nkw=test&LH_Sold=1&LH_Complete=1"),
        "actual_title": _obs("Sold listings"),
        "visible_text": _obs("Sold listings AU $12.00"),
        "facts": [],
        "inert_item_references": [
            {"node_id": "n1", "item_url": "https://www.ebay.com.au/itm/1", "raw_text": "Test Card"},
        ],
        "payload_binding": {
            "html_sha256": "0" * 64,
            "html_byte_length": 100,
            "visible_text_sha256": "1" * 64,
            "visible_text_byte_length": 20,
            "artifact_root": "/tmp/inert",
            "manifest_binding_sha256": None,
        },
        "adapter_id": "network_disabled_inert_fixture",
        "adapter_capabilities": ["rendered_dom"],
        "unsupported_observations": [],
    }
    base.update(overrides)
    return page_snapshot_from_mapping(base)


def fact(kind: str, state: Tristate, *, assurance: Confidence = Confidence.QUALIFIED_CURRENT) -> EvidenceFact:
    return EvidenceFact(
        kind=kind,
        state=state,
        evidence_id=f"fact:{kind}",
        assurance=assurance,
        source_origin=ObservationOrigin.CURRENT_CAPTURE,
    )


def with_facts(*facts: EvidenceFact) -> dict[str, Any]:
    return {
        "facts": [
            {
                "kind": f.kind,
                "state": f.state.value,
                "evidence_id": f.evidence_id,
                "assurance": f.assurance.value,
                "source_origin": f.source_origin.value,
            }
            for f in facts
        ]
    }
