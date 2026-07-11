from __future__ import annotations

import json
import math
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import write_json_atomic


ROOT = Path(__file__).resolve().parent.parent
REPORT_DIR = ROOT / "reports" / "global_rollout"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _git_value(*args: str) -> str:
    process = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return process.stdout.strip() if process.returncode == 0 else "unknown"


def _storage_estimate(
    candidate_count: int,
    current: dict[str, Any],
    migration: dict[str, Any],
) -> dict[str, Any]:
    supabase = current.get("supabaseImageRecords") or {}
    completed = int((supabase.get("byStatus") or {}).get("completed") or 0)
    measured_thumb_bytes = int(supabase.get("thumbBytes") or 0)
    average_thumb = round(measured_thumb_bytes / completed) if completed else 20_000
    display_count = int(migration.get("displaySourceObjectsAvailable") or 0)
    measured_display_bytes = int(supabase.get("displayBytes") or 0)
    average_display = (
        round(measured_display_bytes / display_count)
        if display_count
        else max(120_000, average_thumb * 6)
    )
    projected = candidate_count * (average_thumb + average_display)
    current_r2_bytes = int((current.get("r2") or {}).get("byteSize") or 0)
    projected_total_decimal_gb = (current_r2_bytes + projected) / 1_000_000_000
    free_storage_gb_month = 10
    billable_storage_gb_month = max(
        0,
        math.ceil(projected_total_decimal_gb - free_storage_gb_month),
    )
    storage_cost_usd = round(billable_storage_gb_month * 0.015, 3)
    projected_writes = candidate_count * 2
    class_a_cost_if_free_tier_exhausted = (
        math.ceil(projected_writes / 1_000_000) * 4.50
        if projected_writes
        else 0
    )
    return {
        "currentR2Bytes": current_r2_bytes,
        "currentR2PokemonImageObjects": int(
            (current.get("r2") or {}).get("pokemonImageObjectCount") or 0
        ),
        "measuredAverageExistingThumbBytes": average_thumb,
        "measuredAverageExistingDisplayBytes": average_display,
        "displaySamples": display_count,
        "candidatePrintings": candidate_count,
        "projectedThumbAndDisplayBytes": projected,
        "projectedGiB": round(projected / (1024**3), 3),
        "projectedTotalBucketDecimalGB": round(projected_total_decimal_gb, 3),
        "projectedObjectWrites": projected_writes,
        "cloudflarePricingReviewedAt": "2026-07-11",
        "cloudflarePricingUrl": "https://developers.cloudflare.com/r2/pricing/",
        "standardStorageFreeTierGBMonth": free_storage_gb_month,
        "billableStorageGBMonthAfterRounding": billable_storage_gb_month,
        "estimatedMonthlyStorageCostUsd": storage_cost_usd,
        "estimatedClassACostUsdIfMonthlyFreeTierAvailable": 0,
        "estimatedClassACostUsdIfFreeTierExhausted": class_a_cost_if_free_tier_exhausted,
        "classBReadCost": "not estimable until application request volume is known",
        "estimateOnly": True,
        "paidExecutionApproved": False,
    }


def build_master_status() -> dict[str, Any]:
    current = _load_json(REPORT_DIR / "current_state.json")
    coverage = _load_json(REPORT_DIR / "language_coverage.json")
    credentials = _load_json(REPORT_DIR / "credential_status.json")
    preflight = _load_json(REPORT_DIR / "public_image_preflight_report.json")
    canary = _load_json(REPORT_DIR / "multilingual_100_card_canary_plan.json")
    migration = _load_json(REPORT_DIR / "supabase_to_r2_migration.json")
    provider_ledger = _load_json(REPORT_DIR / "provider_ledger.json")
    checkpoint = _load_json(REPORT_DIR / "checkpoints" / "tcgdex_metadata.json")
    catalogue_manifest = _load_json(ROOT / "data" / "global" / "catalogue" / "manifest.json")
    test_report = _load_json(REPORT_DIR / "test_report.json")
    direct_catalogue = _load_json(REPORT_DIR / "direct_image_catalogue.json")
    direct_canary = _load_json(REPORT_DIR / "direct_image_canary_report.json")
    direct_failures = _load_json(REPORT_DIR / "direct_image_permanent_failures.json")
    global_index = _load_json(REPORT_DIR / "global_search_index.json")

    language_rows = coverage.get("languages") or []
    languages = [str(row.get("language")) for row in language_rows if row.get("language")]
    ingested_languages = [
        str(row.get("language"))
        for row in language_rows
        if row.get("language") and int(row.get("sets") or 0) > 0
    ]
    totals = coverage.get("totals") or {}
    candidate_count = int(totals.get("imageCandidatesPresent") or 0)
    storage_estimate = _storage_estimate(candidate_count, current, migration)

    missing_credentials = [
        item
        for item in credentials.get("providers") or []
        if item.get("keyPresent") == "no"
        and item.get("requiredEnvironmentVariables")
        and item.get("requiredForCurrentMetadata")
    ]
    optional_missing_credentials = [
        item
        for item in credentials.get("providers") or []
        if item.get("keyPresent") == "no"
        and item.get("requiredEnvironmentVariables")
        and not item.get("requiredForCurrentMetadata")
    ]
    pokewallet_credential = next(
        (
            item
            for item in credentials.get("providers") or []
            if item.get("provider") == "pokewallet"
        ),
        {},
    )
    pokewallet_quota = (
        (pokewallet_credential.get("accountQuotaState") or {}).get("quota") or {}
    )
    pokewallet_daily_remaining = int(
        pokewallet_quota.get("X-RateLimit-Remaining-Day") or 0
    )
    terms_blockers = [
        {
            "provider": provider.get("provider"),
            "termsStatus": provider.get("termsStatus"),
            "imageRehostingStatus": provider.get("imageRehostingStatus"),
            "reason": provider.get("selfHostingOrRehostingPolicy"),
        }
        for provider in provider_ledger.get("providers") or []
        if provider.get("imageRehostingStatus")
        not in {"approved", "approved_with_conditions", "not_applicable"}
    ]
    paid_blockers = [
        {
            "provider": provider.get("provider"),
            "requirement": provider.get("paidPlanRequirements"),
            "termsStatus": provider.get("termsStatus"),
        }
        for provider in provider_ledger.get("providers") or []
        if provider.get("provider") == "scrydex"
    ]
    metadata_by_language = {
        str(row.get("language")): {
            "region": row.get("region"),
            "sets": row.get("sets"),
            "canonicalPrintingGroups": row.get("canonicalPrintings"),
            "nativeNamePercent": (row.get("coveragePercent") or {}).get("nativeNames"),
            "releaseDatePercent": (row.get("coveragePercent") or {}).get("releaseDates"),
            "rarityPercent": (row.get("coveragePercent") or {}).get("rarity"),
        }
        for row in language_rows
    }
    image_by_language = {
        str(row.get("language")): {
            "region": row.get("region"),
            "candidates": row.get("imageCandidatesPresent"),
            "verified": row.get("verifiedImages"),
        }
        for row in language_rows
    }
    status = {
        "schemaVersion": "1.0.0",
        "updatedAtUtc": utc_now_iso(),
        "classification": "PARTIAL",
        "currentPhase": "Phase 6 source-gap research and Phase 9 Flutter QA integration in progress",
        "phaseCompletion": {
            "phase1CurrentStateAudit": "complete",
            "phase2LanguageRegionContract": "complete",
            "phase3CanonicalIdentityContract": "complete_provisional_variant_evidence",
            "phase4ProviderLedger": "complete_terms_gate_open",
            "phase5CredentialPreflight": "complete",
            "phase6PublicMetadataIngestion": "complete_tcgdex_all_accessible_languages",
            "phase7CoverageReconciliation": "complete",
            "phase8ImageAcquisitionModel": "implemented",
            "phase9ProviderSafeDownloads": "implemented_and_tested_no_global_download",
            "phase10PublicPreflight": "complete",
            "phase10MultilingualCanaryPlans": "complete",
            "phase10R2MirrorCanaryExecution": "blocked_terms_identity_and_r2_write_gate",
            "briefPhase4DirectImageCatalogue": "complete",
            "briefPhase5DirectImageCanaries": "pass_human_visual_review_pending",
            "phase12SupabaseMigrationPlan": "complete_execution_blocked",
            "phase13ProductionWiring": "not_started",
            "phase14GlobalSearchIndex": "complete_non_production_canary_v2",
            "phase15FlutterQa": "not_started_flutter_unchanged",
        },
        "repository": {
            "branch": _git_value("branch", "--show-current"),
            "head": _git_value("rev-parse", "HEAD"),
            "commitCreated": False,
        },
        "languagesDiscovered": languages,
        "languagesIngested": ingested_languages,
        "totalCanonicalPrintingGroups": int(
            totals.get("canonicalPrintings")
            or catalogue_manifest.get("canonicalPrintingGroups")
            or 0
        ),
        "totalCanonicalSets": int(
            totals.get("sets") or catalogue_manifest.get("canonicalSets") or 0
        ),
        "metadataCoverageByLanguage": metadata_by_language,
        "imageCoverageByLanguage": image_by_language,
        "publicFreeImageCandidates": candidate_count,
        "directImageCatalogue": direct_catalogue,
        "directImageCanary": {
            "classification": direct_canary.get("classification"),
            "tested": direct_canary.get("tested"),
            "stateCounts": direct_canary.get("stateCounts") or {},
            "byLanguage": direct_canary.get("byLanguage") or {},
            "humanVisualApproval": direct_canary.get("humanVisualApproval", False),
        },
        "globalSearchIndex": global_index,
        "verifiedR2Thumbnails": 0,
        "verifiedR2DisplayImages": 0,
        "migratedExistingImages": int(migration.get("migratedAndR2Verified") or 0),
        "providerBreakdown": {
            "metadata": {"tcgdex": int(totals.get("canonicalPrintings") or 0)},
            "imageCandidates": coverage.get("imageCandidatesByProvider") or {},
            "existingCompletedSupabase": (
                (current.get("supabaseImageRecords") or {}).get("completedByProvider") or {}
            ),
        },
        "credentialBlockers": missing_credentials,
        "optionalMissingCredentials": optional_missing_credentials,
        "paidProviderBlockers": paid_blockers,
        "legalTermsBlockers": terms_blockers,
        "unresolvedIdentityCount": int(totals.get("unresolvedRecords") or 0),
        "ambiguousIdentityCount": int(totals.get("ambiguousRecords") or 0),
        "variantUnresolvedCount": int(totals.get("variantUnresolved") or 0),
        "missingImageCount": (
            int(totals.get("canonicalPrintings") or 0) - candidate_count
        ),
        "permanentProviderFailures": max(
            int(checkpoint.get("permanent404s") or 0),
            int(direct_failures.get("failureCount") or 0),
        ),
        "publicImagePreflight": {
            "classification": preflight.get("classification"),
            "requests": preflight.get("requestsPerformed"),
            "available": preflight.get("available"),
            "states": preflight.get("stateCounts"),
        },
        "canary": {
            "classification": canary.get("classification"),
            "plannedCards": canary.get("totalPlannedCards"),
            "executed": canary.get("executionPerformed", False),
            "blockers": canary.get("blockedProviders") or [],
        },
        "supabaseMigration": {
            "recordsAudited": migration.get("supabaseRecordsAudited"),
            "sourceObjectsAvailable": migration.get("sourceObjectsAvailable"),
            "sourceChecksumsVerified": migration.get("sourceChecksumsVerified"),
            "r2Writes": migration.get("r2WritesPerformed", 0),
        },
        "storage": storage_estimate,
        "apiQuotaState": {
            item.get("provider"): item.get("accountQuotaState")
            for item in credentials.get("providers") or []
        },
        "tests": test_report,
        "lastCheckpoint": (
            "reports/global_rollout/checkpoints/tcgdex_metadata.json"
            if checkpoint
            else None
        ),
        "productionPublication": {
            "cataloguePublished": False,
            "searchManifestReplaced": False,
            "flutterModified": False,
            "supabaseAssetsDeleted": False,
            "r2AssetsDeleted": False,
        },
        "exactBlockers": [
            "TCGdex artwork rehosting permission is not explicit; public image candidates cannot be copied to R2.",
            "All set-level canonical records remain cardVariant=unspecified, so physical finish identity is provisional.",
            (
                f"{int((migration.get('migrationStateCounts') or {}).get('blocked_identity_unresolved') or 0)} "
                "existing Supabase thumbnails lack a safe exact global crosswalk."
            ),
            *(
                [
                    f"PokéWallet has only {pokewallet_daily_remaining} "
                    "free daily requests remaining; no bulk request is permitted in this window."
                ]
                if 0 < pokewallet_daily_remaining < 100
                else []
            ),
            (
                f"Projected R2 storage is {storage_estimate['projectedTotalBucketDecimalGB']} GB, "
                f"above the 10 GB-month free tier; estimated rounded storage cost is "
                f"US${storage_estimate['estimatedMonthlyStorageCostUsd']}/month, while the configured "
                "unexpected-spend budget is US$0."
            ),
            "Scrydex requires a paid Starter plan and its terms prohibit mirroring without prior written authorization.",
            "Production publication and R2 image writes require explicit approval.",
        ],
        "nextSafeAction": (
            "Review/accept an artwork rehosting basis for TCGdex and resolve physical variant evidence; "
            "optionally configure only approved provider credentials."
        ),
        "exactNextCommand": "python tools/global_rollout.py status",
        "resumeCommand": "python tools/global_rollout.py resume",
    }
    write_json_atomic(REPORT_DIR / "MASTER_STATUS.json", status)
    (REPORT_DIR / "MASTER_STATUS.md").write_text(
        render_master_status_markdown(status),
        encoding="utf-8",
    )
    return status


def render_master_status_markdown(status: dict[str, Any]) -> str:
    lines = [
        "# CardScanR Global Rollout — Master Status",
        "",
        f"Classification: **{status['classification']}**",
        "",
        f"- Current phase: {status['currentPhase']}",
        f"- Branch / HEAD: `{status['repository']['branch']}` / `{status['repository']['head']}`",
        f"- Languages: {', '.join(status['languagesIngested'])}",
        f"- Canonical printing groups: {status['totalCanonicalPrintingGroups']}",
        f"- Canonical sets: {status['totalCanonicalSets']}",
        f"- Public/free image candidates: {status['publicFreeImageCandidates']}",
        f"- Verified R2 thumbnails/displays: {status['verifiedR2Thumbnails']}/{status['verifiedR2DisplayImages']}",
        f"- Migrated existing images: {status['migratedExistingImages']}",
        f"- Unresolved identities: {status['unresolvedIdentityCount']}",
        f"- Variant-unresolved groups: {status['variantUnresolvedCount']}",
        f"- Projected image storage: {status['storage']['projectedGiB']} GiB",
        f"- Estimated rounded monthly R2 storage cost: US${status['storage']['estimatedMonthlyStorageCostUsd']}",
        "",
        "## Blockers",
        "",
    ]
    lines.extend(f"- {blocker}" for blocker in status["exactBlockers"])
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- Production catalogue/index publication: **not performed**",
            "- Flutter repository modification: **not performed**",
            "- R2 image writes/deletes: **not performed**",
            "- Supabase deletes: **not performed**",
            "",
            f"Next safe command: `{status['exactNextCommand']}`",
            f"Resume command: `{status['resumeCommand']}`",
            "",
        ]
    )
    return "\n".join(lines)
