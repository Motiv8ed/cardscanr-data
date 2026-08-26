"""Entry point for bulk/reference pricing sync."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.bulk.reference_refresh import BulkReferenceRefreshRunner, BulkRefreshConfig
from cardscanr_market_engine.config import MarketEngineConfig, REPORTS_DIR
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient
from cardscanr_market_engine.supabase_env_loader import load_supabase_env


def main() -> int:
    parser = argparse.ArgumentParser(description="Bulk/reference pricing sync for shared market_price_cache")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-keys", type=int, default=None)
    args = parser.parse_args()

    load_supabase_env()
    engine_config = MarketEngineConfig.from_env()
    refresh_config = BulkRefreshConfig.from_env()
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
