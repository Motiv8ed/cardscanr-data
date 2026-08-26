"""Bounded live throughput probe for pricing scale-up verification."""
from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.config import supabase_secret_key_from_env
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient
from cardscanr_market_engine.supabase_env_loader import load_supabase_env


def snapshot(label: str) -> dict:
    load_supabase_env()
    url = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")
    key = supabase_secret_key_from_env()
    client = SupabaseMarketEngineClient(supabase_url=url, service_role_key=key)
    now = datetime.now(timezone.utc)
    hour_iso = datetime.fromtimestamp(now.timestamp() - 3600, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    since = os.environ.get("PROBE_SINCE_UTC", "").strip() or hour_iso

    jobs = client._table_get(
        "market_price_refresh_jobs",
        params={
            "select": "id,price_key_id,status,completed_at,error_message",
            "completed_at": f"gte.{since}",
            "status": "in.(completed,failed)",
            "limit": "1000",
        },
    )
    snaps = client._table_get(
        "market_price_snapshots",
        params={
            "select": "id,price_key_id,recommended_price,included_count,created_at,confidence",
            "created_at": f"gte.{since}",
            "limit": "1000",
        },
    )
    queue_rows = client._table_get(
        "market_price_refresh_jobs",
        params={"select": "id", "status": "in.(queued,running)", "limit": "1000"},
    )
    completed = [j for j in jobs if j.get("status") == "completed"]
    failed = [j for j in jobs if j.get("status") == "failed"]
    unique = {j.get("price_key_id") for j in completed if j.get("price_key_id")}
    usable = [
        s
        for s in snaps
        if s.get("recommended_price") is not None and int(s.get("included_count") or 0) > 0
    ]
    elapsed_h = max(
        (now - datetime.fromisoformat(since.replace("Z", "+00:00"))).total_seconds() / 3600.0,
        1e-6,
    )
    unique_per_hour = len(unique) / elapsed_h
    payload = {
        "label": label,
        "capturedAtUtc": now.isoformat().replace("+00:00", "Z"),
        "sinceUtc": since,
        "elapsedHours": round(elapsed_h, 4),
        "jobsCompleted": len(completed),
        "jobsFailed": len(failed),
        "uniqueKeysCompleted": len(unique),
        "snapshots": len(snaps),
        "usableSnapshots": len(usable),
        "queueDepth": len(queue_rows),
        "projectedUniquePerHour": round(unique_per_hour, 2),
        "projected50kHours": round(50000 / unique_per_hour, 2) if unique_per_hour > 0 else None,
        "sampleUniqueKeys": sorted(str(x) for x in unique)[:12],
        "failureSamples": [
            {"error": (j.get("error_message") or "")[:120], "id": j.get("id")} for j in failed[:8]
        ],
    }
    out = ROOT / "reports" / f"_throughput_probe_{label}.json"
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return payload


if __name__ == "__main__":
    try:
        label = sys.argv[1] if len(sys.argv) > 1 else "now"
        snapshot(label)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
