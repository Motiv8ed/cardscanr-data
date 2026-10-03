"""Representation-aware eBay challenge classification regression tests."""
from __future__ import annotations

import hashlib
import unittest
from datetime import datetime, timezone
from pathlib import Path

from cardscanr_market_engine.price_source_precedence import can_proposed_replace_selected
from cardscanr_market_engine.providers.ebay_browser_provider import (
    classify_browser_page_state,
    contains_block_marker,
)

ROOT = Path(__file__).resolve().parents[1]
MAGNEZONE_HTML = ROOT / "reports" / "artifacts" / "ebay_sold_capture_20261001T022754Z.html"
MAGNEZONE_BODY = ROOT / "reports" / "artifacts" / "post_sold_capture_last" / "last_capture_body.txt"
MAGNEZONE_SHA = "fb52de156d209d694ea599e3409651c458ce0f9efea7c369bc05024f54d55a9c"

ORDINARY_SOLD_BODY = """
Sold listings
34 results for Magnezone 47 mega evolution Pokemon
Sold 30 Sep 2026
Magnezone 047/132 Mega Evolution Pokemon Card
AU $2.50
"""

ORDINARY_SOLD_HTML_WITH_RECAPTCHA = f"""<!DOCTYPE html>
<html><head><title>Magnezone 47 Mega Evolution Pokemon for sale | eBay</title>
<style>
.ifh-captcha .ifh-captcha-header {{ font-size: 1.25rem; }}
.ifh-captcha .ifh-captcha-prompt {{ font-weight: 700; }}
</style>
</head><body>
Sold listings
34 results for Magnezone 47 mega evolution Pokemon
<li class="s-item"><a href="https://www.ebay.com.au/itm/123456789012">Magnezone 47 Mega Evolution</a>
<span>AU $2.50</span><span>Sold 30 Sep 2026</span></li>
<iframe src="https://www.google.com/recaptcha/api2/aframe" width="0" height="0" style="display: none;"></iframe>
<script src="https://www.google.com/recaptcha/api.js"></script>
</body></html>
"""

HIDDEN_CHALLENGE_MARKUP_HTML = """<!DOCTYPE html>
<html><body>
Sold listings
results for Charizard
<a href="https://www.ebay.com.au/itm/111222333444">Charizard 4 Base Set</a>
<div class="ifh-captcha" style="display:none" aria-hidden="true">
  <div class="ifh-captcha-header">unused</div>
</div>
</body></html>
"""

VISIBLE_CHALLENGE_BODY = """
Please verify yourself to continue.
Are you a robot?
Security challenge
"""

VISIBLE_CHALLENGE_WIDGET_UI = {
    "visibleChallengeText": False,
    "visibleCaptchaFrameCount": 1,
    "visibleChallengeWidget": True,
    "captchaFrameCandidates": [
        {
            "src": "https://www.google.com/recaptcha/api2/anchor",
            "w": 304,
            "h": 78,
            "display": "block",
            "visibility": "visible",
            "captchaLike": True,
            "renderedVisible": True,
        }
    ],
}


class ChallengeClassificationTests(unittest.TestCase):
    def test_ordinary_sold_no_captcha_resources_is_results(self) -> None:
        state = classify_browser_page_state(
            title="Magnezone for sale | eBay",
            body_text=ORDINARY_SOLD_BODY,
            url="https://www.ebay.com.au/sch/i.html?_nkw=Magnezone+47&LH_Sold=1",
            selector_counts={"canonical_itm_href_count": 12, "li.s-item": 12},
            x11_sold_state_verified=True,
        )
        self.assertEqual(state["outcome"], "success")
        self.assertEqual(state.get("securityClass"), "ORDINARY_RESULTS")
        self.assertFalse(state.get("passiveChallengeResources"))

    def test_ordinary_sold_with_recaptcha_script_is_results(self) -> None:
        state = classify_browser_page_state(
            title="Magnezone for sale | eBay",
            body_text=ORDINARY_SOLD_BODY,
            html_document=ORDINARY_SOLD_HTML_WITH_RECAPTCHA,
            url="https://www.ebay.com.au/sch/i.html?_nkw=Magnezone+47&LH_Sold=1",
            selector_counts={"canonical_itm_href_count": 12},
            x11_sold_state_verified=True,
        )
        self.assertEqual(state["outcome"], "success")
        self.assertTrue(state.get("passiveChallengeResources"))
        self.assertFalse(state.get("activeChallengeEvidence"))

    def test_ordinary_sold_with_hidden_inactive_challenge_markup_not_active(self) -> None:
        state = classify_browser_page_state(
            title="Charizard for sale | eBay",
            body_text="Sold listings\nresults for Charizard\nAU $10",
            html_document=HIDDEN_CHALLENGE_MARKUP_HTML,
            url="https://www.ebay.com.au/sch/i.html?_nkw=Charizard&LH_Sold=1",
            selector_counts={"canonical_itm_href_count": 3},
        )
        self.assertEqual(state["outcome"], "success")
        self.assertNotEqual(state.get("securityClass"), "ACTIVE_CHALLENGE")

    def test_visible_active_challenge_widget_is_challenge(self) -> None:
        state = classify_browser_page_state(
            title="eBay",
            body_text="Please wait",
            url="https://www.ebay.com.au/sch/i.html?_nkw=x",
            challenge_ui=VISIBLE_CHALLENGE_WIDGET_UI,
        )
        self.assertEqual(state["outcome"], "challenge_detected")
        self.assertEqual(state.get("securityClass"), "ACTIVE_CHALLENGE")

    def test_visible_challenge_text_is_challenge(self) -> None:
        state = classify_browser_page_state(
            title="Verify yourself",
            body_text=VISIBLE_CHALLENGE_BODY,
            url="https://www.ebay.com.au/",
        )
        self.assertEqual(state["outcome"], "challenge_detected")

    def test_known_challenge_security_url_is_challenge(self) -> None:
        state = classify_browser_page_state(
            title="Security Measure",
            body_text="Loading",
            url="https://www.ebay.com.au/splashui/captcha",
        )
        self.assertEqual(state["outcome"], "challenge_detected")

    def test_sorry_403_page_keeps_sorry_classification(self) -> None:
        state = classify_browser_page_state(
            title="Error Page | eBay",
            body_text="SORRY Something went wrong on our end",
            url="https://www.ebay.com.au/sch/i.html?_nkw=x",
        )
        self.assertEqual(state["outcome"], "provider_unavailable")
        self.assertEqual(state["reason"], "ebay_sorry_error_page")

    def test_strong_sold_plus_passive_captcha_resources_is_results(self) -> None:
        # Full HTML as body_text must not false-positive on CSS/iframe captcha tokens.
        state = classify_browser_page_state(
            title="Magnezone for sale | eBay",
            body_text=ORDINARY_SOLD_HTML_WITH_RECAPTCHA,
            url="https://www.ebay.com.au/sch/i.html?_nkw=Magnezone+47&LH_Sold=1",
            selector_counts={"canonical_itm_href_count": 20},
            x11_sold_state_verified=True,
        )
        self.assertEqual(state["outcome"], "success")
        self.assertTrue(any("captcha" in str(x).lower() or "recaptcha" in str(x).lower() for x in (state.get("passiveChallengeResources") or [])))

    def test_conflicting_security_evidence_fails_closed_ambiguous(self) -> None:
        state = classify_browser_page_state(
            title="Magnezone for sale | eBay",
            body_text=ORDINARY_SOLD_BODY + "\nPlease verify yourself to continue.",
            url="https://www.ebay.com.au/sch/i.html?_nkw=Magnezone+47&LH_Sold=1",
            selector_counts={"canonical_itm_href_count": 15},
            x11_sold_state_verified=True,
        )
        self.assertEqual(state["outcome"], "ambiguous_security_state")
        self.assertEqual(state.get("securityClass"), "AMBIGUOUS_SECURITY_STATE")

    def test_challenge_detection_not_solely_raw_substring_captcha(self) -> None:
        self.assertFalse(
            contains_block_marker(
                title="Magnezone listings",
                body_text=ORDINARY_SOLD_HTML_WITH_RECAPTCHA,
            )
        )
        self.assertTrue(
            contains_block_marker(title="Verify yourself", body_text="Are you a robot?")
        )
        # Bare captcha token in CSS must not classify as active challenge.
        state = classify_browser_page_state(
            title="Magnezone for sale | eBay",
            body_text='<style>.ifh-captcha{color:red}</style><body>Sold listings results for Magnezone</body>',
            url="https://www.ebay.com.au/sch/i.html?LH_Sold=1",
            selector_counts={"canonical_itm_href_count": 5},
        )
        self.assertEqual(state["outcome"], "success")

    @unittest.skipUnless(MAGNEZONE_HTML.is_file() and MAGNEZONE_BODY.is_file(), "Magnezone artifact missing")
    def test_persisted_magnezone_artifact_not_false_positive(self) -> None:
        html = MAGNEZONE_HTML.read_bytes()
        self.assertEqual(hashlib.sha256(html).hexdigest(), MAGNEZONE_SHA)
        body = MAGNEZONE_BODY.read_text(encoding="utf-8", errors="replace")
        state = classify_browser_page_state(
            title="Magnezone 47 Mega Evolution Pokemon for sale | eBay",
            body_text=body,
            html_document=html.decode("utf-8", errors="replace"),
            url=(
                "https://www.ebay.com.au/sch/i.html?_nkw=Magnezone+47+mega+evolution+Pokemon"
                "&_sacat=0&_from=R40&rt=nc&LH_Sold=1"
            ),
            selector_counts={"process_capture": 1, "canonical_itm_href_count": 195},
            x11_sold_state_verified=True,
        )
        self.assertEqual(state["outcome"], "success")
        self.assertNotEqual(state.get("outcome"), "challenge_detected")
        self.assertTrue(state.get("passiveChallengeResources"))

    def test_source_precedence_last_good_safety(self) -> None:
        ok, _reason = can_proposed_replace_selected(
            current_provider="ebay_browser",
            current_price=2.12,
            current_display_source="verified_local",
            current_observed_at=datetime.now(timezone.utc),
            proposed_provider="tcgplayer",
            proposed_price=0.11,
            proposed_display_source="reference",
            proposed_observed_at=datetime.now(timezone.utc),
        )
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
