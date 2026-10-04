#!/usr/bin/env python3
"""Exact Sold-items control identity for X11 activation (read-only DOM/CDP).

Ctrl+F / orange-pixel highlights are NOT authoritative. Before any physical click:
  - normalized visible/accessible label must exactly equal an approved label
  - unique visible candidate with positive bounding box
  - click point derived from that element rectangle (current page only)

CDP/accessibility may identify; activation remains a single X11 mouse click.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

APPROVED_SOLD_LABELS = frozenset({"sold items"})
APPROVED_SOLD_ROLES = frozenset({"checkbox", "button", "link", "menuitemcheckbox", ""})

SOLD_CONTROL_IDENTITY_NOT_PROVEN = "SOLD_CONTROL_IDENTITY_NOT_PROVEN"
SOLD_CONTROL_AMBIGUOUS = "SOLD_CONTROL_AMBIGUOUS"
SOLD_CONTROL_INVALID_BOUNDS = "SOLD_CONTROL_INVALID_BOUNDS"
SOLD_CONTROL_OFFSCREEN = "SOLD_CONTROL_OFFSCREEN"
SOLD_CONTROL_NOT_VISIBLE = "SOLD_CONTROL_NOT_VISIBLE"
SOLD_CONTROL_DISABLED = "SOLD_CONTROL_DISABLED"
SOLD_CONTROL_OUTSIDE_FILTER_REGION = "SOLD_CONTROL_OUTSIDE_FILTER_REGION"
PIXEL_HIGHLIGHT_AUTHORITATIVE = False  # contract: never true in production

# Left/filter rail heuristics (viewport CSS px).
FILTER_RAIL_X_MAX_VIEWPORT = 360.0
FILTER_RAIL_Y_MIN_VIEWPORT = 80.0

# JS run via read-only CDP Runtime.evaluate — never clicks.
SOLD_CONTROL_CANDIDATES_JS = r"""
() => {
  const norm = (v) => (v || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const approved = new Set(['sold items']);
  const out = [];
  const push = (el, labelSource) => {
    if (!el || !(el instanceof Element)) return;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    const aria = el.getAttribute('aria-label') || '';
    const text = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
    const labelledBy = el.getAttribute('aria-labelledby');
    let labelled = '';
    if (labelledBy) {
      labelled = labelledBy.split(/\s+/).map((id) => {
        const n = document.getElementById(id);
        return n ? (n.innerText || n.textContent || '') : '';
      }).join(' ').replace(/\s+/g, ' ').trim();
    }
    const label = text || aria || labelled;
    const nlabel = norm(label);
    if (!approved.has(nlabel)) return;
    const role = (el.getAttribute('role') || el.getAttribute('type') || el.tagName || '').toLowerCase();
    const inFilter = !!(el.closest(
      'aside, nav, [class*="filter"], [class*="refinement"], [id*="filter"], [data-testid*="filter"], form'
    ));
    out.push({
      tag: (el.tagName || '').toLowerCase(),
      role,
      label,
      labelNormalized: nlabel,
      labelSource,
      boundingRect: {
        x: rect.x, y: rect.y, width: rect.width, height: rect.height,
        top: rect.top, left: rect.left, right: rect.right, bottom: rect.bottom
      },
      visible: style.visibility !== 'hidden' && style.display !== 'none'
        && rect.width > 0 && rect.height > 0
        && parseFloat(style.opacity || '1') > 0.01,
      enabled: !(el.disabled === true || el.getAttribute('aria-disabled') === 'true'),
      checked: !!(el.checked || el.getAttribute('aria-checked') === 'true'),
      inFilterRegion: inFilter || (rect.left >= 0 && rect.left <= 360),
      viewportIntersection: {
        intersects: rect.bottom > 0 && rect.right > 0
          && rect.top < window.innerHeight && rect.left < window.innerWidth,
        area: Math.max(0, Math.min(rect.bottom, window.innerHeight) - Math.max(rect.top, 0))
          * Math.max(0, Math.min(rect.right, window.innerWidth) - Math.max(rect.left, 0))
      },
      href: el.href || el.getAttribute('href') || null,
      id: el.id || null,
      className: typeof el.className === 'string' ? el.className.slice(0, 120) : '',
    });
  };
  const roots = Array.from(document.querySelectorAll(
    'a, button, label, input[type="checkbox"], input[type="radio"], [role="checkbox"], [role="button"], span, div, li'
  ));
  for (const el of roots) {
    const aria = el.getAttribute('aria-label') || '';
    const text = (el.innerText || '').replace(/\s+/g, ' ').trim();
    if (approved.has(norm(text))) push(el, 'innerText');
    else if (approved.has(norm(aria))) push(el, 'aria-label');
  }
  // Prefer interactive ancestors for pure text nodes wrapped in label/a
  const dedup = [];
  const seen = new Set();
  for (const c of out) {
    const key = [c.labelNormalized, c.boundingRect.x, c.boundingRect.y, c.boundingRect.width, c.boundingRect.height].join('|');
    if (seen.has(key)) continue;
    seen.add(key);
    dedup.push(c);
  }
  return {
    candidates: dedup,
    viewport: {
      innerWidth: window.innerWidth,
      innerHeight: window.innerHeight,
      scrollX: window.scrollX,
      scrollY: window.scrollY,
      devicePixelRatio: window.devicePixelRatio || 1,
      outerWidth: window.outerWidth,
      outerHeight: window.outerHeight,
      screenX: window.screenX,
      screenY: window.screenY,
    },
    url: location.href,
  };
}
"""

VIEWPORT_METRICS_JS = r"""
() => ({
  innerWidth: window.innerWidth,
  innerHeight: window.innerHeight,
  outerWidth: window.outerWidth,
  outerHeight: window.outerHeight,
  screenX: window.screenX,
  screenY: window.screenY,
  scrollX: window.scrollX,
  scrollY: window.scrollY,
  devicePixelRatio: window.devicePixelRatio || 1,
  contentOriginX: 0,
  contentOriginY: Math.max(0, (window.outerHeight || 0) - (window.innerHeight || 0)),
})
"""


def normalize_label(text: str | None) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def is_exact_sold_label(text: str | None) -> bool:
    return normalize_label(text) in APPROVED_SOLD_LABELS


@dataclass
class SoldControlCandidate:
    tag: str
    role: str
    label: str
    bounding_rect: dict[str, float]
    visible: bool = True
    enabled: bool = True
    in_filter_region: bool = True
    viewport_intersection: dict[str, Any] = field(default_factory=dict)
    label_source: str = "innerText"
    href: str | None = None
    checked: bool = False
    id: str | None = None
    class_name: str = ""
    hidden: bool = False

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SoldControlCandidate":
        rect = raw.get("boundingRect") or raw.get("bounding_rect") or {}
        return cls(
            tag=str(raw.get("tag") or ""),
            role=str(raw.get("role") or ""),
            label=str(raw.get("label") or ""),
            bounding_rect={
                "x": float(rect.get("x", rect.get("left", 0)) or 0),
                "y": float(rect.get("y", rect.get("top", 0)) or 0),
                "width": float(rect.get("width") or 0),
                "height": float(rect.get("height") or 0),
                "top": float(rect.get("top", rect.get("y", 0)) or 0),
                "left": float(rect.get("left", rect.get("x", 0)) or 0),
                "right": float(rect.get("right") or 0)
                or float(rect.get("left", rect.get("x", 0)) or 0) + float(rect.get("width") or 0),
                "bottom": float(rect.get("bottom") or 0)
                or float(rect.get("top", rect.get("y", 0)) or 0) + float(rect.get("height") or 0),
            },
            visible=bool(raw.get("visible", True)) and not bool(raw.get("hidden")),
            enabled=bool(raw.get("enabled", True)),
            in_filter_region=bool(raw.get("inFilterRegion", raw.get("in_filter_region", True))),
            viewport_intersection=dict(raw.get("viewportIntersection") or raw.get("viewport_intersection") or {}),
            label_source=str(raw.get("labelSource") or raw.get("label_source") or "innerText"),
            href=raw.get("href"),
            checked=bool(raw.get("checked")),
            id=raw.get("id"),
            class_name=str(raw.get("className") or raw.get("class_name") or ""),
            hidden=bool(raw.get("hidden")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "role": self.role,
            "label": self.label,
            "labelNormalized": normalize_label(self.label),
            "labelSource": self.label_source,
            "boundingRect": dict(self.bounding_rect),
            "visible": self.visible,
            "enabled": self.enabled,
            "inFilterRegion": self.in_filter_region,
            "viewportIntersection": dict(self.viewport_intersection),
            "href": self.href,
            "checked": self.checked,
            "id": self.id,
            "className": self.class_name,
            "hidden": self.hidden,
        }


@dataclass
class SoldControlIdentity:
    proven: bool
    reason_code: str | None
    candidate: SoldControlCandidate | None
    click_point_viewport: tuple[float, float] | None
    click_point_x11: tuple[int, int] | None
    candidates_considered: int
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "soldControlIdentityProven": self.proven,
            "reasonCode": self.reason_code,
            "intendedControlText": None if not self.candidate else self.candidate.label,
            "intendedControlRole": None if not self.candidate else self.candidate.role,
            "intendedBoundingRect": None if not self.candidate else self.candidate.bounding_rect,
            "chosenClickPointViewport": list(self.click_point_viewport) if self.click_point_viewport else None,
            "chosenClickPoint": list(self.click_point_x11) if self.click_point_x11 else None,
            "candidatesConsidered": self.candidates_considered,
            "pixelHighlightAuthoritative": PIXEL_HIGHLIGHT_AUTHORITATIVE,
            "exactVisibleControlRequired": True,
            "diagnostics": self.diagnostics,
            "candidate": None if not self.candidate else self.candidate.to_dict(),
        }


def validate_bounding_rect(
    rect: dict[str, float],
    *,
    viewport: dict[str, Any] | None = None,
    require_filter_region: bool = True,
) -> tuple[bool, str | None]:
    w = float(rect.get("width") or 0)
    h = float(rect.get("height") or 0)
    x = float(rect.get("x", rect.get("left", 0)) or 0)
    y = float(rect.get("y", rect.get("top", 0)) or 0)
    if w <= 0 or h <= 0:
        return False, SOLD_CONTROL_INVALID_BOUNDS
    vp = viewport or {}
    iw = float(vp.get("innerWidth") or 1e9)
    ih = float(vp.get("innerHeight") or 1e9)
    if x + w <= 0 or y + h <= 0 or x >= iw or y >= ih:
        return False, SOLD_CONTROL_OFFSCREEN
    # Must have meaningful intersection with viewport
    inter_w = max(0.0, min(x + w, iw) - max(x, 0.0))
    inter_h = max(0.0, min(y + h, ih) - max(y, 0.0))
    if inter_w * inter_h <= 0:
        return False, SOLD_CONTROL_OFFSCREEN
    if require_filter_region:
        cx = x + w / 2.0
        cy = y + h / 2.0
        if cx > FILTER_RAIL_X_MAX_VIEWPORT or cy < FILTER_RAIL_Y_MIN_VIEWPORT:
            return False, SOLD_CONTROL_OUTSIDE_FILTER_REGION
    return True, None


def click_point_from_rect(rect: dict[str, float]) -> tuple[float, float]:
    x = float(rect.get("x", rect.get("left", 0)) or 0)
    y = float(rect.get("y", rect.get("top", 0)) or 0)
    w = float(rect.get("width") or 0)
    h = float(rect.get("height") or 0)
    return (x + w / 2.0, y + h / 2.0)


def viewport_to_x11(
    vx: float,
    vy: float,
    *,
    window_x: int,
    window_y: int,
    content_origin_x: float = 0.0,
    content_origin_y: float = 0.0,
    device_scale_factor: float = 1.0,
) -> tuple[int, int]:
    """Map CSS viewport coordinates to X11 screen coordinates.

    Does not assume raw DOM x/y equals screen x/y.
    screen = window_origin + browser_chrome_offset + viewport * dpr
    """
    dpr = float(device_scale_factor) if device_scale_factor else 1.0
    sx = int(round(window_x + content_origin_x + vx * dpr))
    sy = int(round(window_y + content_origin_y + vy * dpr))
    return sx, sy


def content_origin_from_viewport_metrics(metrics: dict[str, Any] | None) -> tuple[float, float, float]:
    m = metrics or {}
    if "contentOriginX" in m or "contentOriginY" in m:
        return (
            float(m.get("contentOriginX") or 0),
            float(m.get("contentOriginY") or 0),
            float(m.get("devicePixelRatio") or 1),
        )
    outer_h = float(m.get("outerHeight") or 0)
    inner_h = float(m.get("innerHeight") or 0)
    outer_w = float(m.get("outerWidth") or 0)
    inner_w = float(m.get("innerWidth") or 0)
    origin_y = max(0.0, outer_h - inner_h) if outer_h and inner_h else 0.0
    origin_x = max(0.0, outer_w - inner_w) if outer_w and inner_w else 0.0
    # Prefer left-docked vertical chrome = 0; vertical toolbar difference is primary.
    return origin_x * 0.0, origin_y, float(m.get("devicePixelRatio") or 1)


def select_unique_sold_control(
    raw_candidates: list[dict[str, Any]] | list[SoldControlCandidate],
    *,
    viewport: dict[str, Any] | None = None,
) -> SoldControlIdentity:
    candidates: list[SoldControlCandidate] = []
    for raw in raw_candidates:
        c = raw if isinstance(raw, SoldControlCandidate) else SoldControlCandidate.from_dict(raw)
        if not is_exact_sold_label(c.label):
            continue
        if c.hidden or not c.visible:
            continue
        if not c.enabled:
            continue
        ok, reason = validate_bounding_rect(
            c.bounding_rect, viewport=viewport, require_filter_region=c.in_filter_region or True
        )
        if not ok:
            # Keep reason for diagnostics; skip candidate
            c.viewport_intersection = {**(c.viewport_intersection or {}), "rejectReason": reason}
            continue
        # Exact label already enforced; role soft-preference only
        candidates.append(c)

    if not candidates:
        return SoldControlIdentity(
            proven=False,
            reason_code=SOLD_CONTROL_IDENTITY_NOT_PROVEN,
            candidate=None,
            click_point_viewport=None,
            click_point_x11=None,
            candidates_considered=len(raw_candidates),
            diagnostics={"rejectedOrAbsent": True},
        )

    # Prefer interactive roles / filter-region / larger intersection area
    def rank(c: SoldControlCandidate) -> tuple:
        role = normalize_label(c.role)
        interactive = 1 if role in {"checkbox", "button", "link", "menuitemcheckbox"} or c.tag in {
            "a",
            "button",
            "label",
            "input",
        } else 0
        area = float((c.viewport_intersection or {}).get("area") or 0)
        if area <= 0:
            r = c.bounding_rect
            area = float(r.get("width") or 0) * float(r.get("height") or 0)
        return (interactive, 1 if c.in_filter_region else 0, area)

    candidates.sort(key=rank, reverse=True)
    best = candidates[0]
    # Ambiguity: another visible exact match with comparable rank in filter region
    peers = [
        c
        for c in candidates[1:]
        if c.in_filter_region
        and abs(
            (best.bounding_rect.get("y") or 0) - (c.bounding_rect.get("y") or 0)
        )
        > 1
    ]
    if len(peers) >= 1 and rank(peers[0])[0] == rank(best)[0] and rank(peers[0])[1] == rank(best)[1]:
        # Two distinct visible Sold labels — do not guess
        return SoldControlIdentity(
            proven=False,
            reason_code=SOLD_CONTROL_AMBIGUOUS,
            candidate=None,
            click_point_viewport=None,
            click_point_x11=None,
            candidates_considered=len(raw_candidates),
            diagnostics={
                "ambiguousCount": 1 + len(peers),
                "labels": [best.label] + [p.label for p in peers],
            },
        )

    vp_click = click_point_from_rect(best.bounding_rect)
    return SoldControlIdentity(
        proven=True,
        reason_code=None,
        candidate=best,
        click_point_viewport=vp_click,
        click_point_x11=None,
        candidates_considered=len(raw_candidates),
        diagnostics={"selectedRank": list(rank(best))},
    )


def prove_sold_control_identity(
    raw_candidates: list[dict[str, Any]] | list[SoldControlCandidate],
    *,
    viewport: dict[str, Any] | None = None,
    window_x: int = 0,
    window_y: int = 0,
    content_origin_x: float = 0.0,
    content_origin_y: float = 0.0,
    device_scale_factor: float = 1.0,
    page_url: str | None = None,
    runtime_mode: str | None = None,
    attempt_id: str | None = None,
    job_id: str | None = None,
    price_key_id: str | None = None,
    target_id: str | None = None,
) -> SoldControlIdentity:
    """Authoritative pre-click identity. No click if proven is False."""
    identity = select_unique_sold_control(raw_candidates, viewport=viewport)
    if not identity.proven or identity.candidate is None or identity.click_point_viewport is None:
        identity.diagnostics.update(
            {
                "pageUrl": page_url,
                "runtimeMode": runtime_mode,
                "attemptId": attempt_id,
                "jobId": job_id,
                "priceKeyId": price_key_id,
                "targetId": target_id,
            }
        )
        return identity
    vx, vy = identity.click_point_viewport
    sx, sy = viewport_to_x11(
        vx,
        vy,
        window_x=window_x,
        window_y=window_y,
        content_origin_x=content_origin_x,
        content_origin_y=content_origin_y,
        device_scale_factor=device_scale_factor,
    )
    # Click point must lie inside element rect in viewport space
    r = identity.candidate.bounding_rect
    inside = (
        float(r["x"]) <= vx <= float(r["x"]) + float(r["width"])
        and float(r["y"]) <= vy <= float(r["y"]) + float(r["height"])
    )
    if not inside:
        return SoldControlIdentity(
            proven=False,
            reason_code=SOLD_CONTROL_INVALID_BOUNDS,
            candidate=identity.candidate,
            click_point_viewport=identity.click_point_viewport,
            click_point_x11=None,
            candidates_considered=identity.candidates_considered,
            diagnostics={"clickOutsideRect": True},
        )
    identity.click_point_x11 = (sx, sy)
    identity.diagnostics.update(
        {
            "pageUrl": page_url,
            "runtimeMode": runtime_mode,
            "attemptId": attempt_id,
            "jobId": job_id,
            "priceKeyId": price_key_id,
            "targetId": target_id,
            "coordinateTransform": {
                "viewport": [vx, vy],
                "window": [window_x, window_y],
                "contentOrigin": [content_origin_x, content_origin_y],
                "deviceScaleFactor": device_scale_factor,
                "x11": [sx, sy],
            },
        }
    )
    return identity


def unexpected_filter_params(url: str | None) -> list[str]:
    from urllib.parse import urlparse, parse_qs

    q = parse_qs(urlparse(str(url or "")).query)
    bad: list[str] = []
    for key, vals in q.items():
        lk = key.lower()
        if lk == "lh_sold" and any(str(v) == "1" for v in vals):
            continue
        if lk.startswith("lh_"):
            bad.append(f"{key}={','.join(str(v) for v in vals)}")
    return bad


def build_click_target_diagnostics(
    identity: SoldControlIdentity,
    *,
    pre_click_url: str | None,
    post_click_url: str | None,
) -> dict[str, Any]:
    from .sold_navigation_phases import url_has_lh_sold

    return {
        "intendedControlText": identity.to_dict().get("intendedControlText"),
        "intendedControlRole": identity.to_dict().get("intendedControlRole"),
        "intendedBoundingRect": identity.to_dict().get("intendedBoundingRect"),
        "clickPoint": identity.to_dict().get("chosenClickPoint"),
        "preClickUrl": pre_click_url,
        "postClickUrl": post_click_url,
        "lhSoldBefore": url_has_lh_sold(pre_click_url),
        "lhSoldAfter": url_has_lh_sold(post_click_url),
        "unexpectedFilterParams": unexpected_filter_params(post_click_url)
        if not url_has_lh_sold(post_click_url)
        else [],
        "soldControlIdentityProven": identity.proven,
    }


def parse_fixture_candidates(fixture: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Offline fixture → same candidate shape as CDP JS."""
    viewport = dict(fixture.get("viewport") or {})
    raw = []
    for el in fixture.get("elements") or []:
        if not isinstance(el, dict):
            continue
        raw.append(el)
    return raw, viewport


# --- Old pixel locator (for regression comparison only; not production authority) ---


def old_pixel_locator_select(
    highlights: list[tuple[int, int]],
    *,
    window_x: int = 0,
    window_y: int = 0,
    left_rail_x_min: int = 55,
    left_rail_x_max: int = 340,
) -> tuple[int, int, int] | None:
    """Reproduce Ctrl+F orange-cluster selection weakness for Meowth-class fixtures."""
    hits = [
        (x, y)
        for x, y in highlights
        if left_rail_x_min <= x <= left_rail_x_max and (window_y + 180) <= y <= (window_y + 720)
    ]
    if not hits:
        return None
    buckets: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for x, y in hits:
        buckets.setdefault((x // 25, y // 12), []).append((x, y))
    best = max(buckets.values(), key=len)
    cx = sum(p[0] for p in best) // len(best)
    cy = sum(p[1] for p in best) // len(best)
    return cx, cy, len(best)


def apply_fixture_click(
    fixture: dict[str, Any],
    click_x11: tuple[int, int],
    *,
    window_x: int = 0,
    window_y: int = 0,
    content_origin_x: float = 0.0,
    content_origin_y: float = 0.0,
    device_scale_factor: float = 1.0,
) -> dict[str, Any]:
    """Map an X11 click onto fixture elements; return local URL transition (no network)."""
    dpr = float(device_scale_factor) or 1.0
    vx = (click_x11[0] - window_x - content_origin_x) / dpr
    vy = (click_x11[1] - window_y - content_origin_y) / dpr
    hit = None
    for el in fixture.get("elements") or []:
        rect = el.get("boundingRect") or {}
        x, y = float(rect.get("x") or 0), float(rect.get("y") or 0)
        w, h = float(rect.get("width") or 0), float(rect.get("height") or 0)
        if x <= vx <= x + w and y <= vy <= y + h and el.get("visible", True) and not el.get("hidden"):
            # Prefer smallest containing element
            area = w * h
            if hit is None or area < hit[0]:
                hit = (area, el)
    base = str(fixture.get("baseUrl") or "http://127.0.0.1/local/sch")
    if hit is None:
        return {"ok": False, "url": base, "hitLabel": None, "param": None}
    el = hit[1]
    param = el.get("localParam") or ""
    sep = "&" if "?" in base else "?"
    url = f"{base}{sep}{param}" if param else base
    return {
        "ok": True,
        "url": url,
        "hitLabel": el.get("label"),
        "param": param,
        "elementId": el.get("id"),
    }


__all__ = [
    "APPROVED_SOLD_LABELS",
    "PIXEL_HIGHLIGHT_AUTHORITATIVE",
    "SOLD_CONTROL_AMBIGUOUS",
    "SOLD_CONTROL_IDENTITY_NOT_PROVEN",
    "SOLD_CONTROL_CANDIDATES_JS",
    "VIEWPORT_METRICS_JS",
    "SoldControlCandidate",
    "SoldControlIdentity",
    "apply_fixture_click",
    "build_click_target_diagnostics",
    "click_point_from_rect",
    "content_origin_from_viewport_metrics",
    "is_exact_sold_label",
    "normalize_label",
    "old_pixel_locator_select",
    "parse_fixture_candidates",
    "prove_sold_control_identity",
    "select_unique_sold_control",
    "unexpected_filter_params",
    "validate_bounding_rect",
    "viewport_to_x11",
]
