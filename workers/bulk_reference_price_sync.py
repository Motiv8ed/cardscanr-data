"""Entry point for bulk/reference pricing sync."""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.bulk.reference_refresh import BulkReferenceRefreshRunner, BulkRefreshConfig
from cardscanr_market_engine.config import MarketEngineConfig, REPORTS_DIR
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient
from cardscanr_market_engine.supabase_env_loader import load_supabase_env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bulk/reference pricing sync for shared market_price_cache")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-keys", type=int, default=None)
    args = parser.parse_args(argv)
    try:
        return _run_sync(args)
    except Exception:
        # Setup failures (list_keys, load_cache_map, preload) happen outside the
        # per-key handler. Print the traceback on stderr and return non-zero
        # so the scheduled task does not look like a silent success.
        traceback.print_exc(file=sys.stderr)
        return 1


def _run_sync(args: argparse.Namespace) -> int:
    load_supabase_env()
    engine_config = MarketEngineConfig.from_env()
    refresh_config = BulkRefreshConfig.from_env()

    # Shared ECB FX refresh with TTL — not per card; keeps international conversion healthy.
    try:
        # Imported here so a missing FX module is skipped instead of aborting
        # the price sync before list_keys.
        from cardscanr_market_engine.international.fx_cache import maybe_refresh_ecb_fx_cache

        fx_payload = maybe_refresh_ecb_fx_cache()
        print(
            f"[bulk-reference] fx_source={fx_payload.get('source')} "
            f"provider_rate_date={fx_payload.get('providerRateDate')} "
            f"fetched_at={fx_payload.get('fetchedAt')}",
            flush=True,
        )
    except Exception as exc:
        print(f"[bulk-reference] fx_refresh_skipped error={exc}", flush=True)
    if args.dry_run:
        refresh_config = BulkRefreshConfig(
            dry_run=True,
            max_keys=refresh_config.max_keys,
            enable_live_tcgdex=refresh_config.enable_live_tcgdex,
            verification_budget_per_run=refresh_config.verification_budget_per_run,
            high_value_threshold=refresh_config.high_value_threshold,
            reference_fresh_hours=refresh_config.reference_fresh_hours,
        )
    if args.max_keys is not None:
        refresh_config = BulkRefreshConfig(
            dry_run=refresh_config.dry_run,
            max_keys=max(1, args.max_keys),
            enable_live_tcgdex=refresh_config.enable_live_tcgdex,
            verification_budget_per_run=refresh_config.verification_budget_per_run,
            high_value_threshold=refresh_config.high_value_threshold,
            reference_fresh_hours=refresh_config.reference_fresh_hours,
        )

    client = SupabaseMarketEngineClient(
        supabase_url=engine_config.supabase_url,
        service_role_key=engine_config.supabase_service_role_key,
    )

    def _log(msg: str) -> None:
        print(msg, flush=True)

    runner = BulkReferenceRefreshRunner(
        client=client,
        engine_config=engine_config,
        refresh_config=refresh_config,
        logger=_log,
    )
    report = runner.run()
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORTS_DIR / "bulk_reference_sync_latest.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report.get("errors", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
