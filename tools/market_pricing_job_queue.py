#!/usr/bin/env python3
"""Shared foundations for CardScanR market pricing worker/query tooling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable, Protocol


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANUAL_PROVIDER_PATH = ROOT / "data" / "manual_market_prices" / "sample_market_sold_listings.json"
BANNED_TERMS = ["proxy", "custom", "fake", "digital", "lot", "bundle"]


MARKET_CONFIG: dict[str, dict[str, str]] = {
    "au": {
        "market": "AU",
        "country": "AU",
        "currency": "AUD",
        "ebayDomain": "www.ebay.com.au",
        "queryLocale": "en-AU",
    },
    "us": {
        "market": "US",
        "country": "US",
        "currency": "USD",
        "ebayDomain": "www.ebay.com",
        "queryLocale": "en-US",
    },
    "gb": {
        "market": "GB",
        "country": "GB",
        "currency": "GBP",
        "ebayDomain": "www.ebay.co.uk",
        "queryLocale": "en-GB",
    },
    "ca": {
        "market": "CA",
        "country": "CA",
        "currency": "CAD",
        "ebayDomain": "www.ebay.ca",
        "queryLocale": "en-CA",
    },
    "eu": {
        "market": "EU",
        "country": "EU",
        "currency": "EUR",
        "ebayDomain": "www.ebay.ie",
        "queryLocale": "en-IE",
    },
}


class MarketPricingError(RuntimeError):
    """Raised for market-pricing input or processing errors."""


@dataclass(frozen=True)
class MarketPriceJob:
    game: str
    language: str
    market: str
    currency: str
    canonical_card_id: str
    set_id: str
    set_name: str
    collector_number: str
    card_name: str
    variant: str
    condition: str
    graded_state: str


@dataclass(frozen=True)
class SoldListingEvidence:
    listing_id: str
    listing_url: str
    title: str
    sold_price: float
    shipping_price: float
    currency: str
    sold_at_utc: str
    shipping_included: bool

    @property
    def total_price(self) -> float:
        return round(self.sold_price + self.shipping_price, 2)


@dataclass(frozen=True)
class ProviderListingsResult:
    provider_name: str
    source: str
    listings: list[SoldListingEvidence]
    notes: str


class MarketListingsProvider(Protocol):
    name: str

    def fetch(self, job: MarketPriceJob, query_text: str) -> ProviderListingsResult:
        """Fetch sold-listing style evidence for a market-price job."""


@dataclass
class MarketPriceAggregate:
    status: str
    sample_count: int
    median_price: float | None
    average_price: float | None
    low_price: float | None
    high_price: float | None
    sold_date_from: str | None
    sold_date_to: str | None
    evidence_links: list[str]
    confidence_score: float
    confidence_label: str
    outlier_filtering_notes: str
    shipping_included: bool


class MarketPricingJobQueue:
    """A tiny in-memory queue with deterministic ordering."""

    def __init__(self, jobs: list[MarketPriceJob]) -> None:
        self._jobs = list(jobs)

    def take(self, max_jobs: int) -> list[MarketPriceJob]:
        if max_jobs <= 0:
            return []
        taken = self._jobs[:max_jobs]
        self._jobs = self._jobs[max_jobs:]
        return taken

    @property
    def remaining(self) -> int:
        return len(self._jobs)


class MockMarketListingsProvider:
    """Deterministic provider for safe non-live worker validation."""

    name = "mock"

    def __init__(self, *, now_utc: datetime | None = None) -> None:
        self._now_utc = now_utc or datetime.now(timezone.utc)

    def fetch(self, job: MarketPriceJob, query_text: str) -> ProviderListingsResult:
        name_key = normalize_name(job.card_name)
        if name_key not in {"charizard", "pikachu", "mewtwo", "umbreon"}:
            return ProviderListingsResult(
                provider_name=self.name,
                source="ebay_sold_listings",
                listings=[],
                notes="No deterministic mock fixture for this card.",
            )

        digest = hashlib.sha256(
            f"{job.market}|{job.language}|{job.canonical_card_id}|{job.variant}|{job.condition}|{job.graded_state}".encode("utf-8")
        ).hexdigest()
        seed = int(digest[:8], 16)
        base = 40.0 + (seed % 120)
        spread = 3.0 + (seed % 7)

        prices = [
            round(base - spread, 2),
            round(base, 2),
            round(base + spread, 2),
            round(base + spread + 1.5, 2),
            round(base + spread * 2.0, 2),
        ]
        shipping = [0.0, 2.99, 0.0, 4.5, 0.0]

        listings: list[SoldListingEvidence] = []
        for idx, sold_price in enumerate(prices, start=1):
            sold_dt = self._now_utc.replace(microsecond=0) - timedelta(days=idx + 1)
            listing_id = f"mock-{digest[:10]}-{idx}"
            listings.append(
                SoldListingEvidence(
                    listing_id=listing_id,
                    listing_url=f"https://{market_config(job.market)['ebayDomain']}/itm/{listing_id}",
                    title=f"{job.card_name} {job.set_name} {job.collector_number} {job.variant} {job.condition}",
                    sold_price=float(sold_price),
                    shipping_price=float(shipping[idx - 1]),
                    currency=job.currency,
                    sold_at_utc=sold_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    shipping_included=shipping[idx - 1] == 0.0,
                )
            )

        return ProviderListingsResult(
            provider_name=self.name,
            source="ebay_sold_listings",
            listings=listings,
            notes=f"Deterministic mock listings for query: {query_text}",
        )


class ManualMarketListingsProvider:
    """Manual provider that reads sold listing evidence from a JSON file."""

    name = "manual"

    def __init__(self, manual_json_path: Path | None = None) -> None:
        self._manual_json_path = manual_json_path or DEFAULT_MANUAL_PROVIDER_PATH

    def fetch(self, job: MarketPriceJob, query_text: str) -> ProviderListingsResult:
        payload = try_load_json(self._manual_json_path)
        if payload is None:
            return ProviderListingsResult(
                provider_name=self.name,
                source="ebay_sold_listings",
                listings=[],
                notes="Manual listing file not found; returning no results.",
            )

        rows = payload if isinstance(payload, list) else payload.get("listings")
        if not isinstance(rows, list):
            rows = []

        listings: list[SoldListingEvidence] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if not _manual_row_matches_job(row, job):
                continue
            try:
                listings.append(
                    SoldListingEvidence(
                        listing_id=str(row.get("listingId") or row.get("id") or "manual"),
                        listing_url=str(row.get("listingUrl") or row.get("url") or ""),
                        title=str(row.get("title") or job.card_name),
                        sold_price=float(row.get("soldPrice") or 0.0),
                        shipping_price=float(row.get("shippingPrice") or 0.0),
                        currency=str(row.get("currency") or job.currency),
                        sold_at_utc=str(row.get("soldAtUtc") or row.get("soldDate") or utc_now_iso()),
                        shipping_included=bool(row.get("shippingIncluded") is True),
                    )
                )
            except (TypeError, ValueError):
                continue

        return ProviderListingsResult(
            provider_name=self.name,
            source="ebay_sold_listings",
            listings=listings,
            notes=f"Loaded {len(listings)} manual listing rows for query: {query_text}",
        )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_market(value: str) -> str:
    market = value.strip().lower()
    if market in {"eu_global", "euglobal"}:
        market = "eu"
    if market not in MARKET_CONFIG:
        known = ", ".join(sorted(MARKET_CONFIG.keys()))
        raise MarketPricingError(f"Unsupported market '{value}'. Expected one of: {known}")
    return market


def market_config(market: str) -> dict[str, str]:
    return MARKET_CONFIG[normalize_market(market)]


def normalize_name(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in value).strip("_")


def build_market_query(job: MarketPriceJob, *, include_damaged: bool = False) -> str:
    tokens: list[str] = [
        f'"{job.card_name}"',
        f'"{job.set_name}"',
        f'"{job.collector_number}"',
        "pokemon",
        "card",
        job.language,
        job.variant,
    ]
    if job.graded_state == "graded":
        tokens.extend(["graded", "psa", "bgs"])
    else:
        tokens.append("raw")

    if job.condition:
        tokens.append(job.condition.replace("_", " "))

    exclusions = list(BANNED_TERMS)
    if not include_damaged and job.condition.lower() != "damaged":
        exclusions.append("damaged")

    query = " ".join(tokens + [f"-{item}" for item in exclusions])
    return " ".join(part for part in query.split() if part)


def load_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def try_load_json(path: Path) -> Any:
    try:
        return load_json(path)
    except (OSError, json.JSONDecodeError):
        return None


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.tmp")
    with open(temp_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(temp_path, path)


def iter_catalog_cards(
    *,
    root: Path,
    game: str,
    language: str,
    card_id: str | None = None,
    set_id: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    cards_root = root / "public" / "v1" / "catalog" / game / language / "cards"
    if not cards_root.exists():
        return []

    paths: list[Path]
    if set_id:
        single = cards_root / f"{set_id}.json"
        paths = [single] if single.exists() else []
    else:
        paths = sorted(cards_root.glob("*.json"), key=lambda p: p.name.lower())

    rows: list[dict[str, Any]] = []
    for path in paths:
        payload = try_load_json(path)
        cards = payload.get("cards") if isinstance(payload, dict) else None
        if not isinstance(cards, list):
            continue
        for card in cards:
            if not isinstance(card, dict):
                continue
            canonical = str(card.get("canonicalBaseId") or "")
            if card_id and canonical != card_id:
                continue
            rows.append(card)
            if limit is not None and limit > 0 and len(rows) >= limit:
                return rows
    return rows


def build_jobs(
    *,
    cards: Iterable[dict[str, Any]],
    game: str,
    language: str,
    market: str,
    condition: str,
    variant: str,
    graded_state: str,
) -> list[MarketPriceJob]:
    cfg = market_config(market)
    jobs: list[MarketPriceJob] = []

    for card in cards:
        canonical = str(card.get("canonicalBaseId") or "").strip()
        set_id = str(card.get("setId") or "").strip()
        set_name = str(card.get("setName") or "").strip()
        collector = str(card.get("collectorNumber") or "").strip()
        card_name = str(card.get("name") or "").strip()
        if not all([canonical, set_id, collector, card_name]):
            continue

        jobs.append(
            MarketPriceJob(
                game=game,
                language=language,
                market=cfg["market"],
                currency=cfg["currency"],
                canonical_card_id=canonical,
                set_id=set_id,
                set_name=set_name,
                collector_number=collector,
                card_name=card_name,
                variant=variant,
                condition=condition,
                graded_state=graded_state,
            )
        )

    return jobs


def aggregate_evidence_listings(listings: list[Any]) -> MarketPriceAggregate:
    """
    Aggregate MarketPriceEvidenceListing objects into a MarketPriceAggregate.

    Accepts any objects that have ``total_price`` (float), ``listing_url`` (str),
    ``sold_date`` (str ISO-8601), and ``shipping_price`` (float) attributes —
    matching the MarketPriceEvidenceListing dataclass from
    market_pricing_provider_contracts.
    """
    if not listings:
        return MarketPriceAggregate(
            status="no_results",
            sample_count=0,
            median_price=None,
            average_price=None,
            low_price=None,
            high_price=None,
            sold_date_from=None,
            sold_date_to=None,
            evidence_links=[],
            confidence_score=0.0,
            confidence_label="low",
            outlier_filtering_notes="No sold-listing evidence returned.",
            shipping_included=False,
        )

    totals = [item.total_price for item in listings]
    filtered_totals = sorted(_drop_outliers_iqr(totals))
    used_outlier_filter = len(filtered_totals) != len(totals)
    if not filtered_totals:
        filtered_totals = sorted(totals)

    sample_count = len(filtered_totals)
    status = "priced" if sample_count >= 3 else "insufficient_data"

    confidence_score = min(1.0, round(0.25 + (sample_count * 0.12), 2))
    confidence_label = confidence_from_score(confidence_score)
    sold_dates = sorted(item.sold_date for item in listings if item.sold_date)

    return MarketPriceAggregate(
        status=status,
        sample_count=sample_count,
        median_price=round(float(median(filtered_totals)), 2),
        average_price=round(float(mean(filtered_totals)), 2),
        low_price=round(float(min(filtered_totals)), 2),
        high_price=round(float(max(filtered_totals)), 2),
        sold_date_from=sold_dates[0] if sold_dates else None,
        sold_date_to=sold_dates[-1] if sold_dates else None,
        evidence_links=[item.listing_url for item in listings][:25],
        confidence_score=confidence_score,
        confidence_label=confidence_label,
        outlier_filtering_notes=(
            "Applied IQR outlier filter to sold totals." if used_outlier_filter else "No outliers removed."
        ),
        shipping_included=all(getattr(item, "shipping_price", 1.0) == 0.0 for item in listings),
    )


def aggregate_listings(listings: list[SoldListingEvidence]) -> MarketPriceAggregate:
    if not listings:
        return MarketPriceAggregate(
            status="no_results",
            sample_count=0,
            median_price=None,
            average_price=None,
            low_price=None,
            high_price=None,
            sold_date_from=None,
            sold_date_to=None,
            evidence_links=[],
            confidence_score=0.0,
            confidence_label="low",
            outlier_filtering_notes="No sold-listing evidence returned.",
            shipping_included=False,
        )

    totals = [item.total_price for item in listings]
    filtered_totals = sorted(_drop_outliers_iqr(totals))
    used_outlier_filter = len(filtered_totals) != len(totals)
    if not filtered_totals:
        filtered_totals = sorted(totals)

    sample_count = len(filtered_totals)
    status = "priced" if sample_count >= 3 else "insufficient_data"

    confidence_score = min(1.0, round(0.25 + (sample_count * 0.12), 2))
    confidence_label = confidence_from_score(confidence_score)
    sold_dates = sorted(item.sold_at_utc for item in listings if item.sold_at_utc)

    return MarketPriceAggregate(
        status=status,
        sample_count=sample_count,
        median_price=round(float(median(filtered_totals)), 2),
        average_price=round(float(mean(filtered_totals)), 2),
        low_price=round(float(min(filtered_totals)), 2),
        high_price=round(float(max(filtered_totals)), 2),
        sold_date_from=sold_dates[0] if sold_dates else None,
        sold_date_to=sold_dates[-1] if sold_dates else None,
        evidence_links=[item.listing_url for item in listings][:25],
        confidence_score=confidence_score,
        confidence_label=confidence_label,
        outlier_filtering_notes=(
            "Applied IQR outlier filter to sold totals." if used_outlier_filter else "No outliers removed."
        ),
        shipping_included=all(item.shipping_included for item in listings),
    )


def confidence_from_score(score: float) -> str:
    if score >= 0.8:
        return "high"
    if score >= 0.5:
        return "medium"
    return "low"


def _drop_outliers_iqr(values: list[float]) -> list[float]:
    if len(values) < 4:
        return list(values)
    sorted_values = sorted(values)
    q1 = sorted_values[len(sorted_values) // 4]
    q3 = sorted_values[(len(sorted_values) * 3) // 4]
    iqr = q3 - q1
    lower = q1 - (1.5 * iqr)
    upper = q3 + (1.5 * iqr)
    return [value for value in sorted_values if lower <= value <= upper]


def _manual_row_matches_job(row: dict[str, Any], job: MarketPriceJob) -> bool:
    row_canonical = str(row.get("canonicalCardId") or "").strip()
    row_set_id = str(row.get("setId") or "").strip()
    row_market = str(row.get("market") or row.get("marketCountry") or "").strip().upper()
    row_language = str(row.get("language") or "").strip().lower()

    if row_canonical and row_canonical != job.canonical_card_id:
        return False
    if row_set_id and row_set_id != job.set_id:
        return False
    if row_market and row_market != job.market:
        return False
    if row_language and row_language != job.language:
        return False
    return True
