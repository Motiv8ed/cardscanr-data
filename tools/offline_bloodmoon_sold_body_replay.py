#!/usr/bin/env python3
"""Offline replay of Bloodmoon X11 Sold clipboard body. NO live eBay. NO DB writes."""
from __future__ import annotations

import json
import re
import statistics
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.providers.ebay_browser_provider import parse_candidate_dict  # noqa: E402
from cardscanr_market_engine.providers.post_sold_capture import (  # noqa: E402
    capture_integrity_ok,
    verify_captured_sold_state,
)
from cardscanr_market_engine.providers.query_builder import ProviderSearchQuery  # noqa: E402
from cardscanr_market_engine.marketplaces import LocalMarketConfig  # noqa: E402
from cardscanr_market_engine.models import MarketPriceKey, ProviderRequest  # noqa: E402

ART = ROOT / "reports" / "artifacts" / "linux_sold_nav_1790797498_body.txt"
OUT = (
    ROOT
    / "reports"
    / "artifacts"
    / "ebay_gui_reliability_speed_pass"
    / "BLOODMOON_OFFLINE_REPLAY.json"
)
SOLD_URL = (
    "https://www.ebay.com.au/sch/i.html?"
    "_nkw=Bloodmoon+Ursaluna+54+prismatic+evolutions+Pokemon&_sacat=0&_from=R40&rt=nc&LH_Sold=1"
)
QUERY = "Bloodmoon Ursaluna 54 prismatic evolutions Pokemon"


def _split_listing_blocks(body: str) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    for line in body.splitlines():
        low = line.strip().lower()
        if low.startswith("sold ") and not low.startswith(("sold items", "sold listings")):
            if current:
                blocks.append("\n".join(current + [line]))
                current = []
            else:
                current = [line]
        else:
            current.append(line)
    return blocks


def main() -> int:
    body = ART.read_text(encoding="utf-8", errors="replace")
    sold_state = verify_captured_sold_state(
        url=SOLD_URL,
        title="Bloodmoon Ursaluna 54 Prismatic Evolutions Pokemon for sale | eBay",
        body_text=body,
    )
    integrity = capture_integrity_ok(
        url=SOLD_URL,
        title="Bloodmoon Ursaluna 54 Prismatic Evolutions Pokemon for sale | eBay",
        body_text=body,
        expected_url=SOLD_URL,
        expected_query=QUERY,
    )
    blocks = _split_listing_blocks(body)

    market_config = LocalMarketConfig(
        market_country="AU",
        currency="AUD",
        marketplace="ebay",
        provider_marketplace_id="EBAY_AU",
        provider_domain="ebay.com.au",
        search_locale="en-AU",
        display_name="Australia",
    )
    price_key = MarketPriceKey(
        id="offline-replay",
        game="pokemon",
        card_name="Bloodmoon Ursaluna",
        normalized_card_name="bloodmoon ursaluna",
        set_name="Prismatic Evolutions",
        set_code="sv8pt5",
        collector_number="54",
        language="en",
        variant="raw",
        condition="raw",
        market_country="AU",
        currency="AUD",
        fingerprint="pokemon|en|sv8pt5|54|bloodmoon_ursaluna|raw|raw|au|aud",
    )
    request = ProviderRequest(
        price_key=price_key,
        market_country="AU",
        currency="AUD",
        marketplace="ebay",
        provider_marketplace_id="EBAY_AU",
        provider_domain="ebay.com.au",
        search_locale="en-AU",
        display_name="Australia",
        market_config=market_config,
    )
    search_query = ProviderSearchQuery(
        query_text=QUERY,
        include_terms=tuple(QUERY.split()),
        exclude_terms=(),
        provider_domain="ebay.com.au",
        provider_marketplace_id="EBAY_AU",
        search_url=SOLD_URL,
        currency="AUD",
        market_country="AU",
        query_index=0,
        query_source="offline_replay",
        diagnostics={},
    )

    accepted_no_href = 0
    rejected_no_href = 0
    for i, block in enumerate(blocks[:80]):
        cand = {"href": "", "text": block, "title": block.splitlines()[0] if block else ""}
        if parse_candidate_dict(cand, request=request, search_query=search_query, index=i) is None:
            rejected_no_href += 1
        else:
            accepted_no_href += 1

    accepted_synth = []
    rejected_synth = 0
    for i, block in enumerate(blocks[:80]):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        title = next((ln for ln in lines if "ursaluna" in ln.lower() and len(ln) > 20), lines[0] if lines else "")
        price_m = re.search(r"AU\s*\$[\d,.]+|A\$[\d,.]+", block)
        date_m = re.search(r"Sold\s+\d{1,2}\s+\w+\s+\d{4}", block, re.I)
        cand = {
            "href": f"https://www.ebay.com.au/itm/900000000{i:03d}",
            "text": block,
            "title": title,
            "priceText": price_m.group(0) if price_m else "",
            "soldDateText": date_m.group(0) if date_m else "",
        }
        comp = parse_candidate_dict(cand, request=request, search_query=search_query, index=i)
        if comp is None:
            rejected_synth += 1
        else:
            accepted_synth.append(
                {
                    "title": comp.title,
                    "soldPrice": comp.sold_price,
                    "shipping": comp.shipping_price,
                    "total": comp.total_price,
                    "currency": comp.currency,
                }
            )

    prices = [float(x["soldPrice"]) for x in accepted_synth]
    heuristic_prices = []
    for m in re.finditer(r"Sold\s+\d{1,2}\s+\w+\s+\d{4}", body, re.I):
        window = body[m.start() : m.start() + 400]
        pm = re.search(r"AU\s*\$([\d,.]+)", window)
        if pm:
            try:
                heuristic_prices.append(float(pm.group(1).replace(",", "")))
            except ValueError:
                pass

    report = {
        "artifact": str(ART),
        "bodyChars": len(body),
        "itmLinksInBody": len(re.findall(r"/itm/\d+", body)),
        "soldState": sold_state,
        "integrity": {k: v for k, v in integrity.items() if k != "soldState"},
        "listingBlockCandidates": len(blocks),
        "canonicalWithoutHref": {
            "accepted": accepted_no_href,
            "rejected": rejected_no_href,
            "note": "Clipboard body has no /itm/ URLs; canonical parser correctly rejects.",
        },
        "canonicalWithSyntheticHrefStructureCheck": {
            "accepted": len(accepted_synth),
            "rejected": rejected_synth,
            "acceptedSample": accepted_synth[:5],
            "calculatedMedianAud": round(statistics.median(prices), 2) if prices else None,
            "confidence": "offline_structure_only_not_production",
            "note": "Synthetic hrefs prove title/price/date extractability only. Not production pricing.",
        },
        "heuristicSoldWindowPrices": {
            "count": len(heuristic_prices),
            "medianAud": round(statistics.median(heuristic_prices), 2) if heuristic_prices else None,
            "sample": heuristic_prices[:10],
        },
        "productionWrites": 0,
        "usableForSoldEvidence": bool(integrity.get("ok")),
        "usableForCanonicalParserAlone": False,
        "secondCdpCaptureRedundantForParser": False,
        "secondCdpCaptureRedundantForSoldEvidence": True,
        "BODY_38143_SOURCE": {
            "function": "tools/linux_x11_ebay_sold.py:copy_body",
            "mechanism": "X11 Ctrl+A / Ctrl+C + xclip clipboard after SOLD_STATE_VERIFIED",
            "artifact": "reports/artifacts/linux_sold_nav_1790797498_body.txt",
            "containsListingText": True,
            "containsSoldDates": bool(sold_state.get("soldDateLines", 0) > 0),
            "containsSoldPrices": bool(heuristic_prices),
            "containsListingTitles": "ursaluna" in body.lower(),
            "completeEnoughForCanonicalParser": False,
            "reasonIncomplete": "no /itm/ hrefs in clipboard text",
            "secondCaptureRedundant": "sold_evidence_yes_parser_no",
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
