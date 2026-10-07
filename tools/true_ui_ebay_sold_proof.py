#!/usr/bin/env python3
"""True UI-driven eBay sold flow proof (homepage → type → Search → Sold).

Constraints for TRUE UI mode:
- No direct /sch/i.html?... navigation for search
- No LH_Sold / query-string sold deep-links
- No page.evaluate DOM .click() or form/filter JS mutation
- Visible search field typing + visible Search + visible Sold items via Playwright
  mouse/keyboard (Input events), matching Andrew's manual path.

CURRENT mode uses homepage warm + clean search_url goto + Sold UI click (existing provider).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.providers.ebay_browser_provider import (
    EbayBrowserProviderConfig,
    EbayBrowserSoldCompsProvider,
    apply_sold_completed_filters_via_ui,
    classify_browser_page_state,
    verify_sold_result_state,
)
from cardscanr_market_engine.providers.errors import (
    ProviderAuthenticationRequiredError,
    ProviderBlockedError,
    ProviderTemporaryError,
)

OUT_DIR = ROOT / "reports" / "artifacts" / "owned_daily_session"


def _is_challenge(title: str, body: str, url: str) -> bool:
    blob = f"{title}\n{body}\n{url}".lower()
    markers = (
        "security measure",
        "please verify yourself",
        "captcha",
        "robot check",
        "unusual traffic",
        "verify yourself to continue",
    )
    return any(m in blob for m in markers)


def _is_sorry(title: str, body: str, url: str) -> bool:
    state = classify_browser_page_state(title=title, body_text=body)
    if state.get("reason") == "ebay_sorry_error_page":
        return True
    blob = f"{title}\n{body}".lower()
    return "sorry" in blob and ("something went wrong" in blob or "we couldn't find" in blob or "error" in title.lower())


def _signed_in_hint(body: str) -> bool | None:
    lower = body.lower()
    if "hi " in lower[:4000] or "my ebay" in lower:
        return True
    if "sign in" in lower and "register" in lower:
        return False
    return None


def _count_sold_candidates(body: str) -> int:
    return sum(1 for line in body.splitlines() if line.strip().lower().startswith("sold "))


def true_ui_sold_flow(page: Any, *, query: str, timeout_ms: int = 90000) -> dict[str, Any]:
    """Manual-equivalent UI path. Homepage goto only; search/Sold via visible controls."""
    report: dict[str, Any] = {
        "mode": "TRUE_UI",
        "query": query,
        "entryMechanism": "goto_homepage_only",
        "searchMechanism": "visible_search_field_type_then_search_button_or_enter",
        "soldActivationMechanism": "visible_Sold_items_control_mouse_click",
        "directUrlManipulation": False,
        "domMutationOrJsClick": False,
        "homepageLoaded": False,
        "signedInHint": None,
        "searchFieldInteraction": False,
        "searchResultsSuccess": False,
        "soldInteraction": False,
        "resultingUrl": None,
        "pageState": None,
        "SORRY": False,
        "challenge": False,
        "SOLD_STATE_VERIFIED": False,
        "soldCandidates": 0,
        "error": None,
        "steps": [],
    }

    def step(name: str, **extra: Any) -> None:
        report["steps"].append({"step": name, "url": getattr(page, "url", None), **extra})

    # 1) Homepage only
    page.goto("https://www.ebay.com.au/", wait_until="domcontentloaded", timeout=timeout_ms)
    time.sleep(1.5)
    title = page.title() or ""
    body = page.inner_text("body") if page.locator("body").count() else ""
    report["homepageLoaded"] = "ebay.com.au" in (page.url or "").lower()
    report["signedInHint"] = _signed_in_hint(body)
    step("homepage", title=title[:120])
    if _is_challenge(title, body, page.url):
        report["challenge"] = True
        report["error"] = "CHALLENGE_REQUIRED on homepage"
        report["pageState"] = classify_browser_page_state(title=title, body_text=body)
        return report
    if _is_sorry(title, body, page.url):
        report["SORRY"] = True
        report["error"] = "SORRY on homepage"
        return report

    # 2–4) Focus visible search, type query, submit via UI (no manufactured search URL)
    search_selectors = (
        "input[type='search'][name='_nkw']",
        "input[name='_nkw']",
        "#gh-ac",
        "input[aria-label*='Search' i]",
        "input[placeholder*='Search' i]",
    )
    search_box = None
    used_search_sel = None
    for sel in search_selectors:
        loc = page.locator(sel)
        try:
            if loc.count() > 0 and loc.first.is_visible():
                search_box = loc.first
                used_search_sel = sel
                break
        except Exception:
            continue
    if search_box is None:
        report["error"] = "visible_search_field_not_found"
        return report

    search_box.click(timeout=10000, delay=60)
    time.sleep(0.3)
    # Keyboard clear + type only (no JS value injection / form mutation).
    search_box.press("Control+a")
    search_box.press("Backspace")
    if hasattr(search_box, "press_sequentially"):
        search_box.press_sequentially(query, delay=45)
    else:
        search_box.type(query, delay=45)
    report["searchFieldInteraction"] = True
    step("typed_query", selector=used_search_sel, query=query)

    search_btn_selectors = (
        "button[type='submit']#gh-btn",
        "#gh-btn",
        "button.gh-search-button",
        "input[type='submit'][value*='Search' i]",
        "button:has-text('Search')",
    )
    submitted = False
    for sel in search_btn_selectors:
        loc = page.locator(sel)
        try:
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.click(timeout=8000, delay=80)
                submitted = True
                step("search_button_click", selector=sel)
                break
        except Exception:
            continue
    if not submitted:
        search_box.press("Enter")
        step("search_enter_key")
        submitted = True

    try:
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except Exception:
        pass
    time.sleep(2.5)
    title = page.title() or ""
    body = page.inner_text("body") if page.locator("body").count() else ""
    report["resultingUrl"] = page.url
    if _is_challenge(title, body, page.url):
        report["challenge"] = True
        report["error"] = "CHALLENGE_REQUIRED after search"
        report["pageState"] = classify_browser_page_state(title=title, body_text=body)
        return report
    if _is_sorry(title, body, page.url):
        report["SORRY"] = True
        report["error"] = "SORRY after search (pre-Sold)"
        report["pageState"] = classify_browser_page_state(title=title, body_text=body)
        return report
    # Active results heuristic
    report["searchResultsSuccess"] = (
        "/sch/" in (page.url or "").lower()
        or "results for" in body.lower()
        or bool(re.search(r"\d[\d,]*\s+results?", body.lower()))
    )
    step("active_results", success=report["searchResultsSuccess"], url=page.url)

    # 5–8) Visible Sold items control (mouse click; filter helper uses locator.click not JS)
    try:
        filter_diag = apply_sold_completed_filters_via_ui(page, timeout_ms=timeout_ms)
        report["soldInteraction"] = True
        report["soldFilterDiagnostics"] = {
            k: filter_diag.get(k)
            for k in (
                "soldSelectorUsed",
                "completedSelectorUsed",
                "SOLD_STATE_VERIFIED",
                "urlAfterFilters",
                "soldDateLines",
            )
        }
    except (ProviderTemporaryError, ProviderBlockedError, ProviderAuthenticationRequiredError) as exc:
        msg = str(exc)
        report["error"] = msg
        title = page.title() or ""
        body = page.inner_text("body") if page.locator("body").count() else ""
        report["resultingUrl"] = page.url
        report["challenge"] = _is_challenge(title, body, page.url) or "authentication" in msg.lower()
        report["SORRY"] = _is_sorry(title, body, page.url) or "sorry" in msg.lower()
        report["pageState"] = classify_browser_page_state(title=title, body_text=body)
        sold_state = verify_sold_result_state(url=page.url, title=title, body_text=body)
        report["SOLD_STATE_VERIFIED"] = bool(sold_state.get("SOLD_STATE_VERIFIED"))
        report["soldCandidates"] = _count_sold_candidates(body)
        return report
    except Exception as exc:
        report["error"] = f"{exc.__class__.__name__}: {exc}"
        title = page.title() or ""
        body = page.inner_text("body") if page.locator("body").count() else ""
        report["resultingUrl"] = page.url
        report["SORRY"] = _is_sorry(title, body, page.url)
        report["challenge"] = _is_challenge(title, body, page.url)
        return report

    time.sleep(1.0)
    title = page.title() or ""
    body = page.inner_text("body") if page.locator("body").count() else ""
    report["resultingUrl"] = page.url
    sold_state = verify_sold_result_state(url=page.url, title=title, body_text=body)
    report["SOLD_STATE_VERIFIED"] = bool(sold_state.get("SOLD_STATE_VERIFIED"))
    report["soldCandidates"] = int(sold_state.get("soldDateLines") or 0)
    report["pageState"] = classify_browser_page_state(title=title, body_text=body)
    report["SORRY"] = _is_sorry(title, body, page.url)
    report["challenge"] = _is_challenge(title, body, page.url)
    step("sold_verified", **{k: sold_state.get(k) for k in ("SOLD_STATE_VERIFIED", "soldDateLines", "soldUrlParam")})
    return report


def current_programmatic_flow(page: Any, *, query: str, timeout_ms: int = 90000) -> dict[str, Any]:
    """Current production-style entry: homepage warm + clean active search_url goto + Sold UI."""
    report: dict[str, Any] = {
        "mode": "CURRENT_PROGRAMMATIC",
        "query": query,
        "entryMechanism": "homepage_then_clean_search_url_goto",
        "searchMechanism": "page.goto(search_url) without LH_Sold",
        "soldActivationMechanism": "visible_Sold_items_control_mouse_click",
        "directUrlManipulation": True,
        "domMutationOrJsClick": False,
        "homepageLoaded": False,
        "searchResultsSuccess": False,
        "soldInteraction": False,
        "resultingUrl": None,
        "SORRY": False,
        "challenge": False,
        "SOLD_STATE_VERIFIED": False,
        "soldCandidates": 0,
        "error": None,
        "searchUrl": None,
    }
    # Clean active search URL (no LH_Sold) — mirrors ebay_browser_provider AU entry.
    search_url = (
        "https://www.ebay.com.au/sch/i.html?"
        f"_nkw={quote_plus(query)}&_sacat=0&_ipg=60&rt=nc"
    )
    report["searchUrl"] = search_url
    page.goto("https://www.ebay.com.au/", wait_until="domcontentloaded", timeout=timeout_ms)
    time.sleep(2.0)
    report["homepageLoaded"] = "ebay.com.au" in (page.url or "").lower()
    page.goto(search_url, wait_until="domcontentloaded", timeout=timeout_ms)
    time.sleep(3.0)
    title = page.title() or ""
    body = page.inner_text("body") if page.locator("body").count() else ""
    report["resultingUrl"] = page.url
    if _is_challenge(title, body, page.url):
        report["challenge"] = True
        report["error"] = "CHALLENGE_REQUIRED after search_url goto"
        return report
    if _is_sorry(title, body, page.url):
        report["SORRY"] = True
        report["error"] = "PRE_SOLD_SORRY on clean active search_url"
        return report
    report["searchResultsSuccess"] = "/sch/" in (page.url or "").lower()
    try:
        filter_diag = apply_sold_completed_filters_via_ui(page, timeout_ms=timeout_ms)
        report["soldInteraction"] = True
        report["soldFilterDiagnostics"] = {
            k: filter_diag.get(k)
            for k in ("soldSelectorUsed", "SOLD_STATE_VERIFIED", "urlAfterFilters", "soldDateLines")
        }
    except Exception as exc:
        msg = str(exc)
        report["error"] = msg
        title = page.title() or ""
        body = page.inner_text("body") if page.locator("body").count() else ""
        report["resultingUrl"] = page.url
        report["SORRY"] = _is_sorry(title, body, page.url) or "sorry" in msg.lower()
        report["challenge"] = _is_challenge(title, body, page.url)
        sold_state = verify_sold_result_state(url=page.url, title=title, body_text=body)
        report["SOLD_STATE_VERIFIED"] = bool(sold_state.get("SOLD_STATE_VERIFIED"))
        report["soldCandidates"] = _count_sold_candidates(body)
        return report
    title = page.title() or ""
    body = page.inner_text("body") if page.locator("body").count() else ""
    report["resultingUrl"] = page.url
    sold_state = verify_sold_result_state(url=page.url, title=title, body_text=body)
    report["SOLD_STATE_VERIFIED"] = bool(sold_state.get("SOLD_STATE_VERIFIED"))
    report["soldCandidates"] = int(sold_state.get("soldDateLines") or 0)
    report["SORRY"] = _is_sorry(title, body, page.url)
    report["challenge"] = _is_challenge(title, body, page.url)
    return report


def _launch_page(config: EbayBrowserProviderConfig):
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    context = pw.chromium.launch_persistent_context(
        user_data_dir=str(config.ensure_profile_dir()),
        channel=config.channel,
        headless=False,
        viewport={"width": 1365, "height": 900},
        timeout=config.launch_timeout_seconds * 1000,
    )
    page = context.pages[0] if context.pages else context.new_page()
    page.set_default_timeout(config.timeout_seconds * 1000)
    return pw, context, page


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", default="Ivysaur Base Set 30/102 Pokemon")
    parser.add_argument("--mode", choices=("true_ui", "current", "both"), default="both")
    parser.add_argument("--out", default="true_ui_ivysaur_proof.json")
    parser.add_argument("--cool-seconds", type=int, default=45)
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("EBAY_BROWSER_ENABLED", "true")
    os.environ.setdefault("EBAY_BROWSER_HEADLESS", "false")
    config = EbayBrowserProviderConfig.from_env()
    config.validate()

    payload: dict[str, Any] = {
        "query": args.query,
        "profile": str(config.ensure_profile_dir()),
        "modes": {},
    }

    modes = ["true_ui", "current"] if args.mode == "both" else [args.mode]
    for i, mode in enumerate(modes):
        if i > 0 and args.cool_seconds > 0:
            print(f"[true-ui-proof] cool {args.cool_seconds}s between modes", flush=True)
            time.sleep(args.cool_seconds)
        print(f"[true-ui-proof] launching headed Chrome for mode={mode}", flush=True)
        pw = context = page = None
        try:
            pw, context, page = _launch_page(config)
            if mode == "true_ui":
                result = true_ui_sold_flow(page, query=args.query, timeout_ms=config.timeout_seconds * 1000)
            else:
                result = current_programmatic_flow(page, query=args.query, timeout_ms=config.timeout_seconds * 1000)
            payload["modes"][mode] = result
            print(json.dumps({k: result.get(k) for k in (
                "mode", "homepageLoaded", "searchResultsSuccess", "soldInteraction",
                "SOLD_STATE_VERIFIED", "soldCandidates", "SORRY", "challenge", "error", "resultingUrl"
            )}, indent=2), flush=True)
            if result.get("challenge"):
                print("[true-ui-proof] CHALLENGE_REQUIRED — STOP", flush=True)
                break
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            if pw is not None:
                try:
                    pw.stop()
                except Exception:
                    pass

    out_path = OUT_DIR / args.out
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"[true-ui-proof] wrote {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
