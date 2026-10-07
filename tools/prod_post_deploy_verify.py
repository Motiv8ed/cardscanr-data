"""Post-deploy pricing verification report."""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

from cardscanr_market_engine.config import supabase_secret_key_from_env
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient
from cardscanr_market_engine.supabase_env_loader import load_supabase_env

load_supabase_env()
import os

DEPLOY_AFTER = datetime(2026, 8, 25, 20, 10, 50, tzinfo=timezone.utc)

client = SupabaseMarketEngineClient(
    supabase_url=os.getenv("SUPABASE_URL", "").strip().rstrip("/"),
    service_role_key=supabase_secret_key_from_env(),
)


def parse_ts(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


jobs = client._table_get(
    "market_price_refresh_jobs",
    params={
        "select": "id,price_key_id,reason,status,requested_at,started_at,completed_at,error_message,created_snapshot_id",
        "requested_at": f"gte.{DEPLOY_AFTER.isoformat().replace('+00:00', 'Z')}",
        "order": "requested_at.asc",
    },
)
snaps = client._table_get(
    "market_price_snapshots",
    params={
        "select": "id,price_key_id,query_used,included_count,rejected_count,recommended_price,confidence,diagnostics_json,created_at,provider",
        "created_at": f"gte.{DEPLOY_AFTER.isoformat().replace('+00:00', 'Z')}",
        "order": "created_at.asc",
    },
)
key_ids = {j["price_key_id"] for j in jobs} | {s["price_key_id"] for s in snaps}
keys_by_id: dict[str, dict] = {}
if key_ids:
    keys = client._table_get(
        "market_price_keys",
        params={
            "select": "id,card_name,set_code,set_name,collector_number,language",
            "id": f"in.({','.join(key_ids)})",
        },
    )
    keys_by_id = {k["id"]: k for k in keys}

cycles: list[dict] = []
for job in jobs:
    key = keys_by_id.get(job["price_key_id"], {})
    snap = next((s for s in snaps if s.get("id") == job.get("created_snapshot_id")), None)
    if not snap:
        snap = next(
            (
                s
                for s in reversed(snaps)
                if s["price_key_id"] == job["price_key_id"]
                and parse_ts(s.get("created_at"))
                and parse_ts(job.get("completed_at"))
                and parse_ts(s["created_at"]) >= (parse_ts(job.get("started_at")) or DEPLOY_AFTER)
            ),
            None,
        )
    diag = {}
    if snap:
        diag = snap.get("diagnostics_json") or {}
        if isinstance(diag, str):
            try:
                diag = json.loads(diag)
            except json.JSONDecodeError:
                diag = {}
    cycles.append(
        {
            "jobId": job.get("id"),
            "requestedAt": job.get("requested_at"),
            "card": key.get("card_name"),
            "setCode": key.get("set_code"),
            "collector": key.get("collector_number"),
            "reason": job.get("reason"),
            "status": job.get("status"),
            "query": snap.get("query_used") if snap else None,
            "included": snap.get("included_count") if snap else None,
            "rejected": snap.get("rejected_count") if snap else None,
            "price": snap.get("recommended_price") if snap else None,
            "dominantRejection": diag.get("dominantRejectionReason"),
            "rejectionReasonCounts": diag.get("rejectionReasonCounts"),
            "hasCatalogueIdInQuery": bool(
                snap
                and snap.get("query_used")
                and (
                    "pokemon-asia" in str(snap.get("query_used")).lower()
                    or any(part.isdigit() and len(part) >= 4 for part in str(snap.get("query_used")).split())
                )
            ),
        }
    )

rej_post = Counter()
for s in snaps:
    diag = s.get("diagnostics_json") or {}
    if isinstance(diag, str):
        try:
            diag = json.loads(diag)
        except json.JSONDecodeError:
            diag = {}
    counts = diag.get("rejectionReasonCounts") or {}
    if counts:
        rej_post.update({k: int(v) for k, v in counts.items()})

golbat_umbreon = client._table_get(
    "market_price_refresh_jobs",
    params={
        "select": "id,requested_at,price_key_id,reason,status",
        "requested_at": f"gte.{DEPLOY_AFTER.isoformat().replace('+00:00', 'Z')}",
        "order": "requested_at.desc",
    },
)
golbat_jobs = []
for j in golbat_umbreon:
    k = keys_by_id.get(j["price_key_id"])
    if not k:
        row = client._table_get("market_price_keys", params={"select": "card_name", "id": f"eq.{j['price_key_id']}"})
        if row:
            k = row[0]
    name = (k or {}).get("card_name", "").lower()
    if name in {"golbat", "umbreon ex", "umbreon"}:
        golbat_jobs.append({"card": k.get("card_name"), "reason": j.get("reason"), "at": j.get("requested_at")})

report = {
    "deployAfterUtc": DEPLOY_AFTER.isoformat().replace("+00:00", "Z"),
    "jobsSinceDeploy": len(jobs),
    "uniqueKeysSinceDeploy": len({j["price_key_id"] for j in jobs}),
    "snapshotsSinceDeploy": len(snaps),
    "includedGt0": sum(1 for s in snaps if (s.get("included_count") or 0) > 0),
    "rejectionTelemetrySnapshots": sum(
        1
        for s in snaps
        if isinstance((s.get("diagnostics_json") or {}), dict)
        and (s.get("diagnostics_json") or {}).get("rejectionReasonCounts")
        or (
            isinstance(s.get("diagnostics_json"), str)
            and "rejectionReasonCounts" in s.get("diagnostics_json", "")
        )
    ),
    "postDeployRejectionTop": rej_post.most_common(15),
    "cycles": cycles,
    "golbatUmbreonJobsSinceDeploy": golbat_jobs,
}
sys.stdout.reconfigure(encoding="utf-8")
print(json.dumps(report, indent=2, default=str))
