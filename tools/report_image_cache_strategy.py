#!/usr/bin/env python3
"""Report image cache strategy and recommendations for app/device behavior."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CATALOG_ROOT = ROOT / "public" / "v1" / "catalog" / "pokemon"
CACHE_POLICY_PATH = ROOT / "public" / "v1" / "images" / "cache-policy.json"
LOCAL_IMAGE_CACHE_DIR = ROOT / "public" / "v1" / "images" / "cards"
REPORT_JSON_PATH = ROOT / "reports" / "image_cache_strategy_latest.json"
REPORT_MD_PATH = ROOT / "reports" / "image_cache_strategy_latest.md"


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


def count_catalog_image_manifest_records() -> dict[str, dict[str, int]]:
    by_language: dict[str, dict[str, int]] = {}
    if not CATALOG_ROOT.exists():
        return by_language

    for language_dir in sorted([item for item in CATALOG_ROOT.iterdir() if item.is_dir()], key=lambda item: item.name.lower()):
        cards_dir = language_dir / "cards"
        total_cards = 0
        cards_with_image_url = 0
        cards_with_pokewallet_source = 0
        if cards_dir.exists():
            for path in sorted(cards_dir.glob("*.json"), key=lambda item: item.name.lower()):
                payload = try_load_json(path)
                if not isinstance(payload, dict):
                    continue
                cards = payload.get("cards")
                if not isinstance(cards, list):
                    continue
                for card in cards:
                    if not isinstance(card, dict):
                        continue
                    total_cards += 1
                    has_image = bool(str(card.get("imageSmall") or "").strip() or str(card.get("imageLarge") or "").strip())
                    if has_image:
                        cards_with_image_url += 1
                    if str(card.get("imageSource") or "").strip().lower() == "pokewallet":
                        cards_with_pokewallet_source += 1

        by_language[language_dir.name] = {
            "cardCount": total_cards,
            "imageUrlRecordCount": cards_with_image_url,
            "pokewalletImageSourceCount": cards_with_pokewallet_source,
        }
    return by_language


def count_local_cached_binaries() -> int:
    if not LOCAL_IMAGE_CACHE_DIR.exists():
        return 0
    return sum(1 for item in LOCAL_IMAGE_CACHE_DIR.rglob("*") if item.is_file())


def build_report() -> dict[str, Any]:
    cache_policy = try_load_json(CACHE_POLICY_PATH)
    if not isinstance(cache_policy, dict):
        cache_policy = {}

    image_manifest_by_language = count_catalog_image_manifest_records()
    local_cached_binary_count = count_local_cached_binaries()

    not_recommended_reasons = [
        "Repository size and clone times grow quickly when binary images are committed.",
        "Git diff/review quality degrades for binary-only updates.",
        "Frequent image churn produces noisy commit history and costly CI sync.",
        "Mobile apps already have stronger local disk caching and eviction controls.",
    ]

    app_behavior = {
        "loadUrlFirst": True,
        "cacheOnDevice": True,
        "prefetchSavedInventoryAndRecentScans": True,
        "placeholderAndErrorState": True,
        "boundedCacheSize": {
            "enabled": True,
            "recommendedMaxMb": 512,
            "evictionPolicy": "lru",
        },
    }

    external_storage_options = [
        {
            "option": "Cloudflare R2",
            "fit": "Good for low-cost object storage close to edge delivery.",
            "notes": "Use signed/public URL strategy and immutable cache-control headers.",
        },
        {
            "option": "Supabase Storage",
            "fit": "Good when CardScanR metadata and auth already rely on Supabase.",
            "notes": "Use bucket policies and predictable object paths by canonical image key.",
        },
        {
            "option": "Static CDN/Bucket",
            "fit": "Good for globally cached immutable assets with simple deployment.",
            "notes": "Publish-only flow can be automated from validated image manifests.",
        },
    ]

    recommendation = (
        "Keep public repository image delivery URL-first and binary-free. "
        "Cache images on-device with bounded size and selective prefetch for saved inventory/recent scans. "
        "If server-side binary hosting is needed later, use external object storage/CDN instead of Git blobs."
    )

    return {
        "schemaVersion": "1.0.0",
        "generatedAtUtc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "currentPolicy": {
            "strategy": cache_policy.get("strategy"),
            "localCacheEnabled": bool(cache_policy.get("localCacheEnabled")),
            "notes": cache_policy.get("notes", []),
        },
        "imageManifestRecordsByLanguage": image_manifest_by_language,
        "localCachedBinaryCount": local_cached_binary_count,
        "gitBinaryImageCacheNotRecommended": {
            "recommended": False,
            "reasons": not_recommended_reasons,
        },
        "appDeviceCacheRecommendation": app_behavior,
        "externalStorageOptions": external_storage_options,
        "recommendation": recommendation,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    a = lines.append

    a("# Image Cache Strategy Report")
    a("")
    a(f"Generated: {report.get('generatedAtUtc', 'n/a')}")
    a("")

    current_policy = report.get("currentPolicy", {}) if isinstance(report.get("currentPolicy"), dict) else {}
    a(f"- Current strategy: {current_policy.get('strategy', 'n/a')}")
    a(f"- Local cache enabled in policy: {'yes' if current_policy.get('localCacheEnabled') else 'no'}")
    a(f"- Local cached binary count in repo: {int(report.get('localCachedBinaryCount', 0)):,}")
    a("")

    per_language = report.get("imageManifestRecordsByLanguage", {}) if isinstance(report.get("imageManifestRecordsByLanguage"), dict) else {}
    if per_language:
        a("## Image Manifest Records by Language")
        a("")
        a("| Language | Catalogue cards | Cards with image URL | imageSource=pokewallet |")
        a("|----------|----------------:|---------------------:|----------------------:|")
        for language, row in sorted(per_language.items()):
            if not isinstance(row, dict):
                continue
            a(
                f"| {language} | "
                f"{int(row.get('cardCount', 0)):,} | "
                f"{int(row.get('imageUrlRecordCount', 0)):,} | "
                f"{int(row.get('pokewalletImageSourceCount', 0)):,} |"
            )
        a("")

    not_recommended = report.get("gitBinaryImageCacheNotRecommended", {}) if isinstance(report.get("gitBinaryImageCacheNotRecommended"), dict) else {}
    reasons = not_recommended.get("reasons", []) if isinstance(not_recommended.get("reasons"), list) else []
    if reasons:
        a("## Why Git Binary Image Cache Is Not Recommended")
        a("")
        for reason in reasons:
            a(f"- {reason}")
        a("")

    app_behavior = report.get("appDeviceCacheRecommendation", {}) if isinstance(report.get("appDeviceCacheRecommendation"), dict) else {}
    bounded = app_behavior.get("boundedCacheSize", {}) if isinstance(app_behavior.get("boundedCacheSize"), dict) else {}
    a("## Recommended App Behavior")
    a("")
    a(f"- Load URL first: {'yes' if app_behavior.get('loadUrlFirst') else 'no'}")
    a(f"- Cache on device: {'yes' if app_behavior.get('cacheOnDevice') else 'no'}")
    a(f"- Prefetch saved inventory/recent scans: {'yes' if app_behavior.get('prefetchSavedInventoryAndRecentScans') else 'no'}")
    a(f"- Placeholder/error state: {'yes' if app_behavior.get('placeholderAndErrorState') else 'no'}")
    a(
        "- Bounded cache size: "
        f"{'yes' if bounded.get('enabled') else 'no'} "
        f"(max MB: {int(bounded.get('recommendedMaxMb', 0))}, policy: {bounded.get('evictionPolicy', 'n/a')})"
    )
    a("")

    options = report.get("externalStorageOptions", []) if isinstance(report.get("externalStorageOptions"), list) else []
    if options:
        a("## Optional Future External Storage")
        a("")
        for option in options:
            if not isinstance(option, dict):
                continue
            a(f"- {option.get('option', '')}: {option.get('fit', '')} {option.get('notes', '')}")
        a("")

    a("## Recommendation")
    a("")
    a(str(report.get("recommendation") or ""))
    a("")
    a("---")
    a("Generated by tools/report_image_cache_strategy.py")
    return "\n".join(lines)


def main() -> int:
    report = build_report()
    markdown = render_markdown(report)
    write_json(REPORT_JSON_PATH, report)
    write_text(REPORT_MD_PATH, markdown)

    print("Image cache strategy report")
    print(f"  local cached binary count: {int(report.get('localCachedBinaryCount', 0)):,}")
    by_lang = report.get("imageManifestRecordsByLanguage", {}) if isinstance(report.get("imageManifestRecordsByLanguage"), dict) else {}
    for language, row in sorted(by_lang.items()):
        if not isinstance(row, dict):
            continue
        print(
            "  "
            f"{language}: cards={int(row.get('cardCount', 0)):,}, "
            f"with image URL={int(row.get('imageUrlRecordCount', 0)):,}"
        )
    print(f"  wrote: {REPORT_JSON_PATH.relative_to(ROOT)}")
    print(f"  wrote: {REPORT_MD_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
