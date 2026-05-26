#!/usr/bin/env python3
"""CardScanR market pricing worker foundation (mock/manual only)."""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
from typing import Any

from market_pricing_job_queue import (
    ManualMarketListingsProvider,
    MarketPricingJobQueue,
    MarketPricingError,
    MockMarketListingsProvider,
    ROOT,
    aggregate_listings,
    build_jobs,
    build_market_query,
    iter_catalog_cards,
    market_config,
    normalize_market,
    utc_now_iso,
    write_json_atomic,
)


def output_paths_for_root(root: Path) -> dict[str, Path]:
    return {
        "worker_json": root / "reports" / "market_pricing_worker_latest.json",
        "worker_md": root / "reports" / "market_pricing_worker_latest.md",
        "jobs_json": root / "reports" / "market_pricing_jobs_latest.json",
        "market_status": root / "public" / "v1" / "markets" / "market-price-status.json",
    }


STATUS_VALUES = {
    "priced",
    "no_results",
    "insufficient_data",
    "stale",
    "error",
    "unavailable",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run market pricing worker foundation")
    parser.add_argument("--market", default="AU", help="Market: AU/US/GB/CA/EU")
    parser.add_argument("--language", default="en", choices=["en", "jp"], help="Card language")
    parser.add_argument("--game", default="pokemon", help="Game id")
    parser.add_argument("--max-jobs", type=int, default=25, help="Maximum jobs to process")
    parser.add_argument("--dry-run", action="store_true", help="Report only; no public writes")
    parser.add_argument("--provider", default="mock", choices=["mock", "manual"], help="Evidence provider")
    parser.add_argument("--write", action="store_true", help="Write market price JSON files")
    parser.add_argument("--commit-safe-report", action="store_true", help="Write commit-safe report fields")
    parser.add_argument("--card-id", default=None, help="Optional canonical card id")
    parser.add_argument("--set-id", default=None, help="Optional set id")
    parser.add_argument("--query-only", action="store_true", help="Only build queries; skip providers")
    parser.add_argument(
        "--manual-source-path",
        default=str(ROOT / "data" / "manual_market_prices" / "sample_market_sold_listings.json"),
        help="Manual provider JSON path",
    )
    parser.add_argument("--condition", default="near_mint", help="Condition profile")
    parser.add_argument("--variant", default="raw", help="Variant profile")
    parser.add_argument("--graded", action="store_true", help="Use graded profile")
    return parser.parse_args()


def provider_for(args: argparse.Namespace):
    if args.provider == "mock":
        return MockMarketListingsProvider()
    return ManualMarketListingsProvider(Path(args.manual_source_path))


def record_from_job(
    *,
    job,
    query_text: str,
    provider_name: str,
    source: str,
    aggregate,
    last_updated_at: str,
    notes: str,
) -> dict[str, Any]:
    status = aggregate.status
    if status not in STATUS_VALUES:
        status = "error"

    return {
        "game": job.game,
        "language": job.language,
        "canonicalCardId": job.canonical_card_id,
        "setId": job.set_id,
        "setName": job.set_name,
        "collectorNumber": job.collector_number,
        "cardName": job.card_name,
        "variant": job.variant,
        "condition": job.condition,
        "gradedState": job.graded_state,
        "marketCountry": job.market,
        "currency": job.currency,
        "source": source,
        "sourceProvider": provider_name,
        "sampleCount": aggregate.sample_count,
        "medianPrice": aggregate.median_price,
        "averagePrice": aggregate.average_price,
        "lowPrice": aggregate.low_price,
        "highPrice": aggregate.high_price,
        "shippingIncluded": aggregate.shipping_included,
        "soldDateRange": {
            "from": aggregate.sold_date_from,
            "to": aggregate.sold_date_to,
        },
        "evidenceListingLinks": aggregate.evidence_links,
        "confidenceScore": aggregate.confidence_score,
        "confidenceLabel": aggregate.confidence_label,
        "outlierFilteringNotes": aggregate.outlier_filtering_notes,
        "lastUpdatedAtUtc": last_updated_at,
        "status": status,
        "query": query_text,
        "providerNotes": notes,
    }


def run_worker(
    args: argparse.Namespace,
    *,
    root: Path = ROOT,
    output_paths: dict[str, Path] | None = None,
) -> dict[str, Any]:
    started_at = utc_now_iso()
    paths = output_paths or output_paths_for_root(root)
    market = normalize_market(args.market)
    cfg = market_config(market)

    cards = iter_catalog_cards(
        root=root,
        game=args.game,
        language=args.language,
        card_id=args.card_id,
        set_id=args.set_id,
        limit=max(args.max_jobs, 1),
    )
    jobs = build_jobs(
        cards=cards,
        game=args.game,
        language=args.language,
        market=market,
        condition=args.condition,
        variant=args.variant,
        graded_state="graded" if args.graded else "ungraded",
    )
    queue = MarketPricingJobQueue(jobs)
    selected_jobs = queue.take(max(args.max_jobs, 0))

    provider = provider_for(args)
    records: list[dict[str, Any]] = []
    job_rows: list[dict[str, Any]] = []
    errors: list[str] = []

    for job in selected_jobs:
        query_text = build_market_query(job)
        if args.query_only:
            aggregate = aggregate_listings([])
            record = record_from_job(
                job=job,
                query_text=query_text,
                provider_name=args.provider,
                source="ebay_sold_listings",
                aggregate=aggregate,
                last_updated_at=utc_now_iso(),
                notes="query_only mode: provider fetch skipped",
            )
            record["status"] = "unavailable"
            records.append(record)
            job_rows.append(
                {
                    "canonicalCardId": job.canonical_card_id,
                    "setId": job.set_id,
                    "cardName": job.card_name,
                    "query": query_text,
                    "status": "query_only",
                }
            )
            continue

        try:
            provider_result = provider.fetch(job, query_text)
            aggregate = aggregate_listings(provider_result.listings)
            record = record_from_job(
                job=job,
                query_text=query_text,
                provider_name=provider_result.provider_name,
                source=provider_result.source,
                aggregate=aggregate,
                last_updated_at=utc_now_iso(),
                notes=provider_result.notes,
            )
            records.append(record)
            job_rows.append(
                {
                    "canonicalCardId": job.canonical_card_id,
                    "setId": job.set_id,
                    "cardName": job.card_name,
                    "query": query_text,
                    "status": record["status"],
                    "sampleCount": record["sampleCount"],
                }
            )
        except Exception as exc:
            errors.append(str(exc))
            failed = record_from_job(
                job=job,
                query_text=query_text,
                provider_name=args.provider,
                source="ebay_sold_listings",
                aggregate=aggregate_listings([]),
                last_updated_at=utc_now_iso(),
                notes=f"provider_error: {exc}",
            )
            failed["status"] = "error"
            records.append(failed)

    status_counts: dict[str, int] = defaultdict(int)
    for record in records:
        status_counts[str(record.get("status") or "unknown")] += 1

    report = {
        "schemaVersion": "1.0.0",
        "generatedAtUtc": utc_now_iso(),
        "startedAtUtc": started_at,
        "finishedAtUtc": utc_now_iso(),
        "status": "ok" if not errors else "partial_error",
        "liveEbayEnabled": False,
        "legalTermsReviewRequiredBeforeLive": True,
        "provider": args.provider,
        "mode": "dry_run" if args.dry_run else "write" if args.write else "report_only",
        "input": {
            "market": cfg["market"],
            "language": args.language,
            "game": args.game,
            "maxJobs": args.max_jobs,
            "dryRun": bool(args.dry_run),
            "write": bool(args.write),
            "queryOnly": bool(args.query_only),
            "cardId": args.card_id,
            "setId": args.set_id,
            "condition": args.condition,
            "variant": args.variant,
            "graded": bool(args.graded),
            "commitSafeReport": bool(args.commit_safe_report),
        },
        "summary": {
            "jobsDiscovered": len(jobs),
            "jobsProcessed": len(selected_jobs),
            "recordsBuilt": len(records),
            "statusCounts": dict(sorted(status_counts.items())),
            "errors": errors,
        },
        "records": records,
    }

    write_json_atomic(paths["worker_json"], report)
    write_json_atomic(
        paths["jobs_json"],
        {
            "schemaVersion": "1.0.0",
            "generatedAtUtc": utc_now_iso(),
            "market": cfg["market"],
            "language": args.language,
            "game": args.game,
            "provider": args.provider,
            "jobs": job_rows,
        },
    )
    paths["worker_md"].parent.mkdir(parents=True, exist_ok=True)
    paths["worker_md"].write_text(render_worker_markdown(report), encoding="utf-8")

    if args.write and not args.dry_run and args.provider in {"mock", "manual"}:
        write_market_price_files(root=root, market=market, game=args.game, language=args.language, records=records)

    if not args.dry_run:
        update_market_status(root=root, provider=args.provider, status_path=paths["market_status"])

    return report


def write_market_price_files(*, root: Path, market: str, game: str, language: str, records: list[dict[str, Any]]) -> None:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        set_id = str(record.get("setId") or "unknown")
        groups[set_id].append(record)

    for set_id, rows in groups.items():
        out_path = root / "public" / "v1" / "markets" / "prices" / market / game / language / f"{set_id}.json"
        payload = {
            "schemaVersion": "1.0.0",
            "generatedAtUtc": utc_now_iso(),
            "market": market_config(market)["market"],
            "game": game,
            "language": language,
            "setId": set_id,
            "recordCount": len(rows),
            "sourceProvider": rows[0].get("sourceProvider") if rows else "unknown",
            "prices": rows,
        }
        write_json_atomic(out_path, payload)


def update_market_status(*, root: Path, provider: str, status_path: Path | None = None) -> None:
    market_status_json = status_path or (root / "public" / "v1" / "markets" / "market-price-status.json")
    payload: dict[str, Any] = {
        "schemaVersion": "1.0.0",
        "generatedAtUtc": utc_now_iso(),
        "status": "enabled_foundation",
        "supportedMarkets": ["AU", "US", "GB", "CA", "EU"],
        "sourceStatus": {
            "ebaySoldListingsWorker": "planned",
            "liveEbayWorker": "disabled",
            "mockProvider": "enabled",
            "manualProvider": "enabled",
        },
        "liveEbayWorkerStatus": "planned_disabled",
        "legalTermsReviewRequiredBeforeLiveScraping": True,
        "lastWorkerRunAtUtc": utc_now_iso(),
        "lastWorkerProvider": provider,
        "notes": [
            "Live eBay scraping is disabled and not implemented in this foundation.",
            "Only mock/manual providers may write market price outputs in this phase.",
            "Do not overwrite EN/JP provider current prices; market prices are stored separately.",
        ],
    }

    existing = None
    if market_status_json.exists():
        try:
            existing = json.loads(market_status_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = None
    if isinstance(existing, dict):
        payload = {**existing, **payload}
        payload["sourceStatus"] = {
            **(existing.get("sourceStatus") if isinstance(existing.get("sourceStatus"), dict) else {}),
            **payload["sourceStatus"],
        }

    write_json_atomic(market_status_json, payload)


def render_worker_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    a = lines.append
    summary = report.get("summary", {})
    a("# Market Pricing Worker")
    a("")
    a(f"Generated: {report.get('generatedAtUtc')}")
    a("")
    a("Live eBay scraping enabled: no")
    a("")
    a(f"Provider: {report.get('provider')}")
    a(f"Mode: {report.get('mode')}")
    a(f"Jobs discovered: {summary.get('jobsDiscovered', 0)}")
    a(f"Jobs processed: {summary.get('jobsProcessed', 0)}")
    a(f"Records built: {summary.get('recordsBuilt', 0)}")
    a(f"Status counts: {summary.get('statusCounts', {})}")
    errors = summary.get("errors", [])
    if errors:
        a("Errors:")
        for err in errors:
            a(f"- {err}")
    a("")
    a("## Sample records")
    a("")
    for row in report.get("records", [])[:8]:
        a(
            "- "
            + f"{row.get('canonicalCardId')} | {row.get('marketCountry')} | {row.get('status')} | "
            + f"sample={row.get('sampleCount')} | median={row.get('medianPrice')} | "
            + f"provider={row.get('sourceProvider')}"
        )
    a("")
    return "\n".join(lines) + "\n"


def main() -> None:
    args = parse_args()
    try:
        report = run_worker(args)
    except MarketPricingError as exc:
        raise SystemExit(str(exc)) from exc

    print("Market pricing worker completed:")
    print(f"  status={report.get('status')}")
    print(f"  records={report.get('summary', {}).get('recordsBuilt', 0)}")
    print("  report=reports/market_pricing_worker_latest.json")


if __name__ == "__main__":
    main()
