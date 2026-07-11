from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cardscanr_global_catalogue.artifacts import write_contract_artifacts
from cardscanr_global_catalogue.audit import audit_repository
from cardscanr_global_catalogue.contracts import write_json_atomic
from cardscanr_global_catalogue.images import (
    create_multilingual_canary_plan,
    plan_supabase_to_r2_migration,
    run_public_image_preflight,
)
from cardscanr_global_catalogue.metadata import (
    RequestBudgetExhausted,
    ingest_tcgdex,
    normalize_global_catalogue,
)
from cardscanr_global_catalogue.providers import (
    credential_status,
    write_credential_status_reports,
    write_provider_ledger,
)
from cardscanr_global_catalogue.reconciliation import (
    reconcile_global_catalogue,
    validate_global_catalogue_schema,
)
from cardscanr_global_catalogue.status import build_master_status
from cardscanr_global_catalogue.permissions import image_canary_guard, permissions_status


REPORT_DIR = ROOT / "reports" / "global_rollout"


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _credential_console_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "providers": [
            {
                "provider": item["provider"],
                "keyPresent": item["keyPresent"],
                "keyValidation": item["keyValidation"],
                "accountQuotaState": item["accountQuotaState"],
                "requiredEnvironmentVariables": item["requiredEnvironmentVariables"],
            }
            for item in payload["providers"]
        ]
    }


def command_credentials_status(args: argparse.Namespace) -> int:
    payload = credential_status(
        validate=args.validate,
        provider_filter=args.provider,
    )
    write_credential_status_reports(payload)
    _print_json(_credential_console_payload(payload))
    return 0


def command_audit(_args: argparse.Namespace) -> int:
    state = audit_repository()
    _print_json(
        {
            "branch": state["repository"]["branch"],
            "head": state["repository"]["head"],
            "workingTreeClean": state["repository"]["workingTreeClean"],
            "r2": {
                key: state["r2"].get(key)
                for key in (
                    "accessible",
                    "objectCount",
                    "byteSize",
                    "pokemonImageObjectCount",
                )
            },
            "supabaseImageRecords": {
                key: state["supabaseImageRecords"].get(key)
                for key in (
                    "reachable",
                    "recordCount",
                    "byStatus",
                    "completedByProvider",
                )
            },
        }
    )
    return 0


def command_write_contracts(_args: argparse.Namespace) -> int:
    _print_json(write_contract_artifacts())
    return 0


def command_provider_ledger(_args: argparse.Namespace) -> int:
    payload = write_provider_ledger()
    _print_json(
        {
            "providers": [
                {
                    "provider": item["provider"],
                    "termsStatus": item["termsStatus"],
                    "imageRehostingStatus": item["imageRehostingStatus"],
                }
                for item in payload["providers"]
            ]
        }
    )
    return 0


def command_ingest_metadata(args: argparse.Namespace) -> int:
    if args.provider != "tcgdex":
        raise SystemExit(
            "Only the safe global TCGdex source is enabled. Existing Pokémon TCG API and "
            "PokéWallet data are reconciled from the preserved local catalogue."
        )
    try:
        payload = ingest_tcgdex(
            refresh=args.refresh,
            max_network_requests=args.max_network_requests,
            request_interval_seconds=args.request_interval_seconds,
        )
    except RequestBudgetExhausted as exc:
        _print_json(
            {
                "classification": "PARTIAL",
                "state": "request_budget_exhausted",
                "message": str(exc),
                "resumeCommand": (
                    "python tools/global_rollout.py ingest-metadata "
                    "--provider tcgdex --resume"
                ),
            }
        )
        return 2
    write_json_atomic(REPORT_DIR / "metadata_ingestion_report.json", payload)
    _print_json(payload)
    return 0


def command_normalize(_args: argparse.Namespace) -> int:
    _print_json(normalize_global_catalogue())
    return 0


def command_reconcile(_args: argparse.Namespace) -> int:
    payload = reconcile_global_catalogue()
    _print_json(
        {
            "classification": payload["classification"],
            "totals": payload["totals"],
            "languageCount": len(payload["languages"]),
        }
    )
    return 0


def command_validate_catalogue(_args: argparse.Namespace) -> int:
    payload = validate_global_catalogue_schema()
    _print_json(payload)
    return 0 if payload["classification"] == "PASS" else 1


def command_image_preflight(args: argparse.Namespace) -> int:
    payload = run_public_image_preflight(
        samples_per_language_region=args.samples_per_language,
        request_interval_seconds=args.request_interval_seconds,
    )
    _print_json(
        {
            "classification": payload["classification"],
            "requestsPerformed": payload["requestsPerformed"],
            "available": payload["available"],
            "stateCounts": payload["stateCounts"],
            "r2Writes": payload["r2Writes"],
        }
    )
    return 0


def command_plan_canaries(args: argparse.Namespace) -> int:
    payload = create_multilingual_canary_plan(sample_size=args.sample_size)
    _print_json(
        {
            "classification": payload["classification"],
            "batchCount": payload["batchCount"],
            "totalPlannedCards": payload["totalPlannedCards"],
            "blockedProviders": payload["blockedProviders"],
            "executionPerformed": payload["executionPerformed"],
        }
    )
    return 0


def command_plan_migration(args: argparse.Namespace) -> int:
    payload = plan_supabase_to_r2_migration(
        verify_source_objects=not args.skip_source_verification
    )
    _print_json(
        {
            "classification": payload["classification"],
            "supabaseRecordsAudited": payload["supabaseRecordsAudited"],
            "sourceObjectsAvailable": payload["sourceObjectsAvailable"],
            "sourceChecksumsVerified": payload["sourceChecksumsVerified"],
            "canonicalPrintingGroupsMatched": payload["canonicalPrintingGroupsMatched"],
            "migratedAndR2Verified": payload["migratedAndR2Verified"],
            "r2WritesPerformed": payload["r2WritesPerformed"],
        }
    )
    return 0


def command_status(_args: argparse.Namespace) -> int:
    status = build_master_status()
    _print_json(
        {
            "classification": status["classification"],
            "currentPhase": status["currentPhase"],
            "languagesIngested": status["languagesIngested"],
            "totalCanonicalPrintingGroups": status["totalCanonicalPrintingGroups"],
            "verifiedR2Thumbnails": status["verifiedR2Thumbnails"],
            "exactBlockers": status["exactBlockers"],
            "exactNextCommand": status["exactNextCommand"],
        }
    )
    return 0


def command_permissions_status(_args: argparse.Namespace) -> int:
    payload=permissions_status(); _print_json(payload)
    return 0 if payload["classification"]=="PASS" else 1


def command_image_canary(args: argparse.Namespace) -> int:
    provider="pokemon_tcg_api" if args.provider=="pokemontcg" else args.provider
    errors=image_canary_guard(provider,dry_run=args.dry_run,credentials_valid=False,
        budget_writes=args.max_writes,requested_writes=args.limit,budget_bytes=args.max_bytes,
        requested_bytes=args.limit*13000,language=args.language,r2_valid=False,
        production_publish=False)
    payload={"classification":"BLOCKED" if errors else "DRY_RUN_READY","provider":provider,"language":args.language,
        "limit":args.limit,"dryRun":args.dry_run,"resume":args.resume,"batchSize":args.batch_size,
        "maxWrites":args.max_writes,"maxBytes":args.max_bytes,"providerRate":args.provider_rate,
        "stopOnMismatch":args.stop_on_mismatch,"contactSheet":args.contact_sheet,"errors":errors,
        "downloadsPerformed":False,"r2WritesPerformed":0}
    _print_json(payload); return 2 if errors else 0


def command_resume(args: argparse.Namespace) -> int:
    write_contract_artifacts()
    write_provider_ledger()
    credentials = credential_status(validate=False)
    write_credential_status_reports(credentials)
    result = ingest_tcgdex(
        refresh=False,
        max_network_requests=args.max_network_requests,
        request_interval_seconds=args.request_interval_seconds,
    )
    write_json_atomic(REPORT_DIR / "metadata_ingestion_report.json", result)
    validation = validate_global_catalogue_schema()
    if validation["classification"] != "PASS":
        raise RuntimeError("canonical catalogue schema validation failed")
    reconcile_global_catalogue()
    run_public_image_preflight(
        samples_per_language_region=args.preflight_samples_per_language,
        request_interval_seconds=args.request_interval_seconds,
    )
    create_multilingual_canary_plan(sample_size=100)
    plan_supabase_to_r2_migration(verify_source_objects=True)
    status = build_master_status()
    _print_json(
        {
            "classification": status["classification"],
            "currentPhase": status["currentPhase"],
            "totalCanonicalPrintingGroups": status["totalCanonicalPrintingGroups"],
            "exactBlockers": status["exactBlockers"],
        }
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="CardScanR staged global Pokémon catalogue rollout"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    audit = subparsers.add_parser("audit")
    audit.set_defaults(func=command_audit)

    contracts = subparsers.add_parser("write-contracts")
    contracts.set_defaults(func=command_write_contracts)

    ledger = subparsers.add_parser("provider-ledger")
    ledger.set_defaults(func=command_provider_ledger)

    credentials = subparsers.add_parser("credentials-status")
    credentials.add_argument("--validate", action="store_true")
    credentials.add_argument(
        "--provider",
        choices=[
            "tcgdex",
            "pokemon_tcg_api",
            "pokewallet",
            "scrydex",
            "ximilar",
        ],
    )
    credentials.set_defaults(func=command_credentials_status)

    ingest = subparsers.add_parser("ingest-metadata")
    ingest.add_argument("--provider", choices=["tcgdex"], default="tcgdex")
    ingest.add_argument("--resume", action="store_true", help="use the persistent cache")
    ingest.add_argument("--refresh", action="store_true")
    ingest.add_argument("--max-network-requests", type=int)
    ingest.add_argument("--request-interval-seconds", type=float, default=0.20)
    ingest.set_defaults(func=command_ingest_metadata)

    normalize = subparsers.add_parser("normalize")
    normalize.set_defaults(func=command_normalize)

    reconcile = subparsers.add_parser("reconcile")
    reconcile.set_defaults(func=command_reconcile)

    validate_catalogue = subparsers.add_parser("validate-catalogue")
    validate_catalogue.set_defaults(func=command_validate_catalogue)

    preflight = subparsers.add_parser("image-preflight")
    preflight.add_argument("--samples-per-language", type=int, default=3)
    preflight.add_argument("--request-interval-seconds", type=float, default=0.25)
    preflight.set_defaults(func=command_image_preflight)

    canaries = subparsers.add_parser("plan-canaries")
    canaries.add_argument("--sample-size", type=int, default=100)
    canaries.set_defaults(func=command_plan_canaries)

    migration = subparsers.add_parser("plan-migration")
    migration.add_argument("--skip-source-verification", action="store_true")
    migration.set_defaults(func=command_plan_migration)

    status = subparsers.add_parser("status")
    status.set_defaults(func=command_status)

    permissions_parser=subparsers.add_parser("permissions-status")
    permissions_parser.set_defaults(func=command_permissions_status)

    image_canary=subparsers.add_parser("image-canary")
    image_canary.add_argument("--provider",required=True,choices=["tcgdex","pokemontcg","pokewallet"])
    image_canary.add_argument("--language",required=True)
    image_canary.add_argument("--limit",type=int,default=100)
    image_canary.add_argument("--dry-run",action=argparse.BooleanOptionalAction,default=True)
    image_canary.add_argument("--resume",action="store_true")
    image_canary.add_argument("--batch-size",type=int,default=100)
    image_canary.add_argument("--max-writes",type=int,default=0)
    image_canary.add_argument("--max-bytes",type=int,default=0)
    image_canary.add_argument("--provider-rate",type=float,default=1.0)
    image_canary.add_argument("--stop-on-mismatch",action=argparse.BooleanOptionalAction,default=True)
    image_canary.add_argument("--contact-sheet",action="store_true")
    image_canary.set_defaults(func=command_image_canary)

    resume = subparsers.add_parser("resume")
    resume.add_argument("--max-network-requests", type=int)
    resume.add_argument("--request-interval-seconds", type=float, default=0.20)
    resume.add_argument("--preflight-samples-per-language", type=int, default=3)
    resume.set_defaults(func=command_resume)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
