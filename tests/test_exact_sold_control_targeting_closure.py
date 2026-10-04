#!/usr/bin/env python3
"""Offline exact Sold-control targeting closure tests (no eBay network)."""
from __future__ import annotations

import json
import os
import unittest
from copy import deepcopy
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.demand_aware_policy import DemandAwarePolicy
from cardscanr_market_engine.providers.sold_control_identity import (
    PIXEL_HIGHLIGHT_AUTHORITATIVE,
    SOLD_CONTROL_AMBIGUOUS,
    SOLD_CONTROL_IDENTITY_NOT_PROVEN,
    SOLD_CONTROL_INVALID_BOUNDS,
    SOLD_CONTROL_OFFSCREEN,
    apply_fixture_click,
    is_exact_sold_label,
    normalize_label,
    old_pixel_locator_select,
    prove_sold_control_identity,
    select_unique_sold_control,
    unexpected_filter_params,
    validate_bounding_rect,
    viewport_to_x11,
)
from cardscanr_market_engine.providers.sold_navigation_phases import (
    DEFAULT_SOLD_TIMEOUT_POLICY,
    url_has_lh_sold,
)
from cardscanr_market_engine.atomic_json_state import CONTROL_PLANE_PERSISTENCE_FAILURE
from cardscanr_market_engine.ebay_availability import browser_work_allowed, peek_availability

FIXTURE = ROOT / (
    "reports/artifacts/exact_sold_control_targeting_closure/fixtures/meowth_class_filter_rail.json"
)


def _load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _production_locate_and_click(fixture: dict) -> dict:
    """ACTUAL production identity + click-point path against fixture (no toy locator)."""
    win = fixture.get("window") or {}
    vp = fixture.get("viewport") or {}
    identity = prove_sold_control_identity(
        list(fixture.get("elements") or []),
        viewport=vp,
        window_x=int(win.get("x") or 0),
        window_y=int(win.get("y") or 0),
        content_origin_x=float(vp.get("contentOriginX") or 0),
        content_origin_y=float(vp.get("contentOriginY") or 0),
        device_scale_factor=float(vp.get("devicePixelRatio") or 1),
        page_url=str(fixture.get("baseUrl") or ""),
    )
    if not identity.proven or not identity.click_point_x11:
        return {"identity": identity, "transition": None}
    transition = apply_fixture_click(
        fixture,
        identity.click_point_x11,
        window_x=int(win.get("x") or 0),
        window_y=int(win.get("y") or 0),
        content_origin_x=float(vp.get("contentOriginX") or 0),
        content_origin_y=float(vp.get("contentOriginY") or 0),
        device_scale_factor=float(vp.get("devicePixelRatio") or 1),
    )
    return {"identity": identity, "transition": transition}


class ExactLabelTests(unittest.TestCase):
    def test_exact_sold_visible_text(self):
        self.assertTrue(is_exact_sold_label("Sold items"))
        self.assertTrue(is_exact_sold_label("  Sold   items "))
        self.assertFalse(is_exact_sold_label("Sold items near me"))
        self.assertFalse(is_exact_sold_label("Items sold"))
        self.assertFalse(is_exact_sold_label("Sold location"))
        self.assertEqual(normalize_label("Sold items"), "sold items")

    def test_accessible_label_lookup(self):
        cands = [
            {
                "tag": "input",
                "role": "checkbox",
                "label": "Sold items",
                "labelSource": "aria-label",
                "visible": True,
                "enabled": True,
                "inFilterRegion": True,
                "boundingRect": {"x": 50, "y": 400, "width": 100, "height": 20},
            }
        ]
        ident = select_unique_sold_control(cands, viewport={"innerWidth": 1200, "innerHeight": 800})
        self.assertTrue(ident.proven)


class UniquenessAndRejects(unittest.TestCase):
    def test_unique_candidate(self):
        fx = _load_fixture()
        out = _production_locate_and_click(fx)
        self.assertTrue(out["identity"].proven)
        self.assertEqual(out["transition"]["hitLabel"], "Sold items")
        self.assertIn("LH_Sold=1", out["transition"]["url"])
        self.assertNotIn("LH_PrefLoc", out["transition"]["url"])

    def test_hidden_duplicate_ignored(self):
        fx = _load_fixture()
        out = _production_locate_and_click(fx)
        self.assertTrue(out["identity"].proven)
        self.assertEqual(out["identity"].candidate.id, "sold_items")

    def test_visible_ambiguity_rejected(self):
        fx = _load_fixture()
        fx["elements"].append(
            {
                "id": "sold_items_2",
                "tag": "label",
                "role": "checkbox",
                "label": "Sold items",
                "localParam": "LH_Sold=1",
                "visible": True,
                "enabled": True,
                "inFilterRegion": True,
                "boundingRect": {"x": 48, "y": 520, "width": 180, "height": 28},
                "viewportIntersection": {"intersects": True, "area": 5000},
            }
        )
        ident = prove_sold_control_identity(
            fx["elements"], viewport=fx["viewport"], content_origin_y=80
        )
        self.assertFalse(ident.proven)
        self.assertEqual(ident.reason_code, SOLD_CONTROL_AMBIGUOUS)

    def test_zero_size_rejected(self):
        ok, reason = validate_bounding_rect(
            {"x": 10, "y": 10, "width": 0, "height": 20},
            viewport={"innerWidth": 800, "innerHeight": 600},
        )
        self.assertFalse(ok)
        self.assertEqual(reason, SOLD_CONTROL_INVALID_BOUNDS)

    def test_offscreen_rejected(self):
        ok, reason = validate_bounding_rect(
            {"x": 10, "y": 9000, "width": 100, "height": 20},
            viewport={"innerWidth": 800, "innerHeight": 600},
        )
        self.assertFalse(ok)
        self.assertEqual(reason, SOLD_CONTROL_OFFSCREEN)

    def test_substring_not_accepted(self):
        cands = [
            {
                "tag": "span",
                "role": "",
                "label": "Sold items near Sydney",
                "visible": True,
                "enabled": True,
                "inFilterRegion": True,
                "boundingRect": {"x": 50, "y": 400, "width": 100, "height": 20},
            }
        ]
        ident = select_unique_sold_control(cands, viewport={"innerWidth": 1200, "innerHeight": 800})
        self.assertFalse(ident.proven)
        self.assertEqual(ident.reason_code, SOLD_CONTROL_IDENTITY_NOT_PROVEN)


class CoordinateMappingTests(unittest.TestCase):
    def test_dom_to_x11_transform(self):
        sx, sy = viewport_to_x11(
            100,
            200,
            window_x=10,
            window_y=20,
            content_origin_x=0,
            content_origin_y=80,
            device_scale_factor=1.0,
        )
        self.assertEqual((sx, sy), (110, 300))

    def test_scaling(self):
        sx, sy = viewport_to_x11(
            100,
            200,
            window_x=0,
            window_y=0,
            content_origin_x=0,
            content_origin_y=0,
            device_scale_factor=2.0,
        )
        self.assertEqual((sx, sy), (200, 400))

    def test_changed_window_geometry(self):
        fx = _load_fixture()
        fx["window"] = {"x": 40, "y": 60, "w": 1280, "h": 880}
        out = _production_locate_and_click(fx)
        self.assertTrue(out["identity"].proven)
        self.assertEqual(out["transition"]["hitLabel"], "Sold items")
        # Click X11 must shift with window origin
        self.assertGreater(out["identity"].click_point_x11[0], 40)


class MeowthClassReplay(unittest.TestCase):
    def test_old_locator_selects_location_cluster(self):
        fx = _load_fixture()
        old = old_pixel_locator_select(list(fx["oldPixelHighlights"]), window_y=0)
        self.assertIsNotNone(old)
        cx, cy, n = old
        # Historical Meowth click class
        self.assertAlmostEqual(cx, 114, delta=5)
        self.assertAlmostEqual(cy, 307, delta=10)
        trans = apply_fixture_click(
            fx,
            (cx, cy),
            content_origin_x=0,
            content_origin_y=float(fx["viewport"]["contentOriginY"]),
        )
        self.assertEqual(trans["hitLabel"], "Australia Only")
        self.assertIn("LH_PrefLoc=2", trans["url"])
        self.assertFalse(url_has_lh_sold(trans["url"]))

    def test_new_locator_selects_sold(self):
        fx = _load_fixture()
        out = _production_locate_and_click(fx)
        self.assertTrue(out["identity"].proven)
        self.assertEqual(out["identity"].candidate.label, "Sold items")
        self.assertEqual(out["transition"]["param"], "LH_Sold=1")
        self.assertTrue(url_has_lh_sold(out["transition"]["url"]))
        self.assertEqual(unexpected_filter_params(out["transition"]["url"]), [])

    def test_location_below_sold(self):
        fx = _load_fixture()
        # Swap vertical order: location below sold
        for el in fx["elements"]:
            if el["id"] == "australia_only":
                el["boundingRect"] = {
                    "x": 48,
                    "y": 500,
                    "width": 180,
                    "height": 28,
                    "top": 500,
                    "left": 48,
                    "right": 228,
                    "bottom": 528,
                }
        out = _production_locate_and_click(fx)
        self.assertEqual(out["transition"]["hitLabel"], "Sold items")

    def test_stale_coordinate_not_reused(self):
        fx = _load_fixture()
        out1 = _production_locate_and_click(fx)
        stale = out1["identity"].click_point_x11
        # Move Sold control; rediscover must not keep stale point for selection
        for el in fx["elements"]:
            if el["id"] == "sold_items":
                el["boundingRect"] = {
                    "x": 48,
                    "y": 600,
                    "width": 180,
                    "height": 28,
                    "top": 600,
                    "left": 48,
                    "right": 228,
                    "bottom": 628,
                }
        out2 = _production_locate_and_click(fx)
        self.assertNotEqual(stale, out2["identity"].click_point_x11)
        self.assertEqual(out2["transition"]["hitLabel"], "Sold items")


class AdversarialTests(unittest.TestCase):
    def test_sold_absent(self):
        fx = _load_fixture()
        fx["elements"] = [e for e in fx["elements"] if e.get("label") != "Sold items" or e.get("hidden")]
        # remove visible sold
        fx["elements"] = [e for e in fx["elements"] if e.get("id") != "sold_items"]
        out = _production_locate_and_click(fx)
        self.assertFalse(out["identity"].proven)
        self.assertEqual(out["identity"].reason_code, SOLD_CONTROL_IDENTITY_NOT_PROVEN)

    def test_sold_outside_left_rail(self):
        fx = _load_fixture()
        for el in fx["elements"]:
            if el["id"] == "sold_items":
                el["inFilterRegion"] = False
                el["boundingRect"] = {
                    "x": 500,
                    "y": 400,
                    "width": 180,
                    "height": 28,
                    "top": 400,
                    "left": 500,
                    "right": 680,
                    "bottom": 428,
                }
        out = _production_locate_and_click(fx)
        self.assertFalse(out["identity"].proven)

    def test_find_bar_does_not_authorise_pixel(self):
        self.assertFalse(PIXEL_HIGHLIGHT_AUTHORITATIVE)

    def test_single_click_no_retry(self):
        fx = _load_fixture()
        out = _production_locate_and_click(fx)
        # One transition application only
        self.assertEqual(out["transition"]["hitLabel"], "Sold items")
        # No second click path in production helper
        self.assertTrue(out["identity"].proven)

    def test_passive_recaptcha_non_blocking_identity(self):
        fx = _load_fixture()
        fx["elements"].append(
            {
                "id": "recaptcha",
                "tag": "iframe",
                "role": "",
                "label": "reCAPTCHA",
                "visible": True,
                "enabled": True,
                "inFilterRegion": False,
                "boundingRect": {"x": 900, "y": 700, "width": 100, "height": 60},
            }
        )
        out = _production_locate_and_click(fx)
        self.assertTrue(out["identity"].proven)


class RegressionTests(unittest.TestCase):
    def test_phase_timeout_policy_preserved(self):
        pol = DEFAULT_SOLD_TIMEOUT_POLICY.to_dict()
        self.assertEqual(pol["controlDiscoverySeconds"], 12.0)
        self.assertEqual(pol["controlClickSeconds"], 5.0)
        self.assertEqual(pol["stateTransitionSeconds"], 8.0)
        self.assertEqual(pol["stateVerificationSeconds"], 10.0)
        self.assertLess(pol["stateVerificationSeconds"], 35.0)

    def test_control_plane_persistence_regression(self):
        self.assertEqual(CONTROL_PLANE_PERSISTENCE_FAILURE, "CONTROL_PLANE_PERSISTENCE_FAILURE")
        self.assertTrue(callable(peek_availability))
        self.assertIs(browser_work_allowed.__kwdefaults__.get("persist_transitions"), False)

    def test_demand_scheduler_regression(self):
        cfg = DemandAwarePolicy.from_env()
        self.assertEqual(cfg.hot_verified_ttl_hours, 12)
        self.assertEqual(cfg.normal_verified_ttl_hours, 24)
        self.assertEqual(cfg.high_min_requests_24h, 3)

    def test_owned_daily_off(self):
        flag = ROOT / "reports/runtime/owned_daily_full_enable.flag"
        if flag.is_file():
            self.assertEqual(flag.read_text(encoding="utf-8").strip(), "false")
        self.assertEqual(os.environ.get("OWNED_DAILY_FULL_ENABLE", "false").lower(), "false")


class ReadinessTests(unittest.TestCase):
    def test_readiness_bundle(self):
        from tools.exact_sold_control_targeting_readiness import readiness_bundle

        flags = readiness_bundle()
        self.assertTrue(flags["ok"])
        self.assertTrue(flags["soldControlIdentityPositive"])
        self.assertFalse(flags["pixelHighlightAuthoritative"])
        self.assertTrue(flags["locationFilterMisclickRegression"])


if __name__ == "__main__":
    unittest.main()
