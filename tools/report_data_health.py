#!/usr/bin/env python3
"""Read-only CardScanR data health report."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
V1_DIR = ROOT / "public" / "v1"


def load_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def try_load_json(path: Path) -> Any:
    try:
        return load_json(path)
    except (OSError, json.JSONDecodeError):
        return None


def run_git(args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def repo_sync_status() -> dict[str, Any]:
    branch = run_git(["branch", "--show-current"]) or "unknown"
    porcelain = run_git(["status", "--porcelain"]) or ""
    ahead_behind = run_git(["rev-list", "--left-right", "--count", "HEAD...origin/main"])
    ahead = behind = None
    if ahead_behind:
        parts = ahead_behind.split()
        if len(parts) == 2:
            ahead, behind = int(parts[0]), int(parts[1])
    return {
        "branch": branch,
        "ahead": ahead,
        "behind": behind,
        "dirtyFiles": len([line for line in porcelain.splitlines() if line.strip()]),
    }


def supported_games_languages_markets() -> tuple[list[str], list[str], list[str]]:
    games_payload = try_load_json(V1_DIR / "supported-games.json")
    games = []
    if isinstance(games_payload, dict):
        games = [str(item.get("game") or item.get("id") or "") for item in games_payload.get("games", []) if isinstance(item, dict)]
    if not games:
        config = try_load_json(DATA_DIR / "catalog_config.json")
        games = [str(item) for item in config.get("games", [])] if isinstance(config, dict) else []

    languages_payload = try_load_json(V1_DIR / "supported-languages.json")
    languages = []
    if isinstance(languages_payload, dict):
        languages = [
            str(item.get("language") or "")
            for item in languages_payload.get("languages", [])
            if isinstance(item, dict) and item.get("enabled") is True
        ]

    markets_payload = try_load_json(V1_DIR / "supported-markets.json")
    markets = []
    if isinstance(markets_payload, dict):
        markets = [
            str(item.get("market") or "")
            for item in markets_payload.get("markets", [])
            if isinstance(item, dict) and item.get("enabled") is True
        ]
    return sorted(filter(None, games)), sorted(filter(None, languages)), sorted(filter(None, markets))


def provider_counts() -> dict[str, int]:
    payload = try_load_json(V1_DIR / "provider-catalog" / "pokewallet" / "status.json")
    result: dict[str, int] = {}
    languages = payload.get("languages") if isinstance(payload, dict) else None
    if isinstance(languages, dict):
        for language, item in languages.items():
            if isinstance(item, dict):
                result[str(language)] = int(item.get("cardCount") or 0)
    return dict(sorted(result.items()))


def app_catalogue_counts() -> dict[str, int]:
    result: dict[str, int] = {}
    root = V1_DIR / "catalog" / "pokemon"
    if not root.exists():
        return result
    for path in sorted(root.glob("*/sets.json"), key=lambda item: item.as_posix().lower()):
        payload = try_load_json(path)
        if isinstance(payload, dict):
            result[path.parent.name] = int(payload.get("cardCount") or 0)
    return dict(sorted(result.items()))


def image_counts() -> tuple[dict[str, int], int]:
    manifest = try_load_json(V1_DIR / "images" / "cards-manifest.json")
    records = manifest.get("records") if isinstance(manifest, dict) else []
    counts: Counter[str] = Counter()
    cached_files = 0
    if isinstance(records, list):
        for record in records:
            if not isinstance(record, dict):
                continue
            counts[str(record.get("language") or "unknown")] += 1
            for field in ("localImageSmallPath", "localImageLargePath"):
                value = record.get(field)
                if not isinstance(value, str) or not value:
                    continue
                path = Path(value)
                if not path.is_absolute():
                    path = ROOT / path
                if path.is_file():
                    cached_files += 1
    return dict(sorted(counts.items())), cached_files


def price_counts() -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    root = V1_DIR / "prices" / "current" / "pokemon"
    if not root.exists():
        return result
    for language_dir in sorted([item for item in root.iterdir() if item.is_dir()], key=lambda item: item.name.lower()):
        source_counts: Counter[str] = Counter()
        status_counts: Counter[str] = Counter()
        record_count = 0
        for path in sorted(language_dir.glob("*.json"), key=lambda item: item.name.lower()):
            if path.name == "status.json":
                continue
            payload = try_load_json(path)
            prices = payload.get("prices") if isinstance(payload, dict) else []
            if not isinstance(prices, list):
                continue
            for record in prices:
                if not isinstance(record, dict):
                    continue
                record_count += 1
                source_counts[str(record.get("source") or payload.get("source") or "unknown")] += 1
                status_counts[str(record.get("status") or payload.get("status") or "unknown")] += 1
        status_payload = try_load_json(language_dir / "status.json")
        result[language_dir.name] = {
            "recordCount": record_count,
            "fileCount": len([item for item in language_dir.glob("*.json") if item.name != "status.json"]),
            "languageStatus": status_payload.get("status") if isinstance(status_payload, dict) else "missing",
            "sourceCounts": dict(sorted(source_counts.items())),
            "statusCounts": dict(sorted(status_counts.items())),
        }
    return result


def history_coverage() -> dict[str, Any]:
    daily_root = V1_DIR / "history" / "daily"
    dates: set[str] = set()
    languages: Counter[str] = Counter()
    if daily_root.exists():
        for path in daily_root.glob("*/*/*/tracked.json"):
            parts = path.relative_to(daily_root).parts
            if len(parts) >= 3:
                dates.add(parts[0])
                languages[parts[2]] += 1
    return {
        "firstDate": min(dates) if dates else None,
        "lastDate": max(dates) if dates else None,
        "dateCount": len(dates),
        "filesByLanguage": dict(sorted(languages.items())),
    }


def provider_blocked_summary(languages: list[str]) -> dict[str, Any]:
    try:
        from report_provider_blocked_cards import build_report
    except Exception as exc:  # pragma: no cover - defensive runtime report guard
        return {"error": str(exc)}
    try:
        report = build_report(languages or ["en", "jp"], include_zh=False, sample_limit=0)
    except Exception as exc:  # pragma: no cover - defensive runtime report guard
        return {"error": str(exc)}
    missing = report.get("missingCollectorNumberSummary", {})
    return {
        "blockedReasonCounts": report.get("blockedReasonCounts", {}),
        "blockedReasonCountsByLanguage": report.get("blockedReasonCountsByLanguage", {}),
        "missingCollectorNumber": {
            "total": missing.get("total", 0),
            "safeRecoverableCount": missing.get("safeRecoverableCount", 0),
            "remainingBlockedCount": missing.get("remainingBlockedCount", 0),
            "looksLikeCounts": missing.get("looksLikeCounts", {}),
        },
    }


def app_source_summary(languages: list[str]) -> dict[str, Any]:
    try:
        from report_app_catalogue_sources import build_report
    except Exception as exc:  # pragma: no cover - defensive runtime report guard
        return {"error": str(exc)}
    try:
        report = build_report(languages or None)
    except Exception as exc:  # pragma: no cover - defensive runtime report guard
        return {"error": str(exc)}
    return {
        "primarySourceCountsByLanguage": report.get("primarySourceCountsByLanguage", {}),
        "appRecordsWithPokewalletProviderIds": report.get("appRecordsWithPokewalletProviderIds", {}),
        "appRecordsWithoutPokewalletProviderIds": report.get("appRecordsWithoutPokewalletProviderIds", {}),
        "appRecordsWithUnknownSource": report.get("appRecordsWithUnknownSource", {}),
        "appRecordsWithMultipleProviderIds": report.get("appRecordsWithMultipleProviderIds", {}),
    }


def production_checklist(
    *,
    provider: dict[str, int],
    app: dict[str, int],
    images: dict[str, int],
    cached_files: int,
    prices: dict[str, dict[str, Any]],
    history: dict[str, Any],
) -> list[tuple[str, bool, str]]:
    return [
        ("provider_catalogue_present", bool(provider), "Pokewallet provider catalogue has records."),
        ("app_catalogue_en_present", app.get("en", 0) > 0, "EN app catalogue is available."),
        ("app_catalogue_jp_present", app.get("jp", 0) > 0, "JP app catalogue is available, currently partial."),
        ("image_manifest_present", bool(images), "Image manifest exists."),
        ("jp_images_present", images.get("jp", 0) > 0, "JP image manifest records are available."),
        ("local_image_cache_empty_ok", cached_files >= 0, "Local binary cache is optional and should stay ignored."),
        ("en_stage1_prices_present", prices.get("en", {}).get("recordCount", 0) > 0, "EN current prices exist."),
        ("jp_prices_available", prices.get("jp", {}).get("recordCount", 0) > 0, "JP current prices are only ready when real records exist."),
        ("history_present", bool(history.get("lastDate")), "Tracked-card history has at least one daily snapshot."),
    ]


def next_action(checklist: list[tuple[str, bool, str]], provider: dict[str, int], app: dict[str, int]) -> str:
    failed = [item for item in checklist if not item[1]]
    failed_keys = {item[0] for item in failed}
    if "jp_images_present" in failed_keys:
        return "Run .\\scripts\\run_cardscanr_full_data_pipeline.ps1 -NoFetch -BuildImages to rebuild the multi-language image manifest."
    if "jp_prices_available" in failed_keys:
        return "Keep JP pricing marked unavailable until a non-eBay source produces confident JP records."
    if provider.get("zh", 0) and "zh" not in app:
        return "Review ZH downstream support before enabling app catalogue or public language support."
    if failed:
        return f"Resolve {failed[0][0]}."
    return "Run the full pipeline on the normal worker cadence and review reports/latest_full_data_pipeline.json."


def main() -> int:
    repo = repo_sync_status()
    games, languages, markets = supported_games_languages_markets()
    provider = provider_counts()
    app = app_catalogue_counts()
    images, cached_files = image_counts()
    prices = price_counts()
    history = history_coverage()
    provider_blocked = provider_blocked_summary(languages)
    source_audit = app_source_summary(languages)
    checklist = production_checklist(
        provider=provider,
        app=app,
        images=images,
        cached_files=cached_files,
        prices=prices,
        history=history,
    )

    print("CardScanR data health")
    print("=====================")
    print(f"generatedAtUtc: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    print(f"repo: branch={repo['branch']} ahead={repo['ahead']} behind={repo['behind']} dirtyFiles={repo['dirtyFiles']}")
    print(f"supported games: {', '.join(games) or 'none'}")
    print(f"supported languages: {', '.join(languages) or 'none'}")
    print(f"enabled markets: {', '.join(markets) or 'none'}")
    print(f"provider records by language: {provider}")
    print(f"app catalogue records by language: {app}")
    print(f"blocked provider records by reason: {provider_blocked.get('blockedReasonCounts', provider_blocked)}")
    missing_summary = provider_blocked.get("missingCollectorNumber", {})
    if missing_summary:
        print(
            "missing collector number recoverability: "
            f"total={missing_summary.get('total', 0)} "
            f"safeRecoverable={missing_summary.get('safeRecoverableCount', 0)} "
            f"remainingBlocked={missing_summary.get('remainingBlockedCount', 0)} "
            f"looksLike={missing_summary.get('looksLikeCounts', {})}"
        )
    print(f"app catalogue source/provenance by language: {source_audit.get('primarySourceCountsByLanguage', source_audit)}")
    print(f"app catalogue records without source attribution: {source_audit.get('appRecordsWithUnknownSource', {})}")
    print(f"app catalogue records without Pokewallet provider IDs: {source_audit.get('appRecordsWithoutPokewalletProviderIds', {})}")
    print(f"image records by language: {images}")
    print(f"actual cached image files: {cached_files}")
    print("current price records by language/source/status:")
    for language, item in sorted(prices.items()):
        print(
            f"  {language}: records={item['recordCount']} files={item['fileCount']} "
            f"languageStatus={item['languageStatus']} sources={item['sourceCounts']} statuses={item['statusCounts']}"
        )
    print(
        "history coverage: "
        f"first={history['firstDate']} last={history['lastDate']} dates={history['dateCount']} filesByLanguage={history['filesByLanguage']}"
    )
    print("production readiness checklist:")
    for key, passed, note in checklist:
        print(f"  {'PASS' if passed else 'FAIL'} {key}: {note}")
    print(f"exact next recommended action: {next_action(checklist, provider, app)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
