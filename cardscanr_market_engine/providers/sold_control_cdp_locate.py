#!/usr/bin/env python3
"""Read-only CDP Sold-control candidate fetch (no click / no navigate)."""
from __future__ import annotations

import os
from typing import Any

from .post_sold_capture_cdp import DirectCdpSession, list_page_targets
from .sold_control_identity import (
    SOLD_CONTROL_CANDIDATES_JS,
    VIEWPORT_METRICS_JS,
    content_origin_from_viewport_metrics,
)


def default_cdp_http_base() -> str:
    port = int(os.environ.get("EBAY_BROWSER_CDP_PORT") or os.environ.get("CARDSCANR_CDP_PORT") or "9444")
    return f"http://127.0.0.1:{port}"


def fetch_sold_control_candidates_via_cdp(
    *,
    cdp_http_base: str | None = None,
    timeout: float = 4.0,
) -> dict[str, Any]:
    """Return {candidates, viewport, url, targetId, metrics, error?} — never activates controls."""
    base = (cdp_http_base or default_cdp_http_base()).rstrip("/")
    try:
        targets, _ = list_page_targets(base, timeout=min(2.0, timeout))
    except Exception as exc:
        return {"candidates": [], "viewport": {}, "url": None, "targetId": None, "error": f"cdp_list:{exc}"}
    page = None
    for t in targets:
        url = str(t.get("url") or "").lower()
        if "ebay." in url or "/sch/" in url or "local/sch" in url:
            page = t
            break
    if page is None and targets:
        page = targets[0]
    if page is None or not page.get("webSocketDebuggerUrl"):
        return {
            "candidates": [],
            "viewport": {},
            "url": None,
            "targetId": None,
            "error": "cdp_target_not_found",
        }
    session: DirectCdpSession | None = None
    try:
        session = DirectCdpSession(str(page["webSocketDebuggerUrl"]), timeout=timeout)
        blob = session.evaluate(f"({SOLD_CONTROL_CANDIDATES_JS})()", timeout=timeout)
        metrics = session.evaluate(f"({VIEWPORT_METRICS_JS})()", timeout=min(2.0, timeout))
        if not isinstance(blob, dict):
            return {
                "candidates": [],
                "viewport": {},
                "url": page.get("url"),
                "targetId": page.get("id"),
                "error": "cdp_sold_candidates_invalid",
            }
        viewport = dict(blob.get("viewport") or {})
        if isinstance(metrics, dict):
            ox, oy, dpr = content_origin_from_viewport_metrics(metrics)
            viewport.setdefault("contentOriginX", ox)
            viewport.setdefault("contentOriginY", oy)
            viewport.setdefault("devicePixelRatio", dpr)
            viewport.update({k: metrics[k] for k in metrics if k not in viewport})
        return {
            "candidates": list(blob.get("candidates") or []),
            "viewport": viewport,
            "url": blob.get("url") or page.get("url"),
            "targetId": page.get("id"),
            "metrics": metrics if isinstance(metrics, dict) else {},
            "error": None,
        }
    except Exception as exc:
        return {
            "candidates": [],
            "viewport": {},
            "url": page.get("url") if page else None,
            "targetId": page.get("id") if page else None,
            "error": f"cdp_sold_identify:{type(exc).__name__}:{exc}",
        }
    finally:
        if session is not None:
            session.close()


__all__ = ["default_cdp_http_base", "fetch_sold_control_candidates_via_cdp"]
