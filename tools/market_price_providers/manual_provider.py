"""
manual_provider.py

Manual sold-listing provider for CardScanR market price evidence.

Reads already-imported / manually collected sold-listing rows from a JSON file.
Compatible with the existing ManualMarketListingsProvider in market_pricing_job_queue.py,
but adapted to the new MarketPriceSearchRequest / MarketPriceProviderResult contracts.

No live network calls.  No secrets required.  Safe for cloud/Codex.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from market_pricing_provider_contracts import (
    MarketPriceEvidenceListing,
    MarketPriceProviderCapabilities,
    MarketPriceProviderResult,
    MarketPriceSearchRequest,
)
from market_price_evidence_normalizer import normalize_evidence


_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_MANUAL_JSON_PATH = _ROOT / "data" / "manual_market_prices" / "sample_market_sold_listings.json"


class ManualMarketPriceProvider:
    """
    Reads manually collected sold-listing JSON files and returns evidence
    matching the search request.

    The JSON file may be a bare list of row objects or a dict with a
    ``listings`` key.
    """

    name = "manual"

    CAPABILITIES = MarketPriceProviderCapabilities(
        provider_name="manual",
        enabled=True,
        live_network_required=False,
        secrets_required=False,
        supported_markets=("AU", "US", "GB", "CA", "EU"),
        supported_languages=("en", "jp"),
        supported_currencies=("AUD", "USD", "GBP", "CAD", "EUR"),
        returns_evidence_listings=True,
        returns_confidence_score=False,
        safe_for_cloud=True,
        next_implementation_step=(
            "Add real sold-listing CSV/JSON export data to "
            "data/manual_market_prices/ and re-run the importer."
        ),
        notes="Reads from a local JSON file — no network required.",
    )

    def __init__(self, manual_json_path: Path | None = None) -> None:
        self._path = manual_json_path or DEFAULT_MANUAL_JSON_PATH

    def fetch(self, request: MarketPriceSearchRequest) -> MarketPriceProviderResult:
        payload = _try_load_json(self._path)
        if payload is None:
            return MarketPriceProviderResult(
                provider_name=self.name,
                source="ebay_sold_listings_manual",
                listings=[],
                notes="Manual listing file not found; returning no results.",
            )

        rows: list[dict[str, Any]] = payload if isinstance(payload, list) else payload.get("listings", [])
        if not isinstance(rows, list):
            rows = []

        matched: list[dict[str, Any]] = [
            row for row in rows
            if isinstance(row, dict) and _row_matches_request(row, request)
        ]

        listings: list[MarketPriceEvidenceListing] = []
        for row in matched:
            listing, _ = normalize_evidence(
                row,
                source_provider=self.name,
                marketplace_hint=f"ebay.{_market_to_domain_suffix(request.market)}",
                condition_hint=request.condition,
            )
            if listing is not None:
                listings.append(listing)

        return MarketPriceProviderResult(
            provider_name=self.name,
            source="ebay_sold_listings_manual",
            listings=listings,
            notes=f"Loaded {len(listings)} manual listing rows from {self._path.name}.",
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_matches_request(row: dict[str, Any], request: MarketPriceSearchRequest) -> bool:
    canonical = str(row.get("canonicalCardId") or "").strip()
    set_id = str(row.get("setId") or "").strip()
    market = str(row.get("market") or row.get("marketCountry") or "").strip().upper()
    language = str(row.get("language") or "").strip().lower()

    if canonical and canonical != request.canonical_id:
        return False
    if set_id and set_id != request.set_id:
        return False
    if market and market != request.market.upper():
        return False
    if language and language != request.language.lower():
        return False
    return True


def _market_to_domain_suffix(market: str) -> str:
    mapping = {"AU": "com.au", "US": "com", "GB": "co.uk", "CA": "ca", "EU": "ie"}
    return mapping.get(market.upper(), "com")


def _try_load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
