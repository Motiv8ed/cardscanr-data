"""Production pricing baseline snapshot for closeout verification."""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

from cardscanr_market_engine.config import supabase_secret_key_from_env
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient
from cardscanr_market_engine.supabase_env_loader import load_supabase_env

load_supabase_env()
import os

client = SupabaseMarketEngineClient(
    supabase_url=os.getenv("SUPABASE_URL", "").strip().rstrip("/"),
    service_role_key=supabase_secret_key_from_env(),
)
now = datetime.now(timezone.utc)
day_ago = now - timedelta(hours=24)
two_h_ago = now - timedelta(hours=2)


def parse_ts(value: object) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


cache = client._table_get(
    "market_price_cache",
    params={"select": "price_key_id,current_market_price,next_refresh_due_at,stale_after,refresh_status,last_error_message,last_updated_at"},
)
keys = client._table_get("market_price_keys", params={"select": "id"})
jobs_24h = client._table_get(
    "market_price_refresh_jobs",
    params={"select": "id,price_key_id,status,requested_at,completed_at", "requested_at": f"gte.{day_ago.isoformat().replace('+00:00', 'Z')}"},
)
jobs_2h = [j for j in jobs_24h if parse_ts(j.get("requested_at")) and parse_ts(j["requested_at"]) >= two_h_ago]
snaps_24h = client._table_get(
    "market_price_snapshots",
    params={
        "select": "id,price_key_id,included_count,recommended_price,created_at,diagnostics_json,rejected_count",
        "created_at": f"gte.{day_ago.isoformat().replace('+00:00', 'Z')}",
    },
)
snaps_2h = [s for s in snaps_24h if parse_ts(s.get("created_at")) and parse_ts(s["created_at"]) >= two_h_ago]
queue = client._table_get(
    "market_price_refresh_jobs",
    params={"select": "id,status,requested_at,started_at,locked_at", "status": "in.(queued,running)"},
)
stuck_cutoff = now - timedelta(minutes=90)
stuck = client._table_get(
    "market_price_refresh_jobs",
    params={"select": "id,status,locked_at,started_at", "status": "eq.running"},
)

usable_fresh = stale_priced = missing = overdue = priced = 0
for row in cache:
    price = row.get("current_market_price")
    due = parse_ts(row.get("next_refresh_due_at") or row.get("stale_after"))
    if price is not None:
        priced += 1
        if due and due > now:
            usable_fresh += 1
        elif due and due <= now:
            stale_priced += 1
            overdue += 1
    else:
        missing += 1
        if due and due <= now:
            overdue += 1

failed_cache = sum(1 for r in cache if r.get("refresh_status") == "failed" or (r.get("last_error_message") or "").strip())
checked_no_price_24h = len({s["price_key_id"] for s in snaps_24h if (s.get("included_count") or 0) == 0 and not s.get("recommended_price")})
usable_snap_24h = sum(1 for s in snaps_24h if (s.get("included_count") or 0) > 0 and s.get("recommended_price"))
no_price_snap_24h = sum(1 for s in snaps_24h if (s.get("included_count") or 0) == 0)

zero_snaps = client._table_get(
    "market_price_snapshots",
    params={
        "select": "id,rejected_count,included_count,query_used,diagnostics_json,created_at,market_price_keys(card_name,set_code,collector_number)",
        "included_count": "eq.0",
        "order": "created_at.desc",
        "limit": "10",
    },
)
rej = Counter()
for s in zero_snaps:
    diag = s.get("diagnostics_json") or {}
    if isinstance(diag, str):
        try:
            diag = json.loads(diag)
        except json.JSONDecodeError:
            diag = {}
    counts = diag.get("rejectionReasonCounts") or {}
    if counts:
        rej.update({k: int(v) for k, v in counts.items()})
    else:
        rej.update((diag.get("rejectedReasons") or {}).values())

hb = client._table_get("market_price_pipeline_heartbeats", params={"select": "*"})

report = {
    "capturedAtUtc": now.isoformat().replace("+00:00", "Z"),
    "inventory": {
        "totalKeys": len(keys),
        "cacheRows": len(cache),
        "priced": priced,
        "usableFresh": usable_fresh,
        "stalePriced": stale_priced,
        "missing": missing,
        "overdue": overdue,
        "failedCache": failed_cache,
        "checkedNoPrice24h": checked_no_price_24h,
    },
    "activity24h": {
        "jobs": len(jobs_24h),
        "uniqueKeys": len({j["price_key_id"] for j in jobs_24h}),
        "snapshots": len(snaps_24h),
        "uniqueSnapshotKeys": len({s["price_key_id"] for s in snaps_24h}),
        "usablePriceSnapshots": usable_snap_24h,
        "noPriceSnapshots": no_price_snap_24h,
        "failedJobs": sum(1 for j in jobs_24h if j.get("status") == "failed"),
    },
    "activity2h": {
        "jobs": len(jobs_2h),
        "uniqueKeys": len({j["price_key_id"] for j in jobs_2h}),
        "snapshots": len(snaps_2h),
        "uniqueSnapshotKeys": len({s["price_key_id"] for s in snaps_2h}),
    },
    "queue": {
        "queued": sum(1 for q in queue if q.get("status") == "queued"),
        "running": sum(1 for q in queue if q.get("status") == "running"),
        "stuck": sum(
            1
            for s in stuck
            if (parse_ts(s.get("locked_at") or s.get("started_at")) or now) < stuck_cutoff
        ),
    },
    "zeroAcceptRejectionTop": rej.most_common(12),
    "heartbeats": hb,
}
sys.stdout.reconfigure(encoding="utf-8")
print(json.dumps(report, indent=2, default=str))
