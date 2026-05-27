#!/usr/bin/env python3
"""
import_manual_sold_listings.py

Manual sold-listing import pipeline for CardScanR market pricing.

Reads manually exported/collected sold-listing CSV or JSON data, normalises,
filters, matches, aggregates, and optionally writes market price files.

Does NOT scrape eBay live. Does NOT call any external API. Compatible with the
existing market pricing worker foundation.

Usage:
  python tools/import_manual_sold_listings.py \\
      --market AU --language en \\
      --input data/manual_market_prices/examples/sample_sold_listings.csv \\
      --dry-run --commit-safe-report

  python tools/import_manual_sold_listings.py \\
      --market AU --language en \\
      --input data/manual_market_prices/examples/sample_sold_listings.csv \\
      --write --commit-safe-report
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR_DEFAULT = ROOT / "reports"
MARKET_PRICES_ROOT = ROOT / "public" / "v1" / "markets" / "prices"
MARKET_STATUS_PATH = ROOT / "public" / "v1" / "markets" / "market-price-status.json"

SOURCE_ID = "ebay_sold_listings_manual"

# ---------------------------------------------------------------------------
# Secret / suspicious field redaction
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = re.compile(
    r"(api[_\-]?key|token|secret|password|credential|bearer|auth|access[_\-]?key)",
    re.IGNORECASE,
)


def _redact_suspicious(value: str) -> str:
    if _SECRET_PATTERNS.search(value):
        return "[REDACTED]"
    return value


# ---------------------------------------------------------------------------
# Exclusion / filter terms
# ---------------------------------------------------------------------------

EXCLUSION_TERMS: dict[str, tuple[str, ...]] = {
    "proxy": (" proxy ", " custom ", " fan art ", " fanart ", " alter "),
    "fake": (" fake ", " replica ", " counterfeit "),
    "digital": (" digital ", " online code ", " ptcgo ", " code card ", " ptcgl "),
    "lot_or_bundle": (" lot ", " bundle ", " x2 ", " x3 ", " playset ", " collection "),
}

# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------


def _norm_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    return re.sub(r"\s+", " ", text)


def _contains_any(haystack: str, needles: tuple[str, ...]) -> bool:
    return any(needle in haystack for needle in needles)


CONDITION_MAP: dict[str, str] = {
    "nm": "near_mint",
    "near_mint": "near_mint",
    "near mint": "near_mint",
    "nearmint": "near_mint",
    "mint": "near_mint",
    "m": "near_mint",
    "lp": "lightly_played",
    "lightly_played": "lightly_played",
    "lightly played": "lightly_played",
    "lightlyplayed": "lightly_played",
    "ex": "lightly_played",
    "excellent": "lightly_played",
    "mp": "moderately_played",
    "moderately_played": "moderately_played",
    "moderately played": "moderately_played",
    "moderatelyplayed": "moderately_played",
    "gd": "moderately_played",
    "good": "moderately_played",
    "hp": "heavily_played",
    "heavily_played": "heavily_played",
    "heavily played": "heavily_played",
    "heavilyplayed": "heavily_played",
    "poor": "heavily_played",
    "played": "lightly_played",
    "damaged": "damaged",
    "dmg": "damaged",
    "dm": "damaged",
}

LANGUAGE_MAP: dict[str, str] = {
    "en": "en",
    "english": "en",
    "eng": "en",
    "jp": "jp",
    "ja": "jp",
    "japanese": "jp",
    "jpn": "jp",
    "zh": "zh",
    "chinese": "zh",
    "zhs": "zh",
    "zht": "zh",
    "kor": "ko",
    "ko": "ko",
    "korean": "ko",
    "de": "de",
    "german": "de",
    "fr": "fr",
    "french": "fr",
    "es": "es",
    "spanish": "es",
    "it": "it",
    "italian": "it",
    "pt": "pt",
    "portuguese": "pt",
}

CURRENCY_MAP: dict[str, str] = {
    "aud": "AUD",
    "usd": "USD",
    "gbp": "GBP",
    "cad": "CAD",
    "eur": "EUR",
    "jpy": "JPY",
    "nzd": "NZD",
    "¥": "JPY",
    "$": "USD",
    "£": "GBP",
    "€": "EUR",
}

MARKET_MAP: dict[str, str] = {
    "au": "AU",
    "australia": "AU",
    "us": "US",
    "usa": "US",
    "united states": "US",
    "gb": "GB",
    "uk": "GB",
    "united kingdom": "GB",
    "ca": "CA",
    "canada": "CA",
    "eu": "EU",
    "europe": "EU",
    "jp": "JP",
    "japan": "JP",
    "nz": "NZ",
    "new zealand": "NZ",
}

MARKETPLACE_ALIASES: dict[str, str] = {
    "ebay.com.au": "ebay_au",
    "ebay_au": "ebay_au",
    "ebay au": "ebay_au",
    "ebay.com": "ebay_us",
    "ebay_us": "ebay_us",
    "ebay us": "ebay_us",
    "ebay.co.uk": "ebay_gb",
    "ebay_gb": "ebay_gb",
    "ebay uk": "ebay_gb",
    "ebay.ca": "ebay_ca",
    "ebay_ca": "ebay_ca",
    "ebay ca": "ebay_ca",
    "ebay.co.jp": "ebay_jp",
    "ebay_jp": "ebay_jp",
    "ebay jp": "ebay_jp",
    "ebay": "ebay",
    "tcgplayer": "tcgplayer",
    "cardmarket": "cardmarket",
}


def normalize_price(value: object) -> float | None:
    if value is None:
        return None
    raw_text = str(value).strip()
    # Reject clearly negative values before stripping symbols
    stripped_for_sign = raw_text.lstrip()
    if stripped_for_sign.startswith("-"):
        return None
    text = re.sub(r"[^\d.]", "", raw_text)
    if not text:
        return None
    try:
        result = float(text)
        return result if result >= 0 else None
    except ValueError:
        return None


def normalize_currency(value: object) -> str | None:
    text = str(value or "").strip()
    return CURRENCY_MAP.get(text.lower(), text.upper() if text else None) or None


def normalize_date(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in (
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%d/%m/%Y",
        "%m/%d/%Y",
        "%d-%m-%Y",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S%z",
    ):
        try:
            dt = datetime.strptime(text[:len(fmt) + 5], fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Try ISO fromisoformat
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return None


def normalize_condition(value: object) -> str | None:
    text = _norm_text(value)
    return CONDITION_MAP.get(text)


def normalize_graded(value: object) -> str:
    text = _norm_text(value)
    if text in ("true", "yes", "1", "graded", "psa", "bgs", "cgc", "sgc"):
        return "graded"
    return "ungraded"


def normalize_language(value: object) -> str | None:
    text = _norm_text(value)
    return LANGUAGE_MAP.get(text)


def normalize_market(value: object) -> str | None:
    text = _norm_text(value)
    return MARKET_MAP.get(text)


def normalize_marketplace(value: object) -> str:
    text = _norm_text(value)
    return MARKETPLACE_ALIASES.get(text, text.replace(" ", "_"))


def normalize_variant(value: object) -> str:
    text = _norm_text(value)
    if not text or text == "raw":
        return "raw"
    if text in ("graded", "psa", "bgs", "cgc", "sgc"):
        return "graded"
    if text in ("sealed", "product", "booster"):
        return "sealed"
    return text or "raw"


# ---------------------------------------------------------------------------
# Row reading
# ---------------------------------------------------------------------------

REQUIRED_CSV_FIELDS = {
    "title", "soldPrice", "currency", "soldDate",
    "marketplace", "market", "condition", "graded",
    "cardName", "language",
}


def _read_csv(path: Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        return [dict(row) for row in reader]


def _read_json(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "rows" in data:
        return data["rows"]
    raise ValueError(f"Unrecognised JSON structure in {path}")


def read_input(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        return _read_json(path)
    return _read_csv(path)


# ---------------------------------------------------------------------------
# Row normalisation
# ---------------------------------------------------------------------------

def normalize_row(raw: dict[str, Any]) -> dict[str, Any]:
    sold_price = normalize_price(raw.get("soldPrice"))
    shipping_price = normalize_price(raw.get("shippingPrice")) or 0.0
    total_price = normalize_price(raw.get("totalPrice"))
    if total_price is None and sold_price is not None:
        total_price = round(sold_price + shipping_price, 2)

    return {
        "title": str(raw.get("title") or "").strip(),
        "soldPrice": sold_price,
        "shippingPrice": shipping_price,
        "totalPrice": total_price,
        "currency": normalize_currency(raw.get("currency")),
        "soldDate": normalize_date(raw.get("soldDate")),
        "listingUrl": str(raw.get("listingUrl") or "").strip(),
        "marketplace": normalize_marketplace(raw.get("marketplace")),
        "market": normalize_market(raw.get("market")),
        "condition": normalize_condition(raw.get("condition")),
        "graded": normalize_graded(raw.get("graded")),
        "cardName": str(raw.get("cardName") or "").strip(),
        "setName": str(raw.get("setName") or "").strip(),
        "setId": str(raw.get("setId") or "").strip().lower(),
        "collectorNumber": str(raw.get("collectorNumber") or "").strip(),
        "language": normalize_language(raw.get("language")),
        "variant": normalize_variant(raw.get("variant")),
        "canonicalId": str(raw.get("canonicalId") or "").strip(),
        "_raw": raw,
    }


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def filter_row(
    row: dict[str, Any],
    *,
    allow_lots: bool = False,
    allow_damaged: bool = False,
    filter_market: str | None = None,
    filter_language: str | None = None,
) -> tuple[bool, str | None]:
    title_padded = f" {_norm_text(row['title'])} "

    if _contains_any(title_padded, EXCLUSION_TERMS["proxy"]):
        return False, "proxy_or_custom"
    if _contains_any(title_padded, EXCLUSION_TERMS["fake"]):
        return False, "fake"
    if _contains_any(title_padded, EXCLUSION_TERMS["digital"]):
        return False, "digital"
    if not allow_lots and _contains_any(title_padded, EXCLUSION_TERMS["lot_or_bundle"]):
        return False, "lot_or_bundle"

    if row["condition"] == "damaged" and not allow_damaged:
        return False, "damaged_excluded"

    if row["totalPrice"] is None or row["totalPrice"] <= 0:
        return False, "invalid_sold_price"
    if not row["currency"]:
        return False, "missing_currency"
    if not row["soldDate"]:
        return False, "missing_sold_date"

    if filter_market and row["market"] != filter_market:
        return False, f"market_mismatch:{row['market']}"
    if filter_language and row["language"] != filter_language:
        return False, f"language_mismatch:{row['language']}"

    return True, None


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

MatchResult = dict[str, Any]


def match_row(row: dict[str, Any]) -> MatchResult:
    if row["canonicalId"]:
        return {"status": "matched_canonical", "canonicalId": row["canonicalId"]}

    card_name = row["cardName"]
    set_id = row["setId"]
    set_name = row["setName"]
    collector_number = row["collectorNumber"]
    language = row["language"] or ""
    variant = row["variant"]

    if not card_name:
        return {"status": "unmatched", "reason": "missing_card_name"}

    if not (set_id or set_name):
        return {"status": "unmatched", "reason": "missing_set_identity"}

    canonical_id = _build_canonical_id(
        language=language,
        set_id=set_id,
        collector_number=collector_number,
        card_name=card_name,
    )
    return {
        "status": "matched_derived",
        "canonicalId": canonical_id,
        "setId": set_id,
        "setName": set_name,
        "collectorNumber": collector_number,
        "cardName": card_name,
        "language": language,
        "variant": variant,
    }


def _build_canonical_id(
    *,
    language: str,
    set_id: str,
    collector_number: str,
    card_name: str,
) -> str:
    name_slug = re.sub(r"[^\w]+", "_", card_name.lower(), flags=re.UNICODE).strip("_")
    cn = re.sub(r"\s+", "", collector_number.strip().upper())
    return f"pokemon|{language}|{set_id}|{cn}|{name_slug}"


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _confidence_label(sample_count: int) -> tuple[float, str]:
    if sample_count >= 8:
        return 0.9, "high"
    if sample_count >= 3:
        return 0.6, "medium"
    return 0.2, "low"


def aggregate(accepted_rows: list[dict[str, Any]], match_results: list[MatchResult]) -> list[dict[str, Any]]:
    # Use tuple key to avoid splitting issues with | in canonical IDs
    groups: dict[tuple[str, ...], list[tuple[dict[str, Any], MatchResult]]] = {}

    for row, match in zip(accepted_rows, match_results):
        if match["status"] == "unmatched":
            continue
        canonical_id = match["canonicalId"]
        market = row["market"] or "unknown"
        language = row["language"] or "unknown"
        set_id = row["setId"] or match.get("setId") or "unknown"
        condition = row["condition"] or "unknown"
        graded = row["graded"]
        variant = row["variant"]

        key = (market, "pokemon", language, set_id, canonical_id, condition, graded, variant)
        if key not in groups:
            groups[key] = []
        groups[key].append((row, match))

    aggregates: list[dict[str, Any]] = []
    for key, items in sorted(groups.items()):
        market, _, language, set_id, canonical_id, condition, graded_state, variant = key

        prices = [item[0]["totalPrice"] for item in items if item[0]["totalPrice"] is not None]
        if not prices:
            continue

        dates = sorted([item[0]["soldDate"] for item in items if item[0]["soldDate"]])
        evidence_links = [item[0]["listingUrl"] for item in items if item[0]["listingUrl"]][:25]
        sample_count = len(prices)
        confidence_score, confidence_label = _confidence_label(sample_count)

        row0 = items[0][0]
        match0 = items[0][1]

        agg: dict[str, Any] = {
            "game": "pokemon",
            "language": language,
            "canonicalCardId": canonical_id,
            "setId": set_id,
            "setName": row0.get("setName") or match0.get("setName") or "",
            "collectorNumber": row0.get("collectorNumber") or match0.get("collectorNumber") or "",
            "cardName": row0.get("cardName") or match0.get("cardName") or "",
            "variant": variant,
            "condition": condition,
            "gradedState": graded_state,
            "marketCountry": market,
            "currency": row0["currency"] or "",
            "source": SOURCE_ID,
            "sourceProvider": "manual",
            "sampleCount": sample_count,
            "medianPrice": round(median(prices), 2),
            "averagePrice": round(mean(prices), 2),
            "lowPrice": round(min(prices), 2),
            "highPrice": round(max(prices), 2),
            "shippingIncluded": any(
                (item[0]["shippingPrice"] or 0) > 0 for item in items
            ),
            "soldDateRange": {
                "from": dates[0] if dates else None,
                "to": dates[-1] if dates else None,
            },
            "evidenceListingLinks": evidence_links,
            "confidenceScore": confidence_score,
            "confidenceLabel": confidence_label,
            "outlierFilteringNotes": None,
            "lastUpdatedAtUtc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": "priced",
        }
        aggregates.append(agg)

    return aggregates


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def write_market_price_files(
    aggregates: list[dict[str, Any]],
    *,
    market: str,
    language: str,
    now_utc: str,
    dry_run: bool,
) -> list[str]:
    by_set: dict[str, list[dict[str, Any]]] = {}
    for agg in aggregates:
        if agg["marketCountry"] != market or agg["language"] != language:
            continue
        set_id = agg["setId"]
        if set_id not in by_set:
            by_set[set_id] = []
        by_set[set_id].append(agg)

    written_paths: list[str] = []
    for set_id, records in sorted(by_set.items()):
        output_path = MARKET_PRICES_ROOT / market.lower() / "pokemon" / language / f"{set_id}.json"
        payload: dict[str, Any] = {
            "schemaVersion": "1.0.0",
            "generatedAtUtc": now_utc,
            "market": market,
            "game": "pokemon",
            "language": language,
            "setId": set_id,
            "recordCount": len(records),
            "sourceProvider": "manual",
            "source": SOURCE_ID,
            "prices": records,
        }
        rel_path = str(output_path.relative_to(ROOT)) if output_path.is_relative_to(ROOT) else str(output_path)
        written_paths.append(rel_path)
        if not dry_run:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
    return written_paths


# ---------------------------------------------------------------------------
# Status update
# ---------------------------------------------------------------------------

def update_market_price_status(now_utc: str, *, dry_run: bool) -> None:
    existing: dict[str, Any] = {}
    if MARKET_STATUS_PATH.exists():
        try:
            existing = json.loads(MARKET_STATUS_PATH.read_text(encoding="utf-8"))
        except Exception:
            existing = {}

    existing["schemaVersion"] = existing.get("schemaVersion", "1.0.0")
    existing["generatedAtUtc"] = now_utc
    existing["status"] = "enabled_manual_import"

    source_status = dict(existing.get("sourceStatus") or {})
    source_status["ebaySoldListingsManual"] = "enabled"
    source_status["liveEbayWorker"] = source_status.get("liveEbayWorker", "disabled")
    source_status["mockProvider"] = source_status.get("mockProvider", "enabled")
    source_status["manualProvider"] = "enabled"
    existing["sourceStatus"] = source_status

    existing["liveEbayWorkerStatus"] = "planned_disabled"
    existing["legalTermsReviewRequiredBeforeLiveScraping"] = True
    existing["lastManualImportAtUtc"] = now_utc
    existing["manualSoldListingImport"] = "enabled"

    notes: list[str] = [
        "Live eBay scraping is disabled and not implemented in this foundation.",
        "Manual sold-listing import is enabled via tools/import_manual_sold_listings.py.",
        "Do not overwrite EN/JP provider current prices; market prices are stored separately.",
    ]
    existing["notes"] = notes

    if not dry_run:
        MARKET_STATUS_PATH.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def build_report(
    *,
    input_path: str,
    rows_read: int,
    rows_accepted: int,
    exclusions: dict[str, int],
    matched_rows: int,
    unmatched_rows: int,
    ambiguous_rows: int,
    aggregates_built: int,
    write_targets: list[str],
    dry_run: bool,
    now_utc: str,
    market: str | None = None,
    language: str | None = None,
) -> dict[str, Any]:
    return {
        "schemaVersion": "1.0.0",
        "generatedAtUtc": now_utc,
        "inputFile": input_path,
        "mode": "dry_run" if dry_run else "write",
        "market": market,
        "language": language,
        "rowsRead": rows_read,
        "rowsAccepted": rows_accepted,
        "rowsExcluded": rows_read - rows_accepted,
        "exclusionReasons": exclusions,
        "matchedRows": matched_rows,
        "unmatchedRows": unmatched_rows,
        "ambiguousRows": ambiguous_rows,
        "aggregatesBuilt": aggregates_built,
        "writeTargets": write_targets,
        "liveEbayScrapingEnabled": False,
        "source": SOURCE_ID,
    }


def render_report_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    a = lines.append

    a("# Manual Sold Listing Import Report")
    a("")
    a(f"**Generated:** {report['generatedAtUtc']}")
    a(f"**Mode:** {report['mode']}")
    a(f"**Input file:** `{report['inputFile']}`")
    a("")
    a("## Row Counts")
    a("")
    a(f"- **Rows read:** {report['rowsRead']}")
    a(f"- **Rows accepted:** {report['rowsAccepted']}")
    a(f"- **Rows excluded:** {report['rowsExcluded']}")
    a("")
    exclusions = report.get("exclusionReasons", {})
    if exclusions:
        a("### Exclusion Reasons")
        a("")
        for reason, count in sorted(exclusions.items(), key=lambda x: -x[1]):
            a(f"  - **{reason}:** {count}")
        a("")
    a("## Matching")
    a("")
    a(f"- **Matched rows:** {report['matchedRows']}")
    a(f"- **Unmatched rows:** {report['unmatchedRows']}")
    a(f"- **Ambiguous rows:** {report['ambiguousRows']}")
    a("")
    a("## Aggregates")
    a("")
    a(f"- **Aggregates built:** {report['aggregatesBuilt']}")
    a("")
    if report.get("writeTargets"):
        a("## Write Targets")
        a("")
        for path in report["writeTargets"]:
            a(f"  - `{path}`")
        a("")
    a(f"**Live eBay scraping enabled:** no")
    a(f"**Source:** `{report['source']}`")
    a("")
    a("---")
    a("*Generated by `tools/import_manual_sold_listings.py`*")

    return "\n".join(lines)


def write_reports(
    report: dict[str, Any],
    reports_dir: Path,
    *,
    dry_run: bool,
) -> tuple[str, str]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    json_path = reports_dir / "manual_sold_listing_import_latest.json"
    md_path = reports_dir / "manual_sold_listing_import_latest.md"
    markdown = render_report_markdown(report)
    if not dry_run:
        json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        md_path.write_text(markdown, encoding="utf-8")
    return (
        str(json_path.relative_to(ROOT)) if json_path.is_relative_to(ROOT) else str(json_path),
        str(md_path.relative_to(ROOT)) if md_path.is_relative_to(ROOT) else str(md_path),
    )


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_import(
    *,
    input_path: Path,
    market: str,
    language: str,
    dry_run: bool,
    allow_lots: bool,
    allow_damaged: bool,
    max_rows: int | None,
    reports_dir: Path,
    commit_safe_report: bool,
) -> dict[str, Any]:
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Read
    raw_rows = read_input(input_path)
    if max_rows is not None:
        raw_rows = raw_rows[:max_rows]
    rows_read = len(raw_rows)

    # Normalise
    normalised = [normalize_row(r) for r in raw_rows]

    # Filter
    accepted: list[dict[str, Any]] = []
    exclusions: dict[str, int] = {}
    for row in normalised:
        ok, reason = filter_row(
            row,
            allow_lots=allow_lots,
            allow_damaged=allow_damaged,
            filter_market=market,
            filter_language=language,
        )
        if ok:
            accepted.append(row)
        else:
            key = reason or "unknown"
            exclusions[key] = exclusions.get(key, 0) + 1

    rows_accepted = len(accepted)

    # Match
    match_results = [match_row(row) for row in accepted]
    matched_rows = sum(1 for m in match_results if m["status"] != "unmatched")
    unmatched_rows = sum(1 for m in match_results if m["status"] == "unmatched")
    ambiguous_rows = 0

    # Aggregate
    agg_accepted = [r for r, m in zip(accepted, match_results) if m["status"] != "unmatched"]
    agg_matches = [m for m in match_results if m["status"] != "unmatched"]
    aggregates = aggregate(agg_accepted, agg_matches)
    aggregates_built = len(aggregates)

    # Write market price files
    write_targets = write_market_price_files(
        aggregates,
        market=market,
        language=language,
        now_utc=now_utc,
        dry_run=dry_run,
    )

    # Update status
    update_market_price_status(now_utc, dry_run=dry_run)

    # Build report
    report = build_report(
        input_path=str(input_path),
        rows_read=rows_read,
        rows_accepted=rows_accepted,
        exclusions=exclusions,
        matched_rows=matched_rows,
        unmatched_rows=unmatched_rows,
        ambiguous_rows=ambiguous_rows,
        aggregates_built=aggregates_built,
        write_targets=write_targets,
        dry_run=dry_run,
        now_utc=now_utc,
        market=market,
        language=language,
    )

    # Write reports (always write commit-safe report if requested, even in dry-run)
    if commit_safe_report:
        write_reports(report, reports_dir, dry_run=False)
    else:
        write_reports(report, reports_dir, dry_run=dry_run)

    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import manually collected sold listings into CardScanR market prices."
    )
    parser.add_argument("--market", required=True, help="Target market (e.g. AU, US, GB)")
    parser.add_argument("--language", required=True, help="Target language (e.g. en, jp, zh)")
    parser.add_argument("--input", required=True, help="Path to CSV or JSON input file")
    parser.add_argument("--write", action="store_true", help="Write output market price files")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Parse and report without writing public files",
    )
    parser.add_argument(
        "--allow-lots",
        action="store_true",
        help="Allow lot/bundle listings (excluded by default)",
    )
    parser.add_argument(
        "--allow-damaged",
        action="store_true",
        help="Allow damaged condition listings (excluded by default)",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=None,
        help="Maximum number of input rows to process",
    )
    parser.add_argument(
        "--commit-safe-report",
        action="store_true",
        help="Always write report files (even in dry-run mode) for commit/tracking",
    )
    parser.add_argument(
        "--reports-dir",
        default=str(REPORTS_DIR_DEFAULT),
        help="Directory to write report files (default: reports/)",
    )
    args = parser.parse_args()

    dry_run = args.dry_run or not args.write

    input_path = Path(args.input)
    if not input_path.is_absolute():
        input_path = ROOT / args.input
    if not input_path.exists():
        print(f"ERROR: Input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    market = (normalize_market(args.market) or args.market.upper()).upper()
    language = normalize_language(args.language) or args.language.lower()
    reports_dir = Path(args.reports_dir)

    mode_label = "DRY-RUN" if dry_run else "WRITE"
    print(f"[import_manual_sold_listings] mode={mode_label} market={market} language={language}")
    print(f"[import_manual_sold_listings] input={input_path}")

    report = run_import(
        input_path=input_path,
        market=market,
        language=language,
        dry_run=dry_run,
        allow_lots=args.allow_lots,
        allow_damaged=args.allow_damaged,
        max_rows=args.max_rows,
        reports_dir=reports_dir,
        commit_safe_report=args.commit_safe_report,
    )

    print(f"[import_manual_sold_listings] rows_read={report['rowsRead']} accepted={report['rowsAccepted']} excluded={report['rowsExcluded']}")
    print(f"[import_manual_sold_listings] matched={report['matchedRows']} unmatched={report['unmatchedRows']} aggregates={report['aggregatesBuilt']}")
    print(f"[import_manual_sold_listings] write_targets={len(report['writeTargets'])}")
    if report["writeTargets"]:
        for path in report["writeTargets"]:
            print(f"  {'(dry-run) would write' if dry_run else 'wrote'}: {path}")
    if report["exclusionReasons"]:
        print("[import_manual_sold_listings] exclusion reasons:")
        for reason, count in sorted(report["exclusionReasons"].items(), key=lambda x: -x[1]):
            print(f"  {reason}: {count}")


if __name__ == "__main__":
    main()
