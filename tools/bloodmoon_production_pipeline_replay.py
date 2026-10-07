"""Offline Bloodmoon Sold artifact → production exact-comp pipeline (no eBay I/O)."""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.filters import filter_comps  # noqa: E402
from cardscanr_market_engine.models import MarketPriceKey, ProviderRequest, SoldComp  # noqa: E402
from cardscanr_market_engine.marketplaces import resolve_marketplace_config  # noqa: E402
from cardscanr_market_engine.price_source_precedence import (  # noqa: E402
    PriceObservation,
    provider_tier,
    select_customer_market_price,
)
from cardscanr_market_engine.pricing_stats import calculate_pricing_stats  # noqa: E402
from cardscanr_market_engine.config import MarketEngineConfig  # noqa: E402
from cardscanr_market_engine.providers.ebay_browser_provider import (  # noqa: E402
    parse_candidate_dict,
)
from cardscanr_market_engine.providers.post_sold_capture import (  # noqa: E402
    POST_SOLD_CAPTURE_READY,
    capture_integrity_ok,
    verify_captured_sold_state,
)
from cardscanr_market_engine.providers.post_sold_capture_cdp import count_itm_hrefs  # noqa: E402
from cardscanr_market_engine.providers.query_builder import build_provider_search_query  # noqa: E402

# Prefer third-probe body; fall back to second-probe body.
CANDIDATE_BODIES = [
    ROOT / "reports" / "artifacts" / "linux_sold_nav_1790799478_body.txt",
    ROOT / "reports" / "artifacts" / "linux_sold_nav_1790797498_body.txt",
]
OUT = ROOT / "reports" / "artifacts" / "ebay_gui_reliability_speed_pass" / "BLOODMOON_PRODUCTION_PIPELINE_REPLAY.json"


def bloodmoon_request() -> ProviderRequest:
    market = resolve_marketplace_config(market_country="AU", currency="AUD", marketplace="ebay")
    key = MarketPriceKey(
        id="566665ce-d69d-4520-9109-52da6ffe66c8",
        game="pokemon",
        card_name="Bloodmoon Ursaluna",
        normalized_card_name="bloodmoon_ursaluna",
        set_name="Prismatic Evolutions",
        set_code="sv8pt5",
        collector_number="54",
        language="en",
        variant="raw",
        condition="raw",
        market_country="au",
        currency="aud",
        fingerprint="pokemon|en|sv8pt5|54|bloodmoon_ursaluna|raw|raw|au|aud",
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


def extract_text_listing_blocks(body: str) -> list[dict[str, Any]]:
    """Best-effort text blocks from X11 clipboard body (no href synthesis)."""
    blocks: list[dict[str, Any]] = []
    current: list[str] = []
    for line in body.splitlines():
        if line.strip().lower().startswith("sold ") and not line.strip().lower().startswith("sold items"):
            if current:
                text = "\n".join(current)
                blocks.append({"source": "x11_text_block", "href": "", "text": text, "title": ""})
            current = [line]
        elif current:
            current.append(line)
    if current:
        text = "\n".join(current)
        blocks.append({"source": "x11_text_block", "href": "", "text": text, "title": ""})
    return blocks


def safe_write_dry_run(
    *,
    last_good: float,
    proposed: float | None,
    accepted_count: int,
    confidence: str | None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    prior = PriceObservation(
        provider="ebay_browser",
        price=last_good,
        observed_at=datetime.fromisoformat("2026-09-29T02:57:30.442061+00:00"),
        confidence="medium",
        sample_size=4,
        marketplace="EBAY_AU",
        display_source="verified_local",
    )
    if proposed is None or proposed <= 0 or accepted_count <= 0:
        selected = select_customer_market_price(
            observations=[prior],
            market_country="AU",
            now=now,
        )
        return {
            "mode": "NO_DATA_RETAIN_LAST_GOOD",
            "previousLastGood": last_good,
            "proposedNewPrice": None,
            "wouldWriteCache": False,
            "wouldWriteSnapshot": False,
            "freshnessAdvanced": False,
            "ownershipMutations": 0,
            "unknownNeverZero": True,
            "selectedTier": selected.tier if selected else None,
            "selectedPrice": selected.price if selected else last_good,
            "selectedProvider": selected.provider if selected else "ebay_browser",
            "sourcePrecedence": "retain_verified_ebay_sold",
            "behaviorIfWriteFailed": "n/a_no_write_attempted",
        }

    new_obs = PriceObservation(
        provider="ebay_browser",
        price=proposed,
        observed_at=now,
        confidence=confidence,
        sample_size=accepted_count,
        marketplace="EBAY_AU",
        display_source="verified_local",
    )
    # Reference must not overwrite verified eBay.
    reference = PriceObservation(
        provider="tcgplayer",
        price=0.26,
        observed_at=now,
        confidence="low",
        sample_size=1,
        marketplace=None,
        display_source="reference",
    )
    selected = select_customer_market_price(
        observations=[prior, new_obs, reference],
        market_country="AU",
        now=now,
    )
    return {
        "mode": "SUCCESS_WOULD_WRITE",
        "previousLastGood": last_good,
        "proposedNewPrice": proposed,
        "wouldWriteCache": True,
        "wouldWriteSnapshot": True,
        "freshnessAdvanced": True,
        "ownershipMutations": 0,
        "unknownNeverZero": True,
        "ebayRemainsPrimary": selected.provider == "ebay_browser",
        "selectedTier": selected.tier,
        "selectedPrice": selected.price,
        "selectedProvider": selected.provider,
        "selectedDisplaySource": selected.display_source,
        "sourcePrecedence": selected.reason,
        "referenceCannotOverwrite": provider_tier(provider="tcgplayer", display_source="reference")
        > provider_tier(provider="ebay_browser", display_source="verified_local", observed_at=now),
        "behaviorIfWriteFailed": "retain_last_good_fail_job_no_ownership_mutation",
        "snapshotWouldInclude": {
            "provider": "ebay_browser",
            "market": "AU",
            "currency": "AUD",
            "acceptedCompCount": accepted_count,
            "confidence": confidence,
            "recommendedPrice": proposed,
        },
    }


def main() -> int:
    body_path = next((p for p in CANDIDATE_BODIES if p.is_file()), None)
    if body_path is None:
        OUT.write_text(
            json.dumps({"status": "INSUFFICIENT_ARTIFACT_EVIDENCE", "reason": "no_body_artifact"}, indent=2),
            encoding="utf-8",
        )
        print(json.dumps({"status": "INSUFFICIENT_ARTIFACT_EVIDENCE"}, indent=2))
        return 2

    body = body_path.read_text(encoding="utf-8", errors="replace")
    itm = count_itm_hrefs(body)
    url = (
        "https://www.ebay.com.au/sch/i.html?_nkw=Bloodmoon+Ursaluna+54+prismatic+evolutions+Pokemon"
        "&_sacat=0&_from=R40&rt=nc&LH_Sold=1"
    )
    title = "Bloodmoon Ursaluna 54 Prismatic Evolutions Pokemon for sale | eBay"
    sold_state = verify_captured_sold_state(url=url, title=title, body_text=body)
    integrity = capture_integrity_ok(
        url=url,
        title=title,
        body_text=body,
        expected_url=url,
        expected_query="Bloodmoon Ursaluna 54 prismatic evolutions Pokemon",
        expected_origin="ebay.com.au",
    )
    request = bloodmoon_request()
    search_query = build_provider_search_query(request)

    blocks = extract_text_listing_blocks(body)
    comps: list[SoldComp] = []
    parse_errors: list[dict[str, Any]] = []
    for index, candidate in enumerate(blocks):
        try:
            comp = parse_candidate_dict(candidate, index=index, request=request, search_query=search_query)
        except Exception as exc:
            parse_errors.append({"index": index, "errorType": type(exc).__name__})
            continue
        if comp is None:
            url_meta_missing = not str(candidate.get("href") or "").strip()
            parse_errors.append(
                {
                    "index": index,
                    "errorType": "candidate_not_parseable",
                    "reason": "missing_direct_item_href" if url_meta_missing else "unparseable",
                }
            )
            continue
        comps.append(comp)

    evaluated = filter_comps(request.price_key, comps)
    accepted = [e for e in evaluated if e.included_in_estimate]
    rejected = [e for e in evaluated if not e.included_in_estimate]
    rejection_counts = Counter(e.rejection_reason or "unknown" for e in rejected)
    # Also count parse-stage rejects (no href).
    rejection_counts["missing_canonical_itm_href_at_parse"] += sum(
        1 for e in parse_errors if e.get("reason") == "missing_direct_item_href"
    )

    config = MarketEngineConfig.from_env(require_supabase=False)
    stats = calculate_pricing_stats(accepted, config=config) if accepted else None
    proposed = float(stats.recommended_price) if stats and stats.recommended_price else None
    dry = safe_write_dry_run(
        last_good=2.56,
        proposed=proposed,
        accepted_count=len(accepted),
        confidence=stats.confidence if stats else None,
    )

    artifact_sufficient_for_production = int(itm.get("hrefCount") or 0) > 0 and len(accepted) > 0
    report = {
        "status": "OK" if artifact_sufficient_for_production else "INSUFFICIENT_ARTIFACT_EVIDENCE",
        "POST_SOLD_CAPTURE_READY_simulated": bool(integrity.get("ok") and sold_state.get("SOLD_STATE_VERIFIED")),
        "capturePhaseAssumed": POST_SOLD_CAPTURE_READY if integrity.get("ok") else "POST_SOLD_CAPTURE_FAILED",
        "CAPTURE_INPUT": {
            "artifactPath": str(body_path),
            "bodyChars": len(body),
            "htmlChars": 0,
            "canonicalItmHrefCount": int(itm.get("hrefCount") or 0),
            "uniqueItemIdCount": int(itm.get("uniqueItemIdCount") or 0),
            "note": "X11 clipboard/text body only — no DOM /itm/ hrefs persisted from successful CDP capture",
        },
        "CANDIDATE_EXTRACTION": {
            "rawListingCandidates": len(blocks),
            "canonicalCandidatesWithHref": sum(1 for b in blocks if "/itm/" in str(b.get("href") or "")),
            "deduplicatedCompsAfterParse": len(comps),
            "parseErrors": len(parse_errors),
        },
        "EXACT_COMP_FILTERING": {
            "acceptedExactComps": len(accepted),
            "rejectedTotal": len(rejected) + sum(1 for e in parse_errors if e.get("reason") == "missing_direct_item_href"),
            "rejectionBreakdown": dict(rejection_counts),
            "acceptedSamples": [
                {
                    "title": e.comp.title[:120],
                    "soldPrice": e.comp.sold_price,
                    "currency": e.comp.currency,
                    "soldDate": e.comp.sold_date.isoformat() if e.comp.sold_date else None,
                    "listingUrl": e.comp.listing_url,
                    "matchScore": e.match_score,
                }
                for e in accepted[:10]
            ],
        },
        "PRICE_RESULT": None
        if not accepted
        else {
            "acceptedCompCount": len(accepted),
            "valuesUsed": [e.comp.sold_price for e in accepted],
            "median": stats.median_price if stats else None,
            "recommended": stats.recommended_price if stats else None,
            "p25": getattr(stats, "p25_price", None) if stats else None,
            "p75": getattr(stats, "p75_price", None) if stats else None,
            "confidence": stats.confidence if stats else None,
            "currency": "AUD",
            "provider": "ebay_browser",
            "market": "AU",
            "provenance": "verified_sold_exact_comps",
        },
        "SAFE_WRITE_DRY_RUN": dry,
        "soldState": sold_state,
        "integrity": {k: v for k, v in integrity.items() if k != "soldState"},
        "priorSuccessfulCdpCaptureNote": {
            "from": "reports/runtime/post_sold_capture_process_local_cdp.json",
            "htmlChars": 3489186,
            "itmHrefs": 210,
            "candidates": 60,
            "persistedToDisk": False,
            "blocker": "successful CDP document was not written to an artifact path; cannot offline-replay production comps from that capture",
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if artifact_sufficient_for_production else 3


if __name__ == "__main__":
    raise SystemExit(main())
