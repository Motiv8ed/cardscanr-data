#!/usr/bin/env python3
"""Phase 4A — Market Price Engine: market-aware seed/demo data script.

Generates realistic Market Price Engine data for AU, US, GB, and CA markets
using the existing mock provider/worker pipeline.  Safe to run repeatedly.

Usage examples
--------------
Dry-run (no network calls, shows plan only):

    python scripts/seed_market_price_demo_data.py --dry-run

Enqueue demo jobs without processing them:

    python scripts/seed_market_price_demo_data.py --markets AU,US,GB,CA \\
        --cards smoke,classic --enqueue-only

Enqueue and process in one pass:

    python scripts/seed_market_price_demo_data.py --markets AU,US,GB,CA \\
        --cards all --process --max-jobs 50

Reports are written to:
    reports/market_price_demo_seed_latest.json
    reports/market_price_demo_seed_runs.jsonl
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.config import MarketEngineConfig
from cardscanr_market_engine.fingerprints import build_market_price_fingerprint, normalize_name
from cardscanr_market_engine.job_runner import MarketPriceJobRunner
from cardscanr_market_engine.marketplaces import resolve_marketplace_config
from cardscanr_market_engine.providers import MockMarketCompsProvider
from cardscanr_market_engine.smoke_utils import (
    append_jsonl,
    missing_smoke_env_vars,
    sanitize_for_report,
    write_json,
)
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient


# ---------------------------------------------------------------------------
# Demo data definitions
# ---------------------------------------------------------------------------

DEMO_REPORT_LATEST = "market_price_demo_seed_latest.json"
DEMO_REPORT_RUNS = "market_price_demo_seed_runs.jsonl"

# market_country -> (currency, expected_ebay_domain)
MARKET_CONFIG: dict[str, tuple[str, str]] = {
    "AU": ("AUD", "ebay.com.au"),
    "US": ("USD", "ebay.com"),
    "GB": ("GBP", "ebay.co.uk"),
    "CA": ("CAD", "ebay.ca"),
}

# Cards tagged "smoke" — safe minimal set for quick CI-style runs
SMOKE_CARDS: list[dict[str, str]] = [
    {
        "label": "smoke",
        "card_name": "Smoke Test Charizard ex",
        "set_name": "Smoke Test Set",
        "set_code": "smoke-test",
        "collector_number": "001/999",
    },
]

# Cards tagged "classic" — realistic Pokémon demo set
CLASSIC_CARDS: list[dict[str, str]] = [
    {
        "label": "classic",
        "card_name": "[DEMO] Charizard ex",
        "set_name": "Obsidian Flames",
        "set_code": "obf",
        "collector_number": "125/197",
    },
    {
        "label": "classic",
        "card_name": "[DEMO] Umbreon VMAX",
        "set_name": "Evolving Skies",
        "set_code": "evs",
        "collector_number": "215/203",
    },
    {
        "label": "classic",
        "card_name": "[DEMO] Pikachu",
        "set_name": "Base Set",
        "set_code": "base1",
        "collector_number": "58/102",
    },
    {
        "label": "classic",
        "card_name": "[DEMO] Mewtwo",
        "set_name": "Base Set",
        "set_code": "base1",
        "collector_number": "10/102",
    },
]

# Shared defaults for all demo cards
_CARD_DEFAULTS: dict[str, str] = {
    "game": "pokemon",
    "language": "en",
    "variant": "raw",
    "condition": "raw",
}

ALL_CARDS = SMOKE_CARDS + CLASSIC_CARDS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_markets(raw: str) -> list[str]:
    """Parse a comma-separated market string and validate each code."""
    codes = [c.strip().upper() for c in raw.split(",") if c.strip()]
    if not codes:
        raise ValueError(f"No valid market codes provided. Supported: {', '.join(sorted(MARKET_CONFIG))}")
    unknown = [c for c in codes if c not in MARKET_CONFIG]
    if unknown:
        raise ValueError(f"Unknown market code(s): {', '.join(unknown)}. Supported: {', '.join(sorted(MARKET_CONFIG))}")
    return codes


def parse_card_filter(raw: str) -> list[dict[str, str]]:
    """Return demo card list filtered by label spec (smoke / classic / all)."""
    specs = {s.strip().lower() for s in raw.split(",") if s.strip()}
    if "all" in specs:
        return ALL_CARDS
    selected: list[dict[str, str]] = []
    if "smoke" in specs:
        selected.extend(SMOKE_CARDS)
    if "classic" in specs:
        selected.extend(CLASSIC_CARDS)
    if not selected:
        raise ValueError(f"Unknown card filter '{raw}'. Use: smoke, classic, all")
    return selected


def build_demo_fingerprint(card: dict[str, str], market_country: str, currency: str) -> str:
    return build_market_price_fingerprint(
        game=_CARD_DEFAULTS["game"],
        language=_CARD_DEFAULTS["language"],
        set_code=card["set_code"],
        set_name=card["set_name"],
        collector_number=card["collector_number"],
        card_name=card["card_name"],
        variant=_CARD_DEFAULTS["variant"],
        condition=_CARD_DEFAULTS["condition"],
        market_country=market_country,
        currency=currency,
    )


def _domain(url: str) -> str:
    return urlparse(str(url or "")).netloc.lower().lstrip("www.")


def _assert_str(value: Any, field: str) -> str:
    if not value:
        raise AssertionError(f"{field} is missing or empty")
    return str(value)


# ---------------------------------------------------------------------------
# Seed planner
# ---------------------------------------------------------------------------


class DemoSeedPlan:
    """Holds the set of (card, market) pairs to seed."""

    def __init__(self, cards: list[dict[str, str]], markets: list[str]) -> None:
        self.items: list[dict[str, Any]] = []
        for card in cards:
            for market in markets:
                currency, expected_domain = MARKET_CONFIG[market]
                fingerprint = build_demo_fingerprint(card, market, currency)
                self.items.append(
                    {
                        "card": card,
                        "market_country": market,
                        "currency": currency,
                        "expected_domain": expected_domain,
                        "fingerprint": fingerprint,
                    }
                )

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "card_name": item["card"]["card_name"],
                "set_code": item["card"]["set_code"],
                "collector_number": item["card"]["collector_number"],
                "market_country": item["market_country"],
                "currency": item["currency"],
                "expected_domain": item["expected_domain"],
                "fingerprint": item["fingerprint"],
            }
            for item in self.items
        ]


# ---------------------------------------------------------------------------
# Seed runner
# ---------------------------------------------------------------------------


class DemoSeedRunner:
    def __init__(
        self,
        *,
        client: SupabaseMarketEngineClient,
        job_runner: MarketPriceJobRunner,
        plan: DemoSeedPlan,
        dry_run: bool = False,
        enqueue_only: bool = False,
        process: bool = False,
        max_jobs: int = 50,
    ) -> None:
        self.client = client
        self.job_runner = job_runner
        self.plan = plan
        self.dry_run = dry_run
        self.enqueue_only = enqueue_only
        self.process = process
        self.max_jobs = max_jobs

    def run(self) -> dict[str, Any]:
        started_at = utc_iso()
        steps: list[dict[str, Any]] = []
        enqueued_count = 0
        skipped_active_count = 0
        processed_count = 0
        verify_results: list[dict[str, Any]] = []

        if self.dry_run:
            return {
                "status": "dry_run",
                "startedAtUtc": started_at,
                "finishedAtUtc": utc_iso(),
                "dryRun": True,
                "plan": self.plan.describe(),
                "planItemCount": len(self.plan.items),
            }

        # Step 1: get_or_create price keys + enqueue refresh jobs
        price_key_ids: list[str] = []
        for item in self.plan.items:
            card = item["card"]
            market_country = item["market_country"]
            currency = item["currency"]
            fingerprint = item["fingerprint"]
            normalized_name = normalize_name(card["card_name"])

            price_key_id = self.client.get_or_create_price_key(
                game=_CARD_DEFAULTS["game"],
                card_name=card["card_name"],
                normalized_card_name=normalized_name,
                set_name=card["set_name"],
                set_code=card["set_code"],
                collector_number=card["collector_number"],
                language=_CARD_DEFAULTS["language"],
                variant=_CARD_DEFAULTS["variant"],
                condition=_CARD_DEFAULTS["condition"],
                market_country=market_country,
                currency=currency,
                fingerprint=fingerprint,
            )
            price_key_ids.append(price_key_id)
            item["price_key_id"] = price_key_id
            steps.append(
                {
                    "step": "get_or_create_price_key",
                    "card_name": card["card_name"],
                    "market_country": market_country,
                    "currency": currency,
                    "price_key_id": price_key_id,
                    "fingerprint": fingerprint,
                }
            )

        # Step 2: check for already-active jobs and enqueue where missing
        active_jobs = self.client.get_active_jobs_for_keys(price_key_ids=price_key_ids)
        for item in self.plan.items:
            price_key_id = item.get("price_key_id", "")
            card = item["card"]
            market_country = item["market_country"]
            currency = item["currency"]
            fingerprint = item["fingerprint"]
            dedupe_key = f"demo-seed:{fingerprint}"

            if price_key_id in active_jobs:
                skipped_active_count += 1
                steps.append(
                    {
                        "step": "enqueue_skipped_active",
                        "card_name": card["card_name"],
                        "market_country": market_country,
                        "currency": currency,
                        "price_key_id": price_key_id,
                        "active_job_id": active_jobs[price_key_id].get("id"),
                    }
                )
                item["job_id"] = str(active_jobs[price_key_id].get("id", ""))
                continue

            job_row = self.client.enqueue_refresh_job(
                price_key_id=price_key_id,
                reason="demo_seed_phase4a",
                priority=10,
                dedupe_key=dedupe_key,
            )
            enqueued_count += 1
            item["job_id"] = str(job_row.get("id", ""))
            steps.append(
                {
                    "step": "enqueue_refresh_job",
                    "card_name": card["card_name"],
                    "market_country": market_country,
                    "currency": currency,
                    "price_key_id": price_key_id,
                    "job_id": item["job_id"],
                    "job_status": job_row.get("status"),
                }
            )

        # Step 3 (optional): process queued jobs with the mock worker
        process_results: list[dict[str, Any]] = []
        if self.process:
            results = self.job_runner.run_once(max_jobs=self.max_jobs)
            processed_count = len(results)
            process_results = results
            steps.append(
                {
                    "step": "worker_run_once",
                    "processedCount": processed_count,
                    "results": results,
                }
            )

        # Step 4: verify each seeded key (only when not enqueue-only)
        if not self.enqueue_only or self.process:
            for item in self.plan.items:
                price_key_id = item.get("price_key_id", "")
                fingerprint = item["fingerprint"]
                card = item["card"]
                market_country = item["market_country"]
                currency = item["currency"]
                expected_domain = item["expected_domain"]

                verify: dict[str, Any] = {
                    "card_name": card["card_name"],
                    "market_country": market_country,
                    "currency": currency,
                    "price_key_id": price_key_id,
                    "fingerprint": fingerprint,
                    "expected_domain": expected_domain,
                }
                try:
                    bundle = self.client.get_market_price_bundle(fingerprint=fingerprint, evidence_limit=5)
                    if bundle is None:
                        verify["status"] = "no_bundle"
                    else:
                        cache = bundle.get("cache") or {}
                        snapshot = bundle.get("latest_snapshot") or {}
                        evidence = bundle.get("sold_listing_evidence") or []

                        cache_ok = bool(cache)
                        snapshot_ok = bool(snapshot)
                        evidence_ok = isinstance(evidence, list) and len(evidence) > 0
                        currency_ok = str(cache.get("currency") or "").upper() == currency.upper()
                        domain_ok = all(
                            _domain(row.get("listing_url", "")) == expected_domain
                            for row in evidence
                        ) if evidence else False

                        verify["status"] = "ok" if (cache_ok and snapshot_ok and evidence_ok and currency_ok) else "partial"
                        verify["cacheExists"] = cache_ok
                        verify["snapshotExists"] = snapshot_ok
                        verify["evidenceCount"] = len(evidence)
                        verify["cacheCurrency"] = str(cache.get("currency") or "")
                        verify["currencyMatch"] = currency_ok
                        verify["domainMatch"] = domain_ok
                        verify["sampleDomains"] = [
                            _domain(row.get("listing_url", ""))
                            for row in evidence[:3]
                        ]
                except Exception as exc:
                    verify["status"] = "error"
                    verify["error"] = str(exc)

                verify_results.append(verify)

        return {
            "status": "success",
            "startedAtUtc": started_at,
            "finishedAtUtc": utc_iso(),
            "dryRun": False,
            "enqueueOnly": self.enqueue_only,
            "process": self.process,
            "maxJobs": self.max_jobs,
            "planItemCount": len(self.plan.items),
            "enqueued": enqueued_count,
            "skippedActive": skipped_active_count,
            "processed": processed_count,
            "plan": self.plan.describe(),
            "steps": steps,
            "verifyResults": verify_results,
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Phase 4A: Seed market-aware demo data for the CardScanR Market Price Engine.\n"
            "Uses mock provider only. Safe to re-run."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--markets",
        default="AU,US,GB,CA",
        help="Comma-separated market codes to seed (default: AU,US,GB,CA).",
    )
    parser.add_argument(
        "--cards",
        default="smoke,classic",
        help="Card set to seed: smoke, classic, all (default: smoke,classic).",
    )
    parser.add_argument(
        "--enqueue-only",
        action="store_true",
        help="Enqueue refresh jobs but do not process them with the mock worker.",
    )
    parser.add_argument(
        "--process",
        action="store_true",
        help="Run the mock worker to process queued demo jobs after enqueueing.",
    )
    parser.add_argument(
        "--max-jobs",
        type=int,
        default=50,
        help="Max jobs to process per worker run (default: 50).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show seed plan without making any network calls or DB writes.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    latest_path = ROOT / "reports" / DEMO_REPORT_LATEST
    runs_path = ROOT / "reports" / DEMO_REPORT_RUNS

    # Dry-run short-circuits before env/config checks
    if args.dry_run:
        markets = parse_markets(args.markets)
        cards = parse_card_filter(args.cards)
        plan = DemoSeedPlan(cards=cards, markets=markets)
        report: dict[str, Any] = {
            "status": "dry_run",
            "startedAtUtc": utc_iso(),
            "finishedAtUtc": utc_iso(),
            "dryRun": True,
            "plan": plan.describe(),
            "planItemCount": len(plan.items),
        }
        clean = sanitize_for_report(report)
        write_json(latest_path, clean)
        append_jsonl(runs_path, clean)
        print(f"[demo-seed] DRY-RUN planItemCount={len(plan.items)} report={latest_path}")
        print(json.dumps(clean, indent=2, sort_keys=True, ensure_ascii=False))
        return 0

    missing = missing_smoke_env_vars()
    if missing:
        print(f"[demo-seed] ERROR: Missing/invalid env vars: {', '.join(missing)}", file=sys.stderr)
        return 1

    try:
        markets = parse_markets(args.markets)
        cards = parse_card_filter(args.cards)
    except ValueError as exc:
        print(f"[demo-seed] ERROR: {exc}", file=sys.stderr)
        return 1

    config = MarketEngineConfig.from_env(require_supabase=True)
    if config.provider_name != "mock":
        print("[demo-seed] ERROR: MARKET_LOOKUP_PROVIDER must be 'mock'", file=sys.stderr)
        return 1

    client = SupabaseMarketEngineClient(
        supabase_url=config.supabase_url,
        service_role_key=config.supabase_service_role_key,
    )
    job_runner = MarketPriceJobRunner(
        client=client,
        provider=MockMarketCompsProvider(),
        config=config,
    )

    plan = DemoSeedPlan(cards=cards, markets=markets)
    runner = DemoSeedRunner(
        client=client,
        job_runner=job_runner,
        plan=plan,
        dry_run=False,
        enqueue_only=args.enqueue_only,
        process=args.process,
        max_jobs=args.max_jobs,
    )

    try:
        report = runner.run()
    except Exception as exc:
        report = {
            "status": "failed",
            "startedAtUtc": utc_iso(),
            "finishedAtUtc": utc_iso(),
            "error": str(exc),
        }
        clean = sanitize_for_report(report)
        write_json(latest_path, clean)
        append_jsonl(runs_path, clean)
        print(f"[demo-seed] FAILED: {exc}", file=sys.stderr)
        return 1

    clean = sanitize_for_report(report)
    write_json(latest_path, clean)
    append_jsonl(runs_path, clean)

    enqueued = report.get("enqueued", 0)
    skipped = report.get("skippedActive", 0)
    processed = report.get("processed", 0)
    print(
        f"[demo-seed] SUCCESS planItems={len(plan.items)} enqueued={enqueued} "
        f"skippedActive={skipped} processed={processed} report={latest_path}"
    )
    print(json.dumps(clean, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
