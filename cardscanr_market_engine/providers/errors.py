from __future__ import annotations

from typing import Any


SECRET_FIELD_MARKERS = ("key", "token", "secret", "password", "authorization", "cookie")
SAFE_IDENTIFIER_FIELDS = {
    "cacheid",
    "cacherowid",
    "jobid",
    "key_id",
    "price_key_id",
    "pricekeyid",
    "cache_row_id",
    "snapshot_id",
    "snapshotid",
    "job_id",
}


def sanitize_provider_diagnostics(value: Any) -> Any:
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            normalized_key = key_text.lower()
            if normalized_key in SAFE_IDENTIFIER_FIELDS or normalized_key.endswith("_id"):
                clean[key_text] = sanitize_provider_diagnostics(item)
            elif any(marker in normalized_key for marker in SECRET_FIELD_MARKERS):
                clean[key_text] = "***REDACTED***"
            else:
                clean[key_text] = sanitize_provider_diagnostics(item)
        return clean
    if isinstance(value, list):
        return [sanitize_provider_diagnostics(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_provider_diagnostics(item) for item in value]
    return value


class ProviderError(RuntimeError):
    error_code = "provider_error"
    retryable = False

    def __init__(self, message: str, *, diagnostics: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.diagnostics = sanitize_provider_diagnostics(diagnostics or {})

    def safe_message(self) -> str:
        return str(self)


class ProviderDisabledError(ProviderError):
    error_code = "provider_disabled"


class ProviderRateLimitedError(ProviderError):
    error_code = "provider_rate_limited"
    retryable = True


class ProviderBlockedError(ProviderError):
    error_code = "provider_blocked"
    retryable = True


class ProviderUnsupportedMarketError(ProviderError):
    error_code = "provider_unsupported_market"


class ProviderTemporaryError(ProviderError):
    error_code = "provider_temporary"
    retryable = True


class ProviderPermanentError(ProviderError):
    error_code = "provider_permanent"


class ProviderIdentityUnavailableError(ProviderPermanentError):
    error_code = "provider_identity_unavailable"


class ProviderParseError(ProviderError):
    error_code = "provider_parse_error"
    retryable = True
