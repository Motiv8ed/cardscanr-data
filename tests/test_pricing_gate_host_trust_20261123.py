"""Offline trust-boundary regression for eBay gate-page classification.

No browser or network activity; never changes the live pricing gate.
"""
from __future__ import annotations

from cardscanr_market_engine.marketplace_gate_url import (
    is_ebay_marketplace_gate_url,
    is_marketplace_gate_page,
)
from cardscanr_market_engine.browser_lifecycle_policy import (
    RUNTIME_INTER_CARD,
    REASON_CHALLENGE,
    evaluate_runtime_targets,
)


def test_true_ebay_challenge_remains_blocked() -> None:
    url = "https://www.ebay.com.au/splashui/challenge"
    assert is_ebay_marketplace_gate_url(url)
    assert is_marketplace_gate_page(url)
    verdict = evaluate_runtime_targets(
        [{"type": "page", "id": "ebay", "url": url, "title": "Verify yourself to continue"}],
        mode=RUNTIME_INTER_CARD,
    )
    assert not verdict.ok
    assert REASON_CHALLENGE in verdict.reason_codes


def test_foreign_host_cannot_impersonate_ebay_challenge_via_query() -> None:
    url = "https://example.invalid/challenge?return=https%3A%2F%2Fwww.ebay.com.au"
    assert not is_ebay_marketplace_gate_url(url)
    assert not is_marketplace_gate_page(url, title="Regular partner redirect")
    verdict = evaluate_runtime_targets(
        [{"type": "page", "id": "foreign", "url": url, "title": "Regular partner redirect"}],
        mode=RUNTIME_INTER_CARD,
    )
    assert REASON_CHALLENGE not in verdict.reason_codes


def test_foreign_access_denied_title_does_not_prove_ebay_challenge() -> None:
    url = "https://example.invalid/blocked?next=www.ebay.com.au"
    assert not is_marketplace_gate_page(url, title="Access denied")
    verdict = evaluate_runtime_targets(
        [{"type": "page", "id": "foreign", "url": url, "title": "Access denied"}],
        mode=RUNTIME_INTER_CARD,
    )
    assert REASON_CHALLENGE not in verdict.reason_codes


def test_ebay_access_denied_title_still_blocks() -> None:
    url = "https://www.ebay.com.au/sch/i.html?_nkw=example"
    assert is_marketplace_gate_page(url, title="Access denied")
    verdict = evaluate_runtime_targets(
        [{"type": "page", "id": "ebay", "url": url, "title": "Access denied"}],
        mode=RUNTIME_INTER_CARD,
    )
    assert not verdict.ok
    assert REASON_CHALLENGE in verdict.reason_codes
