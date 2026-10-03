#!/usr/bin/env python3
"""Offline dry-run: scheduler + job-runner eligibility for the five post-unicode cards.

Uses recorded scheduler_selection.json cache state only.
Stops immediately before provider/browser invocation.
SEARCH_SUBMISSION_STARTED must remain 0. No network / no eBay.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.capture_evidence_correlation import not_run_capture_block
from cardscanr_market_engine.config import MarketEngineConfig
from cardscanr_market_engine.job_runner import MarketPriceJobRunner
from cardscanr_market_engine.live_navigation_attempt import count_search_submission_started
from cardscanr_market_engine.marketplaces import resolve_marketplace_config
from cardscanr_market_engine.models import MarketPriceKey, MarketPriceRefreshJob, ProviderRequest
from cardscanr_market_engine.owned_daily_source_policy import classify_owned_daily_band
from cardscanr_market_engine.owned_verified_local_execution import (
    evaluate_owned_verified_local_execution,
    scheduler_jobrunner_agreement,
)
from cardscanr_market_engine.providers.query_builder import build_provider_search_queries
from tests.test_market_price_job_runner_cache_states import _FakeClient, _StaticProvider, _sold_comp

OUT = ROOT / "reports" / "artifacts" / "execution_eligibility_evidence_correlation"
SELECTION = (
    ROOT
    / "reports"
    / "artifacts"
    / "ebay_five_consecutive_post_unicode"
    / "scheduler_selection.json"
)
NOW = datetime(2026, 10, 2, 11, 21, tzinfo=timezone.utc)


def _due_from_reference(last_updated: str | None) -> str | None:
    """Mirror reference TTL (~12h) used when next_refresh_due_at was set from last_updated."""
    if not last_updated:
        return None
    try:
        dt = datetime.fromisoformat(str(last_updated).replace("Z", "+00:00"))
    except ValueError:
        return None
    from datetime import timedelta

    return (dt + timedelta(hours=12)).isoformat().replace("+00:00", "Z")


def _cache_from_selection(row: dict) -> dict:
    before = row.get("beforeCache") or {}
    last_updated = before.get("freshness")
    due = _due_from_reference(last_updated)
    return {
        "current_market_price": before.get("price"),
        "display_price_source": before.get("displayPriceSource") or before.get("source"),
        "provider": before.get("provider"),
        "last_updated_at": last_updated,
        "next_refresh_due_at": due,
        "stale_after": due,
        "refresh_status": before.get("refreshStatus") or "completed",
        "last_error_message": None,
    }


def _key_from_selection(row: dict) -> MarketPriceKey:
    fp = str(row.get("fingerprint") or "")
    parts = fp.split("|")
    # pokemon|en|me2|20|ceruledge|raw|raw|au|aud
    game = parts[0] if len(parts) > 0 else "pokemon"
    language = parts[1] if len(parts) > 1 else "en"
    set_code = parts[2] if len(parts) > 2 else ""
    collector = parts[3] if len(parts) > 3 else str(row.get("collector") or "")
    norm_name = parts[4] if len(parts) > 4 else str(row.get("card") or "").lower()
    variant = parts[5] if len(parts) > 5 else "raw"
    condition = parts[6] if len(parts) > 6 else "raw"
    market = parts[7] if len(parts) > 7 else "au"
    currency = parts[8] if len(parts) > 8 else "aud"
    return MarketPriceKey(
        id=str(row["priceKeyId"]),
        game=game,
        card_name=str(row.get("card") or norm_name.title()),
        normalized_card_name=norm_name,
        set_name=str(row.get("set") or ""),
        set_code=set_code,
        collector_number=collector,
        language=language,
        variant=variant,
        condition=condition,
        market_country=market,
        currency=currency,
        fingerprint=fp,
    )


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    selections = json.loads(SELECTION.read_text(encoding="utf-8"))
    cfg = MarketEngineConfig.from_env(require_supabase=False)
    submissions_before = count_search_submission_started()
    cards_out = []
    all_ok = True

    for idx, sel in enumerate(selections[:5], start=1):
        kid = str(sel["priceKeyId"])
        cache = _cache_from_selection(sel)
        key = _key_from_selection(sel)

        band, due, reason_suffix, _view, _ex = classify_owned_daily_band(cache, now=NOW)
        execution = evaluate_owned_verified_local_execution(cache, now=NOW)
        agr = scheduler_jobrunner_agreement(scheduler_due=due, execution=execution)

        market = resolve_marketplace_config(
            market_country=str(key.market_country or "au"),
            currency=str(key.currency or "AUD"),
            marketplace="ebay",
        )
        request = ProviderRequest(
            price_key=key,
            market_country=market.market_country,
            currency=market.currency,
            marketplace=market.marketplace,
            provider_marketplace_id=market.provider_marketplace_id,
            provider_domain=market.provider_domain,
            search_locale=market.search_locale,
            display_name=market.display_name,
            market_config=market,
        )
        queries = build_provider_search_queries(request, max_attempts=1)
        query = queries[0].query_text if queries else None

        client = _FakeClient(key)
        client._cache_row = cache
        runner = MarketPriceJobRunner(
            client=client,
            provider=_StaticProvider([_sold_comp(title=f"{key.card_name} Pokemon")]),
            config=cfg,
            now_func=lambda: NOW,
            logger=lambda _m: None,
        )
        would_skip = None
        would_execute_provider = False
        runner_status = None
        provider_reached = {"called": False}

        def _stop_before_provider(**_kwargs):
            provider_reached["called"] = True
            raise RuntimeError("DRY_RUN_STOP_BEFORE_PROVIDER")

        with mock.patch.object(runner, "_assert_market_allowed_for_worker", return_value=None):
            with mock.patch.object(
                runner,
                "fetch_fallback_result",
                side_effect=_stop_before_provider,
            ):
                job = MarketPriceRefreshJob(
                    id=f"dry-run-{kid[:8]}",
                    price_key_id=kid,
                    reason=f"offline_dry_run:owned_daily:{reason_suffix}",
                    priority=1,
                    status="running",
                    attempt_count=1,
                )
                result = runner.run_job(job)
                runner_status = result.get("status")
                would_skip = runner_status == "skipped_already_fresh"
                would_execute_provider = bool(provider_reached["called"]) and not would_skip
                if would_execute_provider:
                    runner_status = "WOULD_EXECUTE_PROVIDER"

        capture_before = not_run_capture_block()
        card_ok = bool(
            due
            and execution.should_execute
            and not would_skip
            and would_execute_provider
            and query
            and agr["agrees"]
            and capture_before["status"] == "NOT_RUN"
        )
        all_ok = all_ok and card_ok
        cards_out.append(
            {
                "position": idx,
                "card": sel.get("card"),
                "set": sel.get("set"),
                "collector": sel.get("collector"),
                "priceKeyId": kid,
                "fingerprint": sel.get("fingerprint"),
                "sourceClass": execution.source_class,
                "verifiedLocal": execution.verified_local,
                "referenceOnly": execution.reference_only,
                "schedulerBand": band,
                "schedulerDue": due,
                "schedulerReasonSuffix": reason_suffix,
                "executionEligible": execution.should_execute,
                "executionReasonCode": execution.reason_code,
                "wouldSkipFresh": bool(would_skip),
                "jobRunnerStatus": runner_status,
                "wouldExecuteProvider": would_execute_provider,
                "query": query,
                "queryReady": bool(query),
                "agreement": agr,
                "captureEvidenceBeforeProvider": capture_before,
                "cacheDueUsed": cache.get("next_refresh_due_at"),
                "referenceUpdated": cache.get("last_updated_at"),
                "ok": card_ok,
            }
        )

    submissions_after = count_search_submission_started()
    payload = {
        "provedAtUtc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "evaluationNowUtc": NOW.isoformat().replace("+00:00", "Z"),
        "network": "NONE",
        "ebayNavigations": 0,
        "candidates": len(cards_out),
        "allSchedulerDue": all(c["schedulerDue"] for c in cards_out),
        "allExecutionEligible": all(c["executionEligible"] for c in cards_out),
        "allWouldSkipFreshFalse": all(not c["wouldSkipFresh"] for c in cards_out),
        "allQueryReady": all(c["queryReady"] for c in cards_out),
        "allWouldExecuteProvider": all(c["wouldExecuteProvider"] for c in cards_out),
        "allCaptureNotRun": all(
            c["captureEvidenceBeforeProvider"]["status"] == "NOT_RUN" for c in cards_out
        ),
        "searchSubmissionStartedBefore": submissions_before,
        "searchSubmissionStartedAfter": submissions_after,
        "searchSubmissionStartedDelta": submissions_after - submissions_before,
        "ok": all_ok and (submissions_after - submissions_before) == 0 and len(cards_out) == 5,
        "cards": cards_out,
    }
    out_path = OUT / "five_card_offline_dry_run.json"
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": payload["ok"], "path": str(out_path), "candidates": payload["candidates"]}, indent=2))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
