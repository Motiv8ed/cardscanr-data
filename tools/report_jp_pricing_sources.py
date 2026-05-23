#!/usr/bin/env python3
"""Audit whether JP prices can be built from existing non-eBay local sources."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
PUBLIC_V1 = ROOT / "public" / "v1"
REPORT_JSON = ROOT / "reports" / "jp_pricing_source_audit_latest.json"
REPORT_MD = ROOT / "reports" / "jp_pricing_source_audit_latest.md"

POKEWALLET_PROVIDER_JP_DIR = PUBLIC_V1 / "provider-catalog" / "pokewallet" / "cards" / "jp"
POKEWALLET_PROVIDER_EN_DIR = PUBLIC_V1 / "provider-catalog" / "pokewallet" / "cards" / "en"
TCGDEX_JP_CATALOG_DIR = PUBLIC_V1 / "catalog" / "pokemon" / "jp" / "cards"
PRICES_CURRENT_JP_DIR = PUBLIC_V1 / "prices" / "current" / "pokemon" / "jp"
JP_SAMPLE_PATH = PUBLIC_V1 / "prices" / "pokemon" / "jp" / "sample.json"
HISTORY_DAILY_ROOT = PUBLIC_V1 / "history" / "daily"
TRACKED_HISTORY_PATH = PUBLIC_V1 / "history" / "tracked-cards.json"
PRICES_STATUS_PATH = PUBLIC_V1 / "prices" / "status.json"
JP_STATUS_PATH = PRICES_CURRENT_JP_DIR / "status.json"
BUILD_PRICE_CACHE_PATH = ROOT / "tools" / "build_price_cache.py"
BUILD_PW_JP_PATH = ROOT / "tools" / "build_pokewallet_jp_prices.py"

PRICE_LIKE_KEYS = {
    "price",
    "prices",
    "marketprice",
    "lowprice",
    "highprice",
    "avgprice",
    "saleprice",
    "value",
    "currency",
    "yen",
    "jpy",
    "aud",
    "usd",
    "cardmarket",
    "tcgplayer",
    "pokewallet",
}

NUMERIC_PRICE_KEYS = {
    "marketprice",
    "lowprice",
    "highprice",
    "avgprice",
    "saleprice",
    "price",
    "value",
    "mid",
    "market",
    "trend",
    "avg",
    "low",
    "high",
    "mid_price",
    "market_price",
    "low_price",
    "high_price",
}


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def try_load_json(path: Path) -> Any | None:
    try:
        return load_json(path)
    except (OSError, json.JSONDecodeError):
        return None


def iter_json_files(path: Path):
    if not path.exists():
        return
    for file_path in sorted(path.glob("*.json"), key=lambda p: p.name.lower()):
        yield file_path


def walk_price_signals(node: Any, *, key_counts: Counter[str], numeric_hits: list[dict[str, Any]], currencies: Counter[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            key_norm = str(key).strip().lower()
            if key_norm in PRICE_LIKE_KEYS:
                key_counts[key_norm] += 1
            if key_norm in NUMERIC_PRICE_KEYS and isinstance(value, (int, float)):
                numeric_hits.append({"key": key_norm, "value": value})
            if key_norm == "currency" and isinstance(value, str) and value.strip():
                currencies[value.strip().upper()] += 1
            walk_price_signals(value, key_counts=key_counts, numeric_hits=numeric_hits, currencies=currencies)
    elif isinstance(node, list):
        for item in node:
            walk_price_signals(item, key_counts=key_counts, numeric_hits=numeric_hits, currencies=currencies)


def scan_pokewallet_provider_cards(path: Path) -> dict[str, Any]:
    set_file_count = 0
    card_count = 0
    has_price_flag_count = 0
    has_cardmarket_flag_count = 0
    has_tcgplayer_flag_count = 0
    key_counts: Counter[str] = Counter()
    numeric_hits: list[dict[str, Any]] = []
    currencies: Counter[str] = Counter()

    for file_path in iter_json_files(path):
        payload = try_load_json(file_path)
        if not isinstance(payload, dict):
            continue
        cards = payload.get("cards")
        if not isinstance(cards, list):
            continue
        set_file_count += 1
        card_count += len(cards)

        for card in cards:
            if not isinstance(card, dict):
                continue
            if bool(card.get("hasPriceFields")):
                has_price_flag_count += 1
            if bool(card.get("hasCardmarketFields")):
                has_cardmarket_flag_count += 1
            if bool(card.get("hasTcgplayerFields")):
                has_tcgplayer_flag_count += 1
            walk_price_signals(card, key_counts=key_counts, numeric_hits=numeric_hits, currencies=currencies)

    return {
        "setFileCount": set_file_count,
        "cardCount": card_count,
        "hasPriceFieldsFlagCount": has_price_flag_count,
        "hasCardmarketFieldsFlagCount": has_cardmarket_flag_count,
        "hasTcgplayerFieldsFlagCount": has_tcgplayer_flag_count,
        "numericPriceLikeValueCount": len(numeric_hits),
        "currencyCounts": dict(sorted(currencies.items())),
        "topPriceLikeKeys": dict(sorted(key_counts.items(), key=lambda item: (-item[1], item[0]))[:20]),
        "usableNumericPriceFieldsPresent": len(numeric_hits) > 0,
    }


def scan_tcgdex_jp_catalog() -> dict[str, Any]:
    set_file_count = 0
    card_count = 0
    pricing_refs_cardmarket_true = 0
    pricing_refs_tcgplayer_true = 0
    key_counts: Counter[str] = Counter()
    numeric_hits: list[dict[str, Any]] = []
    currencies: Counter[str] = Counter()

    for file_path in iter_json_files(TCGDEX_JP_CATALOG_DIR):
        payload = try_load_json(file_path)
        if not isinstance(payload, dict):
            continue
        cards = payload.get("cards")
        if not isinstance(cards, list):
            continue
        set_file_count += 1
        card_count += len(cards)
        for card in cards:
            if not isinstance(card, dict):
                continue
            refs = card.get("pricingReferences") if isinstance(card.get("pricingReferences"), dict) else {}
            if bool(refs.get("cardmarketAvailable")):
                pricing_refs_cardmarket_true += 1
            if bool(refs.get("tcgplayerAvailable")):
                pricing_refs_tcgplayer_true += 1
            walk_price_signals(card, key_counts=key_counts, numeric_hits=numeric_hits, currencies=currencies)

    return {
        "setFileCount": set_file_count,
        "cardCount": card_count,
        "pricingReferencesCardmarketTrueCount": pricing_refs_cardmarket_true,
        "pricingReferencesTcgplayerTrueCount": pricing_refs_tcgplayer_true,
        "numericPriceLikeValueCount": len(numeric_hits),
        "currencyCounts": dict(sorted(currencies.items())),
        "topPriceLikeKeys": dict(sorted(key_counts.items(), key=lambda item: (-item[1], item[0]))[:20]),
        "usableNumericPriceFieldsPresent": len(numeric_hits) > 0,
    }


def collect_price_records(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    prices = payload.get("prices")
    if not isinstance(prices, list):
        return []
    return [item for item in prices if isinstance(item, dict)]


def scan_local_jp_price_values() -> dict[str, Any]:
    source_counts: Counter[str] = Counter()
    currency_counts: Counter[str] = Counter()
    record_count = 0
    files_with_records: list[str] = []

    if PRICES_CURRENT_JP_DIR.exists():
        for file_path in sorted(PRICES_CURRENT_JP_DIR.glob("*.json"), key=lambda p: p.name.lower()):
            if file_path.name == "status.json":
                continue
            records = collect_price_records(try_load_json(file_path))
            if not records:
                continue
            files_with_records.append(str(file_path.relative_to(ROOT).as_posix()))
            for item in records:
                record_count += 1
                source_counts[str(item.get("source") or "unknown")] += 1
                currency = str(item.get("currency") or "").strip().upper()
                if currency:
                    currency_counts[currency] += 1

    for candidate in [JP_SAMPLE_PATH, TRACKED_HISTORY_PATH]:
        records = collect_price_records(try_load_json(candidate))
        if records:
            files_with_records.append(str(candidate.relative_to(ROOT).as_posix()))
        for item in records:
            if str(item.get("language") or "jp").lower() != "jp" and "|jp|" not in str(item.get("canonicalId") or ""):
                continue
            record_count += 1
            source_counts[str(item.get("source") or "unknown")] += 1
            currency = str(item.get("currency") or "").strip().upper()
            if currency:
                currency_counts[currency] += 1

    if HISTORY_DAILY_ROOT.exists():
        for file_path in sorted(HISTORY_DAILY_ROOT.glob("*/pokemon/jp/tracked.json"), key=lambda p: p.as_posix()):
            records = collect_price_records(try_load_json(file_path))
            if records:
                files_with_records.append(str(file_path.relative_to(ROOT).as_posix()))
            for item in records:
                record_count += 1
                source_counts[str(item.get("source") or "unknown")] += 1
                currency = str(item.get("currency") or "").strip().upper()
                if currency:
                    currency_counts[currency] += 1

    files_with_records = sorted(set(files_with_records))

    return {
        "jpPriceRecordCountAcrossLocalFiles": record_count,
        "sourceCounts": dict(sorted(source_counts.items())),
        "currencyCounts": dict(sorted(currency_counts.items())),
        "jpyRecordCount": int(currency_counts.get("JPY", 0)),
        "filesWithRecords": files_with_records,
    }


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def analyze_builder_behavior() -> dict[str, Any]:
    build_cache_text = read_text(BUILD_PRICE_CACHE_PATH)
    build_pw_text = read_text(BUILD_PW_JP_PATH)

    return {
        "mainBuilderMarksJpUnavailable": "Japanese current price cache is not available yet." in build_cache_text,
        "mainBuilderExplicitSkipStatus": "skipped_no_set_level_pricing" in build_cache_text,
        "mainBuilderWritesJpStatusFile": "write_json(JP_CURRENT_STATUS_PATH" in build_cache_text,
        "hasDedicatedPokewalletJpBuilder": BUILD_PW_JP_PATH.exists(),
        "dedicatedBuilderExtractsTcgplayer": "extract_tcgplayer_prices" in build_pw_text,
        "dedicatedBuilderExtractsCardmarket": "extract_cardmarket_prices" in build_pw_text,
        "dedicatedBuilderRequiresApiKey": "POKEWALLET_API_KEY" in build_pw_text,
        "dedicatedBuilderPartOfMainPipeline": "build_pokewallet_jp_prices" in build_cache_text,
    }


def current_jp_status_snapshot() -> dict[str, Any]:
    prices_status = try_load_json(PRICES_STATUS_PATH)
    jp_status = try_load_json(JP_STATUS_PATH)

    prices_jp = {}
    if isinstance(prices_status, dict):
        languages = prices_status.get("languages")
        if isinstance(languages, dict) and isinstance(languages.get("jp"), dict):
            prices_jp = languages.get("jp")

    return {
        "pricesStatusLanguagesJp": prices_jp,
        "jpCurrentStatusFile": jp_status if isinstance(jp_status, dict) else {},
    }


def build_report() -> dict[str, Any]:
    generated_at = now_utc()

    pokewallet_jp = scan_pokewallet_provider_cards(POKEWALLET_PROVIDER_JP_DIR)
    pokewallet_en = scan_pokewallet_provider_cards(POKEWALLET_PROVIDER_EN_DIR)
    tcgdex_jp = scan_tcgdex_jp_catalog()
    local_jp_values = scan_local_jp_price_values()
    builder = analyze_builder_behavior()
    status_snapshot = current_jp_status_snapshot()

    jp_before = int((status_snapshot.get("pricesStatusLanguagesJp") or {}).get("currentPriceRecordCount") or 0)

    jp_data_exists_in_local_provider_files = bool(pokewallet_jp.get("usableNumericPriceFieldsPresent") or tcgdex_jp.get("usableNumericPriceFieldsPresent"))

    conclusion = {
        "pokewalletJpHasUsablePriceFields": bool(pokewallet_jp.get("usableNumericPriceFieldsPresent")),
        "tcgdexJpHasUsablePriceFields": bool(tcgdex_jp.get("usableNumericPriceFieldsPresent")),
        "localFilesHaveAnyJpPriceLikeValues": int(local_jp_values.get("jpPriceRecordCountAcrossLocalFiles") or 0) > 0,
        "localFilesHaveJpyRecords": int(local_jp_values.get("jpyRecordCount") or 0) > 0,
        "jpPricingUnavailableBecauseSourceDataMissing": not jp_data_exists_in_local_provider_files,
        "mainPriceBuilderSkipsJpBecauseMissingDataNotMissingCode": bool(builder.get("mainBuilderExplicitSkipStatus")),
        "recommendedAction": (
            "Keep JP as pricing unavailable in main pipeline. Do not fabricate prices. "
            "Use dedicated Pokewallet JP builder only when API-backed priced fields are actually returned."
        ),
    }

    return {
        "schemaVersion": "1.0.0",
        "generatedAtUtc": generated_at,
        "scope": {
            "excluded": ["ebay", "web_scraping"],
            "directoriesScanned": [
                "public/v1/provider-catalog/pokewallet/cards/jp",
                "public/v1/provider-catalog/pokewallet/cards/en",
                "public/v1/catalog/pokemon/jp/cards",
                "public/v1/prices/current/pokemon/jp",
                "public/v1/prices/pokemon/jp",
                "public/v1/history",
                "data",
                "tools/build_price_cache.py",
                "tools/build_pokewallet_jp_prices.py",
            ],
            "keywords": sorted(PRICE_LIKE_KEYS),
        },
        "findings": {
            "pokewalletProviderJp": pokewallet_jp,
            "pokewalletProviderEn": pokewallet_en,
            "tcgdexJpCatalogue": tcgdex_jp,
            "localJpPriceLikeValues": local_jp_values,
            "builderBehavior": builder,
            "statusSnapshot": status_snapshot,
        },
        "conclusion": conclusion,
        "recordCounts": {
            "jpCurrentPriceRecordCountBefore": jp_before,
            "jpCurrentPriceRecordCountAfter": jp_before,
        },
    }


def markdown_report(payload: dict[str, Any]) -> str:
    findings = payload.get("findings", {})
    conclusion = payload.get("conclusion", {})
    counts = payload.get("recordCounts", {})

    pokewallet_jp = findings.get("pokewalletProviderJp", {})
    tcgdex_jp = findings.get("tcgdexJpCatalogue", {})
    local_values = findings.get("localJpPriceLikeValues", {})
    builder = findings.get("builderBehavior", {})

    lines: list[str] = []
    lines.append("# JP Pricing Source Audit")
    lines.append("")
    lines.append(f"Generated at UTC: {payload.get('generatedAtUtc')}")
    lines.append("")
    lines.append("## Key Answers")
    lines.append("")
    lines.append(f"- Pokewallet JP provider files have usable numeric price fields: **{conclusion.get('pokewalletJpHasUsablePriceFields')}**")
    lines.append(f"- TCGdex JP catalogue files have usable numeric price fields: **{conclusion.get('tcgdexJpHasUsablePriceFields')}**")
    lines.append(f"- Any local JP price-like values exist: **{conclusion.get('localFilesHaveAnyJpPriceLikeValues')}**")
    lines.append(f"- Any local JP JPY records exist: **{conclusion.get('localFilesHaveJpyRecords')}**")
    lines.append(f"- JP unavailable due to missing source data in local non-eBay files: **{conclusion.get('jpPricingUnavailableBecauseSourceDataMissing')}**")
    lines.append(f"- Main builder skip is explicit data-path decision: **{conclusion.get('mainPriceBuilderSkipsJpBecauseMissingDataNotMissingCode')}**")
    lines.append("")
    lines.append("## Evidence Summary")
    lines.append("")
    lines.append("### Pokewallet JP provider catalog")
    lines.append(f"- Set files: {pokewallet_jp.get('setFileCount', 0)}")
    lines.append(f"- Cards scanned: {pokewallet_jp.get('cardCount', 0)}")
    lines.append(f"- hasPriceFields=true count: {pokewallet_jp.get('hasPriceFieldsFlagCount', 0)}")
    lines.append(f"- Numeric price-like values found: {pokewallet_jp.get('numericPriceLikeValueCount', 0)}")
    lines.append("")
    lines.append("### TCGdex JP catalog files")
    lines.append(f"- Set files: {tcgdex_jp.get('setFileCount', 0)}")
    lines.append(f"- Cards scanned: {tcgdex_jp.get('cardCount', 0)}")
    lines.append(f"- pricingReferences.cardmarketAvailable=true count: {tcgdex_jp.get('pricingReferencesCardmarketTrueCount', 0)}")
    lines.append(f"- pricingReferences.tcgplayerAvailable=true count: {tcgdex_jp.get('pricingReferencesTcgplayerTrueCount', 0)}")
    lines.append(f"- Numeric price-like values found: {tcgdex_jp.get('numericPriceLikeValueCount', 0)}")
    lines.append("")
    lines.append("### Existing JP local price-like records")
    lines.append(f"- JP records across local files: {local_values.get('jpPriceRecordCountAcrossLocalFiles', 0)}")
    lines.append(f"- Currency counts: {local_values.get('currencyCounts', {})}")
    lines.append(f"- Source counts: {local_values.get('sourceCounts', {})}")
    lines.append(f"- Files with records: {local_values.get('filesWithRecords', [])}")
    lines.append("")
    lines.append("### Builder behavior")
    lines.append(f"- Main builder explicit skip status token present: {builder.get('mainBuilderExplicitSkipStatus')}")
    lines.append(f"- Dedicated Pokewallet JP builder exists: {builder.get('hasDedicatedPokewalletJpBuilder')}")
    lines.append(f"- Dedicated builder is wired into main build pipeline: {builder.get('dedicatedBuilderPartOfMainPipeline')}")
    lines.append("")
    lines.append("## JP Current Price Count")
    lines.append("")
    lines.append(f"- Before: {counts.get('jpCurrentPriceRecordCountBefore', 0)}")
    lines.append(f"- After: {counts.get('jpCurrentPriceRecordCountAfter', 0)}")
    lines.append("")
    lines.append("## Recommendation")
    lines.append("")
    lines.append(f"- {conclusion.get('recommendedAction')}")

    return "\n".join(lines) + "\n"


def main() -> int:
    payload = build_report()

    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(REPORT_JSON, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")

    md = markdown_report(payload)
    REPORT_MD.write_text(md, encoding="utf-8", newline="\n")

    print(f"wrote {REPORT_JSON.relative_to(ROOT).as_posix()}")
    print(f"wrote {REPORT_MD.relative_to(ROOT).as_posix()}")
    print("JP pricing source audit complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
