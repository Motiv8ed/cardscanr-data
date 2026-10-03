#!/usr/bin/env python3
"""X11-only eBay AU search via visible search input (state-machine gated)."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image
from Xlib import X, display

# FSM lives in the repo; when run under WSL the path is mounted.
sys.path.insert(0, "/mnt/d/cardscanr-data")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cardscanr_market_engine.live_navigation_attempt import (  # noqa: E402
    SEARCH_SUBMISSION_STARTED,
    emit_search_submission_started,
)
from cardscanr_market_engine.navigation_runtime_context import (  # noqa: E402
    INTER_CARD_EXPECTED_TARGET_REJECTED,
    PRE_SUBMIT_QUERY_READY,
    load_navigation_runtime_context,
    pre_submit_only_requested,
)
from cardscanr_market_engine.browser_lifecycle_policy import (  # noqa: E402
    RUNTIME_COLD_START,
    evaluate_runtime_targets,
)
from cardscanr_market_engine.providers.linux_x11_gui_diagnostics import (  # noqa: E402
    GuiAttemptTimings,
    build_post_navigation_snapshot,
    build_pre_submit_snapshot,
)
from cardscanr_market_engine.providers.linux_x11_gui_fsm import (  # noqa: E402
    ALTERNATE_EBAY_SURFACE,
    EBAY_ACCESS_DENIED_403,
    EBAY_LIVE_RESULTS,
    LOCAL_GUI_FAILURE,
    LOCAL_SEARCH_SURFACE_RECOVERY_FAILED,
    LOCAL_SEARCH_SURFACE_STATE_LEAK,
    ORDINARY_RESULTS_CONFIRMED,
    SEARCH_SURFACE_VALIDATED,
    SOLD_UNAVAILABLE_ON_ALTERNATE_SURFACE,
    TEMPORARY_EBAY_SERVER_FAILURE,
    SearchGateState,
    SearchPhase,
    classify_search_origin_surface,
    classify_search_surface,
    is_ebay_challenge_page,
    is_ebay_live_search_url,
    is_ebay_sorry_page,
    is_ordinary_sch_results_url,
    may_submit_search,
    on_query_visibility,
    on_search_post_submit_page,
    on_search_surface_validated,
    on_submit_attempt,
    page_is_about_blank,
    search_page_ready,
)

DISPLAY_NAME = os.environ.get("DISPLAY", ":99")
PREFIX = Path(os.path.expanduser("~/.local/cardscanr-gui"))
ART = Path("/mnt/d/cardscanr-data/reports/artifacts")


def run_self_check(*, runtime_mode: str | None = None) -> dict:
    """Local-only readiness check. Never opens eBay or submits keys to a page."""
    _env()
    mode = (runtime_mode or os.environ.get("CARDSCANR_RUNTIME_MODE") or "COLD_START").strip().upper()
    prior_payload: dict = {}
    prior_path = (os.environ.get("CARDSCANR_EXPECTED_PRIOR_JSON_PATH") or "").strip()
    if prior_path and Path(prior_path).is_file():
        try:
            prior_payload = json.loads(Path(prior_path).read_text(encoding="utf-8"))
            if isinstance(prior_payload, dict) and isinstance(prior_payload.get("expectedPrior"), dict):
                prior_payload = prior_payload["expectedPrior"]
        except (OSError, json.JSONDecodeError):
            prior_payload = {}
    elif (os.environ.get("CARDSCANR_EXPECTED_PRIOR_JSON") or "").strip():
        try:
            prior_payload = json.loads(os.environ["CARDSCANR_EXPECTED_PRIOR_JSON"])
        except json.JSONDecodeError:
            prior_payload = {}
    errors: list[str] = []
    ctx = load_navigation_runtime_context()
    details: dict = {
        "display": DISPLAY_NAME,
        "prefix": str(PREFIX),
        "imports": {},
        "binaries": {},
        "x11": {},
        "chromeWindowDiscovery": {},
        "cdp": {},
        "runtimeMode": mode,
        "navContextRuntimeMode": ctx.runtime_mode,
        "ensureChromeWouldAllowExistingEbay": ctx.is_inter_card(),
        "productionEnsureChromeInvoked": False,
        "selfCheckDoesNotCallEnsureChromeWithCdp": True,
        "ebayNavigationPerformed": False,
        "searchSubmissionEventContract": SEARCH_SUBMISSION_STARTED,
    }
    if mode == "INTER_CARD" and not ctx.is_inter_card() and not os.environ.get("CARDSCANR_RUNTIME_MODE"):
        errors.append("INTER_CARD_CONTEXT_NOT_PROPAGATED")
    try:
        from PIL import Image as _Image  # noqa: F401

        details["imports"]["PIL"] = True
    except Exception as exc:  # pragma: no cover - environment specific
        details["imports"]["PIL"] = False
        errors.append(f"PIL:{exc}")
    try:
        from Xlib import display as _display  # noqa: F401

        details["imports"]["Xlib"] = True
    except Exception as exc:  # pragma: no cover
        details["imports"]["Xlib"] = False
        errors.append(f"Xlib:{exc}")

    for binary in ("xdotool", "xwininfo", "xdpyinfo"):
        ok = shutil.which(binary) is not None
        details["binaries"][binary] = ok
        if not ok:
            errors.append(f"binary_missing:{binary}")

    try:
        d = display.Display(DISPLAY_NAME)
        root = d.screen().root
        geom = root.get_geometry()
        details["x11"] = {
            "connected": True,
            "width": int(geom.width),
            "height": int(geom.height),
        }
        d.close()
    except Exception as exc:
        details["x11"] = {"connected": False, "error": str(exc)}
        errors.append(f"x11:{exc}")

    try:
        # Discovery only — do not activate or type.
        out = subprocess.check_output(
            "xwininfo -root -tree | awk '/Google Chrome/{print $1; exit}'",
            shell=True,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        details["chromeWindowDiscovery"] = {
            "available": bool(out),
            "widHex": out or None,
            "mechanism": "xwininfo_root_tree",
        }
    except Exception as exc:
        details["chromeWindowDiscovery"] = {"available": False, "error": str(exc)}

    # Live focus proof (no eBay navigation / no SEARCH_SUBMISSION_STARTED).
    try:
        from cardscanr_market_engine.x11_chrome_focus import focus_chrome_window, probe_ewmh_window_manager

        ewmh = probe_ewmh_window_manager()
        focus = focus_chrome_window(preferred_wid=None)
        details["chromeFocus"] = focus.to_dict()
        details["ewmh"] = ewmh
        details["chromeWindowReady"] = bool(focus.window_id and focus.window_mapped)
        details["windowFocusReady"] = bool(focus.ready)
        if not focus.ready:
            errors.append(f"chrome_focus:{focus.reason_code}")
    except Exception as exc:
        details["chromeFocus"] = {"error": str(exc)}
        details["chromeWindowReady"] = False
        details["windowFocusReady"] = False
        errors.append(f"chrome_focus:{exc}")

    cdp_port = int(os.environ.get("EBAY_BROWSER_CDP_PORT", "9444"))
    try:
        from cardscanr_market_engine.browser_lifecycle_policy import (
            PriorCardContext,
            evaluate_runtime_targets,
        )

        with urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json/version", timeout=2) as resp:
            meta = json.loads(resp.read().decode("utf-8", errors="replace"))
        with urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json/list", timeout=2) as resp:
            targets = json.loads(resp.read().decode("utf-8", errors="replace"))
        raw = [
            {
                "id": t.get("id"),
                "type": t.get("type") or "page",
                "url": t.get("url") or "",
                "title": t.get("title") or "",
            }
            for t in (targets if isinstance(targets, list) else [])
            if isinstance(t, dict)
        ]
        prior = PriorCardContext.from_dict(prior_payload if isinstance(prior_payload, dict) else None)
        policy = evaluate_runtime_targets(raw, mode=mode, prior=prior)
        ebay = [
            c.url
            for c in policy.classified
            if c.top_level and c.origin_class == "ebay_marketplace"
        ]
        details["cdp"] = {
            "ready": True,
            "port": cdp_port,
            "browser": meta.get("Browser"),
            "ebayTargets": ebay,
            "runtimeMode": mode,
            "targetPolicy": policy.to_dict(),
        }
        if not policy.ok:
            errors.append("unexpected_ebay_target_during_self_check:" + ",".join(policy.reason_codes))
    except Exception as exc:
        details["cdp"] = {"ready": False, "port": cdp_port, "error": str(exc)}

    # Keyboard/mouse layer init (xdotool present + no-op version probe).
    try:
        subprocess.check_call(
            ["xdotool", "version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        details["automationLayer"] = {"xdotool": True}
    except Exception as exc:
        details["automationLayer"] = {"xdotool": False, "error": str(exc)}
        errors.append(f"xdotool:{exc}")

    ok = not errors and bool(details["imports"].get("PIL")) and bool(details["imports"].get("Xlib"))
    return {
        "ok": ok,
        "selfCheck": "linux_x11_ebay_search",
        "searchSelfCheck": ok,
        "errors": errors,
        "details": details,
        "ebayNavigationPerformed": False,
    }


def _env() -> None:
    os.environ["DISPLAY"] = DISPLAY_NAME
    os.environ["PATH"] = f"{PREFIX}/root/usr/bin:" + os.environ.get("PATH", "")
    lib = f"{PREFIX}/root/usr/lib/x86_64-linux-gnu:{PREFIX}/root/lib/x86_64-linux-gnu"
    os.environ["LD_LIBRARY_PATH"] = lib + (
        ":" + os.environ["LD_LIBRARY_PATH"] if os.environ.get("LD_LIBRARY_PATH") else ""
    )


def sh(cmd: str) -> None:
    subprocess.check_call(cmd, shell=True)


def clear_modifiers() -> None:
    """Release stuck modifiers in one subprocess (was 4 serial xdotool calls)."""
    try:
        subprocess.call(
            ["xdotool", "keyup", "ctrl", "alt", "shift", "super"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def shot(path: Path | None = None) -> Image.Image:
    d = display.Display(DISPLAY_NAME)
    root = d.screen().root
    g = root.get_geometry()
    raw = root.get_image(0, 0, g.width, g.height, X.ZPixmap, 0xFFFFFFFF)
    im = Image.frombytes("RGB", (g.width, g.height), raw.data, "raw", "BGRX")
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        im.save(path)
    return im


def chrome_geom() -> dict:
    """Resolve the current production Chrome top-level window (no cached IDs)."""
    from cardscanr_market_engine.x11_chrome_focus import discover_chrome_toplevel

    discovered = discover_chrome_toplevel()
    wid = discovered.get("wid")
    if wid is None:
        raise RuntimeError("chrome_window_not_found")
    return {
        "wid_hex": discovered.get("wid_hex") or hex(int(wid)),
        "wid": int(wid),
        "x": int(discovered.get("x") or 0),
        "y": int(discovered.get("y") or 0),
        "w": int(discovered.get("w") or 0),
        "h": int(discovered.get("h") or 0),
        "discoveryMethod": discovered.get("discoveryMethod"),
        "wmClass": discovered.get("wmClass"),
        "wmName": discovered.get("wmName"),
        "pid": discovered.get("pid"),
        "mapped": discovered.get("mapped"),
    }


def chrome_owner_count() -> int:
    try:
        out = subprocess.check_output(
            "pgrep -fc 'chrome.*--user-data-dir=.*/.config/cardscanr-chrome' || true",
            shell=True,
            text=True,
        ).strip()
        return int(out or "0")
    except Exception:
        return -1


def title() -> str:
    return subprocess.check_output(
        "xwininfo -root -tree | awk -F'\"' '/Google Chrome/{print $2; exit}'",
        shell=True,
        text=True,
    ).strip()


def omnibox_url() -> str:
    clear_modifiers()
    sh("xdotool key --clearmodifiers ctrl+l")
    time.sleep(0.25)
    sh("xdotool key --clearmodifiers ctrl+c")
    time.sleep(0.3)
    try:
        u = subprocess.check_output(
            [str(PREFIX / "root/usr/bin/xclip"), "-o", "-selection", "clipboard"],
            text=True,
        ).strip()
    except Exception:
        u = ""
    sh("xdotool key --clearmodifiers Escape")
    time.sleep(0.15)
    clear_modifiers()
    return u


def cdp_active_url(*, port: int | None = None) -> str:
    """Read-only CDP URL peek — does not steal search-field focus."""
    p = int(port or os.environ.get("EBAY_BROWSER_CDP_PORT", "9444"))
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{p}/json/list", timeout=2) as resp:
            tabs = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
        return ""
    if not isinstance(tabs, list):
        return ""
    for tab in tabs:
        if not isinstance(tab, dict):
            continue
        if str(tab.get("type") or "") != "page":
            continue
        url = str(tab.get("url") or "")
        if "ebay." in url.lower():
            return url
    for tab in tabs:
        if isinstance(tab, dict) and str(tab.get("type") or "") == "page":
            return str(tab.get("url") or "")
    return ""


def cdp_raw_targets(*, port: int | None = None) -> list[dict]:
    p = int(port or os.environ.get("EBAY_BROWSER_CDP_PORT", "9444"))
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{p}/json/list", timeout=2) as resp:
            tabs = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
        return []
    if not isinstance(tabs, list):
        return []
    return [t for t in tabs if isinstance(t, dict)]


def url_is_ebay_live_surface(url: str | None) -> bool:
    u = (url or "").strip().lower()
    return "ebaylive" in u or is_ebay_live_search_url(u)


def activate(g: dict | None = None) -> dict:
    """Focus the current Chrome top-level window using the no-WM-safe helper.

    Bare Xvfb has no EWMH WM, so ``xdotool windowactivate`` aborts. Production
    uses ``focus_chrome_window`` (windowfocus / Xlib) and verifies focus before
    any keyboard injection. Failure raises before SEARCH_SUBMISSION_STARTED.
    Always re-resolves the live Chrome top-level window (never trusts a cached id).
    """
    from cardscanr_market_engine.x11_chrome_focus import REASON_READY, focus_chrome_window

    focus = focus_chrome_window(preferred_wid=None)
    if focus.reason_code != REASON_READY or not focus.focus_verified:
        raise RuntimeError(f"{focus.reason_code}:chrome_focus_failed")
    if isinstance(g, dict) and focus.window_id is not None:
        g["wid"] = int(focus.window_id)
        g["wid_hex"] = focus.window_id_hex or hex(int(focus.window_id))
        if focus.geometry:
            g["x"] = int(focus.geometry.get("x") or g.get("x") or 0)
            g["y"] = int(focus.geometry.get("y") or g.get("y") or 0)
            g["w"] = int(focus.geometry.get("w") or g.get("w") or 0)
            g["h"] = int(focus.geometry.get("h") or g.get("h") or 0)
    time.sleep(0.2)
    clear_modifiers()
    sh("xdotool key --clearmodifiers Escape")
    time.sleep(0.1)
    return focus.to_dict()


def click_xy(x: int, y: int) -> None:
    # Avoid mousemove --sync: under load it can stall multi-second (Ambipom T3→T4).
    # click --clearmodifiers already clears stuck modifiers for the click itself.
    sh(f"xdotool mousemove {x} {y}")
    sh("xdotool click --clearmodifiers 1")


def find_search_button(im: Image.Image, g: dict) -> tuple[int, int, int] | None:
    best = None
    y0, y1 = g["y"] + 100, g["y"] + 165
    x0, x1 = g["x"] + 700, g["x"] + g["w"] - 15
    for y in range(y0, y1):
        x = x0
        while x < x1:
            r, gv, b = im.getpixel((x, y))
            if b > 200 and r < 100 and gv < 150 and b > r + 100:
                xs = x
                while x < x1:
                    r, gv, b = im.getpixel((x, y))
                    if not (b > 200 and r < 100 and gv < 150 and b > r + 100):
                        break
                    x += 1
                w = x - xs
                if w >= 70:
                    cand = (w, xs + w // 2, y)
                    if best is None or cand[0] > best[0]:
                        best = cand
            else:
                x += 1
    if not best:
        return None
    return best[1], best[2], best[0]


def field_and_category_from_button(bx: int, by: int, g: dict) -> dict:
    cat_w = 150
    cat_right = bx - 45
    cat_left = cat_right - cat_w
    field_right = cat_left - 8
    field_left = g["x"] + 210
    field_cx = (field_left + field_right) // 2
    field_click_x = field_left + int((field_right - field_left) * 0.35)
    return {
        "field_click": (field_click_x, by),
        "field_center": (field_cx, by),
        "category_click": ((cat_left + cat_right) // 2, by),
        "search_btn": (bx, by),
        "field_left": field_left,
        "field_right": field_right,
        "cat_left": cat_left,
        "cat_right": cat_right,
    }


def row_has_typed_text(im: Image.Image, layout: dict) -> bool:
    x0, x1 = layout["field_left"] + 20, layout["field_right"] - 30
    y0, y1 = layout["field_click"][1] - 10, layout["field_click"][1] + 10
    dark = total = 0
    for y in range(y0, y1 + 1, 2):
        for x in range(x0, x1, 3):
            r, gv, b = im.getpixel((x, y))
            total += 1
            if r < 90 and gv < 90 and b < 90:
                dark += 1
    return total > 0 and (dark / total) > 0.01


def ensure_all_categories(layout: dict, tag: str, *, force: bool = False) -> str:
    """Reset category dropdown to All Categories via visible UI.

    When force=False and homepage Ordinary marketplace is already validated with
    sacat=0 / All Categories positively indicated by URL, skip the GUI reset.
    """
    url = cdp_active_url()
    u = (url or "").lower()
    # Positive: domain root (no /sch) or explicit _sacat=0 — already All Categories.
    if not force and u and "ebay." in u and "ebaylive" not in u:
        if ("_sacat=0" in u) or (
            "/sch/" not in u
            and "lh_sold" not in u
            and u.rstrip("/").endswith("ebay.com.au")
        ):
            return "skipped_all_categories_already_verified"
    cx, cy = layout["category_click"]
    click_xy(cx, cy)
    time.sleep(0.28)
    shot(ART / f"linux_cat_open_{tag}.png")
    click_xy(cx, cy + 36)
    time.sleep(0.32)
    shot(ART / f"linux_cat_after_{tag}.png")
    clear_modifiers()
    sh("xdotool key --clearmodifiers Escape")
    time.sleep(0.15)
    return "gui_all_categories_reset"


def close_blank_tabs_if_any(g: dict) -> None:
    """If active tab is about:blank, close it and activate remaining eBay tab."""
    t = title()
    if "untitled" in t.lower() or "loading" in t.lower():
        clear_modifiers()
        sh("xdotool key --clearmodifiers ctrl+w")
        time.sleep(0.6)
        activate(g)


def navigate_omnibox_home() -> None:
    """GUI omnibox navigation to ebay homepage (not a /sch manufactured search URL)."""
    clear_modifiers()
    sh("xdotool key --clearmodifiers ctrl+l")
    time.sleep(0.25)
    Path("/tmp/ebay_home.txt").write_text("https://www.ebay.com.au/", encoding="utf-8")
    subprocess.check_call(["bash", "-lc", "xclip -selection clipboard < /tmp/ebay_home.txt"])
    sh("xdotool key --clearmodifiers ctrl+a")
    time.sleep(0.05)
    sh("xdotool key --clearmodifiers ctrl+v")
    time.sleep(0.2)
    sh("xdotool key --clearmodifiers Return")
    time.sleep(2.0)


def go_ebay_home(g: dict) -> None:
    close_blank_tabs_if_any(g)
    # Prefer read-only CDP URL (does not steal focus / omnibox) for home readiness.
    u = cdp_active_url() or omnibox_url()
    if page_is_about_blank(u, title()) or "LH_Sold=1" in (u or "") or "/sch/" in (u or ""):
        # Leave sold/results/blank via homepage omnibox (domain root only)
        navigate_omnibox_home()
    else:
        # Already on marketplace root — skip logo click + long wait.
        if u and "ebay." in u.lower() and "/sch/" not in u.lower() and "ebaylive" not in u.lower():
            time.sleep(0.2)
        else:
            click_xy(g["x"] + 95, g["y"] + 175)
            time.sleep(1.2)
    for _ in range(20):
        t = title()
        u2 = cdp_active_url()
        if re.search(r"Electronics, Cars, Fashion|eBay Australia", t) and "for sale" not in t.lower():
            if u2 and "LH_Sold" not in u2 and "/sch/" not in u2 and not page_is_about_blank(u2, t):
                break
            if not u2:
                u2 = omnibox_url()
                if "LH_Sold" not in u2 and "/sch/" not in u2 and not page_is_about_blank(u2, t):
                    break
        time.sleep(0.25)
    # Final guarantee: not on sold/results
    u3 = cdp_active_url() or omnibox_url()
    if "LH_Sold" in u3 or "/sch/" in u3 or page_is_about_blank(u3, title()):
        navigate_omnibox_home()
        time.sleep(1.0)
    time.sleep(0.25)


def type_query(query: str) -> None:
    clear_modifiers()
    # 12ms/char is reliable under XTEST; 25ms was adding ~0.5s with no reliability gain.
    subprocess.check_call(["xdotool", "type", "--clearmodifiers", "--delay", "12", "--", query])


def wait_results(query: str, timeout: int = 40) -> dict:
    tokens = [t for t in re.split(r"\s+", query) if len(t) > 2][:3]
    last_t = last_u = ""
    polls = max(1, int(timeout / 0.4))
    for i in range(polls):
        time.sleep(0.4)
        last_t = title()
        tl = last_t.lower()
        if page_is_about_blank("", last_t):
            return {
                "ok": False,
                "title": last_t,
                "url": "about:blank",
                "waitSec": (i + 1) * 0.4,
                "sorry": False,
                "challenge": False,
                "aboutBlank": True,
                "routeClass": "ABOUT_BLANK",
            }
        # Peek URL early for error pages OR eBay Live / sch results titles.
        peek = (
            "error page" in tl
            or "sorry" in tl
            or "something went wrong" in tl
            or is_ebay_challenge_page(title=last_t)
            or "ebaylive" in tl
            or any(tok.lower() in tl for tok in tokens)
            or ("for sale" in tl and "ebay" in tl)
            or "global marketplace" in tl
        )
        elapsed = round((i + 1) * 0.4, 2)
        if peek:
            last_u = cdp_active_url() or omnibox_url()
        if is_ebay_challenge_page(title=last_t, url=last_u):
            return {
                "ok": False,
                "title": last_t,
                "url": last_u or cdp_active_url() or omnibox_url(),
                "waitSec": elapsed,
                "sorry": False,
                "challenge": True,
                "classification": "EBAY_CHALLENGE_REQUIRED",
                "routeClass": "EBAY_CHALLENGE",
            }
        if is_ebay_sorry_page(title=last_t, url=last_u):
            return {
                "ok": False,
                "title": last_t,
                "url": last_u or cdp_active_url() or omnibox_url(),
                "waitSec": elapsed,
                "sorry": True,
                "challenge": False,
                "classification": TEMPORARY_EBAY_SERVER_FAILURE,
                "routeClass": TEMPORARY_EBAY_SERVER_FAILURE,
            }
        # Detect ebaylive even when title is generic marketplace chrome.
        if last_u and is_ebay_live_search_url(last_u):
            surface = classify_search_surface(
                title=last_t,
                url=last_u,
                expected_query=query,
                query_visible_confirmed=True,
                submitted=True,
                sold_control_available=False,
            )
            return {
                "ok": False,
                "title": last_t,
                "url": last_u,
                "waitSec": elapsed,
                "sorry": False,
                "challenge": False,
                "ebayLive": True,
                "alternateSurface": True,
                "ordinaryResults": False,
                "classification": surface.get("outcome") or EBAY_LIVE_RESULTS,
                "routeClass": EBAY_LIVE_RESULTS,
                "surface": surface,
            }
        hit = any(tok.lower() in tl for tok in tokens) or ("for sale" in tl and "ebay" in tl)
        if hit and "electronics, cars, fashion" not in tl:
            last_u = cdp_active_url() or omnibox_url()
            if is_ebay_sorry_page(title=last_t, url=last_u):
                return {
                    "ok": False,
                    "title": last_t,
                    "url": last_u,
                    "waitSec": elapsed,
                    "sorry": True,
                    "challenge": False,
                    "classification": TEMPORARY_EBAY_SERVER_FAILURE,
                    "routeClass": TEMPORARY_EBAY_SERVER_FAILURE,
                }
            if is_ebay_live_search_url(last_u):
                surface = classify_search_surface(
                    title=last_t,
                    url=last_u,
                    expected_query=query,
                    query_visible_confirmed=True,
                    submitted=True,
                    sold_control_available=False,
                )
                return {
                    "ok": False,
                    "title": last_t,
                    "url": last_u,
                    "waitSec": elapsed,
                    "sorry": False,
                    "challenge": False,
                    "ebayLive": True,
                    "alternateSurface": True,
                    "classification": surface.get("outcome") or EBAY_LIVE_RESULTS,
                    "routeClass": EBAY_LIVE_RESULTS,
                    "surface": surface,
                }
            ok = is_ordinary_sch_results_url(last_u)
            return {
                "ok": ok,
                "title": last_t,
                "url": last_u,
                "waitSec": elapsed,
                "sorry": False,
                "challenge": False,
                "ordinaryResults": ok,
                "routeClass": ORDINARY_RESULTS_CONFIRMED if ok else "SEARCH_RESULTS_NOT_CONFIRMED",
            }
    last_u = cdp_active_url() or omnibox_url()
    if is_ebay_sorry_page(title=last_t, url=last_u):
        return {
            "ok": False,
            "title": last_t,
            "url": last_u,
            "waitSec": timeout,
            "sorry": True,
            "challenge": False,
            "classification": TEMPORARY_EBAY_SERVER_FAILURE,
            "routeClass": TEMPORARY_EBAY_SERVER_FAILURE,
        }
    if is_ebay_challenge_page(title=last_t, url=last_u):
        return {
            "ok": False,
            "title": last_t,
            "url": last_u,
            "waitSec": timeout,
            "sorry": False,
            "challenge": True,
            "classification": "EBAY_CHALLENGE_REQUIRED",
            "routeClass": "EBAY_CHALLENGE",
        }
    if is_ebay_live_search_url(last_u):
        surface = classify_search_surface(
            title=last_t,
            url=last_u,
            expected_query=query,
            query_visible_confirmed=True,
            submitted=True,
            sold_control_available=False,
        )
        return {
            "ok": False,
            "title": last_t,
            "url": last_u,
            "waitSec": timeout,
            "sorry": False,
            "challenge": False,
            "ebayLive": True,
            "alternateSurface": True,
            "classification": surface.get("outcome") or EBAY_LIVE_RESULTS,
            "routeClass": EBAY_LIVE_RESULTS,
            "surface": surface,
        }
    return {
        "ok": False,
        "title": last_t,
        "url": last_u,
        "waitSec": timeout,
        "sorry": False,
        "challenge": False,
        "routeClass": "SEARCH_RESULTS_NOT_CONFIRMED",
    }


def _type_and_confirm(query: str, layout: dict, g: dict, tag: str, attempt: int, gate: SearchGateState) -> tuple[bool, dict, SearchGateState]:
    """Type into the visible search field. Never click page-body (Live promo tiles)."""
    fx, fy = layout["field_click"]
    ts = {"fieldClick": time.time(), "xy": [fx, fy]}
    # Dismiss overlays without navigating: Escape only — Ceruledge forensic showed
    # body-center click (x+600,y+400) landed on an eBay Live tile and scoped search to Live.
    clear_modifiers()
    sh("xdotool key --clearmodifiers Escape")
    time.sleep(0.08)
    # Single focus click (Ambipom: dual mousemove --sync burned ~16s before Ctrl+A).
    click_xy(fx, fy)
    time.sleep(0.12)
    gate.phase = SearchPhase.SEARCH_FIELD_CLICKED
    gate.note(f"SEARCH_FIELD_CLICKED attempt={attempt} xy={fx},{fy}")
    sh("xdotool key --clearmodifiers ctrl+a")
    ts["ctrlA"] = time.time()
    time.sleep(0.05)
    sh("xdotool key --clearmodifiers BackSpace")
    time.sleep(0.08)
    gate.phase = SearchPhase.SEARCH_FIELD_FOCUS_PROBE
    type_query(query)
    ts["typed"] = time.time()
    gate.phase = SearchPhase.QUERY_TYPED
    time.sleep(0.25)
    im_t = shot(ART / f"linux_search_{tag}_typed_{attempt}.png")
    visible = row_has_typed_text(im_t, layout)
    gate = on_query_visibility(gate, visible=visible)
    ts["queryVisible"] = visible
    ts["phase"] = gate.phase.value
    ts["urlAfterType"] = cdp_active_url()
    ts["titleAfterType"] = title()
    return visible, ts, gate


def _recover_from_live_before_submit(g: dict, diag: dict, timings: GuiAttemptTimings) -> bool:
    """If still on an eBay Live surface before Enter, return to homepage via visible UI."""
    url = cdp_active_url()
    diag["urlImmediatelyBeforeSubmit"] = url
    diag["titleImmediatelyBeforeSubmit"] = title()
    if not url_is_ebay_live_surface(url):
        return True
    diag["liveSurfaceDetectedBeforeSubmit"] = True
    diag["liveSurfaceUrl"] = url
    diag["surfaceLeakClass"] = LOCAL_SEARCH_SURFACE_STATE_LEAK
    # Do not submit Live-scoped search — recover via homepage omnibox (allowed domain root).
    navigate_omnibox_home()
    g2 = chrome_geom()
    activate(g2)
    time.sleep(0.5)
    url2 = cdp_active_url() or omnibox_url()
    diag["urlAfterLiveRecover"] = url2
    timings.mark("T2_search_surface_ready")
    return not url_is_ebay_live_surface(url2)


def _fail_surface(
    *,
    tag: str,
    gate: SearchGateState,
    diag: dict,
    timings: GuiAttemptTimings,
    error: str,
    reason: str,
    classification: str,
    phase: SearchPhase,
) -> dict:
    out = {
        "ok": False,
        "error": error,
        "reason": reason,
        "classification": classification,
        "diagnostics": diag,
        "manufacturedUrl": False,
        "searchMethod": "visible_ebay_search_input_x11",
        "phase": phase.value,
        "events": gate.events,
        "guiAttemptTimings": timings.to_dict(),
        "searchSurfaceValidated": bool(gate.surface_validated),
        "searchSurfaceClass": gate.search_surface_class,
        "searchOriginUrl": gate.search_origin_url,
    }
    (ART / f"linux_search_{tag}_state.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


def _validate_surface_or_fail(
    gate: SearchGateState,
    *,
    url: str,
    title_s: str,
    tag: str,
    diag: dict,
    timings: GuiAttemptTimings,
    scope_label: str | None = "All Categories",
) -> tuple[SearchGateState, dict | None]:
    ok, gate, origin = on_search_surface_validated(
        gate, url=url, title=title_s, scope_label=scope_label
    )
    diag["searchOrigin"] = origin
    diag["searchOriginUrl"] = url
    diag["searchSurfaceClass"] = origin.get("surfaceClass")
    diag["searchScopeLabel"] = scope_label
    if ok:
        return gate, None
    cls = str(origin.get("surfaceClass") or "")
    if cls == "REJECTED_ORIGIN_EBAY_LIVE" or "ebaylive" in (url or "").lower():
        return gate, _fail_surface(
            tag=tag,
            gate=gate,
            diag=diag,
            timings=timings,
            error=LOCAL_SEARCH_SURFACE_STATE_LEAK,
            reason="search_origin_ebay_live",
            classification=LOCAL_SEARCH_SURFACE_STATE_LEAK,
            phase=SearchPhase.LOCAL_SEARCH_SURFACE_STATE_LEAK,
        )
    return gate, _fail_surface(
        tag=tag,
        gate=gate,
        diag=diag,
        timings=timings,
        error=LOCAL_GUI_FAILURE,
        reason=f"search_origin_rejected:{cls}",
        classification=LOCAL_GUI_FAILURE,
        phase=SearchPhase.LOCAL_GUI_FAILURE,
    )


def gui_search(
    query: str,
    *,
    go_home: bool,
    tag: str,
    submit: str = "enter",
    pre_submit_only: bool = False,
) -> dict:
    _env()
    ART.mkdir(parents=True, exist_ok=True)
    ctx = load_navigation_runtime_context()
    if ctx.is_inter_card():
        go_home = False
    gate = SearchGateState()
    timings = GuiAttemptTimings()
    timings.mark("T0_job_claimed")
    g = chrome_geom()
    activate(g)
    timings.mark("T1_browser_ready")
    diag: dict = {
        "query": query,
        "tag": tag,
        "goHome": go_home,
        "runtimeMode": ctx.runtime_mode,
        "expectedPriorTargetId": ctx.expected_prior.target_id if ctx.expected_prior is not None else None,
        "chromeOwners": chrome_owner_count(),
        "display": DISPLAY_NAME,
        "phases": [],
        "attemptId": (os.environ.get("CARDSCANR_LIVE_ATTEMPT_ID") or "").strip() or None,
        "priceKeyId": (os.environ.get("CARDSCANR_PRICE_KEY_ID") or "").strip() or None,
        "x11Window": {
            "wid": g.get("wid"),
            "widHex": g.get("wid_hex"),
            "discoveryMethod": g.get("discoveryMethod"),
        },
    }
    policy = evaluate_runtime_targets(
        cdp_raw_targets(),
        mode=ctx.runtime_mode or RUNTIME_COLD_START,
        prior=ctx.expected_prior,
    )
    diag["targetPolicy"] = policy.to_dict()
    diag["x11WindowId"] = g.get("wid")
    diag["cdpTargetIds"] = [
        c.target_id for c in policy.classified if c.top_level
    ]
    if not policy.ok:
        out = {
            "ok": False,
            "error": INTER_CARD_EXPECTED_TARGET_REJECTED
            if ctx.is_inter_card()
            else "COLD_START_UNEXPECTED_EBAY_TARGET",
            "resultCode": ",".join(policy.reason_codes),
            "reason": "target_policy_rejected",
            "diagnostics": diag,
            "manufacturedUrl": False,
            "submitted": False,
            "searchSubmissionStarted": False,
        }
        (ART / f"linux_search_{tag}_state.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
        return out

    if go_home:
        go_ebay_home(g)
        g = chrome_geom()
        activate(g)

    # Guard: never search from about:blank
    url0 = omnibox_url()
    t0 = title()
    diag["urlBeforeSearch"] = url0
    diag["titleBeforeSearch"] = t0
    if page_is_about_blank(url0, t0):
        diag["phases"].append("ABOUT_BLANK_ABORT")
        # Recover homepage only after classifying failure for this attempt's readiness
        go_ebay_home(g)
        g = chrome_geom()
        activate(g)
        url0 = omnibox_url()
        t0 = title()
        diag["urlAfterBlankRecover"] = url0
        if page_is_about_blank(url0, t0):
            out = {
                "ok": False,
                "error": "SEARCH_INPUT_NOT_CONFIRMED",
                "reason": "about_blank_unrecoverable",
                "diagnostics": diag,
                "manufacturedUrl": False,
                "searchMethod": "visible_ebay_search_input_x11",
                "phase": SearchPhase.SEARCH_INPUT_NOT_CONFIRMED.value,
            }
            (ART / f"linux_search_{tag}_state.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
            return out

    im = shot(ART / f"linux_search_{tag}_before.png")
    btn = find_search_button(im, g)
    ready, reason = search_page_ready(url=url0, title=t0, search_button_found=bool(btn))
    if not ready:
        gate.phase = SearchPhase.SEARCH_INPUT_NOT_CONFIRMED
        out = {
            "ok": False,
            "error": "SEARCH_INPUT_NOT_CONFIRMED",
            "reason": reason,
            "diagnostics": diag,
            "manufacturedUrl": False,
            "searchMethod": "visible_ebay_search_input_x11",
            "phase": gate.phase.value,
        }
        (ART / f"linux_search_{tag}_state.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
        return out
    gate.phase = SearchPhase.SEARCH_PAGE_READY
    gate.note("SEARCH_PAGE_READY")
    timings.mark("T2_search_surface_ready")

    if not btn:
        bx, by, bw = g["x"] + 903, g["y"] + 159, 160
        diag["button"] = "fallback"
    else:
        bx, by, bw = btn
        diag["button"] = {"x": bx, "y": by, "w": bw}
    gate.phase = SearchPhase.SEARCH_BUTTON_LOCATED
    layout = field_and_category_from_button(bx, by, g)
    diag["layout"] = layout

    # Category reset then RELOCATE (never reuse stale coords).
    # Skip GUI reset when homepage + All Categories already positively indicated.
    diag["categoryAction"] = ensure_all_categories(layout, tag, force=False)
    time.sleep(0.2)
    im = shot(ART / f"linux_search_{tag}_pretype.png")
    btn2 = find_search_button(im, g)
    if not btn2:
        out = {
            "ok": False,
            "error": "SEARCH_INPUT_NOT_CONFIRMED",
            "reason": "search_button_missing_after_category_reset",
            "diagnostics": diag,
            "manufacturedUrl": False,
            "searchMethod": "visible_ebay_search_input_x11",
            "phase": SearchPhase.SEARCH_INPUT_NOT_CONFIRMED.value,
            "guiAttemptTimings": timings.to_dict(),
        }
        (ART / f"linux_search_{tag}_state.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
        return out
    bx, by, bw = btn2
    layout = field_and_category_from_button(bx, by, g)
    diag["layoutAfterCategory"] = layout
    diag["buttonAfterCategory"] = {"x": bx, "y": by, "w": bw}

    # SEARCH_SURFACE_VALIDATED before any typing — do not trust prior card left a valid surface.
    origin_url = cdp_active_url() or str(diag.get("urlBeforeSearch") or "")
    origin_title = title()
    gate, fail = _validate_surface_or_fail(
        gate,
        url=origin_url,
        title_s=origin_title,
        tag=tag,
        diag=diag,
        timings=timings,
        scope_label="All Categories",
    )
    if fail is not None:
        # One visible-UI recovery attempt from Live/specialized origin.
        if fail.get("classification") == LOCAL_SEARCH_SURFACE_STATE_LEAK:
            recovered = _recover_from_live_before_submit(g, diag, timings)
            if not recovered:
                return _fail_surface(
                    tag=tag,
                    gate=gate,
                    diag=diag,
                    timings=timings,
                    error=LOCAL_SEARCH_SURFACE_RECOVERY_FAILED,
                    reason="ordinary_marketplace_surface_unrecoverable",
                    classification=LOCAL_SEARCH_SURFACE_RECOVERY_FAILED,
                    phase=SearchPhase.LOCAL_SEARCH_SURFACE_RECOVERY_FAILED,
                )
            g = chrome_geom()
            activate(g)
            im = shot(ART / f"linux_search_{tag}_surface_recover.png")
            btn2 = find_search_button(im, g)
            if not btn2:
                return _fail_surface(
                    tag=tag,
                    gate=gate,
                    diag=diag,
                    timings=timings,
                    error=LOCAL_SEARCH_SURFACE_RECOVERY_FAILED,
                    reason="search_button_missing_after_surface_recover",
                    classification=LOCAL_SEARCH_SURFACE_RECOVERY_FAILED,
                    phase=SearchPhase.LOCAL_SEARCH_SURFACE_RECOVERY_FAILED,
                )
            bx, by, bw = btn2
            layout = field_and_category_from_button(bx, by, g)
            diag["categoryAction"] = ensure_all_categories(layout, f"{tag}_surf")
            im = shot(ART / f"linux_search_{tag}_pretype_surf.png")
            btn2 = find_search_button(im, g)
            if btn2:
                bx, by, bw = btn2
                layout = field_and_category_from_button(bx, by, g)
            gate = SearchGateState()
            gate.phase = SearchPhase.SEARCH_PAGE_READY
            gate.note("SEARCH_PAGE_READY_AFTER_SURFACE_RECOVER")
            origin_url = cdp_active_url() or omnibox_url()
            origin_title = title()
            gate, fail2 = _validate_surface_or_fail(
                gate,
                url=origin_url,
                title_s=origin_title,
                tag=tag,
                diag=diag,
                timings=timings,
                scope_label="All Categories",
            )
            if fail2 is not None:
                return _fail_surface(
                    tag=tag,
                    gate=gate,
                    diag=diag,
                    timings=timings,
                    error=LOCAL_SEARCH_SURFACE_RECOVERY_FAILED,
                    reason="surface_still_invalid_after_recover",
                    classification=LOCAL_SEARCH_SURFACE_RECOVERY_FAILED,
                    phase=SearchPhase.LOCAL_SEARCH_SURFACE_RECOVERY_FAILED,
                )
        else:
            return fail

    if not gate.surface_validated or gate.phase != SearchPhase.SEARCH_SURFACE_VALIDATED:
        return _fail_surface(
            tag=tag,
            gate=gate,
            diag=diag,
            timings=timings,
            error=LOCAL_SEARCH_SURFACE_STATE_LEAK,
            reason="search_surface_not_validated_before_type",
            classification=LOCAL_SEARCH_SURFACE_STATE_LEAK,
            phase=SearchPhase.LOCAL_SEARCH_SURFACE_STATE_LEAK,
        )
    diag["phases"].append(SEARCH_SURFACE_VALIDATED)

    visible, ts0, gate = _type_and_confirm(query, layout, g, tag, 0, gate)
    diag["typeAttempt0"] = ts0
    timings.mark("T3_search_input_focused", ts0.get("fieldClick"))
    if visible:
        timings.mark("T4_query_visible_confirmed", ts0.get("typed"))
    # Re-assert surface after typing — Ceruledge leak happened between pretype and typed.
    post_type_url = str(ts0.get("urlAfterType") or cdp_active_url() or "")
    if url_is_ebay_live_surface(post_type_url) or not gate.surface_validated:
        gate.surface_validated = False
        gate.phase = SearchPhase.LOCAL_SEARCH_SURFACE_STATE_LEAK
        gate.note(LOCAL_SEARCH_SURFACE_STATE_LEAK)
        diag["urlAfterTypeLeak"] = post_type_url
        recovered = _recover_from_live_before_submit(g, diag, timings)
        if not recovered:
            return _fail_surface(
                tag=tag,
                gate=gate,
                diag=diag,
                timings=timings,
                error=LOCAL_SEARCH_SURFACE_RECOVERY_FAILED,
                reason="live_leak_after_type_unrecoverable",
                classification=LOCAL_SEARCH_SURFACE_RECOVERY_FAILED,
                phase=SearchPhase.LOCAL_SEARCH_SURFACE_RECOVERY_FAILED,
            )
        g = chrome_geom()
        activate(g)
        im = shot(ART / f"linux_search_{tag}_after_live_recover.png")
        btn_r = find_search_button(im, g)
        if not btn_r:
            return _fail_surface(
                tag=tag,
                gate=gate,
                diag=diag,
                timings=timings,
                error=LOCAL_SEARCH_SURFACE_RECOVERY_FAILED,
                reason="search_button_missing_after_live_recover",
                classification=LOCAL_SEARCH_SURFACE_RECOVERY_FAILED,
                phase=SearchPhase.LOCAL_SEARCH_SURFACE_RECOVERY_FAILED,
            )
        bx, by, bw = btn_r
        layout = field_and_category_from_button(bx, by, g)
        diag["categoryActionAfterLiveRecover"] = ensure_all_categories(layout, f"{tag}_livefix")
        im = shot(ART / f"linux_search_{tag}_pretype_livefix.png")
        btn_r2 = find_search_button(im, g)
        if btn_r2:
            bx, by, bw = btn_r2
            layout = field_and_category_from_button(bx, by, g)
        gate = SearchGateState()
        gate.phase = SearchPhase.SEARCH_PAGE_READY
        gate.note("SEARCH_PAGE_READY_AFTER_LIVE_RECOVER")
        origin_url = cdp_active_url() or omnibox_url()
        gate, fail3 = _validate_surface_or_fail(
            gate,
            url=origin_url,
            title_s=title(),
            tag=tag,
            diag=diag,
            timings=timings,
            scope_label="All Categories",
        )
        if fail3 is not None:
            return _fail_surface(
                tag=tag,
                gate=gate,
                diag=diag,
                timings=timings,
                error=LOCAL_SEARCH_SURFACE_RECOVERY_FAILED,
                reason="surface_invalid_after_live_recover",
                classification=LOCAL_SEARCH_SURFACE_RECOVERY_FAILED,
                phase=SearchPhase.LOCAL_SEARCH_SURFACE_RECOVERY_FAILED,
            )
        visible, ts_r, gate = _type_and_confirm(query, layout, g, tag, 2, gate)
        diag["typeAttemptLiveRecover"] = ts_r
        post_type_url = str(ts_r.get("urlAfterType") or "")
        if not visible or url_is_ebay_live_surface(post_type_url):
            return _fail_surface(
                tag=tag,
                gate=gate,
                diag=diag,
                timings=timings,
                error=LOCAL_SEARCH_SURFACE_RECOVERY_FAILED,
                reason="live_surface_or_query_fail_after_recover",
                classification=LOCAL_SEARCH_SURFACE_RECOVERY_FAILED,
                phase=SearchPhase.LOCAL_SEARCH_SURFACE_RECOVERY_FAILED,
            )
        timings.mark("T4_query_visible_confirmed")
        # Keep surface_validated True through retype; on_query_visibility already set query.
        if not gate.surface_validated:
            gate.surface_validated = True
            gate.search_surface_class = "ORDINARY_MARKETPLACE_SEARCH"

    if not visible:
        # ONE controlled refocus: re-locate button, re-click, retype
        im = shot(ART / f"linux_search_{tag}_refocus_before.png")
        btn3 = find_search_button(im, g)
        if btn3:
            bx, by, bw = btn3
            layout = field_and_category_from_button(bx, by, g)
        # Preserve surface_validated across refocus.
        surf_ok = gate.surface_validated
        surf_cls = gate.search_surface_class
        surf_url = gate.search_origin_url
        visible, ts1, gate = _type_and_confirm(query, layout, g, tag, 1, gate)
        gate.surface_validated = surf_ok
        gate.search_surface_class = surf_cls
        gate.search_origin_url = surf_url
        diag["typeAttempt1"] = ts1
        timings.mark("T3_search_input_focused", ts1.get("fieldClick"))
        if visible:
            timings.mark("T4_query_visible_confirmed", ts1.get("typed"))
        if url_is_ebay_live_surface(str(ts1.get("urlAfterType") or "")):
            return _fail_surface(
                tag=tag,
                gate=gate,
                diag=diag,
                timings=timings,
                error=LOCAL_SEARCH_SURFACE_STATE_LEAK,
                reason="ebay_live_after_refocus_type",
                classification=LOCAL_SEARCH_SURFACE_STATE_LEAK,
                phase=SearchPhase.LOCAL_SEARCH_SURFACE_STATE_LEAK,
            )

    if gate.phase == SearchPhase.SEARCH_INPUT_NOT_CONFIRMED or not gate.query_visible:
        out = {
            "ok": False,
            "error": "SEARCH_INPUT_NOT_CONFIRMED",
            "reason": "query_not_visible_after_refocus",
            "diagnostics": diag,
            "manufacturedUrl": False,
            "searchMethod": "visible_ebay_search_input_x11",
            "phase": SearchPhase.SEARCH_INPUT_NOT_CONFIRMED.value,
            "events": gate.events,
            "guiAttemptTimings": timings.to_dict(),
        }
        (ART / f"linux_search_{tag}_state.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
        return out

    # Ensure surface flag survives QUERY_VISIBLE_CONFIRMED transition.
    if not gate.surface_validated:
        return _fail_surface(
            tag=tag,
            gate=gate,
            diag=diag,
            timings=timings,
            error=LOCAL_SEARCH_SURFACE_STATE_LEAK,
            reason="surface_not_validated_before_submit",
            classification=LOCAL_SEARCH_SURFACE_STATE_LEAK,
            phase=SearchPhase.LOCAL_SEARCH_SURFACE_STATE_LEAK,
        )

    # Submit ONLY if SEARCH_SURFACE_VALIDATED + QUERY_VISIBLE_CONFIRMED
    if not may_submit_search(gate):
        out = {
            "ok": False,
            "error": "SEARCH_INPUT_NOT_CONFIRMED",
            "reason": "submit_blocked",
            "diagnostics": diag,
            "manufacturedUrl": False,
            "phase": gate.phase.value,
            "events": gate.events,
            "guiAttemptTimings": timings.to_dict(),
        }
        (ART / f"linux_search_{tag}_state.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
        return out

    pre_url = cdp_active_url()
    pre_title = title()
    diag["urlImmediatelyBeforeSubmit"] = pre_url
    diag["titleImmediatelyBeforeSubmit"] = pre_title
    # Final gate: reject Live origin at submit moment.
    ok_final, gate, origin_final = on_search_surface_validated(
        gate, url=pre_url or gate.search_origin_url, title=pre_title, scope_label="All Categories"
    )
    diag["searchOriginAtSubmit"] = origin_final
    if not ok_final or url_is_ebay_live_surface(pre_url):
        return _fail_surface(
            tag=tag,
            gate=gate,
            diag=diag,
            timings=timings,
            error=LOCAL_SEARCH_SURFACE_STATE_LEAK,
            reason="ebay_live_surface_at_submit_gate",
            classification=LOCAL_SEARCH_SURFACE_STATE_LEAK,
            phase=SearchPhase.LOCAL_SEARCH_SURFACE_STATE_LEAK,
        )
    # Restore QUERY_VISIBLE_CONFIRMED after re-validation (validation sets SEARCH_SURFACE_VALIDATED).
    gate.query_visible = True
    gate.phase = SearchPhase.QUERY_VISIBLE_CONFIRMED

    if pre_submit_only_requested(flag=pre_submit_only):
        out = {
            "ok": True,
            "resultCode": PRE_SUBMIT_QUERY_READY,
            "preSubmitQueryReady": True,
            "error": None,
            "query": query,
            "url": pre_url,
            "title": pre_title,
            "submitted": False,
            "searchSubmissionStarted": False,
            "queryVisibleConfirmed": True,
            "manufacturedUrl": False,
            "runtimeMode": ctx.runtime_mode,
            "diagnostics": diag,
            "guiAttemptTimings": timings.to_dict(),
            "phase": gate.phase.value,
            "events": gate.events,
        }
        (ART / f"linux_search_{tag}_state.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
        return out

    allowed, gate = on_submit_attempt(gate)
    if not allowed:
        out = {
            "ok": False,
            "error": "SEARCH_INPUT_NOT_CONFIRMED",
            "reason": "submit_blocked",
            "diagnostics": diag,
            "manufacturedUrl": False,
            "phase": gate.phase.value,
            "events": gate.events,
            "guiAttemptTimings": timings.to_dict(),
        }
        (ART / f"linux_search_{tag}_state.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
        return out

    diag["submitAt"] = time.time()
    diag["preSubmit"] = build_pre_submit_snapshot(
        current_url=pre_url or str(diag.get("urlBeforeSearch") or ""),
        page_title=pre_title or str(diag.get("titleBeforeSearch") or ""),
        query_expected=query,
        query_visibly_confirmed=True,
        search_field_geometry={
            "click": list(layout.get("field_click") or []),
            "center": list(layout.get("field_center") or []),
            "left": layout.get("field_left"),
            "right": layout.get("field_right"),
        },
        search_button_geometry={"xy": list(layout.get("search_btn") or []), "button": diag.get("button")},
        selected_category_label="All Categories",
        form_action=None,
        tab_count=None,
        modifier_key_state="clearmodifiers",
        fsm_state=gate.phase.value,
        focused_element_role="search_input_assumed",
        search_origin_url=pre_url or gate.search_origin_url,
        search_surface_class=gate.search_surface_class or str(origin_final.get("surfaceClass") or ""),
        search_scope_label="All Categories",
        search_surface_validated=True,
    )
    # Authoritative live-attempt boundary: emit BEFORE the submit keystroke.
    attempt_id = (
        str(diag.get("attemptId") or os.environ.get("CARDSCANR_LIVE_ATTEMPT_ID") or "").strip()
        or None
    )
    price_key_id = (
        str(diag.get("priceKeyId") or os.environ.get("CARDSCANR_PRICE_KEY_ID") or "").strip()
        or None
    )
    if attempt_id:
        try:
            submission_event = emit_search_submission_started(
                attempt_id=attempt_id,
                query=query,
                price_key_id=price_key_id,
            )
            diag["searchSubmissionStarted"] = submission_event.to_dict()
            timings.mark("T5_search_submission_started_event")
        except Exception as exc:
            out = {
                "ok": False,
                "error": f"SEARCH_SUBMISSION_EVENT_FAILED:{exc}",
                "attemptId": attempt_id,
                "manufacturedUrl": False,
                "phase": gate.phase.value,
                "events": gate.events,
                "guiAttemptTimings": timings.to_dict(),
                "submitted": False,
            }
            (ART / f"linux_search_{tag}_state.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
            return out

    if submit == "button":
        click_xy(*layout["search_btn"])
        diag["submit"] = "search_button_click"
    else:
        clear_modifiers()
        sh("xdotool key --clearmodifiers Return")
        diag["submit"] = "enter_in_field"
    timings.mark("T5_search_submitted")
    diag["submittedAt"] = time.time()

    result = wait_results(query)
    shot(ART / f"linux_search_{tag}_results.png")
    gate = on_search_post_submit_page(
        gate,
        title=str(result.get("title") or ""),
        url=str(result.get("url") or ""),
        results_ok=bool(result.get("ok")),
        expected_query=query,
        sold_control_available=False if result.get("ebayLive") else None,
    )
    if gate.phase == SearchPhase.ORDINARY_RESULTS_CONFIRMED or result.get("ordinaryResults"):
        timings.mark("T6_results_confirmed")
    diag["postNavigation"] = build_post_navigation_snapshot(
        resulting_url=str(result.get("url") or ""),
        title=str(result.get("title") or ""),
        main_document_status=None,
        redirect_count=None,
        route_classification=str(result.get("routeClass") or gate.route_class or ""),
        sorry_detected=bool(result.get("sorry")),
        challenge_detected=bool(result.get("challenge")),
        ordinary_results=bool(result.get("ordinaryResults")),
        ebay_live=bool(result.get("ebayLive")),
        sold_control_available=False if result.get("ebayLive") else None,
    )
    diag["guiAttemptTimings"] = timings.to_dict()
    out = {
        "ok": bool(result.get("ok")),
        "query": query,
        "searchMethod": "visible_ebay_search_input_x11",
        "submit": diag.get("submit"),
        "resultPageReached": bool(result.get("ok")),
        "url": result.get("url"),
        "title": result.get("title"),
        "sorry": bool(result.get("sorry")) or gate.phase
        in {SearchPhase.TEMPORARY_EBAY_SERVER_FAILURE, SearchPhase.EBAY_ACCESS_DENIED_403},
        "challenge": bool(result.get("challenge")) or gate.phase == SearchPhase.EBAY_CHALLENGE,
        "ebayLive": bool(result.get("ebayLive")) or gate.phase == SearchPhase.EBAY_LIVE_RESULTS,
        "alternateSurface": bool(result.get("alternateSurface"))
        or gate.phase in {SearchPhase.EBAY_LIVE_RESULTS, SearchPhase.ALTERNATE_EBAY_SURFACE},
        "ordinaryResults": bool(result.get("ordinaryResults"))
        or gate.phase == SearchPhase.ORDINARY_RESULTS_CONFIRMED,
        "aboutBlank": bool(result.get("aboutBlank")),
        "manufacturedUrl": False,
        "cdpDomUsed": False,
        "phase": gate.phase.value,
        "routeClass": result.get("routeClass") or gate.route_class,
        "events": gate.events,
        "diagnostics": diag,
        "waitSec": result.get("waitSec"),
        "queryVisibleConfirmed": bool(gate.query_visible),
        "submitted": bool(gate.submitted),
        "guiAttemptTimings": timings.to_dict(),
    }
    if out["sorry"]:
        out["error"] = TEMPORARY_EBAY_SERVER_FAILURE
        out["classification"] = TEMPORARY_EBAY_SERVER_FAILURE
    elif out["challenge"]:
        out["error"] = "EBAY_CHALLENGE_REQUIRED"
        out["classification"] = "EBAY_CHALLENGE_REQUIRED"
    elif out["aboutBlank"]:
        out["error"] = "ABOUT_BLANK_ABORT"
        out["classification"] = "ABOUT_BLANK_ABORT"
    elif out["ebayLive"] or gate.phase == SearchPhase.EBAY_LIVE_RESULTS:
        out["error"] = SOLD_UNAVAILABLE_ON_ALTERNATE_SURFACE
        out["classification"] = EBAY_LIVE_RESULTS
        out["ok"] = False
    elif gate.phase == SearchPhase.ALTERNATE_EBAY_SURFACE:
        out["error"] = ALTERNATE_EBAY_SURFACE
        out["classification"] = ALTERNATE_EBAY_SURFACE
        out["ok"] = False
    elif not out["ok"] and not out.get("error"):
        if gate.query_visible and gate.submitted:
            out["error"] = "search_results_not_confirmed"
            out["classification"] = "SEARCH_RESULTS_NOT_CONFIRMED"
        else:
            out["error"] = "SEARCH_INPUT_NOT_CONFIRMED"
            out["classification"] = "SEARCH_INPUT_NOT_CONFIRMED"
    (ART / f"linux_search_{tag}_state.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-check", action="store_true", help="Local readiness only; no eBay navigation")
    ap.add_argument("--query", required=False)
    ap.add_argument("--tag", default="run")
    ap.add_argument("--home", action="store_true")
    ap.add_argument("--submit", choices=("enter", "button"), default="enter")
    ap.add_argument("--attempt-id", default=None)
    ap.add_argument("--price-key-id", default=None)
    ap.add_argument(
        "--runtime-mode",
        choices=("COLD_START", "INTER_CARD"),
        default=None,
        help="Explicit readiness context; never inferred from open tabs",
    )
    ap.add_argument(
        "--pre-submit-only",
        action="store_true",
        help="Local tests only: type query then stop before SEARCH_SUBMISSION_STARTED. Requires CARDSCANR_PRE_SUBMIT_ONLY=1.",
    )
    args = ap.parse_args()
    if args.runtime_mode:
        os.environ["CARDSCANR_RUNTIME_MODE"] = str(args.runtime_mode)
    if args.self_check:
        out = run_self_check(runtime_mode=args.runtime_mode)
        print(json.dumps(out, indent=2))
        return 0 if out.get("ok") else 2
    if not args.query:
        print(json.dumps({"ok": False, "error": "query_required_unless_self_check"}, indent=2))
        return 2
    if args.attempt_id:
        os.environ["CARDSCANR_LIVE_ATTEMPT_ID"] = str(args.attempt_id)
    if args.price_key_id:
        os.environ["CARDSCANR_PRICE_KEY_ID"] = str(args.price_key_id)
    if args.runtime_mode:
        os.environ["CARDSCANR_RUNTIME_MODE"] = str(args.runtime_mode)
    try:
        out = gui_search(
            args.query,
            go_home=args.home,
            tag=args.tag,
            submit=args.submit,
            pre_submit_only=bool(args.pre_submit_only),
        )
        if args.attempt_id:
            out["attemptId"] = args.attempt_id
    except Exception as exc:
        out = {"ok": False, "error": str(exc), "manufacturedUrl": False}
        print(json.dumps(out, indent=2))
        return 2
    print(json.dumps(out, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
