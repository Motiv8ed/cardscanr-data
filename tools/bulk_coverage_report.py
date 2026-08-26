"""Report production bulk pricing coverage gaps."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.bulk.coverage_diagnostic import aggregate_coverage, classify_key
from cardscanr_market_engine.bulk.tcgdex_client import TcgdexRunCache
from cardscanr_market_engine.config import MarketEngineConfig, REPORTS_DIR
from cardscanr_market_engine.models import MarketPriceKey
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient
from cardscanr_market_engine.supabase_env_loader import load_supabase_env


def main() -> int:
    parser = argparse.ArgumentParser(description="Bulk pricing coverage diagnostic for production keys")
    parser.add_argument("--limit", type=int, default=500)
    args = parser.parse_args()

    load_supabase_env()
    engine_config = MarketEngineConfig.from_env()
    client = SupabaseMarketEngineClient(
        supabase_url=engine_config.supabase_url,
        service_role_key=engine_config.supabase_service_role_key,
    )
    rows = client._table_get(
        "market_price_keys",
        params={"select": "*", "order": "last_seen_at.desc.nullslast", "limit": str(max(1, args.limit))},
    )
    keys = [MarketPriceKey.from_row(row) for row in rows]
    cache = TcgdexRunCache()
    for key in keys:
        from cardscanr_market_engine.bulk.set_id_aliases import resolve_tcgdex_set_id, resolve_static_set_id

        resolved = resolve_static_set_id(key.set_code)
        if resolved:
            tcg_id = resolve_tcgdex_set_id(key.set_code) or resolved
            cache.preload_set(language=key.language, set_id=tcg_id)

    results = [classify_key(key, tcgdx_cache=cache) for key in keys]
    report = {
        "keysAnalyzed": len(keys),
        "summary": aggregate_coverage(results),
        "samples": [
            {
                "priceKeyId": row.price_key_id,
                "setCode": row.set_code,
                "language": row.language,
                "collectorNumber": row.collector_number,
                "cardName": row.card_name,
                "reason": row.reason,
                "provider": row.provider,
                "mapped": row.mapped,
                "bulkUsable": row.bulk_usable,
                "recommendedAction": row.recommended_action,
            }
            for row in results
            if not row.bulk_usable
        ][:50],
    }
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORTS_DIR / "bulk_coverage_diagnostic_latest.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
