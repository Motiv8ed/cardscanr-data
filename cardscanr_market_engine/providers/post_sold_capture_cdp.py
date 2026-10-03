"""Read-only direct CDP client for post-Sold capture (no Playwright, no navigation).

Uses HTTP /json/* with hard timeouts and a short-timeout WebSocket session.
Never calls Page.navigate / reload / Input / Emulation click helpers.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

# Candidate extraction script — mirrors collect_candidate_dicts in ebay_browser_provider.
COLLECT_CANDIDATES_JS = """
({ maxResults }) => {
  const norm = (value) => (value || '').replace(/\\s+/g, ' ').trim();
  const blockText = (value) => (value || '').replace(/\\r/g, '').trim();
  const chromeExact = new Set([
    'buy it now', 'best offer', 'best offer accepted', 'or', 'add to cart',
    'shop now', 'watch', 'watching', 'bids', '1 bid', '2 bids', '3 bids',
    'sponsored', 'pre-owned', 'pre owned', 'brand new', 'new listing',
    'see all', 'more options', 'make offer', 'shop on ebay'
  ]);
  const isChrome = (value) => {
    const t = norm(value).toLowerCase();
    if (!t) return true;
    if (chromeExact.has(t)) return true;
    if (t.length <= 3 && !/\\d/.test(t)) return true;
    return false;
  };
  const textOf = (root, selectors) => {
    for (const selector of selectors) {
      const node = root.querySelector(selector);
      if (!node) continue;
      const heading = node.querySelector('[role="heading"]') || node;
      let text = norm(heading.innerText || heading.textContent);
      text = text.replace(/^(?:new listing)\\s+/i, '').trim();
      if (text && !isChrome(text)) return text;
    }
    return '';
  };
  const hrefOf = (root) => {
    const link = root.matches && root.matches('a[href*="/itm/"]') ? root : root.querySelector('a[href*="/itm/"]');
    if (!link) return { href: '', anchorText: '', titleSource: '' };
    const aria = norm(link.getAttribute('aria-label') || '');
    const titleAttr = norm(link.getAttribute('title') || '');
    let anchorText = '';
    let titleSource = '';
    for (const [candidate, source] of [[aria, 'aria-label'], [titleAttr, 'title-attr']]) {
      if (candidate && !isChrome(candidate) && candidate.length >= 12) {
        anchorText = candidate;
        titleSource = source;
        break;
      }
    }
    return { href: link.href || '', anchorText, titleSource };
  };
  const usefulParent = (anchor) => {
    const selectors = ['li.s-item', '.s-item', '.srp-results li', '[data-view]'];
    for (const selector of selectors) {
      const node = anchor.closest(selector);
      if (node) return node;
    }
    return anchor.parentElement || anchor;
  };
  const seen = new Set();
  const out = [];
  const add = (node, source) => {
    if (!node || out.length >= maxResults) return;
    const link = hrefOf(node);
    if (!link.href || !link.href.includes('/itm/')) return;
    const key = link.href.split('?')[0];
    if (seen.has(key)) return;
    seen.add(key);
    const structuredTitle = textOf(node, [
      'h3.s-item__title [role="heading"]',
      'h3.s-item__title',
      '.s-item__title [role="heading"]',
      '.s-item__title span[role="heading"]',
      '.s-item__title span',
      '.s-item__title',
      '[class*="s-card__title"] [role="heading"]',
      '[class*="s-card__title"]',
      'div[role="heading"]',
      'h3[role="heading"]',
      '.su-card-container__header [role="heading"]',
    ]);
    const text = blockText(node.innerText);
    out.push({
      source,
      href: link.href,
      anchorText: link.anchorText,
      title: structuredTitle,
      titleSource: structuredTitle ? 's-item__title' : (link.titleSource || ''),
      priceText: textOf(node, ['.s-item__price', '.s-item__detail--primary']),
      shippingText: textOf(node, ['.s-item__shipping', '.s-item__logisticsCost']),
      soldDateText: textOf(node, ['.s-item__title--tagblock .POSITIVE', '.s-item__caption--row', '.s-item__caption']),
      conditionText: textOf(node, ['.SECONDARY_INFO', '.s-item__subtitle']),
      itemLocationText: textOf(node, ['.s-item__location', '.s-item__itemLocation', '.s-item__seller-info-text']),
      text
    });
  };
  for (const selector of ['li.s-item', '.s-item', '.srp-results li']) {
    document.querySelectorAll(selector).forEach((node) => add(node, selector));
  }
  document.querySelectorAll('a[href*="/itm/"]').forEach((anchor) => add(usefulParent(anchor), 'a[href*="/itm/"]'));
  return out.slice(0, maxResults);
}
"""

READINESS_JS = """
() => {
  const body = document.body;
  const de = document.documentElement;
  const listing = document.querySelectorAll(
    'li.s-item, .s-item, .srp-results li, [class*="s-card"]'
  ).length;
  return {
    readyState: document.readyState || '',
    documentElementPresent: !!de,
    bodyPresent: !!body,
    bodyInnerTextLength: body && body.innerText ? body.innerText.length : 0,
    outerHTMLLength: de && de.outerHTML ? de.outerHTML.length : 0,
    frameUrl: String(location.href || ''),
    listingNodeCount: listing,
  };
}
"""

ITM_HREF_COUNT_JS = """
() => {
  const hrefs = Array.from(document.querySelectorAll('a[href*="/itm/"]'))
    .map(a => a.href || '')
    .filter(h => /\\/itm\\//i.test(h));
  const ids = [];
  const re = /\\/itm\\/(?:[^/?#]+\\/)?([0-9]+)/i;
  for (const h of hrefs) {
    const m = h.match(re);
    if (m) ids.push(m[1]);
  }
  return {
    hrefCount: hrefs.length,
    uniqueItemIds: Array.from(new Set(ids)),
  };
}
"""

# Read-only challenge UI probe — never clicks/solves CAPTCHA; visibility only.
CHALLENGE_UI_JS = """
() => {
  const bodyText = (document.body && document.body.innerText) || '';
  const visibleChallengeText = /verify you are|verify yourself|are you a robot|security challenge|robot check|confirm you are human|please verify yourself|press and hold/i.test(bodyText);
  const frames = Array.from(document.querySelectorAll('iframe')).map((f) => {
    let display = '';
    let visibility = '';
    try {
      const cs = window.getComputedStyle(f);
      display = cs.display || '';
      visibility = cs.visibility || '';
    } catch (e) {
      display = f.style && f.style.display || '';
      visibility = f.style && f.style.visibility || '';
    }
    const src = String(f.src || '');
    const id = String(f.id || '');
    const cls = String(f.className || '');
    const captchaLike = /captcha|recaptcha|challenge/i.test(src + ' ' + id + ' ' + cls);
    const w = Number(f.offsetWidth || 0);
    const h = Number(f.offsetHeight || 0);
    const renderedVisible = captchaLike && w > 2 && h > 2 && display !== 'none' && visibility !== 'hidden';
    return { src: src.slice(0, 180), w, h, display, visibility, captchaLike, renderedVisible };
  });
  const captchaFrames = frames.filter((f) => f.captchaLike).slice(0, 12);
  const visibleCaptchaFrames = captchaFrames.filter((f) => f.renderedVisible);
  const listingNodeCount = document.querySelectorAll('li.s-item, .s-item, .srp-results li, a[href*="/itm/"]').length;
  return {
    visibleChallengeText,
    visibleCaptchaFrameCount: visibleCaptchaFrames.length,
    visibleChallengeWidget: visibleCaptchaFrames.length > 0,
    captchaFrameCandidates: captchaFrames,
    listingNodeCount,
    frameUrl: String(location.href || ''),
    title: String(document.title || ''),
    readyState: String(document.readyState || ''),
    bodyTextChars: bodyText.length,
  };
}
"""


def http_json(url: str, *, timeout: float = 2.0) -> tuple[Any, float]:
    """GET JSON with a hard socket timeout. Returns (payload, elapsed_ms)."""
    started = time.monotonic()
    req = Request(url, headers={"Accept": "application/json"})
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310 — localhost CDP only
        raw = resp.read()
    elapsed_ms = (time.monotonic() - started) * 1000.0
    return json.loads(raw.decode("utf-8", errors="replace")), elapsed_ms


def list_page_targets(cdp_http_base: str, *, timeout: float = 2.0) -> tuple[list[dict[str, Any]], float]:
    base = cdp_http_base.rstrip("/")
    payload, elapsed_ms = http_json(f"{base}/json/list", timeout=timeout)
    if not isinstance(payload, list):
        return [], elapsed_ms
    out: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "") != "page":
            continue
        out.append(
            {
                "id": item.get("id"),
                "type": "page",
                "url": item.get("url") or "",
                "title": item.get("title") or "",
                "webSocketDebuggerUrl": item.get("webSocketDebuggerUrl") or "",
            }
        )
    return out, elapsed_ms


def version_info(cdp_http_base: str, *, timeout: float = 2.0) -> tuple[dict[str, Any], float]:
    base = cdp_http_base.rstrip("/")
    payload, elapsed_ms = http_json(f"{base}/json/version", timeout=timeout)
    return payload if isinstance(payload, dict) else {}, elapsed_ms


class DirectCdpSession:
    """Minimal CDP session over websocket-client with per-call timeouts."""

    def __init__(self, ws_url: str, *, timeout: float = 5.0) -> None:
        try:
            import websocket  # type: ignore
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(f"websocket_client_unavailable:{exc}") from exc
        self._timeout = float(timeout)
        # Chrome DevTools rejects WS without an allowed Origin. Prefer the CDP HTTP
        # origin; fall back to suppressing Origin for older / relay setups.
        origin = "http://127.0.0.1"
        try:
            from urllib.parse import urlparse

            parsed = urlparse(ws_url)
            if parsed.hostname and parsed.port:
                origin = f"http://{parsed.hostname}:{parsed.port}"
            elif parsed.hostname:
                origin = f"http://{parsed.hostname}"
        except Exception:
            pass
        try:
            self._ws = websocket.create_connection(
                ws_url,
                timeout=self._timeout,
                enable_multithread=False,
                header=[f"Origin: {origin}"],
            )
        except Exception:
            self._ws = websocket.create_connection(
                ws_url,
                timeout=self._timeout,
                enable_multithread=False,
                suppress_origin=True,
            )
        self._ws.settimeout(self._timeout)
        self._next_id = 1

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass

    def call(self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None) -> Any:
        msg_id = self._next_id
        self._next_id += 1
        payload = {"id": msg_id, "method": method, "params": params or {}}
        self._ws.settimeout(float(timeout if timeout is not None else self._timeout))
        self._ws.send(json.dumps(payload))
        deadline = time.monotonic() + float(timeout if timeout is not None else self._timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"cdp_call_timeout:{method}")
            self._ws.settimeout(max(0.05, remaining))
            raw = self._ws.recv()
            data = json.loads(raw)
            if data.get("id") == msg_id:
                if "error" in data:
                    raise RuntimeError(f"cdp_error:{method}:{data['error']}")
                return data.get("result")

    def evaluate(self, expression: str, *, timeout: float | None = None, await_promise: bool = False) -> Any:
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": bool(await_promise),
            },
            timeout=timeout,
        )
        if not isinstance(result, dict):
            return None
        if result.get("exceptionDetails"):
            detail = result.get("exceptionDetails") or {}
            raise RuntimeError(f"cdp_evaluate_exception:{detail}")
        value = (result.get("result") or {}).get("value")
        return value

    def evaluate_function(self, js_function_source: str, arg: Any = None, *, timeout: float | None = None) -> Any:
        # Wrap function source as IIFE with JSON arg.
        expr = f"({js_function_source})({json.dumps(arg)})"
        return self.evaluate(expr, timeout=timeout)


_ITM_RE = re.compile(r"/itm/(?:[^/?#]+/)?([0-9]+)", re.IGNORECASE)


def count_itm_hrefs(html_or_text: str) -> dict[str, Any]:
    hrefs = re.findall(r'https?://[^"\'\s>]+/itm/[^"\'\s>]+', html_or_text or "", flags=re.IGNORECASE)
    ids: list[str] = []
    for h in hrefs:
        m = _ITM_RE.search(h)
        if m:
            ids.append(m.group(1))
    # Also catch relative /itm/
    for m in _ITM_RE.finditer(html_or_text or ""):
        ids.append(m.group(1))
    unique = sorted(set(ids))
    return {"hrefCount": len(hrefs), "uniqueItemIds": unique, "uniqueItemIdCount": len(unique)}


def probe_endpoint(cdp_http_base: str, *, timeout: float = 2.0) -> dict[str, Any]:
    """Local read-only diagnostics — no page navigation."""
    out: dict[str, Any] = {"cdpHttpBase": cdp_http_base, "ok": False}
    try:
        ver, ver_ms = version_info(cdp_http_base, timeout=timeout)
        out["versionLatencyMs"] = round(ver_ms, 3)
        out["version"] = {
            "Browser": ver.get("Browser"),
            "Protocol-Version": ver.get("Protocol-Version"),
            "webSocketDebuggerUrl": bool(ver.get("webSocketDebuggerUrl")),
        }
    except Exception as exc:
        out["versionError"] = f"{type(exc).__name__}:{exc}"
        return out
    try:
        targets, list_ms = list_page_targets(cdp_http_base, timeout=timeout)
        out["listLatencyMs"] = round(list_ms, 3)
        out["pageTargetCount"] = len(targets)
        out["pageTargets"] = [
            {
                "id": t.get("id"),
                "url": t.get("url"),
                "title": t.get("title"),
                "hasWs": bool(t.get("webSocketDebuggerUrl")),
                "lhSold": "lh_sold=1" in str(t.get("url") or "").lower(),
            }
            for t in targets
        ]
        out["ok"] = True
    except Exception as exc:
        out["listError"] = f"{type(exc).__name__}:{exc}"
    return out


__all__ = [
    "COLLECT_CANDIDATES_JS",
    "READINESS_JS",
    "CHALLENGE_UI_JS",
    "ITM_HREF_COUNT_JS",
    "DirectCdpSession",
    "count_itm_hrefs",
    "http_json",
    "list_page_targets",
    "probe_endpoint",
    "version_info",
]
