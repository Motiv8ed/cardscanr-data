#!/usr/bin/env python3
"""Audit provider language coverage and app support readiness."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PROVIDER_LANG_SUMMARY_PATH = ROOT / "public" / "v1" / "provider-catalog" / "pokewallet" / "languages-summary.json"
PROVIDER_SETS_SUMMARY_PATH = ROOT / "public" / "v1" / "provider-catalog" / "pokewallet" / "sets-summary.json"
SUPPORTED_LANGUAGES_PATH = ROOT / "public" / "v1" / "supported-languages.json"
APP_CATALOG_ROOT = ROOT / "public" / "v1" / "catalog" / "pokemon"
PRICE_STATUS_ROOT = ROOT / "public" / "v1" / "prices" / "current" / "pokemon"
PIPELINE_REPORT_PATH = ROOT / "reports" / "latest_full_data_pipeline.json"
IMAGE_MANIFEST_PATH = ROOT / "public" / "v1" / "images" / "cards-manifest.json"
REPORT_JSON_PATH = ROOT / "reports" / "provider_languages_latest.json"
REPORT_MD_PATH = ROOT / "reports" / "provider_languages_latest.md"


def load_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def try_load_json(path: Path) -> Any:
    try:
        return load_json(path)
    except (OSError, json.JSONDecodeError):
        return None


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False, sort_keys=True)
        fh.write("\n")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)


def detect_promoted_languages() -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    if not APP_CATALOG_ROOT.exists():
        return result

    for language_dir in sorted([item for item in APP_CATALOG_ROOT.iterdir() if item.is_dir()], key=lambda item: item.name.lower()):
        cards_dir = language_dir / "cards"
        set_count = 0
        card_count = 0
        if cards_dir.exists():
            for path in sorted(cards_dir.glob("*.json"), key=lambda item: item.name.lower()):
                payload = try_load_json(path)
                if not isinstance(payload, dict):
                    continue
                cards = payload.get("cards")
                if not isinstance(cards, list):
                    continue
                set_count += 1
                card_count += len(cards)
        result[language_dir.name] = {
            "setCount": set_count,
            "cardCount": card_count,
        }
    return result


def detect_price_support() -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if not PRICE_STATUS_ROOT.exists():
        return result

    for language_dir in sorted([item for item in PRICE_STATUS_ROOT.iterdir() if item.is_dir()], key=lambda item: item.name.lower()):
        status_path = language_dir / "status.json"
        status = try_load_json(status_path)
        if not isinstance(status, dict):
            result[language_dir.name] = {
                "hasCurrentPriceSupport": False,
                "currentPriceRecordCount": 0,
                "currentPriceSetFileCount": 0,
            }
            continue
        result[language_dir.name] = {
            "hasCurrentPriceSupport": bool(status.get("currentPriceFilesAvailable") and int(status.get("currentPriceRecordCount") or 0) > 0),
            "currentPriceRecordCount": int(status.get("currentPriceRecordCount") or 0),
            "currentPriceSetFileCount": int(status.get("currentPriceSetFileCount") or 0),
            "status": status.get("status"),
            "source": status.get("source") or (status.get("sourceSummary") or {}).get("primarySource"),
        }
    return result


def detect_image_manifest_support() -> dict[str, dict[str, Any]]:
    pipeline = try_load_json(PIPELINE_REPORT_PATH)
    by_language: dict[str, dict[str, Any]] = {}
    image_counts: dict[str, int] = {}
    if isinstance(pipeline, dict):
        candidate = pipeline.get("imageManifestCountByLanguage")
        if isinstance(candidate, dict):
            for language, count in candidate.items():
                image_counts[str(language)] = int(count or 0)

    # Fallback to the source-of-truth manifest when pipeline counts are stale.
    manifest = try_load_json(IMAGE_MANIFEST_PATH)
    if isinstance(manifest, dict):
        language_map = manifest.get("languageCountMap")
        if isinstance(language_map, dict):
            for language, count in language_map.items():
                image_counts.setdefault(str(language), int(count or 0))

    for language, count in image_counts.items():
        by_language[str(language)] = {
            "hasImageManifestSupport": int(count) > 0,
            "imageManifestRecordCount": int(count),
        }
    return by_language


def normalize_supported_languages() -> dict[str, dict[str, Any]]:
    payload = try_load_json(SUPPORTED_LANGUAGES_PATH)
    languages = payload.get("languages") if isinstance(payload, dict) else []
    result: dict[str, dict[str, Any]] = {}
    if not isinstance(languages, list):
        return result

    for item in languages:
        if not isinstance(item, dict):
            continue
        language = str(item.get("language") or "").strip().lower()
        if not language:
            continue
        result[language] = {
            "enabled": bool(item.get("enabled")),
            "visibility": str(item.get("visibility") or "").strip(),
            "displayName": str(item.get("displayName") or language.upper()).strip(),
            "catalogueStatus": str(item.get("catalogueStatus") or "unknown").strip(),
            "pricingStatus": str(item.get("pricingStatus") or "unknown").strip(),
        }
    return result


def recommend_language(row: dict[str, Any]) -> str:
    language = row["language"]
    if row["providerCardCount"] <= 0:
        return f"No provider records found for {language}; keep language disabled."
    if not row["appSupported"]:
        return "Provider data exists but language is not app-supported. Run readiness audit before promotion."
    if row["appSupported"] and not row["promotedToAppCatalogue"]:
        return "Language is app-supported but not promoted. Run promotion after normalization checks pass."
    if row["promotedToAppCatalogue"] and not row["hasCurrentPriceSupport"]:
        return "Catalogue-only mode is active. Add a safe pricing source before enabling market pricing in-app."
    if row["promotedToAppCatalogue"] and row["hasCurrentPriceSupport"]:
        return "Language is app-ready for catalogue and prices. Keep normal validation/reporting cadence."
    return "Needs review."


def build_report() -> dict[str, Any]:
    lang_summary = try_load_json(PROVIDER_LANG_SUMMARY_PATH)
    sets_summary = try_load_json(PROVIDER_SETS_SUMMARY_PATH)

    provider_rows = lang_summary.get("languages") if isinstance(lang_summary, dict) else []
    provider_rows = provider_rows if isinstance(provider_rows, list) else []

    sets_written_by_language = (lang_summary or {}).get("setsWrittenByLanguage")
    if not isinstance(sets_written_by_language, dict):
        sets_written_by_language = {}

    provider_language_counts = (sets_summary or {}).get("languagesSeen")
    if not isinstance(provider_language_counts, dict):
        provider_language_counts = {}

    app_supported = normalize_supported_languages()
    promoted = detect_promoted_languages()
    price_support = detect_price_support()
    image_support = detect_image_manifest_support()

    known_languages: set[str] = set()
    known_languages.update(promoted.keys())
    known_languages.update(app_supported.keys())
    known_languages.update(price_support.keys())
    known_languages.update(image_support.keys())
    for row in provider_rows:
        if isinstance(row, dict):
            language = str(row.get("cardScanRLanguage") or "").strip().lower()
            if language:
                known_languages.add(language)

    provider_by_language: dict[str, dict[str, Any]] = {}
    for row in provider_rows:
        if not isinstance(row, dict):
            continue
        language = str(row.get("cardScanRLanguage") or "").strip().lower()
        if not language:
            continue
        provider_by_language[language] = row

    rows: list[dict[str, Any]] = []
    for language in sorted(known_languages):
        provider = provider_by_language.get(language, {})
        supported = app_supported.get(language, {})
        promoted_row = promoted.get(language, {})
        price_row = price_support.get(language, {})
        image_row = image_support.get(language, {})

        provider_lang_codes = provider.get("providerLanguages") if isinstance(provider.get("providerLanguages"), list) else []
        row = {
            "language": language,
            "providerLanguageCodes": sorted({str(code) for code in provider_lang_codes if str(code).strip()}),
            "providerCardCount": int(provider.get("cardsWritten") or (lang_summary or {}).get("cardsWrittenByLanguage", {}).get(language) or 0),
            "providerSetCount": int(provider.get("setCount") or sets_written_by_language.get(language) or 0),
            "providerSetFilesWritten": int(provider.get("setsWritten") or sets_written_by_language.get(language) or 0),
            "appSupported": bool(supported.get("enabled")),
            "appVisibility": supported.get("visibility") or "hidden",
            "appCatalogueStatus": supported.get("catalogueStatus") or "unknown",
            "appPricingStatus": supported.get("pricingStatus") or "unknown",
            "promotedToAppCatalogue": int(promoted_row.get("cardCount") or 0) > 0,
            "appCatalogueCardCount": int(promoted_row.get("cardCount") or 0),
            "appCatalogueSetCount": int(promoted_row.get("setCount") or 0),
            "hasImageManifestSupport": bool(image_row.get("hasImageManifestSupport")),
            "imageManifestRecordCount": int(image_row.get("imageManifestRecordCount") or 0),
            "hasCurrentPriceSupport": bool(price_row.get("hasCurrentPriceSupport")),
            "currentPriceRecordCount": int(price_row.get("currentPriceRecordCount") or 0),
            "currentPriceSetFileCount": int(price_row.get("currentPriceSetFileCount") or 0),
            "currentPriceSource": price_row.get("source"),
        }
        row["recommendation"] = recommend_language(row)
        rows.append(row)

    app_supported_languages = sorted(row["language"] for row in rows if row["appSupported"])
    promoted_languages = sorted(row["language"] for row in rows if row["promotedToAppCatalogue"])
    price_languages = sorted(row["language"] for row in rows if row["hasCurrentPriceSupport"])

    return {
        "schemaVersion": "1.0.0",
        "generatedAtUtc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "provider": "pokewallet",
        "providerLanguageCodesFound": dict(sorted((str(k), int(v)) for k, v in provider_language_counts.items())),
        "languageRows": rows,
        "summary": {
            "languagesFound": sorted(known_languages),
            "appSupportedLanguages": app_supported_languages,
            "promotedToAppCatalogueLanguages": promoted_languages,
            "currentPriceSupportedLanguages": price_languages,
            "languageCount": len(rows),
        },
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    a = lines.append

    a("# Provider Language Audit")
    a("")
    a(f"Generated: {report.get('generatedAtUtc', 'n/a')}")
    a("")

    summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
    a(f"- Languages found: {', '.join(summary.get('languagesFound', []))}")
    a(f"- App-supported languages: {', '.join(summary.get('appSupportedLanguages', []))}")
    a(f"- Promoted to app catalogue: {', '.join(summary.get('promotedToAppCatalogueLanguages', []))}")
    a(f"- Current price supported: {', '.join(summary.get('currentPriceSupportedLanguages', []))}")
    a("")

    rows = report.get("languageRows", []) if isinstance(report.get("languageRows"), list) else []
    if rows:
        a("| Language | Provider cards | Provider sets | App-supported | Promoted | Image manifest | Current prices | Recommendation |")
        a("|----------|---------------:|--------------:|---------------|----------|----------------|----------------|----------------|")
        for row in rows:
            if not isinstance(row, dict):
                continue
            a(
                f"| {row.get('language', '')} | "
                f"{int(row.get('providerCardCount', 0)):,} | "
                f"{int(row.get('providerSetCount', 0)):,} | "
                f"{'yes' if row.get('appSupported') else 'no'} | "
                f"{'yes' if row.get('promotedToAppCatalogue') else 'no'} | "
                f"{'yes' if row.get('hasImageManifestSupport') else 'no'} | "
                f"{'yes' if row.get('hasCurrentPriceSupport') else 'no'} | "
                f"{row.get('recommendation', '')} |"
            )
        a("")

    code_counts = report.get("providerLanguageCodesFound", {})
    if isinstance(code_counts, dict) and code_counts:
        a("## Provider Language Codes")
        a("")
        for code, count in sorted(code_counts.items()):
            a(f"- {code}: {int(count):,} sets")
        a("")

    a("---")
    a("Generated by tools/report_provider_languages.py")
    return "\n".join(lines)


def main() -> int:
    report = build_report()
    markdown = render_markdown(report)
    write_json(REPORT_JSON_PATH, report)
    write_text(REPORT_MD_PATH, markdown)

    rows = report.get("languageRows", []) if isinstance(report.get("languageRows"), list) else []
    print("Provider language audit")
    print(f"  languages found: {len(rows)}")
    for row in rows:
        if not isinstance(row, dict):
            continue
        print(
            "  "
            f"{row.get('language', '')}: provider cards={int(row.get('providerCardCount', 0)):,}, "
            f"provider sets={int(row.get('providerSetCount', 0)):,}, "
            f"app-supported={'yes' if row.get('appSupported') else 'no'}, "
            f"promoted={'yes' if row.get('promotedToAppCatalogue') else 'no'}, "
            f"prices={'yes' if row.get('hasCurrentPriceSupport') else 'no'}"
        )
    print(f"  wrote: {REPORT_JSON_PATH.relative_to(ROOT)}")
    print(f"  wrote: {REPORT_MD_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
