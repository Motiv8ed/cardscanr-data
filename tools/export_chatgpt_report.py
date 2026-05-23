#!/usr/bin/env python3
"""
Export a concise ChatGPT-uploadable report bundle for CardScanR data.

Writes to reports/chatgpt_exports/:
  cardscanr_chatgpt_report_latest.{md,json,zip}
  cardscanr_chatgpt_report_YYYYMMDD_HHMMSS.{md,json,zip}

Usage:
  python tools/export_chatgpt_report.py
  python tools/export_chatgpt_report.py --include-large-reports
  python tools/export_chatgpt_report.py --no-zip
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
EXPORT_DIR = ROOT / "reports" / "chatgpt_exports"
CURRENT_PRICE_ROOT = ROOT / "public" / "v1" / "prices" / "current" / "pokemon"

# Safe files to always include in zip (relative to ROOT)
ZIP_FILES_DEFAULT: list[str] = [
    # These are written by this script itself
    "reports/chatgpt_exports/cardscanr_chatgpt_report_latest.md",
    "reports/chatgpt_exports/cardscanr_chatgpt_report_latest.json",
    # Pipeline & promotion reports
    "reports/latest_full_data_pipeline.json",
    "reports/latest_full_data_pipeline.md",
    "reports/provider_to_app_promotion_gaps.md",
    "reports/provider_blocked_cards_latest.md",
    "reports/app_catalogue_source_audit_latest.md",
    "reports/pokewallet_api_capability_audit_latest.json",
    "reports/pokewallet_api_capability_audit_latest.md",
    # Public v1 status/index files (small, safe)
    "public/v1/provider-catalog/pokewallet/status.json",
    "public/v1/provider-catalog/pokewallet/languages-summary.json",
    "public/v1/provider-catalog/pokewallet/sets-summary.json",
    "public/v1/images/cache-policy.json",
    "public/v1/prices/current/pokemon/en/status.json",
    "public/v1/prices/current/pokemon/jp/status.json",
    "public/v1/index.json",
]

# Large report files — only included with --include-large-reports
ZIP_FILES_LARGE: list[str] = [
    "reports/provider_to_app_promotion_latest.json",
    "reports/provider_blocked_cards_latest.json",
    "reports/app_catalogue_source_audit_latest.json",
    "reports/jp_pricing_source_audit_latest.json",
    "reports/jp_pricing_source_audit_latest.md",
]

# Hard exclude patterns — safety check applied to every path
EXCLUDE_PATTERNS: tuple[str, ...] = (
    ".env",
    "secret",
    "token",
    "credential",
    "password",
    ".cache",
    ".venv",
    "logs/",
    "public/v1/images/cards/",
    ".cache/cardscanr-images/",
    ".cache_build_tmp/",
)


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _git(*args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", *args],
            cwd=ROOT,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except subprocess.CalledProcessError:
        return ""


def _collect_git_info() -> dict[str, Any]:
    branch = _git("rev-parse", "--abbrev-ref", "HEAD") or "unknown"
    commit_hash = _git("rev-parse", "--short", "HEAD") or "unknown"
    commit_title = _git("log", "-1", "--pretty=%s") or ""
    commit_date = _git("log", "-1", "--pretty=%ci") or ""
    porcelain = _git("status", "--porcelain")
    ahead_behind_raw = _git("status", "--porcelain=v2", "--branch")

    ahead = 0
    behind = 0
    for line in ahead_behind_raw.splitlines():
        if line.startswith("# branch.ab "):
            parts = line.split()
            for part in parts:
                if part.startswith("+"):
                    try:
                        ahead = int(part[1:])
                    except ValueError:
                        pass
                elif part.startswith("-"):
                    try:
                        behind = int(part[1:])
                    except ValueError:
                        pass

    changed_files: list[str] = []
    untracked_files: list[str] = []
    for line in porcelain.splitlines():
        if not line or len(line) < 4:
            continue
        status = line[:2]
        path = line[3:]
        if status == "??":
            untracked_files.append(path)
        else:
            changed_files.append(path)

    return {
        "branch": branch,
        "commitHash": commit_hash,
        "commitTitle": commit_title,
        "commitDate": commit_date,
        "aheadOfOrigin": ahead,
        "behindOrigin": behind,
        "changedFiles": changed_files,
        "untrackedFiles": untracked_files,
        "workingTreeClean": len(changed_files) == 0,
    }


# ---------------------------------------------------------------------------
# Report file readers — graceful on missing files
# ---------------------------------------------------------------------------

def _load_json(rel_path: str) -> dict[str, Any] | None:
    p = ROOT / rel_path
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _collect_pipeline_info() -> dict[str, Any]:
    data = _load_json("reports/latest_full_data_pipeline.json")
    if data is None:
        return {"available": False}

    stages_run = data.get("stagesRun", [])
    stages_skipped = data.get("stagesSkipped", [])
    failed_stages = [s["name"] for s in stages_run if s.get("status") != "passed"]

    return {
        "available": True,
        "status": data.get("status", "unknown"),
        "validationResult": data.get("validationResult", "unknown"),
        "startedAtUtc": data.get("startedAtUtc"),
        "finishedAtUtc": data.get("finishedAtUtc"),
        "appCatalogueByLanguage": data.get("appCatalogueCardCountsByLanguage", {}),
        "imageManifestByLanguage": data.get("imageManifestCountByLanguage", {}),
        "providerByLanguage": data.get("providerCardCountsByLanguage", {}),
        "pricesByLanguage": data.get("currentPriceRecordCountsByLanguageSourceStatus", {}),
        "localCachedImageFileCount": data.get("localCachedImageFileCount", 0),
        "historyDateRange": data.get("historyDateRange", {}),
        "missingIncompleteAreas": data.get("missingIncompleteAreas", []),
        "stagesRun": [s["name"] for s in stages_run],
        "stagesSkipped": [s["name"] for s in stages_skipped],
        "failedStages": failed_stages,
        "stageDetails": stages_run,
    }


def _count_current_price_records() -> dict[str, dict[str, Any]]:
    """Count current public price records from the per-set files."""
    result: dict[str, dict[str, Any]] = {}
    if not CURRENT_PRICE_ROOT.exists():
        return result

    for language_dir in sorted([item for item in CURRENT_PRICE_ROOT.iterdir() if item.is_dir()], key=lambda item: item.name.lower()):
        record_count = 0
        source_counts: dict[str, int] = {}
        status_counts: dict[str, int] = {}
        file_count = 0

        for path in sorted(language_dir.glob("*.json"), key=lambda item: item.name.lower()):
            if path.name == "status.json":
                continue
            payload = _load_json(str(path.relative_to(ROOT).as_posix())) or {}
            prices = payload.get("prices")
            if not isinstance(prices, list):
                continue

            file_count += 1
            for record in prices:
                if not isinstance(record, dict):
                    continue
                record_count += 1
                source = str(record.get("source") or payload.get("source") or "unknown")
                status = str(record.get("status") or payload.get("status") or "unknown")
                source_counts[source] = source_counts.get(source, 0) + 1
                status_counts[status] = status_counts.get(status, 0) + 1

        result[language_dir.name] = {
            "recordCount": record_count,
            "fileCount": file_count,
            "sourceCounts": dict(sorted(source_counts.items())),
            "statusCounts": dict(sorted(status_counts.items())),
        }

    return result


def _single_key_or_none(counts: dict[str, int]) -> str | None:
    positive_keys = [key for key, count in counts.items() if count > 0]
    return positive_keys[0] if len(positive_keys) == 1 else None


def _price_status_summary(status_payload: dict[str, Any], actual_counts: dict[str, Any]) -> dict[str, Any]:
    source_summary = status_payload.get("sourceSummary")
    if not isinstance(source_summary, dict):
        source_summary = {}

    staleness = status_payload.get("staleness")
    if not isinstance(staleness, dict):
        staleness = {}

    actual_record_count = int(actual_counts.get("recordCount") or 0)
    status_record_count = status_payload.get("currentPriceRecordCount", status_payload.get("recordCount"))
    record_count = actual_record_count
    if actual_record_count == 0 and isinstance(status_record_count, int):
        record_count = status_record_count

    source_counts = actual_counts.get("sourceCounts") if isinstance(actual_counts.get("sourceCounts"), dict) else {}
    status_counts = actual_counts.get("statusCounts") if isinstance(actual_counts.get("statusCounts"), dict) else {}

    source = source_summary.get("primarySource") or status_payload.get("source") or _single_key_or_none(source_counts) or "none"
    display_status = _single_key_or_none(status_counts)
    if not display_status:
        display_status = str(staleness.get("status") or status_payload.get("status") or "unknown")

    return {
        "recordCount": record_count,
        "statusRecordCount": status_record_count,
        "actualRecordCount": actual_record_count,
        "fileCount": actual_counts.get("fileCount", 0),
        "source": source,
        "status": display_status,
        "languageStatus": status_payload.get("status"),
        "sourceCounts": source_counts,
        "statusCounts": status_counts,
        "lastUpdatedAtUtc": status_payload.get("lastSuccessfulPriceUpdateAtUtc") or status_payload.get("lastUpdatedAtUtc"),
        "recordCountMatchesStatus": status_record_count == actual_record_count if isinstance(status_record_count, int) else None,
    }


def _collect_blocked_info() -> dict[str, Any]:
    data = _load_json("reports/provider_blocked_cards_latest.json")
    if data is None:
        return {"available": False}

    missing_summary = data.get("missingCollectorNumberSummary", {})
    top_sets = missing_summary.get("top50MissingNumberSets", [])[:10]

    return {
        "available": True,
        "generatedAtUtc": data.get("generatedAtUtc"),
        "blockedReasonCounts": data.get("blockedReasonCounts", {}),
        "blockedReasonCountsByLanguage": data.get("blockedReasonCountsByLanguage", {}),
        "missingCollectorNumberLooksLike": missing_summary.get("looksLikeCounts", {}),
        "safeRecoverableCount": missing_summary.get("safeRecoverableCount", 0),
        "top10MissingNumberSets": top_sets,
    }


def _collect_promotion_info() -> dict[str, Any]:
    data = _load_json("reports/provider_to_app_promotion_latest.json")
    if data is None:
        return {"available": False}

    top_dupes = data.get("topDuplicateGroups", [])[:5]
    dupe_examples = [
        {
            "collectorNumber": d.get("collectorNumber"),
            "language": d.get("language"),
            "setId": d.get("setId"),
            "count": d.get("count"),
            "normalizedName": d.get("normalizedName"),
        }
        for d in top_dupes
    ]

    return {
        "available": True,
        "blockedCountByLanguage": data.get("blockedCountByLanguage", {}),
        "top5DuplicateCandidates": dupe_examples,
    }


def _collect_worker_info() -> dict[str, Any]:
    data = _load_json("data/pokewallet_catalog_worker_status.json")
    if data is None:
        return {"available": False}
    return {
        "available": True,
        "running": data.get("running", False),
        "lastStatus": data.get("lastStatus", "unknown"),
        "lastCycleStartedAtUtc": data.get("lastCycleStartedAtUtc"),
        "lastCycleFinishedAtUtc": data.get("lastCycleFinishedAtUtc"),
        "nextCycleAtUtc": data.get("nextCycleAtUtc"),
        "mode": data.get("mode"),
        "lastCommit": data.get("lastCommit"),
        "lastError": data.get("lastError", ""),
    }


def _collect_price_updater_info() -> dict[str, Any]:
    data = _load_json("logs/local_price_updater_status.json")
    if data is None:
        return {"available": False}
    return {
        "available": True,
        "isRunning": data.get("isRunning", False),
        "currentPhase": data.get("currentPhase"),
        "cycleNumber": data.get("cycleNumber"),
        "lastCycleStartedAtUtc": data.get("lastCycleStartedAtUtc"),
        "lastCycleFinishedAtUtc": data.get("lastCycleFinishedAtUtc"),
        "lastSuccessfulUpdateAtUtc": data.get("lastSuccessfulUpdateAtUtc"),
        "lastCommitHash": data.get("lastCommitHash"),
    }


def _collect_pokewallet_api_capability_audit() -> dict[str, Any]:
    data = _load_json("reports/pokewallet_api_capability_audit_latest.json")
    if data is None:
        return {"available": False}

    summary = data.get("endpointAvailabilitySummary")
    if not isinstance(summary, dict):
        summary = {}
    price_plan = data.get("priceImporterPlan")
    if not isinstance(price_plan, dict):
        price_plan = {}
    image_audit = data.get("imageCacheAudit")
    if not isinstance(image_audit, dict):
        image_audit = {}
    set_logo_plan = data.get("setLogoRefreshPlan")
    if not isinstance(set_logo_plan, dict):
        set_logo_plan = {}

    endpoint_rows: list[dict[str, Any]] = []
    expected_pro_endpoints: list[str] = []
    for item in data.get("endpoints", []):
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or item.get("name") or "")
        signals = item.get("priceSignals")
        if not isinstance(signals, dict):
            signals = {}
        if item.get("expectedPro") and label:
            expected_pro_endpoints.append(label)
        endpoint_rows.append(
            {
                "name": item.get("name"),
                "label": label,
                "path": item.get("path"),
                "query": item.get("query") if isinstance(item.get("query"), dict) else {},
                "statusCode": item.get("statusCode"),
                "availability": item.get("availability"),
                "available": item.get("available") is True,
                "expectedPro": item.get("expectedPro") is True,
                "hasUsablePrices": signals.get("hasUsablePrices") is True,
            }
        )

    samples_requested = int(image_audit.get("samplesRequested") or 0)
    samples_succeeded = int(image_audit.get("samplesSucceeded") or 0)

    return {
        "available": True,
        "generatedAtUtc": data.get("generatedAtUtc"),
        "status": data.get("status", "unknown"),
        "apiKeyPresent": data.get("apiKeyPresent") is True,
        "requestsAttempted": data.get("requestsAttempted", 0),
        "requestsSucceeded": data.get("requestsSucceeded", 0),
        "availableEndpoints": summary.get("availableEndpoints", []),
        "planLimitedEndpoints": summary.get("planLimitedEndpoints", []),
        "notFoundEndpoints": summary.get("notFoundEndpoints", []),
        "rateLimitedEndpoints": summary.get("rateLimitedEndpoints", []),
        "otherUnavailableEndpoints": summary.get("otherUnavailableEndpoints", []),
        "expectedProEndpoints": expected_pro_endpoints,
        "endpoints": endpoint_rows,
        "pricesEndpointWorks": price_plan.get("pricesEndpointWorks") is True,
        "jpPriceAvailability": price_plan.get("jpPriceAvailability") or "unknown",
        "cardmarketOnlyUseful": price_plan.get("cardmarketOnlyUseful") is True,
        "tcgplayerUsdUseful": price_plan.get("tcgplayerUsdUseful") is True,
        "imageEndpointWorks": samples_succeeded > 0,
        "imageSamplesRequested": samples_requested,
        "imageSamplesSucceeded": samples_succeeded,
        "setLogoEndpoint": set_logo_plan.get("endpoint"),
        "setLogoTested": set_logo_plan.get("tested") is True,
        "setLogoCandidateCount": len(set_logo_plan.get("candidateSamples", []))
        if isinstance(set_logo_plan.get("candidateSamples"), list)
        else 0,
        "setLogoReason": set_logo_plan.get("reason"),
        "recommendation": data.get("recommendation", ""),
    }


# ---------------------------------------------------------------------------
# Data counts from public v1 (fallback if pipeline report not available)
# ---------------------------------------------------------------------------

def _collect_v1_counts() -> dict[str, Any]:
    """Read lightweight status/index files from public/v1 for quick counts."""
    index = _load_json("public/v1/index.json") or {}
    en_price_status = _load_json("public/v1/prices/current/pokemon/en/status.json") or {}
    jp_price_status = _load_json("public/v1/prices/current/pokemon/jp/status.json") or {}
    cache_policy = _load_json("public/v1/images/cache-policy.json") or {}
    price_counts_by_language = _count_current_price_records()

    return {
        "indexLastUpdated": index.get("generatedAtUtc"),
        "pricesByLanguage": price_counts_by_language,
        "enPriceStatus": _price_status_summary(en_price_status, price_counts_by_language.get("en", {})),
        "jpPriceStatus": _price_status_summary(jp_price_status, price_counts_by_language.get("jp", {})),
        "imageCachePolicy": {
            "strategy": cache_policy.get("strategy"),
            "localCacheEnabled": cache_policy.get("localCacheEnabled", False),
        },
    }


# ---------------------------------------------------------------------------
# Next-action recommendation
# ---------------------------------------------------------------------------

def _recommend_next_action(
    pipeline: dict[str, Any],
    worker: dict[str, Any],
    git: dict[str, Any],
    pokewallet_api_audit: dict[str, Any] | None = None,
) -> str:
    issues: list[str] = []

    if pipeline.get("failedStages"):
        issues.append(
            f"Pipeline has failed stages: {', '.join(pipeline['failedStages'])}. "
            "Re-run: .\\scripts\\run_cardscanr_full_data_pipeline.ps1 -Validate"
        )
    if pipeline.get("validationResult") not in ("passed", None, "unknown") and pipeline.get("available"):
        if pipeline["validationResult"] != "passed":
            issues.append(
                "Validation did not pass. Check errors and re-run: python tools/validate_cache.py"
            )

    if worker.get("available") and worker.get("lastError"):
        issues.append(f"Worker reported an error: {worker['lastError']}")

    if git.get("aheadOfOrigin", 0) > 0:
        issues.append(
            f"Branch is {git['aheadOfOrigin']} commit(s) ahead of origin — push when ready: "
            ".\\scripts\\release_cardscanr_data.ps1 -Push"
        )

    if not issues:
        if (
            pokewallet_api_audit
            and pokewallet_api_audit.get("available")
            and pokewallet_api_audit.get("pricesEndpointWorks")
        ):
            if pokewallet_api_audit.get("jpPriceAvailability") == "usable_prices_found":
                return (
                    "All checks passing. PokeWallet /prices is available and the sampled JP set returned usable price data. "
                    "Build the staged diagnostics-only price importer next; keep public JP prices unavailable until source, "
                    "currency, variant, and count validation pass."
                )
            return (
                "All checks passing. PokeWallet /prices is available for sampled EN data. "
                "Build the staged diagnostics-only price importer next; keep JP pricing unavailable unless a JP endpoint "
                "returns validated usable records."
            )
        missing_jp = (
            pipeline.get("available")
            and pipeline.get("pricesByLanguage", {}).get("jp", {}).get("recordCount", 0) == 0
        )
        if missing_jp:
            return (
                "All checks passing. JP prices are unavailable from non-eBay sources — this is expected. "
                "Run a full pipeline cycle when ready: "
                ".\\scripts\\run_cardscanr_full_data_pipeline.ps1 -NoFetch -BuildAppCatalogue "
                "-BuildImages -BuildHistory -Validate -ExportChatGPTReport"
            )
        return (
            "All checks passing. Data is production-ready. "
            "Release when ready: .\\scripts\\release_cardscanr_data.ps1 -DryRun -ExportChatGPTReport"
        )

    return " | ".join(issues)


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------

def _render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    a = lines.append

    a("# CardScanR Data Report")
    a("")
    a(f"**Generated:** {report['generatedAtUtc']}")
    a("")

    # Git section
    git = report["git"]
    a("## Git Status")
    a("")
    ahead = git["aheadOfOrigin"]
    behind = git["behindOrigin"]
    sync_str = (
        f"{ahead} ahead, {behind} behind origin"
        if (ahead or behind)
        else "up to date with origin"
    )
    a(f"- **Branch:** `{git['branch']}`")
    a(f"- **Latest commit:** `{git['commitHash']}` — {git['commitTitle']} _(_{git['commitDate']}_)_")
    a(f"- **Origin sync:** {sync_str}")
    a(f"- **Working tree clean:** {'yes' if git['workingTreeClean'] else 'no'}")
    if git["changedFiles"]:
        a("")
        a("### Changed Files")
        for f in git["changedFiles"]:
            a(f"  - `{f}`")
    if git["untrackedFiles"]:
        a("")
        a("### Untracked Files")
        for f in git["untrackedFiles"]:
            a(f"  - `{f}`")
    a("")

    # Data summary
    pipeline = report["pipeline"]
    if pipeline.get("available"):
        app_by_lang = pipeline.get("appCatalogueByLanguage", {})
        img_by_lang = pipeline.get("imageManifestByLanguage", {})
        prov_by_lang = pipeline.get("providerByLanguage", {})
        prices_by_lang = pipeline.get("pricesByLanguage", {})

        a("## Data Summary")
        a("")
        a("### App Catalogue")
        for lang, count in sorted(app_by_lang.items()):
            a(f"  - **{lang.upper()}:** {count:,}")
        total_app = sum(app_by_lang.values())
        a(f"  - **Total:** {total_app:,}")
        a("")

        a("### Image Manifest")
        for lang, count in sorted(img_by_lang.items()):
            a(f"  - **{lang.upper()}:** {count:,}")
        total_img = sum(img_by_lang.values())
        a(f"  - **Total:** {total_img:,}")
        a("")

        a("### Provider Catalogue")
        for lang, count in sorted(prov_by_lang.items()):
            a(f"  - **{lang.upper()}:** {count:,}")
        a("")

        a("### Prices")
        for lang, info in sorted(prices_by_lang.items()):
            record_count = info.get("recordCount", 0) if isinstance(info, dict) else 0
            source_counts = info.get("sourceCounts", {}) if isinstance(info, dict) else {}
            status_counts = info.get("statusCounts", {}) if isinstance(info, dict) else {}
            sources = ", ".join(f"{s}: {c:,}" for s, c in source_counts.items()) if source_counts else "none"
            statuses = ", ".join(f"{s}: {c:,}" for s, c in status_counts.items()) if status_counts else "none"
            a(f"  - **{lang.upper()}:** {record_count:,} records (sources: {sources}; statuses: {statuses})")
        a("")

        a(f"### Local Cached Images: {pipeline.get('localCachedImageFileCount', 0):,}")
        a("")

        history = pipeline.get("historyDateRange", {})
        if history.get("firstDate"):
            a(f"### Price History Range")
            a(f"  - {history['firstDate']} → {history['lastDate']} ({history.get('dateCount', 0)} dates)")
            a("")

        missing_areas = pipeline.get("missingIncompleteAreas", [])
        if missing_areas:
            a("### Known Gaps / Incomplete Areas")
            for area in missing_areas:
                a(f"  - {area}")
            a("")

    # Blocked provider records
    blocked = report["blocked"]
    if blocked.get("available"):
        a("## Blocked Provider Records")
        a("")
        reason_counts = blocked.get("blockedReasonCounts", {})
        for reason, count in sorted(reason_counts.items(), key=lambda x: -x[1]):
            a(f"  - **{reason}:** {count:,}")
        a("")

        by_lang = blocked.get("blockedReasonCountsByLanguage", {})
        if by_lang:
            a("### By Language")
            for lang, reasons in sorted(by_lang.items()):
                parts = ", ".join(f"{r}: {c}" for r, c in sorted(reasons.items(), key=lambda x: -x[1]))
                a(f"  - **{lang.upper()}:** {parts}")
            a("")

        looks_like = blocked.get("missingCollectorNumberLooksLike", {})
        if looks_like:
            a("### Missing Collector Number — Card Type Breakdown")
            for kind, count in sorted(looks_like.items(), key=lambda x: -x[1]):
                a(f"  - {kind}: {count:,}")
            a("")

        safe_recoverable = blocked.get("safeRecoverableCount", 0)
        a(f"**Safe-recoverable count:** {safe_recoverable}")
        a("")

        top_sets = blocked.get("top10MissingNumberSets", [])
        if top_sets:
            a("### Top Missing Collector Number Sets (top 10)")
            a("")
            a("| # | Set Code | Set Name | Language | Count |")
            a("|---|----------|----------|----------|-------|")
            for i, s in enumerate(top_sets, 1):
                a(f"| {i} | `{s.get('providerSetCode', '')}` | {s.get('providerSetName', '')} | {s.get('language', '').upper()} | {s.get('count', 0):,} |")
            a("")

    # Duplicate candidates
    promotion = report["promotion"]
    if promotion.get("available"):
        top_dupes = promotion.get("top5DuplicateCandidates", [])
        if top_dupes:
            a("### Top Duplicate Candidate Examples (top 5)")
            a("")
            a("| Collector # | Language | Set ID | Name | Copies |")
            a("|-------------|----------|--------|------|--------|")
            for d in top_dupes:
                a(f"| `{d.get('collectorNumber', '')}` | {d.get('language', '').upper()} | `{d.get('setId', '')}` | {d.get('normalizedName', '')} | {d.get('count', 0)} |")
            a("")

    # Pipeline status
    if pipeline.get("available"):
        a("## Pipeline Status")
        a("")
        a(f"- **Status:** {pipeline.get('status', 'unknown')}")
        a(f"- **Validation:** {pipeline.get('validationResult', 'unknown')}")
        a(f"- **Started:** {pipeline.get('startedAtUtc', 'n/a')}")
        a(f"- **Finished:** {pipeline.get('finishedAtUtc', 'n/a')}")
        stages_run = pipeline.get("stagesRun", [])
        stages_skipped = pipeline.get("stagesSkipped", [])
        failed_stages = pipeline.get("failedStages", [])
        if stages_run:
            a(f"- **Stages run:** {', '.join(stages_run)}")
        if stages_skipped:
            a(f"- **Stages skipped:** {', '.join(stages_skipped)}")
        if failed_stages:
            a(f"- **FAILED stages:** {', '.join(failed_stages)}")
        a("")

    # Worker status
    worker = report["worker"]
    if worker.get("available"):
        a("## Pokewallet Catalog Worker Status")
        a("")
        a(f"- **Running:** {'yes' if worker.get('running') else 'no'}")
        a(f"- **Mode:** {worker.get('mode', 'n/a')}")
        a(f"- **Last status:** {worker.get('lastStatus', 'n/a')}")
        a(f"- **Last cycle started:** {worker.get('lastCycleStartedAtUtc', 'n/a')}")
        a(f"- **Last cycle finished:** {worker.get('lastCycleFinishedAtUtc', 'n/a')}")
        if worker.get("lastError"):
            a(f"- **Last error:** {worker['lastError']}")
        a("")

    # Price updater
    price_updater = report.get("priceUpdater", {})
    if price_updater.get("available"):
        a("## Local Price Updater Status")
        a("")
        a(f"- **Running:** {'yes' if price_updater.get('isRunning') else 'no'}")
        a(f"- **Current phase:** {price_updater.get('currentPhase', 'n/a')}")
        a(f"- **Cycle number:** {price_updater.get('cycleNumber', 'n/a')}")
        a(f"- **Last successful update:** {price_updater.get('lastSuccessfulUpdateAtUtc', 'n/a')}")
        a("")

    # v1 status
    v1 = report.get("v1", {})
    if v1:
        en_ps = v1.get("enPriceStatus", {})
        jp_ps = v1.get("jpPriceStatus", {})
        a("## Public v1 Price Status")
        a("")
        a(f"- **EN price records:** {en_ps.get('recordCount', 0):,} (source: {en_ps.get('source', 'n/a')}, status: {en_ps.get('status', 'n/a')}, last updated: {en_ps.get('lastUpdatedAtUtc') or 'n/a'})")
        a(f"- **JP price records:** {jp_ps.get('recordCount', 0):,} (source: {jp_ps.get('source', 'n/a')}, status: {jp_ps.get('status', 'n/a')}, last updated: {jp_ps.get('lastUpdatedAtUtc') or 'n/a'})")
        a("")

    # PokeWallet API capability audit
    api_audit = report.get("pokewalletApiCapabilityAudit", {})
    if api_audit.get("available"):
        a("## PokeWallet API Capability Audit")
        a("")
        a(f"- **Generated:** {api_audit.get('generatedAtUtc', 'n/a')}")
        a(f"- **Requests:** {api_audit.get('requestsSucceeded', 0)} succeeded / {api_audit.get('requestsAttempted', 0)} attempted")
        a(f"- **/prices works:** {'yes' if api_audit.get('pricesEndpointWorks') else 'no'}")
        a(f"- **JP price availability:** {api_audit.get('jpPriceAvailability', 'unknown')}")
        a(f"- **TCGPlayer USD useful:** {'yes' if api_audit.get('tcgplayerUsdUseful') else 'no'}")
        a(f"- **CardMarket-only useful:** {'yes' if api_audit.get('cardmarketOnlyUseful') else 'no'}")
        a(f"- **Image endpoint samples:** {api_audit.get('imageSamplesSucceeded', 0)} / {api_audit.get('imageSamplesRequested', 0)} succeeded")
        a(f"- **Set logo endpoint:** {api_audit.get('setLogoEndpoint') or 'n/a'} ({'tested' if api_audit.get('setLogoTested') else 'planned only'})")
        plan_limited = api_audit.get("planLimitedEndpoints", [])
        expected_pro = api_audit.get("expectedProEndpoints", [])
        if plan_limited:
            a(f"- **Plan/pro/trial required:** {', '.join(plan_limited)}")
        elif expected_pro:
            a(f"- **Plan/pro/trial required:** none observed on current plan; docs/pro-labeled endpoints tested: {', '.join(expected_pro)}")
        else:
            a("- **Plan/pro/trial required:** none observed")
        if api_audit.get("recommendation"):
            a(f"- **Recommended next action:** {api_audit.get('recommendation')}")
        endpoints = api_audit.get("endpoints", [])
        if endpoints:
            a("")
            a("### Endpoint Availability")
            a("")
            a("| Endpoint | HTTP | Availability | Usable prices |")
            a("|----------|-----:|--------------|---------------|")
            for item in endpoints:
                a(
                    f"| {item.get('label', '')} | "
                    f"{item.get('statusCode') if item.get('statusCode') is not None else 'n/a'} | "
                    f"{item.get('availability', 'unknown')} | "
                    f"{'yes' if item.get('hasUsablePrices') else 'no'} |"
                )
        a("")

    # Next action
    a("## Next Recommended Action")
    a("")
    a(report.get("nextRecommendedAction", ""))
    a("")

    a("---")
    a("*Generated by `tools/export_chatgpt_report.py`*")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Zip builder
# ---------------------------------------------------------------------------

def _is_safe_path(rel_path: str) -> bool:
    lower = rel_path.lower().replace("\\", "/")
    for pattern in EXCLUDE_PATTERNS:
        if pattern in lower:
            return False
    return True


def _build_zip(
    zip_path: Path,
    include_large: bool,
) -> list[str]:
    included_paths: list[str] = []
    files_to_zip = list(ZIP_FILES_DEFAULT)
    if include_large:
        files_to_zip.extend(ZIP_FILES_LARGE)

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel_path in files_to_zip:
            abs_path = ROOT / rel_path
            if not abs_path.exists():
                continue
            if not _is_safe_path(rel_path):
                print(f"  [zip] SKIP (safety): {rel_path}", file=sys.stderr)
                continue
            zf.write(abs_path, arcname=rel_path)
            included_paths.append(rel_path)

    return included_paths


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Export ChatGPT-uploadable CardScanR report")
    parser.add_argument(
        "--include-large-reports",
        action="store_true",
        help="Include large JSON report files in the zip",
    )
    parser.add_argument(
        "--no-zip",
        action="store_true",
        help="Skip zip bundle creation",
    )
    args = parser.parse_args()

    now_utc = datetime.now(timezone.utc)
    timestamp_str = now_utc.strftime("%Y%m%d_%H%M%S")
    generated_at = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    # Collect all data
    git_info = _collect_git_info()
    pipeline_info = _collect_pipeline_info()
    blocked_info = _collect_blocked_info()
    promotion_info = _collect_promotion_info()
    worker_info = _collect_worker_info()
    price_updater_info = _collect_price_updater_info()
    pokewallet_api_audit_info = _collect_pokewallet_api_capability_audit()
    v1_info = _collect_v1_counts()
    if pipeline_info.get("available") and v1_info.get("pricesByLanguage"):
        pipeline_info["pipelineReportPricesByLanguage"] = pipeline_info.get("pricesByLanguage", {})
        pipeline_info["pricesByLanguage"] = v1_info["pricesByLanguage"]
        pipeline_info["pricesByLanguageSource"] = "public_v1_current_price_files"

    next_action = _recommend_next_action(
        pipeline_info,
        worker_info,
        git_info,
        pokewallet_api_audit_info,
    )

    report: dict[str, Any] = {
        "schemaVersion": "1.0.0",
        "generatedAtUtc": generated_at,
        "git": git_info,
        "pipeline": pipeline_info,
        "blocked": blocked_info,
        "promotion": promotion_info,
        "worker": worker_info,
        "priceUpdater": price_updater_info,
        "pokewalletApiCapabilityAudit": pokewallet_api_audit_info,
        "v1": v1_info,
        "nextRecommendedAction": next_action,
    }

    markdown = _render_markdown(report)

    # Write latest + timestamped
    latest_md = EXPORT_DIR / "cardscanr_chatgpt_report_latest.md"
    latest_json = EXPORT_DIR / "cardscanr_chatgpt_report_latest.json"
    ts_md = EXPORT_DIR / f"cardscanr_chatgpt_report_{timestamp_str}.md"
    ts_json = EXPORT_DIR / f"cardscanr_chatgpt_report_{timestamp_str}.json"

    latest_md.write_text(markdown, encoding="utf-8")
    latest_json.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    ts_md.write_text(markdown, encoding="utf-8")
    ts_json.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    created_files: list[str] = [
        str(latest_md.relative_to(ROOT)),
        str(latest_json.relative_to(ROOT)),
        str(ts_md.relative_to(ROOT)),
        str(ts_json.relative_to(ROOT)),
    ]

    # Zip
    if not args.no_zip:
        latest_zip = EXPORT_DIR / "cardscanr_chatgpt_report_latest.zip"
        ts_zip = EXPORT_DIR / f"cardscanr_chatgpt_report_{timestamp_str}.zip"
        included = _build_zip(latest_zip, args.include_large_reports)
        # Copy latest zip to timestamped
        import shutil
        shutil.copy2(latest_zip, ts_zip)
        created_files.extend([
            str(latest_zip.relative_to(ROOT)),
            str(ts_zip.relative_to(ROOT)),
        ])

        print(f"\nZip includes ({len(included)} files):")
        for p in included:
            print(f"  + {p}")

    print("\nChatGPT report created:")
    for f in created_files:
        print(f"  {f}")

    print(f"\nNext recommended action:")
    print(f"  {next_action}")


if __name__ == "__main__":
    main()
