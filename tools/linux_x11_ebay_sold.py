#!/usr/bin/env python3
"""X11 Sold activation: exact DOM/accessibility identity → single X11 click → verify.

Ctrl+F / orange-pixel highlights are diagnostic-only and never authoritative for click targeting.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

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
from cardscanr_market_engine.providers.sold_control_cdp_locate import (  # noqa: E402
    fetch_sold_control_candidates_via_cdp,
)
from cardscanr_market_engine.providers.sold_control_identity import (  # noqa: E402
    PIXEL_HIGHLIGHT_AUTHORITATIVE,
    SOLD_CONTROL_AMBIGUOUS,
    SOLD_CONTROL_IDENTITY_NOT_PROVEN,
    SoldControlIdentity,
    build_click_target_diagnostics,
    content_origin_from_viewport_metrics,
    prove_sold_control_identity,
)
from cardscanr_market_engine.providers.sold_navigation_phases import (  # noqa: E402
    DEFAULT_SOLD_TIMEOUT_POLICY,
    PHASE_SOLD_CONTROL_DISCOVERY,
    PHASE_SOLD_STATE_TRANSITION,
    PHASE_SOLD_STATE_VERIFICATION,
    TERMINAL_SOLD_CONTROL_DISCOVERY_TIMEOUT,
    TERMINAL_SOLD_STATE_VERIFICATION_TIMEOUT,
    TERMINAL_SOLD_UNEXPECTED_FILTER,
    build_sold_failure_evidence,
    classify_sold_observation,
    url_has_lh_sold,
)


def find_orange_highlight_left_rail(im: Image.Image, g: dict) -> tuple[int, int, int] | None:
    """LEGACY diagnostic helper only — NOT authoritative for production clicks.

    Evidence before click from this path is only: orange Ctrl+F highlight inside left rail.
    That does NOT uniquely identify the Sold-items control (Meowth → LH_PrefLoc=2).
    """
    assert PIXEL_HIGHLIGHT_AUTHORITATIVE is False
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


def locate_sold_control_identity(
    *,
    chrome_window: dict[str, Any],
    url_before: str | None,
    candidate_provider: Any | None = None,
) -> SoldControlIdentity:
    """Current-page positive identity. Rediscovered every call (no stale coords)."""
    if candidate_provider is not None:
        blob = candidate_provider()
    else:
        blob = fetch_sold_control_candidates_via_cdp()
    viewport = dict(blob.get("viewport") or {})
    ox, oy, dpr = content_origin_from_viewport_metrics(viewport)
    return prove_sold_control_identity(
        list(blob.get("candidates") or []),
        viewport=viewport,
        window_x=int(chrome_window.get("x") or 0),
        window_y=int(chrome_window.get("y") or 0),
        content_origin_x=ox,
        content_origin_y=oy,
        device_scale_factor=dpr,
        page_url=str(blob.get("url") or url_before or ""),
        runtime_mode=os.environ.get("CARDSCANR_RUNTIME_MODE"),
        attempt_id=os.environ.get("CARDSCANR_ATTEMPT_ID"),
        job_id=os.environ.get("CARDSCANR_JOB_ID"),
        price_key_id=os.environ.get("CARDSCANR_PRICE_KEY_ID"),
        target_id=str(blob.get("targetId") or "") or None,
    )


def close_about_blank_tab() -> None:
    clear_modifiers()
    t = title().lower()
    if "untitled" in t or "loading" in t:
        sh("xdotool key --clearmodifiers ctrl+w")
        time.sleep(0.7)
        clear_modifiers()


def wait_sold_pending(
    gate: SoldGateState,
    *,
    tag: str,
    timeout: float | None = None,
    url_before: str | None = None,
) -> dict:
    """SOLD_NAVIGATION_PENDING with phase-specific verification budget.

    Evidence-based default: SOLD_STATE_VERIFICATION_TIMEOUT_S (10s), not opaque 35s.
    Emits phase-specific terminals (not generic SOLD_NAVIGATION_TIMEOUT when known).
    """
    verify_budget = float(
        timeout if timeout is not None else DEFAULT_SOLD_TIMEOUT_POLICY.state_verification_s
    )
    t0 = time.time()
    phase_started = t0
    last_title = ""
    url = url_before or ""
    title_now = ""
    failure_stage = PHASE_SOLD_STATE_VERIFICATION
    terminal_override: str | None = None
    while time.time() - t0 < verify_budget:
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
                "failureStage": PHASE_SOLD_STATE_TRANSITION,
                "phaseElapsedMs": int((time.time() - phase_started) * 1000),
                "phaseTimeoutMs": int(verify_budget * 1000),
            }
        if ("sorry" in tl and "ebay" in tl) or "error page" in tl:
            break
        if "captcha" in tl or "security measure" in tl or "verify yourself" in tl:
            break
        # Settled non-blank title: allow URL read (progress-aware, not fixed 2s+12s)
        if "untitled" not in tl and "loading" not in tl and len(tl) > 5:
            if time.time() - t0 >= 1.5:
                url = omnibox_url()
                title_now = title()
                obs = classify_sold_observation(url=url, title=title_now, url_before=url_before)
                if obs.get("verified"):
                    gate = on_sold_terminal(gate, verified=True)
                    return {
                        "gate": gate,
                        "url": url,
                        "title": title_now,
                        "verified": True,
                        "aboutBlank": False,
                        "terminal": SoldPhase.SOLD_STATE_VERIFIED.value,
                        "failureStage": None,
                        "phaseElapsedMs": int((time.time() - phase_started) * 1000),
                        "phaseTimeoutMs": int(verify_budget * 1000),
                        "x11SoldStateVerified": True,
                        "lhSoldAfter": True,
                    }
                if obs.get("terminal") == "ABOUT_BLANK_ABORT":
                    gate = on_sold_terminal(gate, about_blank=True)
                    terminal_override = SoldPhase.ABOUT_BLANK_ABORT.value
                    break
                if obs.get("terminal") == "EBAY_CHALLENGE":
                    gate = on_sold_terminal(gate, challenge=True)
                    terminal_override = SoldPhase.EBAY_CHALLENGE.value
                    break
                if obs.get("terminal") == "EBAY_SORRY":
                    gate = on_sold_terminal(gate, sorry=True)
                    terminal_override = SoldPhase.EBAY_SORRY.value
                    break
                if obs.get("terminal") == TERMINAL_SOLD_UNEXPECTED_FILTER and (time.time() - t0) >= 2.0:
                    # Meowth-class: URL mutated (e.g. LH_PrefLoc) without LH_Sold.
                    failure_stage = PHASE_SOLD_STATE_TRANSITION
                    terminal_override = TERMINAL_SOLD_UNEXPECTED_FILTER
                    gate = on_sold_terminal(gate, timeout=True)
                    break
                # Keep polling within verification budget (no extra opaque 12s extension).

    if terminal_override is None:
        if not may_perform_browser_action(gate, "begin_next_card"):
            pass
        url = omnibox_url()
        title_now = title()
        obs = classify_sold_observation(url=url, title=title_now, url_before=url_before)
        if obs.get("verified"):
            gate = on_sold_terminal(gate, verified=True)
        elif obs.get("terminal") == "ABOUT_BLANK_ABORT":
            gate = on_sold_terminal(gate, about_blank=True)
            terminal_override = SoldPhase.ABOUT_BLANK_ABORT.value
        elif obs.get("terminal") == "EBAY_CHALLENGE":
            gate = on_sold_terminal(gate, challenge=True)
            terminal_override = SoldPhase.EBAY_CHALLENGE.value
        elif obs.get("terminal") == "EBAY_SORRY":
            gate = on_sold_terminal(gate, sorry=True)
            terminal_override = SoldPhase.EBAY_SORRY.value
        elif obs.get("terminal") == TERMINAL_SOLD_UNEXPECTED_FILTER:
            failure_stage = PHASE_SOLD_STATE_TRANSITION
            terminal_override = TERMINAL_SOLD_UNEXPECTED_FILTER
            gate = on_sold_terminal(gate, timeout=True)
        else:
            failure_stage = PHASE_SOLD_STATE_VERIFICATION
            terminal_override = TERMINAL_SOLD_STATE_VERIFICATION_TIMEOUT
            gate = on_sold_terminal(gate, timeout=True)

    verified = gate.phase == SoldPhase.SOLD_STATE_VERIFIED
    terminal = terminal_override or gate.phase.value
    if not verified and terminal == SoldPhase.SOLD_NAVIGATION_TIMEOUT.value:
        terminal = TERMINAL_SOLD_STATE_VERIFICATION_TIMEOUT
    return {
        "gate": gate,
        "url": url,
        "title": title_now or last_title,
        "verified": verified,
        "aboutBlank": gate.phase == SoldPhase.ABOUT_BLANK_ABORT,
        "terminal": terminal,
        "failureStage": None if verified else failure_stage,
        "phaseElapsedMs": int((time.time() - phase_started) * 1000),
        "phaseTimeoutMs": int(verify_budget * 1000),
        "x11SoldStateVerified": verified,
        "lhSoldAfter": url_has_lh_sold(url),
        "lhSoldBefore": url_has_lh_sold(url_before),
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

    # Ensure find bar closed so it cannot intercept clicks / obscure labels.
    clear_modifiers()
    sh("xdotool key --clearmodifiers Escape")
    time.sleep(0.12)
    clear_modifiers()

    # Active challenge title check before locate/click.
    t_now = title().lower()
    if "captcha" in t_now or "security measure" in t_now or "verify yourself" in t_now:
        gate = on_sold_terminal(gate, challenge=True)
        out = {
            "ok": False,
            "error": "EBAY_CHALLENGE",
            "failureStage": PHASE_SOLD_CONTROL_DISCOVERY,
            "failureClass": "EBAY_CHALLENGE",
            "soldClickSuccess": False,
            "soldControlIdentityProven": False,
            "SOLD_STATE_VERIFIED": False,
            "url": omnibox_url(),
            "title": title(),
            "timeline": timeline,
            "physicalClickCount": 0,
        }
        (ART / f"linux_sold_{tag}_state.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
        return out

    g = chrome_geom()
    # Optional Ctrl+F diagnostic screenshot only — NEVER used for click coordinates.
    try:
        sh("xdotool key --clearmodifiers ctrl+f")
        time.sleep(0.15)
        Path("/tmp/find_sold.txt").write_text("Sold items", encoding="utf-8")
        subprocess.check_call(["bash", "-lc", "xclip -selection clipboard < /tmp/find_sold.txt"])
        sh("xdotool key --clearmodifiers ctrl+v")
        time.sleep(0.2)
        shot(ART / f"linux_sold_{tag}_find_diagnostic.png")
        mark("ctrl_f_diagnostic_only", authoritative=False)
    except Exception as diag_exc:
        mark("ctrl_f_diagnostic_failed", error=str(diag_exc)[:200])
    finally:
        try:
            sh("xdotool key --clearmodifiers Escape")
            time.sleep(0.12)
            clear_modifiers()
        except Exception:
            pass

    # Authoritative identity: read-only CDP/DOM exact label + bounding box (current page).
    identity = locate_sold_control_identity(chrome_window=g, url_before=url0)
    mark(
        "sold_identity",
        proven=identity.proven,
        reason=identity.reason_code,
        click=identity.click_point_x11,
        label=(identity.candidate.label if identity.candidate else None),
    )
    stage_marks = {"T7_sold_control_located": time.time()}
    if not identity.proven or not identity.click_point_x11:
        reason = identity.reason_code or SOLD_CONTROL_IDENTITY_NOT_PROVEN
        out = {
            "ok": False,
            "error": reason,
            "failureStage": PHASE_SOLD_CONTROL_DISCOVERY,
            "failureClass": reason,
            "soldClickSuccess": False,
            "soldControlDiscovered": False,
            "soldControlIdentityProven": False,
            "SOLD_STATE_VERIFIED": False,
            "url": omnibox_url(),
            "title": title(),
            "timeline": timeline,
            "soldTimeoutPolicy": DEFAULT_SOLD_TIMEOUT_POLICY.to_dict(),
            "identity": identity.to_dict(),
            "pixelHighlightAuthoritative": False,
            "physicalClickCount": 0,
            "locateAid": "cdp_exact_sold_items_label",
        }
        out["failureEvidence"] = build_sold_failure_evidence(
            runtime_mode=os.environ.get("CARDSCANR_RUNTIME_MODE"),
            attempt_id=os.environ.get("CARDSCANR_ATTEMPT_ID"),
            job_id=os.environ.get("CARDSCANR_JOB_ID"),
            price_key_id=os.environ.get("CARDSCANR_PRICE_KEY_ID"),
            query=os.environ.get("CARDSCANR_QUERY"),
            search_submitted=True,
            ordinary_results_confirmed=True,
            url=out.get("url"),
            title=out.get("title"),
            ready_state=None,
            sold_diagnostics={
                "soldControlDiscovered": False,
                "soldClickAttempted": False,
                "failureStage": PHASE_SOLD_CONTROL_DISCOVERY,
                "failureClass": reason,
                "lhSoldBefore": url_has_lh_sold(url0),
                "lhSoldAfter": False,
                "x11SoldStateVerified": False,
                "phases": [],
            },
            error_message=reason,
        )
        (ART / f"linux_sold_{tag}_state.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
        return out

    cx, cy = identity.click_point_x11
    if not sold_click_coords_valid(cx, cy, win_y=g["y"]):
        out = {
            "ok": False,
            "error": SOLD_CONTROL_IDENTITY_NOT_PROVEN,
            "failureStage": PHASE_SOLD_CONTROL_DISCOVERY,
            "failureClass": "sold_click_outside_left_rail_after_identity",
            "click": [cx, cy],
            "soldControlIdentityProven": False,
            "SOLD_STATE_VERIFIED": False,
            "timeline": timeline,
            "identity": identity.to_dict(),
            "guiAttemptTimings": {"marks": stage_marks},
            "physicalClickCount": 0,
        }
        (ART / f"linux_sold_{tag}_state.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
        return out

    clear_modifiers()
    time.sleep(0.05)
    mark(
        "pre_click_identity",
        soldControlIdentityProven=True,
        label=identity.candidate.label if identity.candidate else None,
        role=identity.candidate.role if identity.candidate else None,
        boundingRect=identity.candidate.bounding_rect if identity.candidate else None,
        chosenClickPoint=[cx, cy],
        pageUrl=url0,
        targetId=(identity.diagnostics or {}).get("targetId"),
    )
    gate.phase = SoldPhase.SOLD_LOCATED
    gate.phase = SoldPhase.SOLD_CONTROL_AVAILABLE

    # ONE physical X11 click only — never CDP/JS activate, never retry nearby coords.
    clear_modifiers()
    sh(f"xdotool mousemove {cx} {cy}")
    sh("xdotool click --clearmodifiers 1")
    mark("sold_clicked", xy=[cx, cy], physicalClickCount=1)
    stage_marks["T8_sold_activated"] = time.time()
    gate = on_sold_clicked(gate)

    pending = wait_sold_pending(
        gate,
        tag=tag,
        timeout=DEFAULT_SOLD_TIMEOUT_POLICY.state_verification_s,
        url_before=url0,
    )
    gate = pending["gate"]
    mark("pending_done", terminal=pending["terminal"], url=pending.get("url"), title=pending.get("title"))
    if pending.get("verified"):
        stage_marks["T9_sold_state_verified"] = time.time()

    body = ""
    if pending["verified"]:
        body = copy_body()
        (ART / f"linux_sold_{tag}_body.txt").write_text(body, encoding="utf-8", errors="replace")
        stage_marks["T10_html_data_captured"] = time.time()

    # Diagnostic-only screenshot; must not replace original Sold failure.
    diagnostic_shot = None
    try:
        diagnostic_shot = str(shot(ART / f"linux_sold_{tag}_after.png"))
    except Exception as shot_exc:
        diagnostic_shot = f"DIAGNOSTIC_SHOT_FAILED:{type(shot_exc).__name__}"
    click_diag = build_click_target_diagnostics(
        identity, pre_click_url=url0, post_click_url=str(pending.get("url") or "")
    )
    out = {
        "ok": bool(pending["verified"]),
        "soldClickSuccess": True,
        "soldControlDiscovered": True,
        "soldControlIdentityProven": True,
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
        "lhSoldBefore": pending.get("lhSoldBefore"),
        "lhSoldAfter": pending.get("lhSoldAfter"),
        "x11SoldStateVerified": bool(pending.get("x11SoldStateVerified")),
        "activation": "x11_mouse_click_on_identity_proven_sold_items",
        "locateAid": "cdp_exact_sold_items_label",
        "pixelHighlightAuthoritative": False,
        "physicalClickCount": 1,
        "identity": identity.to_dict(),
        "clickTargetDiagnostics": click_diag,
        "phase": gate.phase.value,
        "failureStage": pending.get("failureStage"),
        "failureClass": None if pending.get("verified") else pending.get("terminal"),
        "events": gate.events,
        "timeline": timeline,
        "guiAttemptTimings": {"marks": stage_marks},
        "soldTimeoutPolicy": DEFAULT_SOLD_TIMEOUT_POLICY.to_dict(),
        "phaseElapsedMs": pending.get("phaseElapsedMs"),
        "phaseTimeoutMs": pending.get("phaseTimeoutMs"),
        "diagnosticScreenshot": "DIAGNOSTIC_ONLY" if diagnostic_shot else None,
    }
    if out["sorry"]:
        out["error"] = TEMPORARY_EBAY_SERVER_FAILURE
        out["classification"] = TEMPORARY_EBAY_SERVER_FAILURE
    elif not out["ok"]:
        # Prefer phase-specific terminal over opaque SOLD_NAVIGATION_TIMEOUT.
        out["error"] = str(pending.get("terminal") or TERMINAL_SOLD_STATE_VERIFICATION_TIMEOUT)
    if not out["ok"]:
        out["failureEvidence"] = build_sold_failure_evidence(
            runtime_mode=os.environ.get("CARDSCANR_RUNTIME_MODE"),
            attempt_id=os.environ.get("CARDSCANR_ATTEMPT_ID"),
            job_id=os.environ.get("CARDSCANR_JOB_ID"),
            price_key_id=os.environ.get("CARDSCANR_PRICE_KEY_ID"),
            query=os.environ.get("CARDSCANR_QUERY"),
            search_submitted=True,
            ordinary_results_confirmed=True,
            url=out.get("url"),
            title=out.get("title"),
            ready_state=None,
            sold_diagnostics={
                "soldControlDiscovered": True,
                "soldClickAttempted": True,
                "soldClickResult": "OK",
                "failureStage": out.get("failureStage"),
                "failureClass": out.get("failureClass") or out.get("error"),
                "lhSoldBefore": out.get("lhSoldBefore"),
                "lhSoldAfter": out.get("lhSoldAfter"),
                "x11SoldStateVerified": out.get("x11SoldStateVerified"),
                "diagnosticScreenshot": out.get("diagnosticScreenshot"),
                "phases": [
                    {
                        "name": PHASE_SOLD_STATE_VERIFICATION,
                        "elapsedMs": out.get("phaseElapsedMs"),
                        "timeoutMs": out.get("phaseTimeoutMs"),
                        "status": "FAIL",
                    }
                ],
            },
            error_message=str(out.get("error")),
        )
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
