#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.config import MarketEngineConfig
from cardscanr_market_engine.fingerprints import build_market_price_fingerprint, normalize_name
from cardscanr_market_engine.job_runner import MarketPriceJobRunner
from cardscanr_market_engine.providers import MockMarketCompsProvider
from cardscanr_market_engine.smoke_utils import (
    SMOKE_REPORT_LATEST,
    SMOKE_REPORT_RUNS,
    append_jsonl,
    missing_smoke_env_vars,
    sanitize_for_report,
    write_json,
)
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient


def utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def test_identity() -> dict[str, str]:
    return {
        "game": "pokemon",
        "card_name": "Smoke Test Charizard ex",
        "set_name": "Smoke Test Set",
        "set_code": "smoke-test",
        "collector_number": "001/999",
        "language": "en",
        "variant": "raw",
        "condition": "raw",
        "market_country": "au",
        "currency": "aud",
    }


def build_fingerprint(identity: dict[str, str]) -> str:
    return build_market_price_fingerprint(
        game=identity["game"],
        language=identity["language"],
        set_code=identity["set_code"],
        set_name=identity["set_name"],
        collector_number=identity["collector_number"],
        card_name=identity["card_name"],
        variant=identity["variant"],
        condition=identity["condition"],
        market_country=identity["market_country"],
        currency=identity["currency"],
    )


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _as_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise AssertionError(f"{field_name} must be numeric, got bool")
    try:
        return int(value)
    except Exception as exc:
        raise AssertionError(f"{field_name} must be numeric: {value}") from exc


def run_smoke() -> dict[str, Any]:
    started_at = utc_iso()
    missing = missing_smoke_env_vars()
    if missing:
        raise RuntimeError(f"Missing/invalid smoke env vars: {', '.join(missing)}")

    config = MarketEngineConfig.from_env(require_supabase=True)
    _assert(config.provider_name == "mock", "MARKET_LOOKUP_PROVIDER must be mock")

    client = SupabaseMarketEngineClient(
        supabase_url=config.supabase_url,
        service_role_key=config.supabase_service_role_key,
    )
    identity = test_identity()
    fingerprint = build_fingerprint(identity)
    normalized_name = normalize_name(identity["card_name"])
    report_steps: list[dict[str, Any]] = []

    price_key_id = client.get_or_create_price_key(
        game=identity["game"],
        card_name=identity["card_name"],
        normalized_card_name=normalized_name,
        set_name=identity["set_name"],
        set_code=identity["set_code"],
        collector_number=identity["collector_number"],
        language=identity["language"],
        variant=identity["variant"],
        condition=identity["condition"],
        market_country=identity["market_country"],
        currency=identity["currency"],
        fingerprint=fingerprint,
    )
    report_steps.append({"step": "get_or_create_market_price_key", "price_key_id": price_key_id})

    first_job = client.enqueue_refresh_job(
        price_key_id=price_key_id,
        reason="phase2_5_smoke_initial",
        priority=10,
        dedupe_key=f"{fingerprint}:initial",
    )
    second_job = client.enqueue_refresh_job(
        price_key_id=price_key_id,
        reason="phase2_5_smoke_dedupe",
        priority=10,
        dedupe_key=f"{fingerprint}:dedupe",
    )
    _assert(str(first_job.get("id")) == str(second_job.get("id")), "Active-job dedupe failed for second enqueue")
    _assert(str(second_job.get("status")) in {"queued", "running"}, "Deduped job is not queued/running")
    report_steps.append(
        {
            "step": "enqueue_dedupe_check",
            "job_id": str(first_job.get("id")),
            "status": str(first_job.get("status")),
            "dedupe_reused": True,
        }
    )

    runner = MarketPriceJobRunner(
        client=client,
        provider=MockMarketCompsProvider(),
        config=config,
    )
    run_results = runner.run_once(max_jobs=1)
    _assert(len(run_results) == 1, "Expected one claimed job for smoke run")
    _assert(run_results[0].get("status") == "completed", "Worker did not complete smoke refresh job")
    report_steps.append({"step": "worker_run_once", "results": run_results})

    bundle = client.get_market_price_bundle(fingerprint=fingerprint, evidence_limit=50)
    _assert(bundle is not None, "get_market_price_bundle returned null for smoke fingerprint")
    cache = bundle.get("cache") or {}
    snapshot = bundle.get("latest_snapshot") or {}
    evidence = bundle.get("sold_listing_evidence") or []
    active_job = bundle.get("active_refresh_job")
    _assert(bool(cache), "market_price_cache missing from bundle")
    _assert(bool(snapshot), "latest_snapshot missing from bundle")
    _assert(isinstance(evidence, list) and len(evidence) > 0, "sold_listing_evidence missing from bundle")
    if active_job is not None:
        _assert(str(active_job.get("status")) not in {"queued", "running"}, "active refresh job still queued/running")

    _assert(bool(cache.get("last_updated_at")), "cache.last_updated_at missing")
    _assert(bool(cache.get("stale_after")), "cache.stale_after missing")
    _assert(str(cache.get("currency") or "").upper() == "AUD", "cache.currency must be AUD")
    _assert(str(cache.get("market_country") or "").upper() == "AU", "cache.market_country must be AU")
    cache_sample_size = _as_int(cache.get("sample_size", 0), "cache.sample_size")
    snapshot_included = _as_int(snapshot.get("included_count", 0), "latest_snapshot.included_count")
    snapshot_rejected = _as_int(snapshot.get("rejected_count", 0), "latest_snapshot.rejected_count")
    _assert(cache_sample_size >= 1, "cache.sample_size must be >= 1")
    _assert(snapshot_included >= 1, "latest_snapshot.included_count must be >= 1")
    _assert(snapshot_rejected >= 0, "latest_snapshot.rejected_count must be >= 0")
    _assert(cache_sample_size == snapshot_included, "cache.sample_size should equal latest_snapshot.included_count")
    confidence = str(cache.get("confidence") or snapshot.get("confidence") or "").lower()
    _assert(confidence in {"low", "medium", "high"}, "confidence must be low/medium/high")
    diagnostics = snapshot.get("diagnostics_json") or {}
    _assert(
        "EBAY_AU" in str(snapshot.get("marketplace") or "")
        or "EBAY_AU" in str(diagnostics.get("providerMarketplaceId") or "")
        or "ebay.com.au" in str(diagnostics.get("providerDomain") or ""),
        "snapshot diagnostics must include AU marketplace/domain context",
    )
    _assert(all(str(item.get("currency") or "").upper() == "AUD" for item in evidence), "evidence currency must be AUD")
    _assert(
        all("https://www.ebay.com.au/itm/mock-" in str(item.get("listing_url") or "") for item in evidence),
        "evidence listing_url must use ebay.com.au",
    )
    report_steps.append(
        {
            "step": "bundle_validation",
            "cache_sample_size": cache_sample_size,
            "included_count": snapshot_included,
            "rejected_count": snapshot_rejected,
            "confidence": confidence,
            "evidence_count": len(evidence),
            "cache_currency": cache.get("currency"),
            "cache_marketplace": cache.get("marketplace"),
            "snapshot_marketplace": snapshot.get("marketplace"),
        }
    )

    third_job = client.enqueue_refresh_job(
        price_key_id=price_key_id,
        reason="phase2_5_smoke_post_complete",
        priority=10,
        dedupe_key=f"{fingerprint}:post-complete",
    )
    _assert(str(third_job.get("id")) != str(first_job.get("id")), "Expected a new job after completed refresh")
    _assert(str(third_job.get("status")) in {"queued", "running"}, "Post-complete enqueue did not create active job")
    report_steps.append(
        {
            "step": "post_complete_enqueue_allowed",
            "first_job_id": str(first_job.get("id")),
            "new_job_id": str(third_job.get("id")),
            "new_status": str(third_job.get("status")),
        }
    )

    return {
        "status": "success",
        "startedAtUtc": started_at,
        "finishedAtUtc": utc_iso(),
        "provider": config.provider_name,
        "supabaseHost": config.supabase_url.split("://", 1)[-1].split("/", 1)[0],
        "fingerprint": fingerprint,
        "identity": identity,
        "priceKeyId": price_key_id,
        "steps": report_steps,
    }


def main() -> int:
    latest_path = ROOT / "reports" / SMOKE_REPORT_LATEST
    runs_path = ROOT / "reports" / SMOKE_REPORT_RUNS
    try:
        report = run_smoke()
    except Exception as exc:
        report = {
            "status": "failed",
            "startedAtUtc": utc_iso(),
            "finishedAtUtc": utc_iso(),
            "error": str(exc),
        }
        clean_report = sanitize_for_report(report)
        write_json(latest_path, clean_report)
        append_jsonl(runs_path, clean_report)
        print(f"[market-engine-smoke] FAILED: {exc}")
        return 1

    clean_report = sanitize_for_report(report)
    write_json(latest_path, clean_report)
    append_jsonl(runs_path, clean_report)
    print(
        "[market-engine-smoke] SUCCESS "
        f"key={clean_report.get('priceKeyId')} fingerprint={clean_report.get('fingerprint')} "
        f"report={latest_path}"
    )
    print(json.dumps(clean_report, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
