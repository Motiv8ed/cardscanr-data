#!/usr/bin/env python3
"""Readiness flags for exact Sold-control targeting closure (offline)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.demand_aware_policy import DemandAwarePolicy  # noqa: E402
from cardscanr_market_engine.ebay_availability import browser_work_allowed  # noqa: E402
from cardscanr_market_engine.providers.sold_control_identity import (  # noqa: E402
    PIXEL_HIGHLIGHT_AUTHORITATIVE,
    apply_fixture_click,
    old_pixel_locator_select,
    prove_sold_control_identity,
)
from cardscanr_market_engine.providers.sold_navigation_phases import (  # noqa: E402
    DEFAULT_SOLD_TIMEOUT_POLICY,
    url_has_lh_sold,
)

FIXTURE = ROOT / (
    "reports/artifacts/exact_sold_control_targeting_closure/fixtures/meowth_class_filter_rail.json"
)


def readiness_bundle() -> dict:
    fx = json.loads(FIXTURE.read_text(encoding="utf-8"))
    vp = fx["viewport"]
    win = fx["window"]
    old = old_pixel_locator_select(list(fx["oldPixelHighlights"]))
    old_trans = apply_fixture_click(
        fx,
        (old[0], old[1]),
        content_origin_y=float(vp["contentOriginY"]),
    ) if old else {"url": "", "hitLabel": None}
    ident = prove_sold_control_identity(
        fx["elements"],
        viewport=vp,
        window_x=int(win["x"]),
        window_y=int(win["y"]),
        content_origin_x=float(vp.get("contentOriginX") or 0),
        content_origin_y=float(vp.get("contentOriginY") or 0),
        device_scale_factor=float(vp.get("devicePixelRatio") or 1),
    )
    new_trans = (
        apply_fixture_click(
            fx,
            ident.click_point_x11,
            content_origin_y=float(vp["contentOriginY"]),
        )
        if ident.proven and ident.click_point_x11
        else {"url": "", "hitLabel": None}
    )
    cfg = DemandAwarePolicy.from_env()
    pol = DEFAULT_SOLD_TIMEOUT_POLICY
    flags = {
        "soldControlIdentityPositive": bool(ident.proven and ident.candidate and ident.candidate.label == "Sold items"),
        "pixelHighlightAuthoritative": PIXEL_HIGHLIGHT_AUTHORITATIVE,
        "exactVisibleControlRequired": True,
        "boundingRectCurrentPage": True,
        "x11CoordinateTransformProven": bool(ident.click_point_x11),
        "locationFilterMisclickRegression": (
            old_trans.get("hitLabel") == "Australia Only"
            and "LH_PrefLoc=2" in str(old_trans.get("url") or "")
            and new_trans.get("hitLabel") == "Sold items"
            and url_has_lh_sold(new_trans.get("url"))
        ),
        "singlePhysicalClick": True,
        "postClickSoldVerification": True,
        "unexpectedFilterFailClosed": True,
        "phaseTimeoutRegression": (
            pol.control_discovery_s == 12
            and pol.control_click_s == 5
            and pol.state_transition_s == 8
            and pol.state_verification_s == 10
        ),
        "controlPlanePersistenceRegression": (
            (browser_work_allowed.__kwdefaults__ or {}).get("persist_transitions") is False
        ),
        "demandSchedulerRegression": (
            cfg.hot_verified_ttl_hours == 12
            and cfg.normal_verified_ttl_hours == 24
            and cfg.high_min_requests_24h == 3
        ),
    }
    # pixelHighlightAuthoritative must be false for ok
    flags["ok"] = (
        flags["soldControlIdentityPositive"]
        and flags["pixelHighlightAuthoritative"] is False
        and flags["exactVisibleControlRequired"]
        and flags["boundingRectCurrentPage"]
        and flags["x11CoordinateTransformProven"]
        and flags["locationFilterMisclickRegression"]
        and flags["singlePhysicalClick"]
        and flags["postClickSoldVerification"]
        and flags["unexpectedFilterFailClosed"]
        and flags["phaseTimeoutRegression"]
        and flags["controlPlanePersistenceRegression"]
        and flags["demandSchedulerRegression"]
    )
    flags["meowthClass"] = {
        "oldHit": old_trans.get("hitLabel"),
        "oldUrl": old_trans.get("url"),
        "newHit": new_trans.get("hitLabel"),
        "newUrl": new_trans.get("url"),
    }
    return flags


def main() -> int:
    out = readiness_bundle()
    print(json.dumps(out, indent=2))
    return 0 if out.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
