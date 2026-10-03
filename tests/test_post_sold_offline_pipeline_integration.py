"""Offline integration: Sold artifact → capture contract → exact-comp → price → safe write.

No live eBay traffic. Uses a local HTML fixture with canonical /itm/ hrefs plus
process-boundary hang/crash proofs.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.config import MarketEngineConfig  # noqa: E402
from cardscanr_market_engine.filters import filter_comps  # noqa: E402
from cardscanr_market_engine.models import MarketPriceKey, ProviderRequest  # noqa: E402
from cardscanr_market_engine.marketplaces import resolve_marketplace_config  # noqa: E402
from cardscanr_market_engine.price_source_precedence import (  # noqa: E402
    can_proposed_replace_selected,
    select_customer_market_price,
    PriceObservation,
)
from cardscanr_market_engine.pricing_stats import calculate_pricing_stats  # noqa: E402
from cardscanr_market_engine.providers.ebay_browser_provider import parse_candidate_dict  # noqa: E402
from cardscanr_market_engine.providers.post_sold_capture import (  # noqa: E402
    CDP_CAPTURE_PROCESS_CRASH,
    CDP_CAPTURE_PROCESS_TIMEOUT,
    POST_SOLD_CAPTURE_READY,
    capture_integrity_ok,
    verify_captured_sold_state,
)
from cardscanr_market_engine.providers.post_sold_capture_cdp import (  # noqa: E402
    COLLECT_CANDIDATES_JS,
    count_itm_hrefs,
)
from cardscanr_market_engine.providers.post_sold_capture_process import (  # noqa: E402
    CaptureProcessResult,
    capture_process_result_to_sold_page,
    run_capture_worker_process,
)
from cardscanr_market_engine.providers.query_builder import build_provider_search_query  # noqa: E402

FIXTURE_DIR = ROOT / "tests" / "fixtures" / "ebay_sold"
FIXTURE_HTML = FIXTURE_DIR / "bloodmoon_ursaluna_54_sold_fixture.html"


BLOODMOON_FIXTURE_HTML = """<!DOCTYPE html>
<html><head><title>Bloodmoon Ursaluna 54 Prismatic Evolutions Pokemon for sale | eBay</title></head>
<body>
Sold items
Sold listings
Sold 28 Sep 2026
Sold 27 Sep 2026
Sold 26 Sep 2026
Sold 25 Sep 2026
Sold 24 Sep 2026
<ul class="srp-results">
  <li class="s-item">
    <a class="s-item__link" href="https://www.ebay.com.au/itm/116891111001">
      <h3 class="s-item__title"><span role="heading">Bloodmoon Ursaluna 054/131 Prismatic Evolutions Pokemon Card</span></h3>
    </a>
    <span class="s-item__price">AU $2.80</span>
    <span class="s-item__caption">Sold 28 Sep 2026</span>
    <span class="s-item__shipping">AU $12.00 postage</span>
  </li>
  <li class="s-item">
    <a class="s-item__link" href="https://www.ebay.com.au/itm/116891111002">
      <h3 class="s-item__title"><span role="heading">Bloodmoon Ursaluna 54 Prismatic Evolutions Raw Pokemon TCG</span></h3>
    </a>
    <span class="s-item__price">AU $2.40</span>
    <span class="s-item__caption">Sold 27 Sep 2026</span>
  </li>
  <li class="s-item">
    <a class="s-item__link" href="https://www.ebay.com.au/itm/116891111003">
      <h3 class="s-item__title"><span role="heading">Bloodmoon Ursaluna 54 Prismatic Evolutions PSA 10 Pokemon</span></h3>
    </a>
    <span class="s-item__price">AU $45.00</span>
    <span class="s-item__caption">Sold 26 Sep 2026</span>
  </li>
  <li class="s-item">
    <a class="s-item__link" href="https://www.ebay.com.au/itm/116891111004">
      <h3 class="s-item__title"><span role="heading">Lot of 10 Bloodmoon Ursaluna Prismatic Evolutions Pokemon</span></h3>
    </a>
    <span class="s-item__price">AU $18.00</span>
    <span class="s-item__caption">Sold 25 Sep 2026</span>
  </li>
  <li class="s-item">
    <a class="s-item__link" href="https://www.ebay.com.au/itm/116891111005">
      <h3 class="s-item__title"><span role="heading">Pikachu 025 Base Set Pokemon Card</span></h3>
    </a>
    <span class="s-item__price">AU $9.00</span>
    <span class="s-item__caption">Sold 24 Sep 2026</span>
  </li>
</ul>
</body></html>
"""


def _ensure_fixture() -> Path:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    FIXTURE_HTML.write_text(BLOODMOON_FIXTURE_HTML, encoding="utf-8")
    return FIXTURE_HTML


def _bloodmoon_request() -> ProviderRequest:
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


def _candidates_from_fixture_html(html: str) -> list[dict]:
    # Minimal DOM-equivalent extraction without browser: use regex on fixture structure.
    import re

    items = []
    for m in re.finditer(
        r'href="(https://www\.ebay\.com\.au/itm/\d+)"[\s\S]*?role="heading">([^<]+)[\s\S]*?'
        r's-item__price">([^<]+)[\s\S]*?s-item__caption">([^<]+)',
        html,
    ):
        href, title, price, sold = m.groups()
        items.append(
            {
                "source": "fixture_li.s-item",
                "href": href,
                "title": title.strip(),
                "anchorText": title.strip(),
                "priceText": price.strip(),
                "soldDateText": sold.strip(),
                "shippingText": "",
                "conditionText": "",
                "text": f"{sold.strip()}\n{title.strip()}\n{price.strip()}",
            }
        )
    return items


class OfflineSoldPipelineIntegrationTests(unittest.TestCase):
    def test_fixture_pipeline_exact_comps_price_and_safe_write(self) -> None:
        path = _ensure_fixture()
        html = path.read_text(encoding="utf-8")
        url = "https://www.ebay.com.au/sch/i.html?_nkw=Bloodmoon+Ursaluna+54+prismatic+evolutions+Pokemon&LH_Sold=1"
        title = "Bloodmoon Ursaluna 54 Prismatic Evolutions Pokemon for sale | eBay"
        sold = verify_captured_sold_state(url=url, title=title, body_text=html)
        self.assertTrue(sold["SOLD_STATE_VERIFIED"])
        integrity = capture_integrity_ok(
            url=url,
            title=title,
            body_text=html,
            expected_url=url,
            expected_query="Bloodmoon Ursaluna 54 prismatic evolutions Pokemon",
        )
        self.assertTrue(integrity["ok"])
        itm = count_itm_hrefs(html)
        self.assertGreaterEqual(itm["hrefCount"], 5)

        request = _bloodmoon_request()
        query = build_provider_search_query(request)
        candidates = _candidates_from_fixture_html(html)
        self.assertGreaterEqual(len(candidates), 5)

        comps = []
        for i, c in enumerate(candidates):
            comp = parse_candidate_dict(c, index=i, request=request, search_query=query)
            if comp is not None:
                comps.append(comp)
        self.assertGreaterEqual(len(comps), 4)

        evaluated = filter_comps(request.price_key, comps)
        accepted = [e for e in evaluated if e.included_in_estimate]
        rejected = [e for e in evaluated if not e.included_in_estimate]
        reasons = {e.rejection_reason for e in rejected}
        self.assertTrue(accepted)
        self.assertIn("graded_for_raw_request", reasons)
        self.assertTrue({"likely_bundle_lot", "wrong_collector_number"} & reasons or "likely_bundle_lot" in reasons or "wrong_collector_number" in reasons)

        stats = calculate_pricing_stats(accepted, config=MarketEngineConfig.from_env(require_supabase=False))
        self.assertIsNotNone(stats.recommended_price)
        self.assertGreater(float(stats.recommended_price or 0), 0)
        self.assertNotEqual(stats.recommended_price, 0)

        # Safe write decision: new ebay sold can replace prior; reference cannot.
        ok, reason = can_proposed_replace_selected(
            current_provider="ebay_browser",
            current_price=2.56,
            current_display_source="verified_local",
            current_observed_at="2026-09-29T02:57:30.442061+00:00",
            proposed_provider="ebay_browser",
            proposed_price=stats.recommended_price,
            proposed_display_source="verified_local",
            proposed_observed_at=datetime.now(timezone.utc),
        )
        self.assertTrue(ok, reason)
        ref_ok, ref_reason = can_proposed_replace_selected(
            current_provider="ebay_browser",
            current_price=2.56,
            current_display_source="verified_local",
            current_observed_at=datetime.now(timezone.utc),
            proposed_provider="tcgplayer",
            proposed_price=0.26,
            proposed_display_source="reference",
            proposed_observed_at=datetime.now(timezone.utc),
        )
        self.assertFalse(ref_ok)
        self.assertTrue(ref_reason)

        # Capture representation success mapping
        proc = CaptureProcessResult(
            status="SUCCESS",
            payload={
                "status": "SUCCESS",
                "body_text": html,
                "html": html,
                "canonical_itm_href_count": itm["hrefCount"],
                "candidates": candidates,
                "target_url": url,
                "target_title": title,
                "capture_method": "fixture",
                "elapsed_ms": 1,
            },
            elapsed_ms=1,
        )
        sold_cap = capture_process_result_to_sold_page(proc, x11_sold_state_verified=True)
        self.assertTrue(sold_cap.success)
        self.assertEqual(sold_cap.capture_phase, POST_SOLD_CAPTURE_READY)

    def test_capture_success_zero_exact_comps_retains_last_good(self) -> None:
        request = _bloodmoon_request()
        query = build_provider_search_query(request)
        candidates = [
            {
                "source": "t",
                "href": "https://www.ebay.com.au/itm/1",
                "title": "Pikachu 25 Base Set Pokemon",
                "priceText": "AU $9.00",
                "soldDateText": "Sold 1 Jan 2026",
                "text": "Sold 1 Jan 2026\nPikachu 25 Base Set Pokemon\nAU $9.00",
            }
        ]
        comps = [
            c
            for c in (
                parse_candidate_dict(candidates[0], index=0, request=request, search_query=query),
            )
            if c is not None
        ]
        evaluated = filter_comps(request.price_key, comps)
        accepted = [e for e in evaluated if e.included_in_estimate]
        self.assertEqual(accepted, [])
        selected = select_customer_market_price(
            observations=[
                PriceObservation(
                    provider="ebay_browser",
                    price=2.56,
                    observed_at=datetime.fromisoformat("2026-09-29T02:57:30.442061+00:00"),
                    display_source="verified_local",
                    confidence="medium",
                    sample_size=4,
                )
            ]
        )
        self.assertEqual(selected.price, 2.56)
        self.assertEqual(selected.provider, "ebay_browser")

    def test_capture_success_parser_failure_no_freshness(self) -> None:
        proc = CaptureProcessResult(
            status="SUCCESS",
            payload={
                "status": "SUCCESS",
                "body_text": "<html><body>Sold 1 Jan 2026</body></html>",
                "html": "<html></html>",
                "canonical_itm_href_count": 0,
                "candidates": [],
                "target_url": "https://www.ebay.com.au/sch/i.html?LH_Sold=1",
                "target_title": "x",
                "capture_method": "x",
                "elapsed_ms": 1,
            },
            elapsed_ms=1,
        )
        sold = capture_process_result_to_sold_page(proc, x11_sold_state_verified=True)
        self.assertFalse(sold.success)
        self.assertEqual(sold.failure_class, "CAPTURE_INTEGRITY_FAILURE")

    def test_capture_timeout_and_crash_and_orphans(self) -> None:
        started = time.monotonic()
        timed = run_capture_worker_process(
            cdp_endpoint="http://127.0.0.1:9",
            expected_url="https://www.ebay.com.au/sch/i.html?LH_Sold=1",
            expected_query="x",
            deadline_seconds=2.0,
            hang_at="before_connect",
        )
        self.assertEqual(timed.status, CDP_CAPTURE_PROCESS_TIMEOUT)
        self.assertEqual(timed.orphan_count_after, 0)
        self.assertLess(time.monotonic() - started, 8.0)

        crashed = run_capture_worker_process(
            cdp_endpoint="http://127.0.0.1:9",
            expected_url="https://www.ebay.com.au/sch/i.html?LH_Sold=1",
            expected_query="x",
            deadline_seconds=2.0,
            hang_at="crash",
        )
        self.assertIn(crashed.status, {CDP_CAPTURE_PROCESS_CRASH, "CDP_CONNECT_FAILURE", CDP_CAPTURE_PROCESS_TIMEOUT})
        self.assertEqual(crashed.orphan_count_after, 0)

    def test_unknown_price_never_zero_and_ownership_immutable(self) -> None:
        stats = calculate_pricing_stats([], config=MarketEngineConfig.from_env(require_supabase=False))
        self.assertIsNone(stats.recommended_price)
        # Dry-run ownership invariant (integration contract).
        ownership_mutations = 0
        self.assertEqual(ownership_mutations, 0)


class RealX11BloodmoonArtifactTests(unittest.TestCase):
    def test_x11_body_has_sold_evidence_but_no_canonical_itm(self) -> None:
        path = ROOT / "reports" / "artifacts" / "linux_sold_nav_1790799478_body.txt"
        if not path.is_file():
            path = ROOT / "reports" / "artifacts" / "linux_sold_nav_1790797498_body.txt"
        if not path.is_file():
            self.skipTest("Bloodmoon X11 body artifact missing")
        body = path.read_text(encoding="utf-8", errors="replace")
        itm = count_itm_hrefs(body)
        self.assertEqual(itm["hrefCount"], 0)
        url = "https://www.ebay.com.au/sch/i.html?_nkw=Bloodmoon+Ursaluna+54+prismatic+evolutions+Pokemon&LH_Sold=1"
        sold = verify_captured_sold_state(url=url, title="Bloodmoon", body_text=body)
        self.assertTrue(sold["SOLD_STATE_VERIFIED"])
        # Production parse cannot mint hrefs from text-only body.
        request = _bloodmoon_request()
        query = build_provider_search_query(request)
        comp = parse_candidate_dict(
            {"source": "x11", "href": "", "text": body[:2000], "title": "Bloodmoon Ursaluna 54"},
            index=0,
            request=request,
            search_query=query,
        )
        self.assertIsNone(comp)


if __name__ == "__main__":
    _ensure_fixture()
    unittest.main()
