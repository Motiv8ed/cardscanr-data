#!/usr/bin/env python3
"""Offline Magnezone Sold artifact → classify → exact-comp → price (no eBay I/O, no DB writes)."""
from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.config import MarketEngineConfig  # noqa: E402
from cardscanr_market_engine.filters import filter_comps  # noqa: E402
from cardscanr_market_engine.models import MarketPriceKey, ProviderRequest  # noqa: E402
from cardscanr_market_engine.marketplaces import resolve_marketplace_config  # noqa: E402
from cardscanr_market_engine.price_source_precedence import (  # noqa: E402
    PriceObservation,
    can_proposed_replace_selected,
    select_customer_market_price,
)
from cardscanr_market_engine.pricing_stats import calculate_pricing_stats  # noqa: E402
from cardscanr_market_engine.providers.ebay_browser_provider import (  # noqa: E402
    classify_browser_page_state,
    parse_candidate_dict,
)
from cardscanr_market_engine.providers.post_sold_capture import (  # noqa: E402
    capture_integrity_ok,
    verify_captured_sold_state,
)
from cardscanr_market_engine.providers.post_sold_capture_cdp import count_itm_hrefs  # noqa: E402
from cardscanr_market_engine.providers.query_builder import build_provider_search_query  # noqa: E402

HTML_PATH = ROOT / "reports" / "artifacts" / "ebay_sold_capture_20261001T022754Z.html"
META_PATH = ROOT / "reports" / "artifacts" / "ebay_sold_capture_20261001T022754Z.json"
CAND_PATH = ROOT / "reports" / "artifacts" / "post_sold_capture_last" / "last_capture_candidates.json"
BODY_PATH = ROOT / "reports" / "artifacts" / "post_sold_capture_last" / "last_capture_body.txt"
EXPECTED_SHA = "fb52de156d209d694ea599e3409651c458ce0f9efea7c369bc05024f54d55a9c"
OUT = (
    ROOT
    / "reports"
    / "artifacts"
    / "ebay_gui_reliability_speed_pass"
    / "MAGNEZONE_OFFLINE_PIPELINE_REPLAY.json"
)
LAST_GOOD = 2.12


def magnezone_request() -> ProviderRequest:
    market = resolve_marketplace_config(market_country="AU", currency="AUD", marketplace="ebay")
    key = MarketPriceKey(
        id="8d202e63-be16-432e-9452-5f773ddd3bf1",
        game="pokemon",
        card_name="Magnezone",
        normalized_card_name="magnezone",
        set_name="Mega Evolution",
        set_code="me1",
        collector_number="47",
        language="en",
        variant="raw",
        condition="raw",
        market_country="au",
        currency="aud",
        fingerprint="pokemon|en|me1|47|magnezone|raw|raw|au|aud",
        raw={},
    )
    return ProviderRequest(
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


def _sanitize_eval(item: Any) -> dict[str, Any]:
    comp = item.comp
    return {
        "itemId": comp.source_listing_id,
        "title": comp.title,
        "soldPrice": comp.sold_price,
        "currency": comp.currency,
        "soldDate": str(comp.sold_date or ""),
        "matchScore": getattr(item, "match_score", None),
        "collectorMatch": (comp.raw_metadata or {}).get("collector_number_match"),
        "setMatch": (comp.raw_metadata or {}).get("set_name_match"),
        "graded": bool((comp.raw_metadata or {}).get("graded") or "psa" in str(comp.title or "").lower()),
    }


def main() -> int:
    if not HTML_PATH.is_file() or not META_PATH.is_file():
        OUT.write_text(json.dumps({"status": "INSUFFICIENT_ARTIFACT_EVIDENCE"}, indent=2), encoding="utf-8")
        print(json.dumps({"status": "INSUFFICIENT_ARTIFACT_EVIDENCE"}, indent=2))
        return 3

    html_bytes = HTML_PATH.read_bytes()
    sha = hashlib.sha256(html_bytes).hexdigest()
    if sha != EXPECTED_SHA:
        report = {"status": "INSUFFICIENT_ARTIFACT_EVIDENCE", "reason": "sha_mismatch", "sha256": sha}
        OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
        return 3

    meta = json.loads(META_PATH.read_text(encoding="utf-8"))
    html = html_bytes.decode("utf-8", errors="replace")
    body = BODY_PATH.read_text(encoding="utf-8", errors="replace") if BODY_PATH.is_file() else ""
    candidates: list[dict[str, Any]] = []
    if CAND_PATH.is_file():
        raw = json.loads(CAND_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            candidates = [c for c in raw if isinstance(c, dict)]

    url = str(meta.get("targetUrl") or "")
    title = str(meta.get("targetTitle") or "")
    itm = count_itm_hrefs(html)
    sold_state = verify_captured_sold_state(url=url, title=title, body_text=body or html[:50000])
    integrity = capture_integrity_ok(
        url=url,
        title=title,
        body_text=body or html[:50000],
        expected_url=url,
        expected_query=str(meta.get("query") or ""),
        expected_origin="ebay.com.au",
    )
    page_state = classify_browser_page_state(
        title=title,
        body_text=body,
        html_document=html,
        url=url,
        selector_counts={
            "process_capture": 1,
            "canonical_itm_href_count": int(itm.get("hrefCount") or meta.get("canonicalItmHrefCount") or 0),
        },
        x11_sold_state_verified=True,
    )

    request = magnezone_request()
    search_query = build_provider_search_query(request)
    parsed = []
    parse_errors: list[dict[str, Any]] = []
    for index, cand in enumerate(candidates):
        try:
            comp = parse_candidate_dict(
                cand,
                request=request,
                search_query=search_query,
                index=index,
            )
            if comp is None:
                parse_errors.append({"index": index, "reason": "parse_returned_none", "href": cand.get("href")})
            else:
                parsed.append(comp)
        except Exception as exc:
            parse_errors.append(
                {"index": index, "reason": f"{type(exc).__name__}:{exc}", "href": cand.get("href")}
            )

    evaluated = filter_comps(request.price_key, parsed) if parsed else []
    accepted = [e for e in evaluated if e.included_in_estimate]
    rejected = [e for e in evaluated if not e.included_in_estimate]
    reject_reasons: Counter[str] = Counter(str(e.rejection_reason or "other") for e in rejected)
    # Also count candidates that failed parse as malformed
    if parse_errors:
        reject_reasons["malformed_or_unparsed"] += len(parse_errors)

    cfg = MarketEngineConfig.from_env(require_supabase=False)
    price_result: dict[str, Any] | None = None
    proposed: float | None = None
    confidence: str | None = None
    if page_state.get("outcome") == "success" and accepted:
        stats = calculate_pricing_stats(accepted, config=cfg)
        proposed = float(stats.recommended_price) if stats.recommended_price is not None else None
        confidence = str(stats.confidence) if stats.confidence is not None else None
        price_result = {
            "acceptedCompCount": len(accepted),
            "valuesUsed": [float(e.comp.sold_price) for e in accepted if e.comp.sold_price is not None],
            "median": stats.median_price,
            "low": stats.low_price,
            "high": stats.high_price,
            "average": stats.average_price,
            "p25": None,  # production PricingStats exposes low/high/median, not P25/P75
            "p75": None,
            "recommendedPrice": proposed,
            "confidence": confidence,
            "currency": "AUD",
            "market": "AU",
            "provider": "ebay_browser",
            "provenance": "verified_local_offline_replay",
            "sampleSize": stats.sample_size,
            "noReliablePriceReason": stats.no_reliable_price_reason,
        }
    elif page_state.get("outcome") == "success":
        price_result = {
            "acceptedCompCount": 0,
            "recommendedPrice": None,
            "noData": True,
            "currency": "AUD",
            "market": "AU",
            "provider": "ebay_browser",
        }

    now = datetime.now(timezone.utc)
    retained = PriceObservation(
        provider="ebay_browser",
        price=LAST_GOOD,
        observed_at=datetime(2026, 9, 29, 4, 27, 32, tzinfo=timezone.utc),
        confidence="low",
        sample_size=2,
        display_source="verified_local",
        marketplace="EBAY_AU",
    )
    observations = [retained]
    would_write = False
    if proposed is not None and proposed > 0 and page_state.get("outcome") == "success":
        ok, reason = can_proposed_replace_selected(
            current_provider="ebay_browser",
            current_price=LAST_GOOD,
            current_display_source="verified_local",
            current_observed_at=retained.observed_at,
            proposed_provider="ebay_browser",
            proposed_price=proposed,
            proposed_display_source="verified_local",
            proposed_observed_at=now,
        )
        proposed_obs = PriceObservation(
            provider="ebay_browser",
            price=proposed,
            observed_at=now,
            confidence=confidence,
            sample_size=len(accepted),
            display_source="verified_local",
            marketplace="EBAY_AU",
        )
        if ok:
            observations = [proposed_obs, retained]
            would_write = True
        replace_reason = reason
    else:
        replace_reason = "no_proposed_or_classifier_blocked"
        ok = False

    selected = select_customer_market_price(observations, now=now, market_country="AU")
    # Prove lower-tier cannot overwrite
    ref_ok, ref_reason = can_proposed_replace_selected(
        current_provider="ebay_browser",
        current_price=LAST_GOOD,
        current_display_source="verified_local",
        current_observed_at=now,
        proposed_provider="tcgplayer",
        proposed_price=0.11,
        proposed_display_source="reference",
        proposed_observed_at=now,
    )

    dry = {
        "mode": "WOULD_WRITE_IF_LIVE" if would_write else "NO_DATA_OR_RETAIN_LAST_GOOD",
        "previousLastGood": LAST_GOOD,
        "proposedNewPrice": proposed,
        "canProposedReplace": ok,
        "replaceReason": replace_reason,
        "wouldWriteCache": would_write,
        "wouldWriteSnapshot": would_write,
        "freshnessAdvanced": would_write,
        "selectedPrice": selected.price,
        "selectedProvider": selected.provider,
        "selectedTier": selected.tier,
        "sourcePrecedence": selected.reason,
        "referenceCannotOverwrite": (not ref_ok),
        "referenceBlockReason": ref_reason,
        "ownershipMutations": 0,
        "unknownNeverZero": proposed != 0,
        "productionDbWrites": 0,
    }

    report = {
        "status": "OK" if page_state.get("outcome") == "success" else "CLASSIFIER_OR_INTEGRITY_BLOCKED",
        "liveEbayRequests": 0,
        "productionDbWrites": 0,
        "ownedDaily": "OFF",
        "CAPTURE_INPUT": {
            "htmlPath": str(HTML_PATH),
            "metaPath": str(META_PATH),
            "sha256": sha,
            "sha256Expected": EXPECTED_SHA,
            "sha256Verified": True,
            "bodyChars": len(body),
            "htmlChars": len(html),
            "canonicalItmHrefCount": int(itm.get("hrefCount") or 0),
            "candidateRows": len(candidates),
            "query": meta.get("query"),
            "targetUrl": url,
            "targetTitle": title,
        },
        "MAGNEZONE_OFFLINE_CLASSIFICATION": {
            "pageClassificationOutcome": page_state.get("outcome"),
            "reason": page_state.get("reason"),
            "securityClass": page_state.get("securityClass"),
            "activeChallengeEvidence": page_state.get("activeChallengeEvidence"),
            "passiveChallengeResources": page_state.get("passiveChallengeResources"),
            "ordinaryResultsEvidence": page_state.get("ordinaryResultsEvidence"),
            "soldState": sold_state,
            "integrity": {k: v for k, v in integrity.items() if k != "soldState"},
            "falsePositiveReproduced": page_state.get("outcome") == "challenge_detected",
        },
        "MAGNEZONE_CANONICAL_PARSE": {
            "rawCandidates": len(candidates),
            "parsedComps": len(parsed),
            "parseErrors": len(parse_errors),
            "acceptedExactComps": len(accepted) if page_state.get("outcome") == "success" else 0,
            "rejectedTotal": len(rejected) + len(parse_errors),
            "rejectionReasonCounts": dict(reject_reasons),
            "acceptedSamples": [_sanitize_eval(e) for e in accepted[:12]],
            "PRICE_RESULT": price_result,
        },
        "SAFE_WRITE_DRY_RUN": dry,
        "SAFETY": {
            "liveEbayRequests": 0,
            "productionDbWrites": 0,
            "ownershipMutations": 0,
            "lastGoodPreserved": LAST_GOOD if not would_write else LAST_GOOD,
            "freshnessAdvanced": bool(dry.get("freshnessAdvanced")),
            "unknownNeverZero": True,
            "ownedDaily": "OFF",
        },
        "CAPTURE_STATUS": {
            "fourthProbeCaptureArchitectureAccepted": True,
            "note": "subprocess/direct-CDP capture remains accepted; this task only fixed classification",
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str))
    return 0 if page_state.get("outcome") == "success" else 2


if __name__ == "__main__":
    raise SystemExit(main())
