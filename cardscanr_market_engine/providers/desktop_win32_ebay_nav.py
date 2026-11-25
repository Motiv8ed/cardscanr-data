"""Windows rendered-UI navigation on the verified dedicated pricing browser."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import os
from pathlib import Path
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit
from websocket import WebSocketTimeoutException

from ..ebay_browser_work_gate import evaluate_ebay_browser_work_gate
from ..live_navigation_attempt import emit_search_submission_started, has_search_submission_started
from ..marketplace_gate_url import is_marketplace_gate_page
from ..native_windows_pricing_browser import (
    PORT as DEFAULT_CDP_PORT, browser_lock, cdp_json, ensure_browser,
    listener_owner, valid_socket,
)
from ..navigation_runtime_context import PRE_SUBMIT_QUERY_READY, pre_submit_only_requested
from .post_sold_capture_cdp import DirectCdpSession

_HOME = "https://www.ebay.com.au/"
_ORIGIN = "https://www.ebay.com.au"
_CONTROLS = r"""
    const visible = e => e.getClientRects().length && getComputedStyle(e).visibility === 'visible';
    const label = e => (e.getAttribute('aria-label') || e.innerText || e.value || e.placeholder || '').trim();
    const fields = [...document.querySelectorAll('input[name="_nkw"]')]
        .filter(e => !e.disabled && !e.readOnly && visible(e));
    const field = fields.length === 1 ? fields[0] : null;
    const buttons = [...(field?.form?.elements || [])].filter(e =>
        e.type === 'submit' && !e.disabled && visible(e) && label(e).toLowerCase() === 'search');
    const sold = [...document.querySelectorAll('a,label,button,[role="checkbox"],input[type="checkbox"]')]
        .filter(e => label(e) === 'Sold items' && !e.disabled && visible(e));
    const point = e => {
        if (!e) return null;
        const r = e.getBoundingClientRect();
        const x = r.x + r.width / 2, y = r.y + r.height / 2;
        return r.width > 0 && r.height > 0 && e.contains(document.elementFromPoint(x, y))
            ? [x, y] : null;
    };
    const controls = {field, submit: buttons.length === 1 ? buttons[0] : null,
                      sold: sold.length === 1 ? sold[0] : null};
"""
_STATE = "(() => {" + _CONTROLS + r"""
    return {url: location.href, title: document.title, body: document.body?.innerText.slice(0, 60000) || '',
        field: !!field, query: field?.value, form: field?.form?.action, method: field?.form?.method,
        submit: !!controls.submit, sold: !!controls.sold};
})()"""


@dataclass
class DesktopNavResult:
    ok: bool
    query: str
    search_success: bool
    sold_click_success: bool
    sold_state_verified: bool
    url: str
    title: str
    sorry: bool
    challenge: bool
    sold_date_lines: int
    error: str | None = None
    diagnostics: dict[str, Any] | None = None


def ensure_chrome_with_cdp(*, profile_dir: Path | None = None, cdp_port: int = DEFAULT_CDP_PORT,
                           start_url: str = "about:blank") -> dict:
    if profile_dir is not None or cdp_port != DEFAULT_CDP_PORT or start_url != "about:blank":
        raise RuntimeError("Native pricing requires its dedicated profile, port and blank startup")
    return ensure_browser()


def _origin(url: str) -> bool:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" == _ORIGIN


def _window(pid: int) -> int:
    user32 = ctypes.windll.user32
    windows = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def collect(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            name = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, name, 64)
            if name.value == "Chrome_WidgetWin_1":
                windows.append(hwnd)
        return True

    if not user32.EnumWindows(collect, 0) or len(windows) != 1:
        raise RuntimeError("Expected one visible dedicated Chrome window")
    return windows[0]


def _page_socket(target_id: str | None = None) -> tuple[str, str]:
    targets = cdp_json("/json/list")
    pages = [t for t in targets if t.get("type") == "page"]
    if len(pages) != 1 or (target_id is not None and pages[0].get("id") != target_id):
        raise RuntimeError("Expected one unchanged dedicated Chrome target")
    page = pages[0]
    socket = page["webSocketDebuggerUrl"]
    if not valid_socket(socket, "/devtools/page/") or not socket.endswith("/" + page["id"]):
        raise RuntimeError("Unexpected native Chrome target socket")
    return page["id"], socket


def _read(target_id: str | None = None) -> tuple[str, dict]:
    target, socket = _page_socket(target_id)
    session = DirectCdpSession(socket, timeout=2)
    try:
        state = session.evaluate(_STATE, timeout=2)
    finally:
        session.close()
    if not isinstance(state, dict):
        raise RuntimeError("Chrome page inspection returned no state")
    return target, state


def _open_home(target: str) -> None:
    _, socket = _page_socket(target)
    session = DirectCdpSession(socket, timeout=3)
    try:
        session.call("Page.navigate", {"url": _HOME}, timeout=3)
    finally:
        session.close()


def _activate_control(target: str, state: dict, control: str, *, query: str = "") -> None:
    """Send input to one verified Chrome tab, with no Windows cursor or focus APIs."""
    if control not in {"field", "submit", "sold"}:
        raise ValueError("Unknown pricing control")

    _, socket = _page_socket(target)
    session = DirectCdpSession(socket, timeout=4)
    try:
        activated = session.evaluate_function(
            "(args) => {" + _CONTROLS + r"""
                if (location.href !== args.url) return false;
                const control = controls[args.control];
                if (!control || (args.control === 'submit' && field.value !== args.query)) return false;
                control.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
                if (!point(control)) return false;
                if (args.control === 'field') {
                    field.focus({preventScroll: true});
                    field.select();
                    return document.activeElement === field;
                }
                control.click();
                return true;
            }""",
            {"url": state["url"], "control": control, "query": state.get("query")},
            timeout=4,
        )
        if activated is not True:
            raise RuntimeError("Rendered pricing control changed or is obscured")
        if control == "field":
            session.call("Input.insertText", {"text": query})
    finally:
        session.close()


def _search_form(state: dict) -> bool:
    return bool(state.get("field") and state.get("submit") and state.get("method") == "get"
                and _origin(state.get("form") or ""))


def _require_gate(market: str) -> None:
    gate = evaluate_ebay_browser_work_gate(market=market)
    if not gate.allowed or not gate.state_integrity_ok:
        raise RuntimeError("MARKETPLACE_WORK_GATE_BLOCKED")


def _check_page(state: dict, *, allow_blank: bool = False) -> None:
    from .ebay_browser_provider import is_ebay_authentication_url

    url, title, body = state["url"], state["title"], state["body"]
    if is_ebay_authentication_url(url):
        raise RuntimeError("EBAY_AUTH_REQUIRED")
    if is_marketplace_gate_page(url, title=title, body=body) or "captcha" in body.lower() or "security measure" in body.lower():
        raise RuntimeError("CHALLENGE_REQUIRED")
    if "error page" in title.lower() or "/error" in urlsplit(url).path.lower():
        raise RuntimeError("TEMPORARY_EBAY_SERVER_FAILURE")
    if not _origin(url) and not (allow_blank and url == "about:blank"):
        raise RuntimeError("UNEXPECTED_PRICING_ORIGIN")


def _wait(target: str, ready, *, timeout: float = 25) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            _, state = _read(target)
        except (TimeoutError, WebSocketTimeoutException):
            continue
        _check_page(state, allow_blank=True)
        if ready(state):
            return state
        time.sleep(0.3)
    raise RuntimeError("WINDOWS_PRICING_PAGE_NOT_CONFIRMED")


def wait_for_search_controls(*, timeout_s: float = 30.0):
    target, _ = _read()
    state = _wait(target, _search_form, timeout=timeout_s)
    return state["field"], state["submit"]


def navigate_query_to_sold(
    query: str, *, reset_homepage: bool = True, attempt_id: str | None = None,
    price_key_id: str | None = None, pre_submit_only: bool = False,
) -> DesktopNavResult:
    from .ebay_browser_provider import verify_sold_result_state

    attempt_id = (attempt_id or os.getenv("CARDSCANR_LIVE_ATTEMPT_ID") or "").strip()
    price_key_id = (price_key_id or os.getenv("CARDSCANR_PRICE_KEY_ID") or "").strip() or None
    market = (os.getenv("CARDSCANR_MARKET") or "AU").strip().upper()
    pre_submit = pre_submit_only or pre_submit_only_requested(flag=True)
    search: dict[str, Any] = {"attemptId": attempt_id, "priceKeyId": price_key_id}
    diagnostics = {"search": search}
    state: dict = {}
    submitted = sold_click = False

    def result(error=None, *, success=False, sold_dates=0):
        return DesktopNavResult(
            success, query, submitted or error == PRE_SUBMIT_QUERY_READY, sold_click,
            success and sold_click, state.get("url", ""), state.get("title", ""),
            error == "TEMPORARY_EBAY_SERVER_FAILURE", error == "CHALLENGE_REQUIRED",
            sold_dates, error, diagnostics,
        )

    try:
        _require_gate(market)
        if market != "AU" or not query.strip() or any(not c.isprintable() for c in query):
            raise RuntimeError("INVALID_WINDOWS_PRICING_QUERY_OR_MARKET")
        with browser_lock():
            if not pre_submit and (not attempt_id or has_search_submission_started(attempt_id)):
                raise RuntimeError("SEARCH_ATTEMPT_MISSING_OR_ALREADY_CONSUMED")
            pid = listener_owner()
            if pid is None:
                raise RuntimeError("NATIVE_PRICING_BROWSER_NOT_READY")
            target, state = _read()
            _check_page(state, allow_blank=True)
            _window(pid)
            if state["url"] == "about:blank" or (reset_homepage and state["url"] != _HOME):
                _require_gate(market)
                _open_home(target)
            state = _wait(target, _search_form)
            search["searchInputLocated"] = True
            if state.get("query") != query:
                _activate_control(target, state, "field", query=query)
            state = _wait(target, lambda s: _search_form(s) and s.get("query") == query, timeout=8)
            search["queryTyped"] = True
            if pre_submit:
                search.update(resultCode=PRE_SUBMIT_QUERY_READY, submitted=False)
                return result(PRE_SUBMIT_QUERY_READY, success=True)
            if listener_owner() != pid:
                raise RuntimeError("NATIVE_PRICING_BROWSER_OWNER_CHANGED")
            _require_gate(market)
            state = _wait(target, lambda s: _search_form(s) and s.get("query") == query, timeout=8)
            event = emit_search_submission_started(
                attempt_id=attempt_id, price_key_id=price_key_id, query=query, market=market,
                currency=os.getenv("CARDSCANR_CURRENCY"), fingerprint=os.getenv("CARDSCANR_FINGERPRINT"),
            )
            search["searchSubmissionStarted"] = event.to_dict()
            _require_gate(market)
            _activate_control(target, state, "submit")
            submitted = True
            search.update(submitted=True, submit="windows_tab_control_click")

            def query_matches(s):
                return query in parse_qs(urlsplit(s["url"]).query).get("_nkw", [])

            state = _wait(target, lambda s: query_matches(s) and "/sch/" in urlsplit(s["url"]).path)
            if not state.get("sold") or listener_owner() != pid:
                raise RuntimeError("SOLD_CONTROL_OR_BROWSER_OWNER_UNVERIFIED")
            _require_gate(market)
            _, state = _read(target)
            _check_page(state)
            if not query_matches(state) or not state.get("sold"):
                raise RuntimeError("SOLD_CONTROL_CHANGED")
            _activate_control(target, state, "sold")
            sold_click = True
            state = _wait(target, lambda s: query_matches(s)
                          and "1" in parse_qs(urlsplit(s["url"]).query).get("LH_Sold", []))
            diagnostics["sold"] = {"body": state["body"]}
            sold_state = verify_sold_result_state(
                url=state["url"], title=state["title"], body_text=state["body"],
            )
            if not sold_state["SOLD_STATE_VERIFIED"]:
                raise RuntimeError("SOLD_STATE_NOT_VERIFIED")
            return result(success=True, sold_dates=int(sold_state["soldDateLines"]))
    except Exception as exc:
        error = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        search["resultCode"] = error
        return result(error)
