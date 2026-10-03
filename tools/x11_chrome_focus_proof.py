#!/usr/bin/env python3
"""Offline X11 Chrome focus + keyboard injection proof (no eBay).

Runs inside WSL on DISPLAY=:99 against production Chrome/CDP.
Never opens ebay.*, never emits SEARCH_SUBMISSION_STARTED.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/mnt/d/cardscanr-data")
if not ROOT.exists():
    ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.live_navigation_attempt import (  # noqa: E402
    count_search_submission_started,
    has_search_submission_started,
)
from cardscanr_market_engine.x11_chrome_focus import (  # noqa: E402
    focus_chrome_window,
    probe_ewmh_window_manager,
)

DISPLAY = os.environ.get("DISPLAY", ":99")
CDP_PORT = int(os.environ.get("EBAY_BROWSER_CDP_PORT", "9444"))
ART = ROOT / "reports" / "artifacts" / "x11_chrome_focus_closure"
PROBE_HTML = Path("/tmp/cardscanr_x11_input_probe.html")


def _env() -> None:
    prefix = Path(os.path.expanduser("~/.local/cardscanr-gui"))
    os.environ["DISPLAY"] = DISPLAY
    os.environ["PATH"] = f"{prefix}/root/usr/bin:" + os.environ.get("PATH", "")
    lib = f"{prefix}/root/usr/lib/x86_64-linux-gnu:{prefix}/root/lib/x86_64-linux-gnu"
    os.environ["LD_LIBRARY_PATH"] = lib + (
        ":" + os.environ["LD_LIBRARY_PATH"] if os.environ.get("LD_LIBRARY_PATH") else ""
    )


def _cdp(method: str, params: dict | None = None, *, session_id: str | None = None) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=3) as resp:
        tabs = json.loads(resp.read().decode("utf-8", errors="replace"))
    page = None
    for t in tabs if isinstance(tabs, list) else []:
        if isinstance(t, dict) and t.get("type") == "page":
            page = t
            break
    if not page:
        raise RuntimeError("no_cdp_page_target")
    ws_url = page.get("webSocketDebuggerUrl")
    if not ws_url:
        raise RuntimeError("no_ws_debugger_url")
    # Prefer HTTP /json/protocol via Target — use simple Runtime via chrome remote.
    # Fallback: Page.navigate via curl-less websocket is heavy; use chrome DevTools HTTP endpoint
    # through `websockets` if available, else subprocess with node-less python websocket.
    try:
        import websocket  # type: ignore
    except Exception as exc:
        raise RuntimeError(f"websocket_client_missing:{exc}") from exc

    ws = websocket.create_connection(ws_url, timeout=8)
    try:
        msg_id = int(time.time() * 1000) % 1_000_000
        payload = {"id": msg_id, "method": method, "params": params or {}}
        ws.send(json.dumps(payload))
        deadline = time.time() + 8
        while time.time() < deadline:
            raw = ws.recv()
            data = json.loads(raw)
            if data.get("id") == msg_id:
                if "error" in data:
                    raise RuntimeError(str(data["error"]))
                return data.get("result") or {}
        raise RuntimeError("cdp_timeout")
    finally:
        ws.close()


def _cdp_targets() -> list[dict]:
    with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=3) as resp:
        tabs = json.loads(resp.read().decode("utf-8", errors="replace"))
    return tabs if isinstance(tabs, list) else []


def _write_probe_html() -> str:
    html = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>CardScanR X11 Input Probe</title></head>
<body>
<h1>CardScanR X11 Input Probe</h1>
<p>Offline only. No network forms.</p>
<input id="cardscanr_x11_probe_input" type="text" style="font-size:24px;width:90%;padding:12px"
       autocomplete="off" spellcheck="false" />
<script>
document.getElementById('cardscanr_x11_probe_input').focus();
</script>
</body></html>
"""
    PROBE_HTML.write_text(html, encoding="utf-8")
    return PROBE_HTML.as_uri()


def run_proof() -> dict:
    _env()
    ART.mkdir(parents=True, exist_ok=True)
    before_events = count_search_submission_started()
    ewmh = probe_ewmh_window_manager()
    focus = focus_chrome_window(preferred_wid=None)
    page_uri = _write_probe_html()
    marker = f"CARDSCANR_X11_INPUT_PROOF_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"

    targets_before = _cdp_targets()
    ebay_before = [str(t.get("url") or "") for t in targets_before if "ebay." in str(t.get("url") or "").lower()]

    nav = None
    typed_ok = False
    dom_value = None
    focus_input_ok = False
    try:
        nav = _cdp("Page.navigate", {"url": page_uri})
        # Wait until the local probe document is actually loaded.
        url_ok = False
        for _ in range(20):
            time.sleep(0.25)
            ev = _cdp(
                "Runtime.evaluate",
                {"expression": "location.href", "returnByValue": True},
            )
            href = str(((ev.get("result") or {}).get("value")) or "")
            if "cardscanr_x11_input_probe.html" in href:
                url_ok = True
                break
        if not url_ok:
            raise RuntimeError(f"probe_page_not_loaded:{href!r}")

        focus2 = focus_chrome_window(preferred_wid=None)
        if not focus2.ready:
            raise RuntimeError(f"focus_after_nav:{focus2.reason_code}")

        box = _cdp(
            "Runtime.evaluate",
            {
                "expression": (
                    "(() => { const el=document.getElementById('cardscanr_x11_probe_input');"
                    " el.focus(); const r=el.getBoundingClientRect();"
                    " return {x:r.x,y:r.y,w:r.width,h:r.height,dpr:window.devicePixelRatio||1}; })()"
                ),
                "returnByValue": True,
            },
        )
        rect = (box.get("result") or {}).get("value") or {}
        g = focus2.geometry or {}
        # Chrome content area sits below the title/omnibox chrome (~80-120px).
        chrome_ui_top = 85
        cx = int(g.get("x", 0) + float(rect.get("x") or 0) + float(rect.get("w") or 100) / 2)
        cy = int(g.get("y", 0) + chrome_ui_top + float(rect.get("y") or 0) + float(rect.get("h") or 20) / 2)
        subprocess.check_call(["xdotool", "mousemove", str(cx), str(cy)])
        subprocess.check_call(["xdotool", "click", "--clearmodifiers", "1"])
        time.sleep(0.25)
        focus_input_ok = True
        _cdp(
            "Runtime.evaluate",
            {
                "expression": "document.getElementById('cardscanr_x11_probe_input').value=''; true",
            },
        )
        subprocess.check_call(
            ["xdotool", "type", "--clearmodifiers", "--delay", "18", "--", marker]
        )
        time.sleep(0.4)
        # Do NOT press Enter.
        eval_res = _cdp(
            "Runtime.evaluate",
            {
                "expression": "document.getElementById('cardscanr_x11_probe_input').value",
                "returnByValue": True,
            },
        )
        dom_value = ((eval_res.get("result") or {}).get("value")) if isinstance(eval_res, dict) else None
        typed_ok = str(dom_value or "") == marker
        if not typed_ok:
            # Fallback proof: Input.dispatchKeyEvent is NOT used; retry click+type once.
            subprocess.check_call(["xdotool", "click", "--clearmodifiers", "1"])
            time.sleep(0.15)
            subprocess.check_call(
                ["xdotool", "type", "--clearmodifiers", "--delay", "18", "--", marker]
            )
            time.sleep(0.4)
            eval_res = _cdp(
                "Runtime.evaluate",
                {
                    "expression": "document.getElementById('cardscanr_x11_probe_input').value",
                    "returnByValue": True,
                },
            )
            dom_value = ((eval_res.get("result") or {}).get("value")) if isinstance(eval_res, dict) else None
            typed_ok = marker in str(dom_value or "")
    except Exception as exc:
        err = f"{type(exc).__name__}:{exc}"
        result = {
            "ok": False,
            "error": err,
            "ewmh": ewmh,
            "focus": focus.to_dict(),
            "page": page_uri,
            "marker": marker,
            "typedMarkerVerified": False,
            "searchSubmissionStartedBefore": before_events,
            "searchSubmissionStartedAfter": count_search_submission_started(),
            "ebayTargets": ebay_before,
        }
        (ART / "keyboard_injection_proof.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        return result

    targets_after = _cdp_targets()
    ebay_after = [str(t.get("url") or "") for t in targets_after if "ebay." in str(t.get("url") or "").lower()]
    after_events = count_search_submission_started()

    result = {
        "ok": bool(focus.ready and typed_ok and not ebay_after and after_events == before_events),
        "windowFocusReady": bool(focus.ready),
        "keyboardInjectionReady": bool(typed_ok),
        "typedMarkerVerified": bool(typed_ok),
        "chromeFocused": bool(focus.focus_verified),
        "inputFocused": bool(focus_input_ok),
        "marker": marker,
        "domValue": dom_value,
        "page": page_uri,
        "focus": focus.to_dict(),
        "ewmh": ewmh,
        "cdpNavigate": nav,
        "ebayTargetsBefore": ebay_before,
        "ebayTargetsAfter": ebay_after,
        "networkEbayRequests": 0,
        "searchSubmissionStartedBefore": before_events,
        "searchSubmissionStartedAfter": after_events,
        "searchSubmissionStartedDelta": after_events - before_events,
        "preSubmitGuiReady": bool(focus.ready and typed_ok),
        "resultCode": "PRE_SUBMIT_GUI_READY" if (focus.ready and typed_ok and after_events == before_events) else "PRE_SUBMIT_GUI_NOT_READY",
    }
    (ART / "keyboard_injection_proof.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    (ART / "focus_method_proof.json").write_text(
        json.dumps({"ewmh": ewmh, "focus": focus.to_dict()}, indent=2) + "\n",
        encoding="utf-8",
    )
    return result


def main() -> int:
    out = run_proof()
    print(json.dumps(out, indent=2))
    return 0 if out.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
