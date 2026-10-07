"""Classify pricing failures and compute deterministic backoff windows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
from typing import Any

from .providers.errors import (
    ProviderAuthenticationRequiredError,
    ProviderBlockedError,
    ProviderError,
    ProviderIdentityUnavailableError,
    ProviderPermanentError,
    ProviderRateLimitedError,
    ProviderTemporaryError,
    ProviderUnsupportedMarketError,
)
from .providers.identity_guard import ENGLISH_MARKET_IDENTITY_UNAVAILABLE

FAILURE_CLASS_TRANSIENT = "retryable_transient"
FAILURE_CLASS_TRANSIENT_EBAY = "retryable_transient_ebay"
FAILURE_CLASS_LATER = "retryable_later"
FAILURE_CLASS_IDENTITY = "identity_unsupported"

# Conservative defaults for classic eBay SORRY / Error Page (not challenge bypass).
# First-key backoff must outlast a typical marketplace cooldown so the same
# failing card is not immediately reselected on the first post-resume cycle.
DEFAULT_EBAY_TRANSIENT_COOLDOWN_MINUTES = 90
DEFAULT_EBAY_TRANSIENT_SECOND_COOLDOWN_MINUTES = 180
DEFAULT_EBAY_TRANSIENT_DEFER_HOURS = 6

IDENTITY_ERROR_MARKERS = frozenset(
    {
        ENGLISH_MARKET_IDENTITY_UNAVAILABLE,
        "provider_identity_unavailable",
        "english_market_identity_unavailable",
    }
)
UNSUPPORTED_ERROR_MARKERS = frozenset(
    {
        "provider_unsupported_market",
        "unsupported_market",
    }
)


@dataclass(frozen=True)
class FailurePolicy:
    classification: str
    retryable: bool
    backoff: timedelta
    reason: str
    next_refresh_due_at: datetime


def _message_blob(exc: BaseException | str | None) -> str:
    if exc is None:
        return ""
    if isinstance(exc, ProviderError):
        parts = [str(exc), str(exc.error_code or "")]
        diagnostics = exc.diagnostics or {}
        parts.append(str(diagnostics.get("blocked_reason") or ""))
        return " ".join(parts).lower()
    return str(exc).lower()


def _ebay_sorry_markers(exc: BaseException | str | None) -> bool:
    text = _message_blob(exc)
    diag = ""
    if isinstance(exc, ProviderError) and isinstance(exc.diagnostics, dict):
        diag = " ".join(
            str(exc.diagnostics.get(k) or "")
            for k in ("reason", "providerOutcome", "preSoldSorry", "error")
        ).lower()
    blob = f"{text} {diag}"
    return any(
        marker in blob
        for marker in (
            "ebay_sorry_error_page",
            "temporary_ebay_server_failure",
            "ebay sorry",
            "pre_sold_sorry",
            "error page | ebay",
            "something went wrong on our end",
        )
    )


def ebay_transient_backoff_minutes(*, consecutive_same_failures: int = 1) -> timedelta:
    """Per printing×market track: 15m → longer → defer to later scheduler cycle."""
    attempts = max(1, int(consecutive_same_failures))
    first = max(
        1,
        int(
            os.getenv(
                "EBAY_TRANSIENT_FAILURE_COOLDOWN_MINUTES",
                str(DEFAULT_EBAY_TRANSIENT_COOLDOWN_MINUTES),
            )
        ),
    )
    second = max(
        first,
        int(
            os.getenv(
                "EBAY_TRANSIENT_FAILURE_SECOND_COOLDOWN_MINUTES",
                str(DEFAULT_EBAY_TRANSIENT_SECOND_COOLDOWN_MINUTES),
            )
        ),
    )
    defer_hours = max(
        1,
        int(
            os.getenv(
                "EBAY_TRANSIENT_FAILURE_DEFER_HOURS",
                str(DEFAULT_EBAY_TRANSIENT_DEFER_HOURS),
            )
        ),
    )
    if attempts <= 1:
        return timedelta(minutes=first)
    if attempts == 2:
        return timedelta(minutes=second)
    return timedelta(hours=defer_hours)


def classify_pricing_failure(exc: BaseException | str | None) -> str:
    text = _message_blob(exc)
    if isinstance(exc, ProviderAuthenticationRequiredError):
        return FAILURE_CLASS_IDENTITY
    if isinstance(exc, ProviderIdentityUnavailableError):
        return FAILURE_CLASS_IDENTITY
    if isinstance(exc, ProviderUnsupportedMarketError):
        return FAILURE_CLASS_IDENTITY
    if any(marker in text for marker in IDENTITY_ERROR_MARKERS):
        return FAILURE_CLASS_IDENTITY
    if any(marker in text for marker in UNSUPPORTED_ERROR_MARKERS):
        return FAILURE_CLASS_IDENTITY
    if _ebay_sorry_markers(exc):
        return FAILURE_CLASS_TRANSIENT_EBAY
    if isinstance(
        exc,
        (
            ProviderTemporaryError,
            ProviderRateLimitedError,
            ProviderBlockedError,
        ),
    ):
        return FAILURE_CLASS_TRANSIENT
    if isinstance(exc, ProviderPermanentError):
        return FAILURE_CLASS_IDENTITY
    if isinstance(exc, ProviderError) and not exc.retryable:
        return FAILURE_CLASS_IDENTITY
    # Completed searches with no usable evidence are handled as completed jobs,
    # not exceptions. Remaining generic exceptions are treated as transient.
    if "timeout" in text or "temporar" in text or "rate limit" in text or "network" in text:
        return FAILURE_CLASS_TRANSIENT
    if "no_safe" in text or "no_reliable" in text or "weak_evidence" in text:
        return FAILURE_CLASS_LATER
    return FAILURE_CLASS_TRANSIENT


def backoff_for_failure(
    classification: str,
    *,
    consecutive_same_failures: int = 1,
) -> timedelta:
    attempts = max(1, int(consecutive_same_failures))
    if classification == FAILURE_CLASS_IDENTITY:
        if attempts <= 1:
            return timedelta(hours=6)
        if attempts == 2:
            return timedelta(hours=24)
        return timedelta(days=3)
    if classification == FAILURE_CLASS_LATER:
        if attempts <= 1:
            return timedelta(hours=3)
        if attempts == 2:
            return timedelta(hours=12)
        return timedelta(hours=24)
    if classification == FAILURE_CLASS_TRANSIENT_EBAY:
        return ebay_transient_backoff_minutes(consecutive_same_failures=attempts)
    # generic transient
    if attempts <= 1:
        return timedelta(minutes=30)
    if attempts == 2:
        return timedelta(hours=1)
    return timedelta(hours=3)


def build_failure_policy(
    exc: BaseException | str | None,
    *,
    now: datetime | None = None,
    consecutive_same_failures: int = 1,
) -> FailurePolicy:
    current = now or datetime.now(timezone.utc)
    classification = classify_pricing_failure(exc)
    backoff = backoff_for_failure(classification, consecutive_same_failures=consecutive_same_failures)
    retryable = classification != FAILURE_CLASS_IDENTITY
    reason = {
        FAILURE_CLASS_IDENTITY: "identity_or_unsupported_backoff",
        FAILURE_CLASS_LATER: "retryable_later_backoff",
        FAILURE_CLASS_TRANSIENT: "retryable_transient_backoff",
        FAILURE_CLASS_TRANSIENT_EBAY: "retryable_transient_ebay_backoff",
    }.get(classification, "failure_backoff")
    return FailurePolicy(
        classification=classification,
        retryable=retryable,
        backoff=backoff,
        reason=reason,
        next_refresh_due_at=current + backoff,
    )


def is_identity_error_message(message: object) -> bool:
    text = str(message or "").lower()
    return any(marker in text for marker in IDENTITY_ERROR_MARKERS | UNSUPPORTED_ERROR_MARKERS)


def failure_policy_diagnostics(policy: FailurePolicy) -> dict[str, Any]:
    return {
        "failureClassification": policy.classification,
        "retryable": policy.retryable,
        "backoffSeconds": int(policy.backoff.total_seconds()),
        "backoffReason": policy.reason,
        "nextRefreshDueAt": policy.next_refresh_due_at.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
    }
