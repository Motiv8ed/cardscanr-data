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
APP_CATALOG_ROOT = ROOT / "public" / "v1" / "catalog" / "pokemon"
IMAGE_MANIFEST_PATH = ROOT / "public" / "v1" / "images" / "cards-manifest.json"

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
    "reports/pokewallet_price_import_latest.json",
    "reports/pokewallet_price_import_latest.md",
    "reports/pokewallet_missing_price_worker_latest.json",
    "reports/pokewallet_missing_price_worker_latest.md",
    "reports/jp_price_coverage_latest.json",
    "reports/jp_price_coverage_latest.md",
    "reports/provider_languages_latest.json",
    "reports/provider_languages_latest.md",
    "reports/zh_catalogue_readiness_latest.json",
    "reports/zh_catalogue_readiness_latest.md",
    "reports/zh_duplicate_identities_latest.json",
    "reports/zh_duplicate_identities_latest.md",
    "reports/zh_promotion_plan_latest.json",
    "reports/zh_promotion_plan_latest.md",
    "reports/image_cache_strategy_latest.json",
    "reports/image_cache_strategy_latest.md",
    "public/v1/markets/cardscanr-markets.json",
    "public/v1/markets/marketplace-sources.json",
    "public/v1/markets/onboarding-questionnaire.json",
    "public/v1/markets/market-price-schema.json",
    "public/v1/markets/market-price-status.json",
    "docs/market_pricing/EBAY_MARKET_PRICING_READINESS.md",
    "docs/market_pricing/MARKET_PRICE_DATA_MODEL.md",
    "reports/market_price_query_samples_latest.json",
    "reports/market_price_query_samples_latest.md",
    "reports/market_pricing_worker_latest.json",
    "reports/market_pricing_worker_latest.md",
    "reports/market_pricing_jobs_latest.json",
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
        output = subprocess.check_output(
            ["git", *args],
            cwd=ROOT,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        # Preserve leading spaces on porcelain lines; trimming both sides can
        # shift the first path by one character when the first status starts
        # with a space (for example: " M path").
        return output.rstrip("\r\n")
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
        currency_counts: dict[str, int] = {}
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
                currency = str(record.get("currency") or payload.get("currency") or "unknown")
                source_counts[source] = source_counts.get(source, 0) + 1
                status_counts[status] = status_counts.get(status, 0) + 1
                currency_counts[currency] = currency_counts.get(currency, 0) + 1

        result[language_dir.name] = {
            "recordCount": record_count,
            "fileCount": file_count,
            "sourceCounts": dict(sorted(source_counts.items())),
            "statusCounts": dict(sorted(status_counts.items())),
            "currencyCounts": dict(sorted(currency_counts.items())),
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
    currency_counts = actual_counts.get("currencyCounts") if isinstance(actual_counts.get("currencyCounts"), dict) else {}

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
        "languageStatus": (
            "partial"
            if actual_record_count > 0
            and status_payload.get("status") in ("not_available", "unavailable", "catalogue_only")
            else status_payload.get("status")
        ),
        "sourceCounts": source_counts,
        "statusCounts": status_counts,
        "currencyCounts": currency_counts,
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


def _collect_pokewallet_price_import_report() -> dict[str, Any]:
    data = _load_json("reports/pokewallet_price_import_latest.json")
    if data is None:
        return {"available": False}

    return {
        "available": True,
        "startedAtUtc": data.get("startedAtUtc"),
        "finishedAtUtc": data.get("finishedAtUtc"),
        "status": data.get("status"),
        "mode": data.get("mode"),
        "languages": data.get("languages", []),
        "sourceMode": data.get("sourceMode"),
        "onlyMissingSetPrices": data.get("onlyMissingSetPrices", False),
        "skipExistingPriceFiles": data.get("skipExistingPriceFiles", False),
        "refreshExistingPriceFiles": data.get("refreshExistingPriceFiles", False),
        "maxNewSets": data.get("maxNewSets", 0),
        "startAfterSet": data.get("startAfterSet"),
        "existingPriceFilesSkipped": data.get("existingPriceFilesSkipped", 0),
        "missingPriceSetsSelected": data.get("missingPriceSetsSelected", 0),
        "selectedSetIds": data.get("selectedSetIds", []),
        "estimatedNewCoverage": data.get("estimatedNewCoverage", {}),
        "plannedRequests": data.get("plannedRequests", 0),
        "requestsAllowedByBudget": data.get("requestsAllowedByBudget", 0),
        "requestsSkippedDueToBudget": data.get("requestsSkippedDueToBudget", 0),
        "hourlyUsed": data.get("hourlyUsed", 0),
        "hourlyRemaining": data.get("hourlyRemaining", 0),
        "dailyUsed": data.get("dailyUsed", 0),
        "dailyRemaining": data.get("dailyRemaining", 0),
        "budgetSource": data.get("budgetSource"),
        "budgetDecision": data.get("budgetDecision"),
        "rateLimitDetected": data.get("rateLimitDetected", False),
        "allEndpointsFailed": data.get("allEndpointsFailed", False),
        "setsSelected": data.get("setsSelected", []),
        "apiRequestsUsed": data.get("apiRequestsUsed", 0),
        "endpointSuccesses": data.get("endpointSuccesses", 0),
        "endpointFailures": data.get("endpointFailures", 0),
        "priceRecordsReceived": data.get("priceRecordsReceived", 0),
        "matchedRecords": data.get("matchedRecords", 0),
        "wouldImportRecords": data.get("wouldImportRecords", 0),
        "importedRecords": data.get("importedRecords", 0),
        "skippedExistingBetterRecords": data.get("skippedExistingBetterRecords", 0),
        "ambiguousRecords": data.get("ambiguousRecords", 0),
        "unmatchedRecords": data.get("unmatchedRecords", 0),
        "unusableRecords": data.get("unusableRecords", 0),
        "recordsByLanguage": data.get("recordsByLanguage", {}),
        "recordsBySource": data.get("recordsBySource", {}),
        "recordsByCurrency": data.get("recordsByCurrency", {}),
        "recordsByVariant": data.get("recordsByVariant", {}),
        "beforeCurrentPriceCounts": data.get("beforeCurrentPriceCounts", {}),
        "afterCurrentPriceCounts": data.get("afterCurrentPriceCounts", {}),
        "validationResult": data.get("validationResult"),
        "nextRecommendedAction": data.get("nextRecommendedAction", ""),
        "nextRecommendedSafeCommand": data.get("nextRecommendedSafeCommand", ""),
    }


def _collect_pokewallet_price_budget_ledger() -> dict[str, Any]:
    data = _load_json("data/pokewallet_price_request_ledger.json")
    if data is None:
        return {"available": False}

    rows = data.get("requests") if isinstance(data.get("requests"), list) else []
    parsed_rows: list[tuple[datetime, dict[str, Any]]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ts = str(row.get("timestampUtc") or "").strip()
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
            parsed_rows.append((dt, row))
        except ValueError:
            continue

    now = datetime.now(timezone.utc)
    hourly_cutoff = now.timestamp() - 3600
    daily_cutoff = now.timestamp() - 86400
    hourly_used = 0
    daily_used = 0
    rate_limited_last_day = 0
    last_timestamp: str | None = None
    for dt, row in parsed_rows:
        epoch = dt.timestamp()
        if epoch >= daily_cutoff:
            daily_used += 1
            if int(row.get("statusCode") or 0) == 429:
                rate_limited_last_day += 1
            if epoch >= hourly_cutoff:
                hourly_used += 1
        if last_timestamp is None or dt.isoformat() > last_timestamp:
            last_timestamp = dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "available": True,
        "schemaVersion": data.get("schemaVersion"),
        "generatedAtUtc": data.get("generatedAtUtc"),
        "requestCount": len(parsed_rows),
        "hourlyUsed": hourly_used,
        "dailyUsed": daily_used,
        "rateLimitedResponsesLast24h": rate_limited_last_day,
        "lastRequestAtUtc": last_timestamp,
    }


def _collect_pokewallet_missing_price_worker_report() -> dict[str, Any]:
    data = _load_json("reports/pokewallet_missing_price_worker_latest.json")
    if data is None:
        return {"available": False}
    return {
        "available": True,
        "startedAtUtc": data.get("startedAtUtc"),
        "finishedAtUtc": data.get("finishedAtUtc"),
        "status": data.get("status"),
        "stopReason": data.get("stopReason"),
        "cyclesAttempted": data.get("cyclesAttempted", 0),
        "cyclesCompleted": data.get("cyclesCompleted", 0),
        "cyclesBlockedByBudget": data.get("cyclesBlockedByBudget", 0),
        "totalApiRequests": data.get("totalApiRequests", 0),
        "totalImportedRecords": data.get("totalImportedRecords", 0),
        "beforeJpPriceCount": data.get("beforeJpPriceCount", 0),
        "afterJpPriceCount": data.get("afterJpPriceCount", 0),
        "beforeJpPriceFileCount": data.get("beforeJpPriceFileCount", 0),
        "afterJpPriceFileCount": data.get("afterJpPriceFileCount", 0),
        "lastSelectedSetIds": data.get("lastSelectedSetIds", []),
        "lastImporterStatus": data.get("lastImporterStatus"),
        "validationResults": data.get("validationResults", []),
        "commitHashesPushed": data.get("commitHashesPushed", []),
        "nextRecommendedCommand": data.get("nextRecommendedCommand", ""),
        "gitSync": data.get("gitSync", {}),
    }


def _collect_jp_price_coverage_audit() -> dict[str, Any]:
    data = _load_json("reports/jp_price_coverage_latest.json")
    if data is None:
        return {"available": False}

    catalogue = data.get("catalogue") if isinstance(data.get("catalogue"), dict) else {}
    current_price_files = data.get("currentPriceFiles") if isinstance(data.get("currentPriceFiles"), dict) else {}
    set_coverage = data.get("setCoverage") if isinstance(data.get("setCoverage"), dict) else {}
    duplicate_and_ambiguous = data.get("duplicateAndAmbiguous") if isinstance(data.get("duplicateAndAmbiguous"), dict) else {}
    breakdowns = data.get("breakdowns") if isinstance(data.get("breakdowns"), dict) else {}
    readiness = data.get("appReadinessSummary") if isinstance(data.get("appReadinessSummary"), dict) else {}
    support = data.get("supportingReports") if isinstance(data.get("supportingReports"), dict) else {}

    return {
        "available": True,
        "generatedAtUtc": data.get("generatedAtUtc"),
        "ledgerPath": data.get("ledgerPath"),
        "totalJpAppCatalogueCards": catalogue.get("totalJpAppCatalogueCards", 0),
        "coveredJpAppCatalogueCards": catalogue.get("coveredJpAppCatalogueCards", 0),
        "uncoveredJpAppCatalogueCards": catalogue.get("uncoveredJpAppCatalogueCards", 0),
        "coveragePct": catalogue.get("coveragePct", 0.0),
        "currentPriceFileCount": current_price_files.get("fileCount", 0),
        "currentPriceRecordCount": current_price_files.get("recordCount", 0),
        "worstMissingCoverageSets": set_coverage.get("worstMissingCoverageSets", []),
        "bestCoverageSets": set_coverage.get("bestCoverageSets", []),
        "cardsWithMultipleCurrentPriceRows": duplicate_and_ambiguous.get("cardsWithMultipleCurrentPriceRows", 0),
        "exactDuplicatePriceRowCount": duplicate_and_ambiguous.get("exactDuplicatePriceRowCount", 0),
        "orphanCurrentPriceRows": duplicate_and_ambiguous.get("orphanCurrentPriceRows", 0),
        "currentPriceRowsWithoutCanonicalCardId": duplicate_and_ambiguous.get("currentPriceRowsWithoutCanonicalCardId", 0),
        "sourceCounts": breakdowns.get("sourceCounts", {}),
        "currencyCounts": breakdowns.get("currencyCounts", {}),
        "variantCounts": breakdowns.get("variantCounts", {}),
        "appReadinessStatus": readiness.get("status"),
        "appReadinessMessage": readiness.get("message"),
        "appReadinessNextStep": readiness.get("nextStep"),
        "appReadinessNotes": readiness.get("notes", []),
        "latestImportMissingPriceSetsSelected": support.get("latestImportMissingPriceSetsSelected"),
        "latestImportUnmatchedRecords": support.get("latestImportUnmatchedRecords"),
        "latestImportUnusableRecords": support.get("latestImportUnusableRecords"),
        "latestImportValidationResult": support.get("latestImportValidationResult"),
        "workerStatus": support.get("workerStatus"),
    }


def _collect_provider_language_audit() -> dict[str, Any]:
    data = _load_json("reports/provider_languages_latest.json")
    if data is None:
        return {"available": False}

    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    rows = data.get("languageRows") if isinstance(data.get("languageRows"), list) else []
    return {
        "available": True,
        "generatedAtUtc": data.get("generatedAtUtc"),
        "provider": data.get("provider"),
        "languagesFound": summary.get("languagesFound", []),
        "appSupportedLanguages": summary.get("appSupportedLanguages", []),
        "promotedToAppCatalogueLanguages": summary.get("promotedToAppCatalogueLanguages", []),
        "currentPriceSupportedLanguages": summary.get("currentPriceSupportedLanguages", []),
        "providerLanguageCodesFound": data.get("providerLanguageCodesFound", {}),
        "languageRows": rows,
    }


def _collect_zh_catalogue_readiness_audit() -> dict[str, Any]:
    data = _load_json("reports/zh_catalogue_readiness_latest.json")
    if data is None:
        return {"available": False}

    readiness = data.get("readiness") if isinstance(data.get("readiness"), dict) else {}
    duplicates = data.get("duplicateCanonicalIdentity") if isinstance(data.get("duplicateCanonicalIdentity"), dict) else {}
    return {
        "available": True,
        "generatedAtUtc": data.get("generatedAtUtc"),
        "zhProviderCardCount": data.get("zhProviderCardCount", 0),
        "zhSetCount": data.get("zhSetCount", 0),
        "recordsWithProviderCardId": data.get("recordsWithProviderCardId", 0),
        "recordsWithUsableNameOrOriginalName": data.get("recordsWithUsableNameOrOriginalName", 0),
        "recordsWithSetIdentity": data.get("recordsWithSetIdentity", 0),
        "recordsWithCollectorNumber": data.get("recordsWithCollectorNumber", 0),
        "recordsWithImageUrl": data.get("recordsWithImageUrl", 0),
        "duplicateIdentityKeyCount": duplicates.get("duplicateIdentityKeyCount", 0),
        "duplicateIdentityRecordCount": duplicates.get("duplicateIdentityRecordCount", 0),
        "estimatedPromotableZhRecords": data.get("estimatedPromotableZhRecords", 0),
        "estimatedPromotableRatio": data.get("estimatedPromotableRatio", 0.0),
        "blockedReasonCounts": data.get("blockedReasonCounts", {}),
        "topBlockedSets": data.get("topBlockedSets", []),
        "topBlockedSetReasons": data.get("topBlockedSetReasons", []),
        "status": readiness.get("status"),
        "safeToPromoteNow": readiness.get("safeToPromoteNow") is True,
        "recommendation": readiness.get("recommendation"),
    }


def _collect_zh_duplicate_identity_audit() -> dict[str, Any]:
    data = _load_json("reports/zh_duplicate_identities_latest.json")
    if data is None:
        return {"available": False}

    duplicate_groups = data.get("duplicateGroups") if isinstance(data.get("duplicateGroups"), list) else []
    top_group = duplicate_groups[0] if duplicate_groups and isinstance(duplicate_groups[0], dict) else {}
    return {
        "available": True,
        "generatedAtUtc": data.get("generatedAtUtc"),
        "candidateRecordCount": data.get("candidateRecordCount", 0),
        "duplicateGroupCount": data.get("duplicateGroupCount", 0),
        "duplicateRecordCount": data.get("duplicateRecordCount", 0),
        "blockedNonDuplicateReasonCounts": data.get("blockedNonDuplicateReasonCounts", {}),
        "duplicateRootCauseCounts": data.get("duplicateRootCauseCounts", {}),
        "topGroupSize": top_group.get("groupSize", 0),
        "topGroupIdentity": top_group.get("canonicalIdentityKey"),
        "topGroupRootCause": top_group.get("suspectedRootCause"),
    }


def _collect_zh_promotion_plan() -> dict[str, Any]:
    data = _load_json("reports/zh_promotion_plan_latest.json")
    if data is None:
        return {"available": False}

    current = data.get("current") if isinstance(data.get("current"), dict) else {}
    after = data.get("afterProposedFix") if isinstance(data.get("afterProposedFix"), dict) else {}
    proposal = data.get("proposal") if isinstance(data.get("proposal"), dict) else {}
    examples = data.get("exampleGeneratedCanonicalIds") if isinstance(data.get("exampleGeneratedCanonicalIds"), list) else []
    return {
        "available": True,
        "generatedAtUtc": data.get("generatedAtUtc"),
        "proposalName": proposal.get("name"),
        "proposalRule": proposal.get("rule"),
        "enJpUnchanged": proposal.get("enJpUnchanged") is True,
        "userFacingDisplayUnchanged": proposal.get("userFacingDisplayUnchanged") is True,
        "currentBlockedCount": current.get("currentBlockedCount", 0),
        "currentPromotableCount": current.get("currentPromotableCount", 0),
        "resolvedDuplicateCount": after.get("resolvedDuplicateCount", 0),
        "finalPromotableCount": after.get("finalPromotableCount", 0),
        "remainingBlockers": after.get("remainingBlockers", 0),
        "safeToPromoteAfterFix": after.get("safeToPromoteAfterFix") is True,
        "exampleGeneratedCanonicalIds": examples[:5],
    }


def _collect_image_cache_strategy_report() -> dict[str, Any]:
    data = _load_json("reports/image_cache_strategy_latest.json")
    if data is None:
        return {"available": False}

    policy = data.get("currentPolicy") if isinstance(data.get("currentPolicy"), dict) else {}
    app_behavior = data.get("appDeviceCacheRecommendation") if isinstance(data.get("appDeviceCacheRecommendation"), dict) else {}
    bounded = app_behavior.get("boundedCacheSize") if isinstance(app_behavior.get("boundedCacheSize"), dict) else {}
    return {
        "available": True,
        "generatedAtUtc": data.get("generatedAtUtc"),
        "strategy": policy.get("strategy"),
        "localCacheEnabled": policy.get("localCacheEnabled") is True,
        "localCachedBinaryCount": data.get("localCachedBinaryCount", 0),
        "imageManifestRecordsByLanguage": data.get("imageManifestRecordsByLanguage", {}),
        "gitBinaryImageCacheRecommended": (data.get("gitBinaryImageCacheNotRecommended") or {}).get("recommended") is True,
        "gitBinaryReasons": (data.get("gitBinaryImageCacheNotRecommended") or {}).get("reasons", []),
        "loadUrlFirst": app_behavior.get("loadUrlFirst") is True,
        "cacheOnDevice": app_behavior.get("cacheOnDevice") is True,
        "prefetchSavedInventoryAndRecentScans": app_behavior.get("prefetchSavedInventoryAndRecentScans") is True,
        "placeholderAndErrorState": app_behavior.get("placeholderAndErrorState") is True,
        "boundedCacheEnabled": bounded.get("enabled") is True,
        "boundedCacheMaxMb": bounded.get("recommendedMaxMb"),
        "boundedCacheEvictionPolicy": bounded.get("evictionPolicy"),
        "externalStorageOptions": data.get("externalStorageOptions", []),
        "recommendation": data.get("recommendation"),
    }


def _collect_market_readiness_config() -> dict[str, Any]:
    markets = _load_json("public/v1/markets/cardscanr-markets.json")
    sources = _load_json("public/v1/markets/marketplace-sources.json")
    onboarding = _load_json("public/v1/markets/onboarding-questionnaire.json")
    readiness_doc_path = ROOT / "docs" / "market_pricing" / "EBAY_MARKET_PRICING_READINESS.md"

    if markets is None and sources is None and onboarding is None and not readiness_doc_path.exists():
        return {"available": False}

    market_rows = markets.get("markets", []) if isinstance(markets, dict) else []
    source_rows = sources.get("sources", []) if isinstance(sources, dict) else []
    questionnaire_rows = (
        ((onboarding.get("questionnaire") or {}).get("questions", []))
        if isinstance(onboarding, dict)
        else []
    )

    planned_sold_markets = []
    for row in market_rows:
        if not isinstance(row, dict):
            continue
        sold = row.get("soldListingPricing") if isinstance(row.get("soldListingPricing"), dict) else {}
        if sold.get("planned") is True:
            planned_sold_markets.append(str(row.get("marketId") or ""))

    sources_by_status: dict[str, int] = {}
    for row in source_rows:
        if not isinstance(row, dict):
            continue
        status = str(row.get("availabilityStatus") or "unknown")
        sources_by_status[status] = int(sources_by_status.get(status, 0)) + 1

    return {
        "available": True,
        "marketsConfigured": len([row for row in market_rows if isinstance(row, dict)]),
        "marketIds": [str(row.get("marketId") or "") for row in market_rows if isinstance(row, dict)],
        "marketSourceDefinitions": len([row for row in source_rows if isinstance(row, dict)]),
        "marketSourcesByAvailabilityStatus": dict(sorted(sources_by_status.items())),
        "plannedSoldListingMarkets": sorted([value for value in planned_sold_markets if value]),
        "onboardingQuestionCount": len([row for row in questionnaire_rows if isinstance(row, dict)]),
        "onboardingQuestionIds": [str(row.get("id") or "") for row in questionnaire_rows if isinstance(row, dict)],
        "ebayReadinessDocAvailable": readiness_doc_path.exists(),
    }


def _collect_market_pricing_foundation() -> dict[str, Any]:
    query_samples = _load_json("reports/market_price_query_samples_latest.json")
    worker_latest = _load_json("reports/market_pricing_worker_latest.json")
    worker_jobs = _load_json("reports/market_pricing_jobs_latest.json")
    market_status = _load_json("public/v1/markets/market-price-status.json")
    market_schema = _load_json("public/v1/markets/market-price-schema.json")

    if all(item is None for item in [query_samples, worker_latest, worker_jobs, market_status, market_schema]):
        return {"available": False}

    query_counts: dict[str, int] = {}
    if isinstance(query_samples, dict):
        market_samples = query_samples.get("marketSamples")
        if isinstance(market_samples, dict):
            for market, rows in market_samples.items():
                if isinstance(rows, list):
                    query_counts[str(market)] = len(rows)

    worker_summary = worker_latest.get("summary") if isinstance(worker_latest, dict) and isinstance(worker_latest.get("summary"), dict) else {}
    source_status = market_status.get("sourceStatus") if isinstance(market_status, dict) and isinstance(market_status.get("sourceStatus"), dict) else {}

    return {
        "available": True,
        "schemaVersion": market_schema.get("schemaVersion") if isinstance(market_schema, dict) else None,
        "statusVersion": market_status.get("schemaVersion") if isinstance(market_status, dict) else None,
        "liveEbayWorkerStatus": source_status.get("liveEbayWorker") or (market_status or {}).get("liveEbayWorkerStatus"),
        "mockProviderStatus": source_status.get("mockProvider"),
        "manualProviderStatus": source_status.get("manualProvider"),
        "lastWorkerRunAtUtc": (market_status or {}).get("lastWorkerRunAtUtc") if isinstance(market_status, dict) else None,
        "workerStatus": (worker_latest or {}).get("status") if isinstance(worker_latest, dict) else None,
        "workerRecordsBuilt": worker_summary.get("recordsBuilt", 0),
        "workerJobsProcessed": worker_summary.get("jobsProcessed", 0),
        "workerMode": (worker_latest or {}).get("mode") if isinstance(worker_latest, dict) else None,
        "querySamplesAvailable": isinstance(query_samples, dict),
        "querySampleCountsByMarket": dict(sorted(query_counts.items())),
        "queryGeneratedAtUtc": (query_samples or {}).get("generatedAtUtc") if isinstance(query_samples, dict) else None,
        "workerGeneratedAtUtc": (worker_latest or {}).get("generatedAtUtc") if isinstance(worker_latest, dict) else None,
        "jobReportGeneratedAtUtc": (worker_jobs or {}).get("generatedAtUtc") if isinstance(worker_jobs, dict) else None,
        "warning": "Live eBay scraping is not enabled yet."
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


def _collect_app_catalogue_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    if not APP_CATALOG_ROOT.exists():
        return counts

    for language_dir in sorted([item for item in APP_CATALOG_ROOT.iterdir() if item.is_dir()], key=lambda item: item.name.lower()):
        cards_dir = language_dir / "cards"
        if not cards_dir.exists():
            continue
        card_count = 0
        for path in sorted(cards_dir.glob("*.json"), key=lambda item: item.name.lower()):
            payload = _load_json(str(path.relative_to(ROOT).as_posix())) or {}
            cards = payload.get("cards")
            if isinstance(cards, list):
                card_count += len([item for item in cards if isinstance(item, dict)])
        counts[language_dir.name] = card_count
    return counts


def _collect_image_manifest_counts() -> dict[str, int]:
    manifest = _load_json("public/v1/images/cards-manifest.json")
    if not isinstance(manifest, dict):
        return {}

    language_map = manifest.get("languageCountMap")
    if isinstance(language_map, dict):
        return {str(language): int(count or 0) for language, count in language_map.items()}

    records = manifest.get("records")
    if not isinstance(records, list):
        return {}
    counts: dict[str, int] = {}
    for row in records:
        if not isinstance(row, dict):
            continue
        language = str(row.get("language") or "").strip().lower()
        if not language:
            continue
        counts[language] = counts.get(language, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Next-action recommendation
# ---------------------------------------------------------------------------

def _recommend_next_action(
    pipeline: dict[str, Any],
    worker: dict[str, Any],
    git: dict[str, Any],
    pokewallet_api_audit: dict[str, Any] | None = None,
    pokewallet_price_import: dict[str, Any] | None = None,
    pokewallet_missing_price_worker: dict[str, Any] | None = None,
    provider_language_audit: dict[str, Any] | None = None,
    zh_catalogue_readiness: dict[str, Any] | None = None,
    image_cache_strategy: dict[str, Any] | None = None,
    market_readiness_config: dict[str, Any] | None = None,
    v1: dict[str, Any] | None = None,
) -> str:
    issues: list[str] = []
    worker_cmd = ".\\scripts\\run_pokewallet_missing_price_worker.ps1 -Language jp -MaxNewSetsPerCycle 20 -UntilComplete"
    if pokewallet_missing_price_worker and pokewallet_missing_price_worker.get("available"):
        preferred = str(pokewallet_missing_price_worker.get("nextRecommendedCommand") or "").strip()
        if preferred and "-DryRunOnly" not in preferred:
            worker_cmd = preferred

    if pokewallet_price_import and pokewallet_price_import.get("available"):
        if pokewallet_price_import.get("rateLimitDetected") and pokewallet_price_import.get("allEndpointsFailed"):
            hourly_reset_hint = ""
            if isinstance(pokewallet_price_import.get("hourlyRemaining"), int):
                hourly_reset_hint = (
                    f" Remaining hourly safe budget: {pokewallet_price_import.get('hourlyRemaining', 0)}."
                )
            worker_sleep_cmd = (
                " .\\scripts\\run_pokewallet_missing_price_worker.ps1 -Language jp -MaxNewSetsPerCycle 20 "
                "-UntilComplete -SleepWhenBudgetBlocked -PollSeconds 300"
            )
            return (
                "PokeWallet price import was fully rate-limited (all endpoints failed with HTTP 429). "
                "Wait for hourly budget reset before another write; prefer the automated worker with sleep mode."
                f" Command:{worker_sleep_cmd}"
                + hourly_reset_hint
            )
        if pokewallet_price_import.get("status") in {"failed", "rate_limited"}:
            return (
                "Latest PokeWallet price import failed. Review status codes/error snippets in "
                "reports/pokewallet_price_import_latest.json, then continue via the worker command: "
                f"{worker_cmd}"
            )

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
        jp_count = 0
        if isinstance(v1, dict):
            jp_count = int((v1.get("jpPriceStatus") or {}).get("recordCount") or 0)
        if jp_count == 0:
            jp_count = int((pipeline.get("pricesByLanguage", {}).get("jp") or {}).get("recordCount") or 0)

        worker_complete = (
            bool(pokewallet_missing_price_worker)
            and pokewallet_missing_price_worker.get("available")
            and pokewallet_missing_price_worker.get("status") == "complete"
        )
        import_missing_sets_selected = None
        if pokewallet_price_import and pokewallet_price_import.get("available"):
            import_missing_sets_selected = int(pokewallet_price_import.get("missingPriceSetsSelected") or 0)

        zh_ready = bool(zh_catalogue_readiness and zh_catalogue_readiness.get("available") and zh_catalogue_readiness.get("safeToPromoteNow"))
        zh_promoted = False
        zh_app_supported = False
        if provider_language_audit and provider_language_audit.get("available"):
            for row in provider_language_audit.get("languageRows", []):
                if not isinstance(row, dict):
                    continue
                if str(row.get("language") or "").strip().lower() != "zh":
                    continue
                zh_promoted = bool(row.get("promotedToAppCatalogue"))
                zh_app_supported = bool(row.get("appSupported"))
                break
        has_market_config = bool(market_readiness_config and market_readiness_config.get("available"))

        if worker_complete and import_missing_sets_selected == 0:
            if has_market_config:
                if zh_promoted and zh_app_supported:
                    return (
                        "All checks passing. JP missing-set price import is complete and market readiness configs are in place. "
                        "ZH is promoted and app-supported in catalogue-only mode (pricing still unavailable). "
                        "Next: wire onboarding/settings integration in app and design eBay sold-listing worker contracts (no live scraping yet)."
                    )
                if zh_ready:
                    return (
                        "All checks passing. JP missing-set price import is complete and market readiness configs are in place. "
                        "Next: wire onboarding/settings integration in app, run controlled ZH promotion validation, "
                        "then design eBay sold-listing worker contracts (no live scraping yet)."
                    )
                return (
                    "All checks passing. JP missing-set price import is complete and market readiness configs are in place. "
                    "Next: wire onboarding/settings integration in app, keep ZH unpromoted until readiness blockers are resolved, "
                    "and design eBay sold-listing worker contracts later (no live scraping yet)."
                )
            return (
                "All checks passing. JP missing-set price import is complete. "
                "Next audits: unmatched/unusable price records audit, JP card price coverage by app card, "
                "and app integration validation."
            )

        if jp_count > 0:
            return (
                "All checks passing. JP prices are partially imported from PokeWallet and currently partially covered. "
                "Next step: continue automated missing-set worker cycles until complete. "
                f"Command: {worker_cmd}"
            )

        if (
            pokewallet_api_audit
            and pokewallet_api_audit.get("available")
            and pokewallet_api_audit.get("pricesEndpointWorks")
        ):
            if pokewallet_api_audit.get("jpPriceAvailability") == "usable_prices_found":
                return (
                    "All checks passing. PokeWallet /prices is available and sampled JP data is usable. "
                    "Next: run a bounded JP dry-run import (max 25 sets), validate source/currency/variant distributions, "
                    "then run a write pass if diagnostics remain clean."
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
    v1 = report.get("v1", {})
    price_import = report.get("pokewalletPriceImport", {})
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

        raw_missing_areas = pipeline.get("missingIncompleteAreas", [])
        provider_audit = report.get("providerLanguageAudit") if isinstance(report.get("providerLanguageAudit"), dict) else {}
        provider_rows = provider_audit.get("languageRows") if isinstance(provider_audit.get("languageRows"), list) else []
        zh_is_promoted = False
        zh_is_app_supported = False
        for row in provider_rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("language") or "").strip().lower() != "zh":
                continue
            zh_is_promoted = bool(row.get("promotedToAppCatalogue"))
            zh_is_app_supported = bool(row.get("appSupported"))
            break
        missing_areas: list[str] = []
        for area in raw_missing_areas:
            area_text = str(area)
            lower_area = area_text.lower()
            if (
                "jp current prices are unavailable" in lower_area
                or "jp prices are unavailable" in lower_area
                or "keep public jp prices unavailable" in lower_area
            ):
                continue
            if (
                zh_is_promoted
                and zh_is_app_supported
                and "zh provider data exists" in lower_area
                and ("not app-supported" in lower_area or "not app supported" in lower_area or "not promoted" in lower_area)
            ):
                continue
            missing_areas.append(area_text)

        jp_price_status = v1.get("jpPriceStatus", {}) if isinstance(v1, dict) else {}
        jp_count = int(jp_price_status.get("recordCount") or 0)
        provider_jp = int((pipeline.get("providerByLanguage", {}) or {}).get("jp") or 0)
        app_jp = int((pipeline.get("appCatalogueByLanguage", {}) or {}).get("jp") or 0)
        jp_reference = provider_jp or app_jp

        source_counts = jp_price_status.get("sourceCounts") if isinstance(jp_price_status.get("sourceCounts"), dict) else {}
        currency_counts = jp_price_status.get("currencyCounts") if isinstance(jp_price_status.get("currencyCounts"), dict) else {}
        if not source_counts and isinstance(price_import, dict):
            source_counts = price_import.get("recordsBySource") if isinstance(price_import.get("recordsBySource"), dict) else {}
        if not currency_counts and isinstance(price_import, dict):
            currency_counts = price_import.get("recordsByCurrency") if isinstance(price_import.get("recordsByCurrency"), dict) else {}

        source_summary = ", ".join(f"{k}: {int(v):,}" for k, v in sorted(source_counts.items())) if source_counts else "n/a"
        currency_summary = ", ".join(f"{k}: {int(v):,}" for k, v in sorted(currency_counts.items())) if currency_counts else "n/a"

        if jp_count == 0:
            missing_areas.append("JP current prices are unavailable from non-eBay sources.")
        elif jp_reference > 0 and jp_count < jp_reference:
            missing_areas.append(
                "JP current prices are partially available from PokeWallet "
                f"({jp_count:,} of {jp_reference:,} records; sources: {source_summary}; currencies: {currency_summary})."
            )

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
    if v1:
        en_ps = v1.get("enPriceStatus", {})
        jp_ps = v1.get("jpPriceStatus", {})

        def _format_counts(counts: dict[str, Any] | None) -> str:
            if not isinstance(counts, dict) or not counts:
                return "n/a"
            return ", ".join(f"{k}: {int(v):,}" for k, v in sorted(counts.items()))

        a("## Public v1 Price Status")
        a("")
        a(
            f"- **EN price records:** {en_ps.get('recordCount', 0):,} "
            f"(source: {en_ps.get('source', 'n/a')}, status: {en_ps.get('status', 'n/a')}, "
            f"sources: {_format_counts(en_ps.get('sourceCounts'))}, currencies: {_format_counts(en_ps.get('currencyCounts'))}, "
            f"last updated: {en_ps.get('lastUpdatedAtUtc') or 'n/a'})"
        )
        a(
            f"- **JP price records:** {jp_ps.get('recordCount', 0):,} "
            f"(source: {jp_ps.get('source', 'n/a')}, status: {jp_ps.get('status', 'n/a')}, "
            f"sources: {_format_counts(jp_ps.get('sourceCounts'))}, currencies: {_format_counts(jp_ps.get('currencyCounts'))}, "
            f"last updated: {jp_ps.get('lastUpdatedAtUtc') or 'n/a'})"
        )
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

    # PokeWallet price import report
    price_import = report.get("pokewalletPriceImport", {})
    jp_price_coverage_audit = report.get("jpPriceCoverageAudit", {})
    provider_language_audit = report.get("providerLanguageAudit", {})
    zh_catalogue_readiness = report.get("zhCatalogueReadiness", {})
    zh_duplicate_identity_audit = report.get("zhDuplicateIdentityAudit", {})
    zh_promotion_plan = report.get("zhPromotionPlan", {})
    image_cache_strategy = report.get("imageCacheStrategy", {})
    market_readiness_config = report.get("marketReadinessConfig", {})
    market_pricing_foundation = report.get("marketPricingFoundation", {})
    missing_price_worker = report.get("pokewalletMissingPriceWorker", {})
    if price_import.get("available"):
        a("## PokeWallet Price Import")
        a("")
        a(f"- **Status:** {price_import.get('status', 'n/a')}")
        a(f"- **Mode:** {price_import.get('mode', 'n/a')}")
        a(f"- **Languages:** {', '.join(price_import.get('languages', []))}")
        a(f"- **Source mode:** {price_import.get('sourceMode', 'n/a')}")
        a(f"- **Only missing set prices:** {'yes' if price_import.get('onlyMissingSetPrices') else 'no'}")
        a(f"- **Existing price files skipped:** {price_import.get('existingPriceFilesSkipped', 0)}")
        a(f"- **Missing price sets selected:** {price_import.get('missingPriceSetsSelected', 0)}")
        a(f"- **Selected set ids:** {price_import.get('selectedSetIds', [])}")
        a(f"- **Estimated new coverage:** {price_import.get('estimatedNewCoverage', {})}")
        a(f"- **Planned requests:** {price_import.get('plannedRequests', 0)}")
        a(f"- **Requests allowed by budget:** {price_import.get('requestsAllowedByBudget', 0)}")
        a(f"- **Requests skipped due to budget:** {price_import.get('requestsSkippedDueToBudget', 0)}")
        a(f"- **Budget source/decision:** {price_import.get('budgetSource', 'n/a')} / {price_import.get('budgetDecision', 'n/a')}")
        a(f"- **Hourly used/remaining:** {price_import.get('hourlyUsed', 0)} / {price_import.get('hourlyRemaining', 0)}")
        a(f"- **Daily used/remaining:** {price_import.get('dailyUsed', 0)} / {price_import.get('dailyRemaining', 0)}")
        a(f"- **Rate limit detected:** {'yes' if price_import.get('rateLimitDetected') else 'no'}")
        a(f"- **API requests:** {price_import.get('apiRequestsUsed', 0)}")
        a(f"- **Endpoint success/failure:** {price_import.get('endpointSuccesses', 0)} / {price_import.get('endpointFailures', 0)}")
        a(f"- **Price records received:** {price_import.get('priceRecordsReceived', 0):,}")
        a(f"- **Matched records:** {price_import.get('matchedRecords', 0):,}")
        a(f"- **Imported records:** {price_import.get('importedRecords', 0):,}")
        a(f"- **Would import records:** {price_import.get('wouldImportRecords', 0):,}")
        a(f"- **Skipped existing better records:** {price_import.get('skippedExistingBetterRecords', 0):,}")
        a(f"- **Ambiguous/unmatched/unusable:** {price_import.get('ambiguousRecords', 0):,} / {price_import.get('unmatchedRecords', 0):,} / {price_import.get('unusableRecords', 0):,}")
        a(f"- **By source:** {price_import.get('recordsBySource', {})}")
        a(f"- **By currency:** {price_import.get('recordsByCurrency', {})}")
        a(f"- **Validation result:** {price_import.get('validationResult', 'n/a')}")
        if price_import.get("nextRecommendedAction"):
            a(f"- **Recommended next action:** {price_import.get('nextRecommendedAction')}")
        if price_import.get("nextRecommendedSafeCommand"):
            a(f"- **Recommended safe command:** {price_import.get('nextRecommendedSafeCommand')}")
        a("")

    if jp_price_coverage_audit.get("available"):
        a("## JP Price Coverage Audit")
        a("")
        a(f"- **Generated:** {jp_price_coverage_audit.get('generatedAtUtc', 'n/a')}")
        a(
            f"- **Coverage:** {int(jp_price_coverage_audit.get('coveredJpAppCatalogueCards', 0)):,} / "
            f"{int(jp_price_coverage_audit.get('totalJpAppCatalogueCards', 0)):,} "
            f"({float(jp_price_coverage_audit.get('coveragePct', 0.0)):.2f}%)"
        )
        a(f"- **Cards without current price:** {int(jp_price_coverage_audit.get('uncoveredJpAppCatalogueCards', 0)):,}")
        a(
            f"- **Current price files / rows:** {int(jp_price_coverage_audit.get('currentPriceFileCount', 0)):,} / "
            f"{int(jp_price_coverage_audit.get('currentPriceRecordCount', 0)):,}"
        )
        a(f"- **Worst missing-coverage sets:** {jp_price_coverage_audit.get('worstMissingCoverageSets', [])[:3]}")
        a(f"- **Best coverage sets:** {jp_price_coverage_audit.get('bestCoverageSets', [])[:3]}")
        a(f"- **Duplicate/ambiguous rows:** multi-row cards={int(jp_price_coverage_audit.get('cardsWithMultipleCurrentPriceRows', 0)):,}, exact duplicates={int(jp_price_coverage_audit.get('exactDuplicatePriceRowCount', 0)):,}")
        a(f"- **Unmatched/unusable summary:** orphan rows={int(jp_price_coverage_audit.get('orphanCurrentPriceRows', 0)):,}, missing canonical ids={int(jp_price_coverage_audit.get('currentPriceRowsWithoutCanonicalCardId', 0)):,}, latest import unmatched={jp_price_coverage_audit.get('latestImportUnmatchedRecords', 'n/a')}, latest import unusable={jp_price_coverage_audit.get('latestImportUnusableRecords', 'n/a')}")
        a(f"- **App readiness:** {jp_price_coverage_audit.get('appReadinessStatus', 'n/a')} — {jp_price_coverage_audit.get('appReadinessMessage', '')}")
        if jp_price_coverage_audit.get("appReadinessNextStep"):
            a(f"- **Next step:** {jp_price_coverage_audit.get('appReadinessNextStep')}")
        a("")

    if provider_language_audit.get("available"):
        a("## Provider Language Audit")
        a("")
        a(f"- **Generated:** {provider_language_audit.get('generatedAtUtc', 'n/a')}")
        a(f"- **Languages found:** {provider_language_audit.get('languagesFound', [])}")
        a(f"- **App-supported languages:** {provider_language_audit.get('appSupportedLanguages', [])}")
        a(f"- **Promoted languages:** {provider_language_audit.get('promotedToAppCatalogueLanguages', [])}")
        a(f"- **Current price languages:** {provider_language_audit.get('currentPriceSupportedLanguages', [])}")
        a(f"- **Provider language codes:** {provider_language_audit.get('providerLanguageCodesFound', {})}")
        rows = provider_language_audit.get("languageRows", [])
        if isinstance(rows, list) and rows:
            a("")
            a("| Language | Provider cards | Provider sets | App-supported | Promoted | Prices |")
            a("|----------|---------------:|--------------:|---------------|----------|--------|")
            for row in rows:
                if not isinstance(row, dict):
                    continue
                a(
                    f"| {row.get('language', '')} | "
                    f"{int(row.get('providerCardCount', 0)):,} | "
                    f"{int(row.get('providerSetCount', 0)):,} | "
                    f"{'yes' if row.get('appSupported') else 'no'} | "
                    f"{'yes' if row.get('promotedToAppCatalogue') else 'no'} | "
                    f"{'yes' if row.get('hasCurrentPriceSupport') else 'no'} |"
                )
        a("")

    if zh_catalogue_readiness.get("available"):
        a("## ZH Catalogue Readiness")
        a("")
        a(f"- **Generated:** {zh_catalogue_readiness.get('generatedAtUtc', 'n/a')}")
        a(f"- **Readiness status:** {zh_catalogue_readiness.get('status', 'unknown')}")
        a(f"- **ZH provider cards/sets:** {int(zh_catalogue_readiness.get('zhProviderCardCount', 0)):,} / {int(zh_catalogue_readiness.get('zhSetCount', 0)):,}")
        a(f"- **Records with provider ID / usable name / set identity / collector / image:** {int(zh_catalogue_readiness.get('recordsWithProviderCardId', 0)):,} / {int(zh_catalogue_readiness.get('recordsWithUsableNameOrOriginalName', 0)):,} / {int(zh_catalogue_readiness.get('recordsWithSetIdentity', 0)):,} / {int(zh_catalogue_readiness.get('recordsWithCollectorNumber', 0)):,} / {int(zh_catalogue_readiness.get('recordsWithImageUrl', 0)):,}")
        a(f"- **Duplicate identity keys/records:** {int(zh_catalogue_readiness.get('duplicateIdentityKeyCount', 0)):,} / {int(zh_catalogue_readiness.get('duplicateIdentityRecordCount', 0)):,}")
        a(f"- **Estimated promotable ZH records:** {int(zh_catalogue_readiness.get('estimatedPromotableZhRecords', 0)):,} ({float(zh_catalogue_readiness.get('estimatedPromotableRatio', 0.0)) * 100:.2f}%)")
        a(f"- **Blocked reasons:** {zh_catalogue_readiness.get('blockedReasonCounts', {})}")
        a(f"- **Safe to promote now:** {'yes' if zh_catalogue_readiness.get('safeToPromoteNow') else 'no'}")
        if zh_catalogue_readiness.get("recommendation"):
            a(f"- **Recommendation:** {zh_catalogue_readiness.get('recommendation')}")
        a("")

    if zh_duplicate_identity_audit.get("available"):
        a("## ZH Duplicate Identity Audit")
        a("")
        a(f"- **Generated:** {zh_duplicate_identity_audit.get('generatedAtUtc', 'n/a')}")
        a(f"- **Candidate records:** {int(zh_duplicate_identity_audit.get('candidateRecordCount', 0)):,}")
        a(f"- **Duplicate groups/records:** {int(zh_duplicate_identity_audit.get('duplicateGroupCount', 0)):,} / {int(zh_duplicate_identity_audit.get('duplicateRecordCount', 0)):,}")
        a(f"- **Root causes:** {zh_duplicate_identity_audit.get('duplicateRootCauseCounts', {})}")
        a(f"- **Top duplicate group:** {zh_duplicate_identity_audit.get('topGroupIdentity', 'n/a')} (size={int(zh_duplicate_identity_audit.get('topGroupSize', 0)):,}, cause={zh_duplicate_identity_audit.get('topGroupRootCause', 'n/a')})")
        a("")

    if zh_promotion_plan.get("available"):
        a("## ZH Promotion Plan (Dry Run)")
        a("")
        a(f"- **Generated:** {zh_promotion_plan.get('generatedAtUtc', 'n/a')}")
        a(f"- **Proposal:** {zh_promotion_plan.get('proposalName', 'n/a')}")
        a(f"- **Rule:** {zh_promotion_plan.get('proposalRule', 'n/a')}")
        a(f"- **EN/JP unchanged:** {'yes' if zh_promotion_plan.get('enJpUnchanged') else 'no'}")
        a(f"- **Current blocked/promotable:** {int(zh_promotion_plan.get('currentBlockedCount', 0)):,} / {int(zh_promotion_plan.get('currentPromotableCount', 0)):,}")
        a(f"- **Resolved duplicates:** {int(zh_promotion_plan.get('resolvedDuplicateCount', 0)):,}")
        a(f"- **Final promotable:** {int(zh_promotion_plan.get('finalPromotableCount', 0)):,}")
        a(f"- **Remaining blockers:** {int(zh_promotion_plan.get('remainingBlockers', 0)):,}")
        a(f"- **Safe to promote after fix:** {'yes' if zh_promotion_plan.get('safeToPromoteAfterFix') else 'no'}")
        examples = zh_promotion_plan.get("exampleGeneratedCanonicalIds", [])
        if isinstance(examples, list) and examples:
            a("- **Example generated IDs:**")
            for item in examples[:3]:
                if isinstance(item, dict):
                    a(
                        f"  - {item.get('currentCanonicalBaseId', '')} -> "
                        f"{item.get('proposedCanonicalBaseId', '')}"
                    )
        a("")

    if image_cache_strategy.get("available"):
        a("## Image Cache Strategy")
        a("")
        a(f"- **Generated:** {image_cache_strategy.get('generatedAtUtc', 'n/a')}")
        a(f"- **Strategy:** {image_cache_strategy.get('strategy', 'n/a')} (local cache enabled: {'yes' if image_cache_strategy.get('localCacheEnabled') else 'no'})")
        a(f"- **Image manifest records by language:** {image_cache_strategy.get('imageManifestRecordsByLanguage', {})}")
        a(f"- **Local cached binary count:** {int(image_cache_strategy.get('localCachedBinaryCount', 0)):,}")
        a(f"- **Load URL first / cache on device:** {'yes' if image_cache_strategy.get('loadUrlFirst') else 'no'} / {'yes' if image_cache_strategy.get('cacheOnDevice') else 'no'}")
        a(f"- **Prefetch saved inventory/recent scans:** {'yes' if image_cache_strategy.get('prefetchSavedInventoryAndRecentScans') else 'no'}")
        a(f"- **Placeholder/error state:** {'yes' if image_cache_strategy.get('placeholderAndErrorState') else 'no'}")
        a(f"- **Bounded cache:** {'yes' if image_cache_strategy.get('boundedCacheEnabled') else 'no'} (max MB={image_cache_strategy.get('boundedCacheMaxMb', 'n/a')}, policy={image_cache_strategy.get('boundedCacheEvictionPolicy', 'n/a')})")
        a(f"- **External storage options:** {image_cache_strategy.get('externalStorageOptions', [])}")
        if image_cache_strategy.get("recommendation"):
            a(f"- **Recommendation:** {image_cache_strategy.get('recommendation')}")
        a("")

    if market_readiness_config.get("available"):
        a("## Market/eBay Readiness Config")
        a("")
        a(f"- **Markets configured:** {int(market_readiness_config.get('marketsConfigured', 0)):,} ({market_readiness_config.get('marketIds', [])})")
        a(f"- **Source definitions:** {int(market_readiness_config.get('marketSourceDefinitions', 0)):,}")
        a(f"- **Sources by status:** {market_readiness_config.get('marketSourcesByAvailabilityStatus', {})}")
        a(f"- **Planned sold-listing markets:** {market_readiness_config.get('plannedSoldListingMarkets', [])}")
        a(f"- **Onboarding question count:** {int(market_readiness_config.get('onboardingQuestionCount', 0)):,}")
        a(f"- **Onboarding question ids:** {market_readiness_config.get('onboardingQuestionIds', [])}")
        a(f"- **eBay readiness doc available:** {'yes' if market_readiness_config.get('ebayReadinessDocAvailable') else 'no'}")
        a("")

    if market_pricing_foundation.get("available"):
        a("## Market Pricing Foundation")
        a("")
        a(f"- **Live eBay worker status:** {market_pricing_foundation.get('liveEbayWorkerStatus', 'disabled')}")
        a(f"- **Mock provider status:** {market_pricing_foundation.get('mockProviderStatus', 'unknown')}")
        a(f"- **Manual provider status:** {market_pricing_foundation.get('manualProviderStatus', 'unknown')}")
        a(f"- **Last worker run:** {market_pricing_foundation.get('lastWorkerRunAtUtc', 'n/a')}")
        a(f"- **Worker summary:** status={market_pricing_foundation.get('workerStatus', 'n/a')}, mode={market_pricing_foundation.get('workerMode', 'n/a')}, jobs={market_pricing_foundation.get('workerJobsProcessed', 0)}, records={market_pricing_foundation.get('workerRecordsBuilt', 0)}")
        a(f"- **Query sample counts by market:** {market_pricing_foundation.get('querySampleCountsByMarket', {})}")
        a(f"- **Warning:** {market_pricing_foundation.get('warning', 'Live eBay scraping is not enabled yet.')}")
        a("")

    if missing_price_worker.get("available"):
        a("## PokeWallet Missing Price Worker")
        a("")
        a(f"- **Status:** {missing_price_worker.get('status', 'n/a')}")
        a(f"- **Stop reason:** {missing_price_worker.get('stopReason', 'n/a')}")
        a(f"- **Cycles attempted/completed:** {missing_price_worker.get('cyclesAttempted', 0)} / {missing_price_worker.get('cyclesCompleted', 0)}")
        a(f"- **Cycles blocked by budget:** {missing_price_worker.get('cyclesBlockedByBudget', 0)}")
        a(f"- **Total API requests:** {missing_price_worker.get('totalApiRequests', 0)}")
        a(f"- **Total imported records:** {missing_price_worker.get('totalImportedRecords', 0)}")
        a(f"- **JP records before/after:** {missing_price_worker.get('beforeJpPriceCount', 0)} / {missing_price_worker.get('afterJpPriceCount', 0)}")
        a(f"- **JP set files before/after:** {missing_price_worker.get('beforeJpPriceFileCount', 0)} / {missing_price_worker.get('afterJpPriceFileCount', 0)}")
        a(f"- **Last importer status:** {missing_price_worker.get('lastImporterStatus', 'n/a')}")
        a(f"- **Last selected set ids:** {missing_price_worker.get('lastSelectedSetIds', [])}")
        a(f"- **Commit hashes pushed:** {missing_price_worker.get('commitHashesPushed', [])}")
        git_sync = missing_price_worker.get("gitSync", {}) if isinstance(missing_price_worker.get("gitSync"), dict) else {}
        if git_sync:
            a(f"- **Git sync status:** {git_sync.get('status', 'n/a')} ({git_sync.get('commandStyle', 'n/a')})")
        if missing_price_worker.get("nextRecommendedCommand"):
            a(f"- **Recommended worker command:** {missing_price_worker.get('nextRecommendedCommand')}")
        a("")

    budget_ledger = report.get("pokewalletPriceBudgetLedger", {})
    if budget_ledger.get("available"):
        a("## PokeWallet Price Budget Ledger")
        a("")
        a(f"- **Ledger generated at:** {budget_ledger.get('generatedAtUtc', 'n/a')}")
        a(f"- **Total request rows:** {budget_ledger.get('requestCount', 0)}")
        a(f"- **Hourly used (rolling):** {budget_ledger.get('hourlyUsed', 0)}")
        a(f"- **Daily used (rolling):** {budget_ledger.get('dailyUsed', 0)}")
        a(f"- **HTTP 429 responses (last 24h):** {budget_ledger.get('rateLimitedResponsesLast24h', 0)}")
        a(f"- **Last request:** {budget_ledger.get('lastRequestAtUtc') or 'n/a'}")
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
    pokewallet_price_import_info = _collect_pokewallet_price_import_report()
    pokewallet_price_budget_ledger_info = _collect_pokewallet_price_budget_ledger()
    pokewallet_missing_price_worker_info = _collect_pokewallet_missing_price_worker_report()
    jp_price_coverage_audit_info = _collect_jp_price_coverage_audit()
    provider_language_audit_info = _collect_provider_language_audit()
    zh_catalogue_readiness_info = _collect_zh_catalogue_readiness_audit()
    zh_duplicate_identity_audit_info = _collect_zh_duplicate_identity_audit()
    zh_promotion_plan_info = _collect_zh_promotion_plan()
    image_cache_strategy_info = _collect_image_cache_strategy_report()
    market_readiness_config_info = _collect_market_readiness_config()
    market_pricing_foundation_info = _collect_market_pricing_foundation()
    v1_info = _collect_v1_counts()
    app_catalogue_counts = _collect_app_catalogue_counts()
    image_manifest_counts = _collect_image_manifest_counts()
    if pipeline_info.get("available") and v1_info.get("pricesByLanguage"):
        pipeline_info["pipelineReportPricesByLanguage"] = pipeline_info.get("pricesByLanguage", {})
        pipeline_info["pricesByLanguage"] = v1_info["pricesByLanguage"]
        pipeline_info["pricesByLanguageSource"] = "public_v1_current_price_files"
    if pipeline_info.get("available") and app_catalogue_counts:
        pipeline_info["pipelineReportAppCatalogueByLanguage"] = pipeline_info.get("appCatalogueByLanguage", {})
        pipeline_info["appCatalogueByLanguage"] = app_catalogue_counts
        pipeline_info["appCatalogueByLanguageSource"] = "public_v1_catalog_files"
    if pipeline_info.get("available") and image_manifest_counts:
        pipeline_info["pipelineReportImageManifestByLanguage"] = pipeline_info.get("imageManifestByLanguage", {})
        pipeline_info["imageManifestByLanguage"] = image_manifest_counts
        pipeline_info["imageManifestByLanguageSource"] = "public_v1_images_cards_manifest"

    next_action = _recommend_next_action(
        pipeline_info,
        worker_info,
        git_info,
        pokewallet_api_audit_info,
        pokewallet_price_import_info,
        pokewallet_missing_price_worker_info,
        provider_language_audit_info,
        zh_catalogue_readiness_info,
        image_cache_strategy_info,
        market_readiness_config_info,
        v1_info,
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
        "pokewalletPriceImport": pokewallet_price_import_info,
        "pokewalletPriceBudgetLedger": pokewallet_price_budget_ledger_info,
        "pokewalletMissingPriceWorker": pokewallet_missing_price_worker_info,
        "jpPriceCoverageAudit": jp_price_coverage_audit_info,
        "providerLanguageAudit": provider_language_audit_info,
        "zhCatalogueReadiness": zh_catalogue_readiness_info,
        "zhDuplicateIdentityAudit": zh_duplicate_identity_audit_info,
        "zhPromotionPlan": zh_promotion_plan_info,
        "imageCacheStrategy": image_cache_strategy_info,
        "marketReadinessConfig": market_readiness_config_info,
        "marketPricingFoundation": market_pricing_foundation_info,
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
