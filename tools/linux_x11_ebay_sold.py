#!/usr/bin/env python3
"""X11 Sold activation: Ctrl+F locate → clear modifiers → left-rail click → pending wait."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image

sys.path.insert(0, "/mnt/d/cardscanr-data")
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from linux_x11_ebay_search import (  # noqa: E402
    ART,
    PREFIX,
    activate,
    chrome_geom,
    chrome_owner_count,
    clear_modifiers,
    click_xy,
    omnibox_url,
    sh,
    shot,
    title,
    _env,
)
from cardscanr_market_engine.providers.linux_x11_gui_fsm import (  # noqa: E402
    LEFT_RAIL_X_MAX,
    LEFT_RAIL_X_MIN,
    TEMPORARY_EBAY_SERVER_FAILURE,
    SoldGateState,
    SoldPhase,
    classify_post_sold_url,
    is_ebay_sorry_page,
    may_perform_browser_action,
    on_sold_clicked,
    on_sold_terminal,
    page_is_about_blank,
    sold_click_coords_valid,
)


def find_orange_highlight_left_rail(im: Image.Image, g: dict) -> tuple[int, int, int] | None:
    """Only accept Ctrl+F orange highlights inside the left filter rail."""
    hits = []
    x_min = max(LEFT_RAIL_X_MIN, g["x"] + 5)
    x_max = min(LEFT_RAIL_X_MAX, g["x"] + 300)
    y_min = g["y"] + 200
    y_max = g["y"] + g["h"] - 40
    for y in range(y_min, y_max, 2):
        for x in range(x_min, x_max, 2):
            r, gv, b = im.getpixel((x, y))
            if r > 220 and 80 < gv < 210 and b < 120 and r > gv and r > b + 40:
                hits.append((x, y))
            elif r > 240 and 140 < gv < 220 and 40 < b < 140:
                hits.append((x, y))
    if not hits:
        return None
    buckets: dict[tuple[int, int], list] = {}
    for x, y in hits:
        key = (x // 25, y // 12)
        buckets.setdefault(key, []).append((x, y))
    best = max(buckets.values(), key=len)
    cx = sum(p[0] for p in best) // len(best)
    cy = sum(p[1] for p in best) // len(best)
    if not sold_click_coords_valid(cx, cy, win_y=g["y"]):
        return None
    return cx, cy, len(best)


def close_about_blank_tab() -> None:
    clear_modifiers()
    t = title().lower()
    if "untitled" in t or "loading" in t:
        sh("xdotool key --clearmodifiers ctrl+w")
        time.sleep(0.7)
        clear_modifiers()


def wait_sold_pending(gate: SoldGateState, *, tag: str, timeout: int = 35) -> dict:
    """SOLD_NAVIGATION_PENDING: no omnibox/Ctrl+L until page looks settled."""
    t0 = time.time()
    last_title = ""
    while time.time() - t0 < timeout:
        if not may_perform_browser_action(gate, "ctrl_l"):
            pass  # enforced by not calling omnibox here
        time.sleep(0.45)
        last_title = title()
        tl = last_title.lower()
        # Detect new blank tab early — do NOT Ctrl+L
        if "untitled" in tl or tl.startswith("loading"):
            shot(ART / f"linux_sold_{tag}_blank_pending.png")
            close_about_blank_tab()
            gate = on_sold_terminal(gate, about_blank=True)
            return {
                "gate": gate,
                "url": "about:blank",
                "title": last_title,
                "verified": False,
                "aboutBlank": True,
                "terminal": SoldPhase.ABOUT_BLANK_ABORT.value,
            }
        if ("sorry" in tl and "ebay" in tl) or "error page" in tl:
            # Title-level SORRY / Error Page — settle then classify (not a focus failure)
            break
        if "captcha" in tl or "security measure" in tl or "verify yourself" in tl:
            break
        # Settled non-blank title: allow ONE omnibox read
        if "untitled" not in tl and "loading" not in tl and len(tl) > 5:
            # Prefer title signals that filter applied (result count change) — then read URL once
            if time.time() - t0 >= 2.0:
                break

    # Terminal URL read — only after pending wait (not during early race)
    if not may_perform_browser_action(gate, "begin_next_card"):
        pass
    # Ending pending for URL classification read is intentional after settle
    url = omnibox_url()
    title_now = title()
    classified = classify_post_sold_url(url, title_now)
    if classified["terminal"] == "ABOUT_BLANK_ABORT":
        gate = on_sold_terminal(gate, about_blank=True)
    elif classified["terminal"] == "EBAY_CHALLENGE":
        gate = on_sold_terminal(gate, challenge=True)
    elif classified["terminal"] == "EBAY_SORRY":
        gate = on_sold_terminal(gate, sorry=True)
    elif classified["verified"]:
        gate = on_sold_terminal(gate, verified=True)
    else:
        # Keep waiting a bit more with additional URL peeks
        for _ in range(12):
            time.sleep(1.0)
            if "untitled" in title().lower() or "loading" in title().lower():
                close_about_blank_tab()
                gate = on_sold_terminal(gate, about_blank=True)
                return {
                    "gate": gate,
                    "url": "about:blank",
                    "title": title(),
                    "verified": False,
                    "aboutBlank": True,
                    "terminal": SoldPhase.ABOUT_BLANK_ABORT.value,
                }
            url = omnibox_url()
            title_now = title()
            classified = classify_post_sold_url(url, title_now)
            if classified["verified"]:
                gate = on_sold_terminal(gate, verified=True)
                break
            if classified["terminal"] in {"EBAY_CHALLENGE", "EBAY_SORRY", "ABOUT_BLANK_ABORT"}:
                gate = on_sold_terminal(
                    gate,
                    challenge=classified["terminal"] == "EBAY_CHALLENGE",
                    sorry=classified["terminal"] == "EBAY_SORRY",
                    about_blank=classified["terminal"] == "ABOUT_BLANK_ABORT",
                )
                break
        else:
            gate = on_sold_terminal(gate, timeout=True)

    return {
        "gate": gate,
        "url": url,
        "title": title_now,
        "verified": gate.phase == SoldPhase.SOLD_STATE_VERIFIED,
        "aboutBlank": gate.phase == SoldPhase.ABOUT_BLANK_ABORT,
        "terminal": gate.phase.value,
    }


def copy_body() -> str:
    g = chrome_geom()
    click_xy(g["x"] + 600, g["y"] + 450)
    time.sleep(0.25)
    sh("xdotool key --clearmodifiers ctrl+a")
    time.sleep(0.3)
    sh("xdotool key --clearmodifiers ctrl+c")
    time.sleep(0.4)
    try:
        body = subprocess.check_output(
            [str(PREFIX / "root/usr/bin/xclip"), "-o", "-selection", "clipboard"],
            text=True,
            errors="replace",
        )
    except Exception:
        body = ""
    sh("xdotool key --clearmodifiers Escape")
    time.sleep(0.15)
    clear_modifiers()
    return body


def gui_sold(*, tag: str) -> dict:
    _env()
    gate = SoldGateState()
    g = chrome_geom()
    activate(g)
    timeline: list[dict] = []

    def mark(event: str, **extra):
        timeline.append({"t": time.time(), "event": event, **extra})

    url0 = omnibox_url()
    t0 = title()
    mark("url_before", url=url0, title=t0, owners=chrome_owner_count())
    if page_is_about_blank(url0, t0):
        gate = on_sold_terminal(gate, about_blank=True)
        out = {
            "ok": False,
            "error": "ABOUT_BLANK_ABORT",
            "SOLD_STATE_VERIFIED": False,
            "url": url0,
            "title": t0,
            "phase": gate.phase.value,
            "timeline": timeline,
        }
        (ART / f"linux_sold_{tag}_state.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
        return out

    if "LH_Sold=1" in url0:
        body = copy_body()
        gate = on_sold_terminal(gate, verified=True)
        out = {
            "ok": True,
            "alreadySold": True,
            "soldClickSuccess": False,
            "SOLD_STATE_VERIFIED": True,
            "url": url0,
            "title": t0,
            "aboutBlank": False,
            "phase": gate.phase.value,
            "bodyChars": len(body),
            "timeline": timeline,
            "events": gate.events,
        }
        (ART / f"linux_sold_{tag}_state.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
        return out

    clear_modifiers()
    sh("xdotool key --clearmodifiers Escape")
    time.sleep(0.1)
    mark("ctrl_f")
    sh("xdotool key --clearmodifiers ctrl+f")
    time.sleep(0.25)
    Path("/tmp/find_sold.txt").write_text("Sold items", encoding="utf-8")
    subprocess.check_call(["bash", "-lc", "xclip -selection clipboard < /tmp/find_sold.txt"])
    sh("xdotool key --clearmodifiers ctrl+v")
    time.sleep(0.35)
    clear_modifiers()
    im = shot(ART / f"linux_sold_{tag}_find.png")
    g = chrome_geom()
    hl = find_orange_highlight_left_rail(im, g)
    if not hl or hl[2] < 4:
        for n in range(4):
            sh("xdotool key --clearmodifiers Return")
            time.sleep(0.28)
            clear_modifiers()
            im = shot(ART / f"linux_sold_{tag}_find{n}.png")
            hl = find_orange_highlight_left_rail(im, g)
            if hl and hl[2] >= 4:
                break
    if not hl:
        out = {
            "ok": False,
            "error": "sold_items_highlight_not_in_left_rail",
            "soldClickSuccess": False,
            "SOLD_STATE_VERIFIED": False,
            "url": omnibox_url(),
            "title": title(),
            "timeline": timeline,
        }
        (ART / f"linux_sold_{tag}_state.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
        return out

    cx, cy, n = hl
    mark("sold_located", xy=[cx, cy], hits=n)
    stage_marks = {"T7_sold_control_located": time.time()}
    if not sold_click_coords_valid(cx, cy, win_y=g["y"]):
        out = {
            "ok": False,
            "error": "sold_click_outside_left_rail",
            "click": [cx, cy],
            "SOLD_STATE_VERIFIED": False,
            "timeline": timeline,
            "guiAttemptTimings": {"marks": stage_marks},
        }
        (ART / f"linux_sold_{tag}_state.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
        return out

    # Close find bar, CLEAR MODIFIERS (prevents Ctrl+Click → new about:blank tab)
    sh("xdotool key --clearmodifiers Escape")
    time.sleep(0.18)
    clear_modifiers()
    time.sleep(0.08)
    mark("modifiers_cleared_pre_click")
    gate.phase = SoldPhase.SOLD_LOCATED
    gate.phase = SoldPhase.SOLD_CONTROL_AVAILABLE

    # Physical click on visible Sold items — never Ctrl+Click
    clear_modifiers()
    sh(f"xdotool mousemove {cx} {cy}")
    sh("xdotool click --clearmodifiers 1")
    mark("sold_clicked", xy=[cx, cy])
    stage_marks["T8_sold_activated"] = time.time()
    gate = on_sold_clicked(gate)

    pending = wait_sold_pending(gate, tag=tag, timeout=35)
    gate = pending["gate"]
    mark("pending_done", terminal=pending["terminal"], url=pending.get("url"), title=pending.get("title"))
    if pending.get("verified"):
        stage_marks["T9_sold_state_verified"] = time.time()

    body = ""
    if pending["verified"]:
        body = copy_body()
        (ART / f"linux_sold_{tag}_body.txt").write_text(body, encoding="utf-8", errors="replace")
        stage_marks["T10_html_data_captured"] = time.time()

    shot(ART / f"linux_sold_{tag}_after.png")
    out = {
        "ok": bool(pending["verified"]),
        "soldClickSuccess": True,
        "SOLD_STATE_VERIFIED": bool(pending["verified"]),
        "click": [cx, cy],
        "url": pending.get("url"),
        "title": pending.get("title"),
        "sorry": gate.phase
        in {SoldPhase.EBAY_SORRY, SoldPhase.TEMPORARY_EBAY_SERVER_FAILURE}
        or is_ebay_sorry_page(title=str(pending.get("title") or ""), url=str(pending.get("url") or "")),
        "challenge": gate.phase == SoldPhase.EBAY_CHALLENGE,
        "aboutBlank": bool(pending.get("aboutBlank")),
        "bodyChars": len(body),
        "lhSoldInjected": False,
        "activation": "x11_mouse_click_on_visible_sold_items",
        "locateAid": "ctrl+f_sold_items_left_rail_only",
        "phase": gate.phase.value,
        "events": gate.events,
        "timeline": timeline,
        "guiAttemptTimings": {"marks": stage_marks},
    }
    if out["sorry"]:
        out["error"] = TEMPORARY_EBAY_SERVER_FAILURE
        out["classification"] = TEMPORARY_EBAY_SERVER_FAILURE
    elif not out["ok"]:
        out["error"] = pending["terminal"]
    (ART / f"linux_sold_{tag}_state.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


def run_self_check(*, runtime_mode: str | None = None) -> dict:
    """Local-only Sold-tool readiness. Never opens eBay or clicks Sold."""
    _env()
    # Reuse search self-check core (imports, DISPLAY, xdotool, CDP inspect).
    from linux_x11_ebay_search import run_self_check as search_self_check

    base = search_self_check(runtime_mode=runtime_mode)
    details = dict(base.get("details") or {})
    details["soldHelpers"] = {
        "find_orange_highlight_left_rail": callable(find_orange_highlight_left_rail),
        "sold_click_coords_valid": True,
        "gui_sold_entry": callable(gui_sold),
    }
    ok = bool(base.get("ok"))
    return {
        "ok": ok,
        "selfCheck": "linux_x11_ebay_sold",
        "soldSelfCheck": ok,
        "errors": list(base.get("errors") or []),
        "details": details,
        "ebayNavigationPerformed": False,
        "soldClickPerformed": False,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-check", action="store_true", help="Local readiness only; no eBay navigation")
    ap.add_argument("--tag", default="sold")
    ap.add_argument(
        "--runtime-mode",
        choices=("COLD_START", "INTER_CARD"),
        default=None,
        help="Explicit readiness context; never inferred from open tabs",
    )
    args = ap.parse_args()
    if args.self_check:
        out = run_self_check(runtime_mode=args.runtime_mode)
        print(json.dumps(out, indent=2))
        return 0 if out.get("ok") else 2
    try:
        out = gui_sold(tag=args.tag)
    except Exception as exc:
        out = {"ok": False, "error": str(exc), "SOLD_STATE_VERIFIED": False}
        print(json.dumps(out, indent=2))
        return 2
    print(json.dumps(out, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
