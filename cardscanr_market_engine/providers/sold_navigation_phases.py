#!/usr/bin/env python3
"""Phase-aware Sold navigation policy and offline fixture runner.

Evidence-based timeouts from successful AU Sold timings (five-card / canary):
  click→verified median ~3.3s, p90 ~3.4s, max ~5.4s
  ordinary→Sold control p90 ~5.0s, max ~6.0s

No live eBay network. Fixtures drive URL/title clocks only.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .linux_x11_gui_fsm import (
    SoldPhase,
    classify_post_sold_url,
    is_ebay_challenge_page,
    is_ebay_sorry_page,
    page_is_about_blank,
)
from .sold_page_health import (
    EBAY_ERROR_PAGE,
    PHASE_PAGE_HEALTH_VERIFICATION,
    evaluate_sold_verification,
)

# Evidence-based finite budgets (seconds). Not unlimited.
SOLD_CONTROL_DISCOVERY_TIMEOUT_S = 12.0
SOLD_CONTROL_CLICK_TIMEOUT_S = 5.0
SOLD_STATE_TRANSITION_TIMEOUT_S = 8.0
SOLD_STATE_VERIFICATION_TIMEOUT_S = 10.0

# Legacy opaque wait (historical Meowth path). Kept for audit only.
LEGACY_SOLD_NAVIGATION_PENDING_TIMEOUT_S = 35.0

PHASE_SOLD_CONTROL_DISCOVERY = "SOLD_CONTROL_DISCOVERY"
PHASE_SOLD_CONTROL_CLICK = "SOLD_CONTROL_CLICK"
PHASE_SOLD_STATE_TRANSITION = "SOLD_STATE_TRANSITION"
PHASE_SOLD_STATE_VERIFICATION = "SOLD_STATE_VERIFICATION"

TERMINAL_SOLD_STATE_VERIFIED = "SOLD_STATE_VERIFIED"
TERMINAL_SOLD_CONTROL_DISCOVERY_TIMEOUT = "SOLD_CONTROL_DISCOVERY_TIMEOUT"
TERMINAL_SOLD_CLICK_FAILURE = "SOLD_CLICK_FAILURE"
TERMINAL_SOLD_STATE_VERIFICATION_TIMEOUT = "SOLD_STATE_VERIFICATION_TIMEOUT"
TERMINAL_SOLD_UNEXPECTED_FILTER = "SOLD_UNEXPECTED_FILTER_TRANSITION"
TERMINAL_SOLD_CHALLENGE = "EBAY_CHALLENGE"
TERMINAL_SOLD_SORRY = "EBAY_SORRY"
TERMINAL_SOLD_ERROR_PAGE = EBAY_ERROR_PAGE
TERMINAL_ABOUT_BLANK = "ABOUT_BLANK_ABORT"

CONTROL_PLANE_PERSISTENCE_FAILURE = "CONTROL_PLANE_PERSISTENCE_FAILURE"


@dataclass
class PhaseRecord:
    name: str
    started_at: float
    timeout_ms: int
    status: str = "RUNNING"
    reason_code: str | None = None
    elapsed_ms: int = 0
    extras: dict[str, Any] = field(default_factory=dict)

    def finish(self, *, status: str, reason_code: str | None = None, now: float | None = None) -> None:
        t = now if now is not None else time.time()
        self.elapsed_ms = int(max(0.0, (t - self.started_at) * 1000.0))
        self.status = status
        self.reason_code = reason_code

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "startedAt": self.started_at,
            "elapsedMs": self.elapsed_ms,
            "timeoutMs": self.timeout_ms,
            "status": self.status,
            "reasonCode": self.reason_code,
            **({"extras": self.extras} if self.extras else {}),
        }


@dataclass
class SoldTimeoutPolicy:
    control_discovery_s: float = SOLD_CONTROL_DISCOVERY_TIMEOUT_S
    control_click_s: float = SOLD_CONTROL_CLICK_TIMEOUT_S
    state_transition_s: float = SOLD_STATE_TRANSITION_TIMEOUT_S
    state_verification_s: float = SOLD_STATE_VERIFICATION_TIMEOUT_S

    def to_dict(self) -> dict[str, Any]:
        return {
            "controlDiscoverySeconds": self.control_discovery_s,
            "controlClickSeconds": self.control_click_s,
            "stateTransitionSeconds": self.state_transition_s,
            "stateVerificationSeconds": self.state_verification_s,
            "legacyOpaquePendingSeconds": LEGACY_SOLD_NAVIGATION_PENDING_TIMEOUT_S,
            "evidenceBasis": {
                "clickToVerifiedMedianMs": 3346,
                "clickToVerifiedP90Ms": 3358,
                "clickToVerifiedMaxMs": 5371,
                "ordinaryToControlP90Ms": 4988,
                "headroomPolicy": "finite_stage_budgets_above_p90",
            },
        }


DEFAULT_SOLD_TIMEOUT_POLICY = SoldTimeoutPolicy()


def url_has_lh_sold(url: str | None) -> bool:
    return "lh_sold=1" in str(url or "").lower()


def url_has_unexpected_filter(url: str | None, *, url_before: str | None = None) -> bool:
    """True when URL mutated to a non-Sold filter (e.g. LH_PrefLoc) without LH_Sold."""
    u = str(url or "").lower()
    if url_has_lh_sold(u):
        return False
    before = str(url_before or "").lower()
    if u == before:
        return False
    markers = ("lh_prefloc=", "lh_itemcondition=", "lh_bin=", "lh_auction=", "lh_complete=")
    return any(m in u for m in markers) and not any(m in before for m in markers if m != "lh_complete=")


def classify_sold_observation(
    *,
    url: str | None,
    title: str | None,
    url_before: str | None = None,
    body: str | None = None,
) -> dict[str, Any]:
    """Classify post-Sold observation.

    FILTER_STATE_CHECKS: LH_Sold=1 / unexpected filter transition.
    PAGE_HEALTH_CHECKS: title/host/error/sorry/challenge (via evaluate_sold_verification).
    x11SoldStateVerified requires soldFilterStateVerified AND soldPageHealthVerified.
    """
    ev = evaluate_sold_verification(url=url, title=title, body=body, url_before=url_before)
    base = {
        "soldFilterStateVerified": bool(ev.get("soldFilterStateVerified")),
        "soldPageHealthVerified": bool(ev.get("soldPageHealthVerified")),
        "x11SoldStateVerified": bool(ev.get("x11SoldStateVerified")),
        "marketplacePageClass": ev.get("marketplacePageClass"),
        "captureReady": bool(ev.get("captureReady")),
        "lhSold": bool(ev.get("soldFilterStateVerified")),
    }
    if ev.get("verified"):
        return {
            **base,
            "terminal": TERMINAL_SOLD_STATE_VERIFIED,
            "verified": True,
            "phase": SoldPhase.SOLD_STATE_VERIFIED.value,
        }
    term = ev.get("terminal")
    if term == TERMINAL_ABOUT_BLANK or page_is_about_blank(url, title):
        return {
            **base,
            "terminal": TERMINAL_ABOUT_BLANK,
            "verified": False,
            "phase": SoldPhase.ABOUT_BLANK_ABORT.value,
        }
    if term == TERMINAL_SOLD_CHALLENGE or is_ebay_challenge_page(title=title, url=url, body=body):
        return {
            **base,
            "terminal": TERMINAL_SOLD_CHALLENGE,
            "verified": False,
            "phase": SoldPhase.EBAY_CHALLENGE.value,
        }
    if term == TERMINAL_SOLD_ERROR_PAGE:
        return {
            **base,
            "terminal": TERMINAL_SOLD_ERROR_PAGE,
            "verified": False,
            "phase": PHASE_PAGE_HEALTH_VERIFICATION,
            # Keep polling within verification budget (transient → healthy possible).
            "stableUnhealthyPending": True,
        }
    if term == TERMINAL_SOLD_SORRY or is_ebay_sorry_page(title=title, url=url, body=body):
        return {
            **base,
            "terminal": TERMINAL_SOLD_SORRY,
            "verified": False,
            "phase": PHASE_PAGE_HEALTH_VERIFICATION,
            "stableUnhealthyPending": True,
        }
    if term == TERMINAL_SOLD_UNEXPECTED_FILTER or url_has_unexpected_filter(url, url_before=url_before):
        return {
            **base,
            "terminal": TERMINAL_SOLD_UNEXPECTED_FILTER,
            "verified": False,
            "phase": PHASE_SOLD_STATE_TRANSITION,
            "reason": "url_mutated_without_lh_sold",
        }
    if term in {
        "SOLD_UNAVAILABLE_ON_ALTERNATE_SURFACE",
        "EBAY_ACCESS_DENIED_403",
    }:
        return {**base, "terminal": str(term), "verified": False, "phase": str(term)}
    # Fallback through legacy classify for alternate surfaces.
    classified = classify_post_sold_url(str(url or ""), str(title or ""), body)
    if classified.get("verified"):
        return {
            **base,
            "terminal": TERMINAL_SOLD_STATE_VERIFIED,
            "verified": True,
            "phase": SoldPhase.SOLD_STATE_VERIFIED.value,
            "x11SoldStateVerified": True,
            "soldFilterStateVerified": True,
            "soldPageHealthVerified": True,
            "captureReady": True,
            "lhSold": True,
        }
    if classified.get("terminal") in {
        "SOLD_UNAVAILABLE_ON_ALTERNATE_SURFACE",
        "ABOUT_BLANK_ABORT",
        "EBAY_CHALLENGE",
        "EBAY_SORRY",
        "EBAY_ERROR_PAGE",
        "EBAY_ACCESS_DENIED_403",
    }:
        return {
            **base,
            "terminal": str(classified["terminal"]),
            "verified": False,
            "phase": str(classified["terminal"]),
            "soldFilterStateVerified": bool(classified.get("soldFilterStateVerified", base["soldFilterStateVerified"])),
            "soldPageHealthVerified": bool(classified.get("soldPageHealthVerified", False)),
            "x11SoldStateVerified": False,
        }
    phase = PHASE_PAGE_HEALTH_VERIFICATION if base["soldFilterStateVerified"] else PHASE_SOLD_STATE_VERIFICATION
    return {**base, "terminal": None, "verified": False, "phase": phase}


@dataclass
class FixtureClock:
    """Offline page clock: absolute elapsed seconds → (url, title, extras)."""

    frames: list[tuple[float, str, str, dict[str, Any]]]
    sold_control_at: float | None = None
    click_at: float | None = None
    challenge_active: bool = False
    passive_recaptcha: bool = False

    def at(self, elapsed_s: float) -> dict[str, Any]:
        url, title, extras = self.frames[0][1], self.frames[0][2], dict(self.frames[0][3])
        for t, u, ti, ex in self.frames:
            if elapsed_s + 1e-9 >= t:
                url, title, extras = u, ti, dict(ex)
        extras.setdefault("passiveRecaptchaIframe", self.passive_recaptcha)
        extras.setdefault("activeChallenge", self.challenge_active and "captcha" in title.lower())
        return {"url": url, "title": title, "elapsedS": elapsed_s, **extras}


def run_sold_fixture(
    clock: FixtureClock,
    *,
    policy: SoldTimeoutPolicy | None = None,
    poll_s: float = 0.05,
    already_sold: bool = False,
    force_click_fail: bool = False,
    never_discover_control: bool = False,
    page_hijack_at: float | None = None,
) -> dict[str, Any]:
    """Exercise production Sold success predicates against an offline clock (no network)."""
    pol = policy or DEFAULT_SOLD_TIMEOUT_POLICY
    t0 = 0.0
    phases: list[PhaseRecord] = []
    url_before = clock.at(0.0)["url"]
    diagnostics: dict[str, Any] = {
        "runtimeMode": "FIXTURE",
        "searchSubmitted": True,
        "ordinaryResultsConfirmed": True,
        "urlBefore": url_before,
        "soldControlDiscovered": False,
        "soldClickAttempted": False,
        "soldClickResult": None,
        "lhSoldBefore": url_has_lh_sold(url_before),
        "lhSoldAfter": False,
        "soldFilterStateVerified": False,
        "soldPageHealthVerified": False,
        "x11SoldStateVerified": False,
        "marketplacePageClass": None,
        "passiveRecaptchaIframe": clock.passive_recaptcha,
        "failureStage": None,
        "failureClass": None,
        "phases": [],
        "retries": 0,
    }

    if already_sold or url_has_lh_sold(url_before):
        obs0 = clock.at(0.0)
        early = classify_sold_observation(url=obs0["url"], title=obs0["title"])
        phase = PhaseRecord(
            PHASE_SOLD_STATE_VERIFICATION,
            started_at=t0,
            timeout_ms=int(pol.state_verification_s * 1000),
        )
        if early.get("verified"):
            phase.finish(status="PASS", reason_code=TERMINAL_SOLD_STATE_VERIFIED, now=t0)
            phases.append(phase)
            diagnostics.update(
                {
                    "alreadySold": True,
                    "soldFilterStateVerified": True,
                    "soldPageHealthVerified": True,
                    "x11SoldStateVerified": True,
                    "lhSoldAfter": True,
                    "marketplacePageClass": early.get("marketplacePageClass"),
                    "pageTitle": obs0["title"],
                    "phases": [p.to_dict() for p in phases],
                }
            )
            return {
                "ok": True,
                "terminal": TERMINAL_SOLD_STATE_VERIFIED,
                "SOLD_STATE_VERIFIED": True,
                "soldClickSuccess": False,
                "alreadySold": True,
                "diagnostics": diagnostics,
                "policy": pol.to_dict(),
            }
        # Filter may be true but page unhealthy (Rowlet-class Error Page).
        phase.finish(status="FAIL", reason_code=str(early.get("terminal") or TERMINAL_SOLD_ERROR_PAGE), now=t0)
        phases.append(phase)
        diagnostics.update(
            {
                "alreadySold": True,
                "soldFilterStateVerified": bool(early.get("soldFilterStateVerified")),
                "soldPageHealthVerified": False,
                "x11SoldStateVerified": False,
                "lhSoldAfter": url_has_lh_sold(obs0["url"]),
                "marketplacePageClass": early.get("marketplacePageClass"),
                "pageTitle": obs0["title"],
                "failureStage": PHASE_PAGE_HEALTH_VERIFICATION,
                "failureClass": str(early.get("terminal") or TERMINAL_SOLD_ERROR_PAGE),
                "phases": [p.to_dict() for p in phases],
            }
        )
        return _fail(str(early.get("terminal") or TERMINAL_SOLD_ERROR_PAGE), diagnostics, pol)

    # Discovery
    disc = PhaseRecord(
        PHASE_SOLD_CONTROL_DISCOVERY,
        started_at=t0,
        timeout_ms=int(pol.control_discovery_s * 1000),
    )
    phases.append(disc)
    elapsed = 0.0
    discovered = False
    while elapsed <= pol.control_discovery_s:
        if never_discover_control:
            elapsed += poll_s
            continue
        if page_hijack_at is not None and elapsed >= page_hijack_at:
            obs = clock.at(elapsed)
            disc.finish(status="FAIL", reason_code="PAGE_CHANGED_UNEXPECTEDLY", now=elapsed)
            diagnostics.update(
                {
                    "failureStage": PHASE_SOLD_CONTROL_DISCOVERY,
                    "failureClass": "PAGE_CHANGED_UNEXPECTEDLY",
                    "urlAfter": obs["url"],
                    "pageTitle": obs["title"],
                    "phases": [p.to_dict() for p in phases],
                }
            )
            return _fail("PAGE_CHANGED_UNEXPECTEDLY", diagnostics, pol)
        if clock.sold_control_at is not None and elapsed + 1e-9 >= clock.sold_control_at:
            discovered = True
            break
        # Active challenge during discovery
        obs = clock.at(elapsed)
        if clock.challenge_active or is_ebay_challenge_page(title=obs["title"], url=obs["url"]):
            disc.finish(status="FAIL", reason_code=TERMINAL_SOLD_CHALLENGE, now=elapsed)
            diagnostics.update(
                {
                    "failureStage": PHASE_SOLD_CONTROL_DISCOVERY,
                    "failureClass": TERMINAL_SOLD_CHALLENGE,
                    "urlAfter": obs["url"],
                    "pageTitle": obs["title"],
                    "phases": [p.to_dict() for p in phases],
                }
            )
            return _fail(TERMINAL_SOLD_CHALLENGE, diagnostics, pol)
        elapsed += poll_s
    if not discovered:
        disc.finish(status="FAIL", reason_code=TERMINAL_SOLD_CONTROL_DISCOVERY_TIMEOUT, now=elapsed)
        diagnostics.update(
            {
                "failureStage": PHASE_SOLD_CONTROL_DISCOVERY,
                "failureClass": TERMINAL_SOLD_CONTROL_DISCOVERY_TIMEOUT,
                "phases": [p.to_dict() for p in phases],
            }
        )
        return _fail(TERMINAL_SOLD_CONTROL_DISCOVERY_TIMEOUT, diagnostics, pol)
    disc.finish(status="PASS", reason_code="SOLD_CONTROL_AVAILABLE", now=elapsed)
    diagnostics["soldControlDiscovered"] = True

    # Click
    click_phase = PhaseRecord(
        PHASE_SOLD_CONTROL_CLICK,
        started_at=elapsed,
        timeout_ms=int(pol.control_click_s * 1000),
    )
    phases.append(click_phase)
    diagnostics["soldClickAttempted"] = True
    if force_click_fail:
        click_phase.finish(status="FAIL", reason_code=TERMINAL_SOLD_CLICK_FAILURE, now=elapsed)
        diagnostics.update(
            {
                "soldClickResult": "FAIL",
                "failureStage": PHASE_SOLD_CONTROL_CLICK,
                "failureClass": TERMINAL_SOLD_CLICK_FAILURE,
                "phases": [p.to_dict() for p in phases],
            }
        )
        return _fail(TERMINAL_SOLD_CLICK_FAILURE, diagnostics, pol)
    click_at = clock.click_at if clock.click_at is not None else elapsed
    if click_at - elapsed > pol.control_click_s:
        click_phase.finish(status="FAIL", reason_code=TERMINAL_SOLD_CLICK_FAILURE, now=elapsed + pol.control_click_s)
        diagnostics.update(
            {
                "soldClickResult": "TIMEOUT",
                "failureStage": PHASE_SOLD_CONTROL_CLICK,
                "failureClass": TERMINAL_SOLD_CLICK_FAILURE,
                "phases": [p.to_dict() for p in phases],
            }
        )
        return _fail(TERMINAL_SOLD_CLICK_FAILURE, diagnostics, pol)
    elapsed = max(elapsed, click_at)
    click_phase.finish(status="PASS", reason_code="SOLD_CLICKED", now=elapsed)
    diagnostics["soldClickResult"] = "OK"

    # Transition + verification (single budget for post-click settle)
    trans = PhaseRecord(
        PHASE_SOLD_STATE_TRANSITION,
        started_at=elapsed,
        timeout_ms=int(pol.state_transition_s * 1000),
    )
    ver = PhaseRecord(
        PHASE_SOLD_STATE_VERIFICATION,
        started_at=elapsed,
        timeout_ms=int(pol.state_verification_s * 1000),
    )
    phases.extend([trans, ver])
    verify_deadline = elapsed + pol.state_verification_s
    unexpected_settle_s = 2.0
    while elapsed <= verify_deadline:
        obs = clock.at(elapsed)
        classified = classify_sold_observation(
            url=obs["url"], title=obs["title"], url_before=url_before
        )
        if classified.get("verified"):
            trans.finish(status="PASS", reason_code="URL_TRANSITIONED", now=elapsed)
            ver.finish(status="PASS", reason_code=TERMINAL_SOLD_STATE_VERIFIED, now=elapsed)
            diagnostics.update(
                {
                    "urlAfter": obs["url"],
                    "pageTitle": obs["title"],
                    "lhSoldAfter": True,
                    "soldFilterStateVerified": True,
                    "soldPageHealthVerified": True,
                    "x11SoldStateVerified": True,
                    "marketplacePageClass": classified.get("marketplacePageClass"),
                    "phases": [p.to_dict() for p in phases],
                    "documentReadyState": obs.get("readyState"),
                }
            )
            return {
                "ok": True,
                "terminal": TERMINAL_SOLD_STATE_VERIFIED,
                "SOLD_STATE_VERIFIED": True,
                "soldClickSuccess": True,
                "url": obs["url"],
                "title": obs["title"],
                "diagnostics": diagnostics,
                "policy": pol.to_dict(),
            }
        term = classified.get("terminal")
        # Challenge / about:blank are immediate. Error/SORRY keep polling until budget
        # (PAGE_HEALTH_VERIFICATION) so a transient interstitial can settle healthy.
        if term in {TERMINAL_SOLD_CHALLENGE, TERMINAL_ABOUT_BLANK}:
            trans.finish(status="FAIL", reason_code=str(term), now=elapsed)
            ver.finish(status="FAIL", reason_code=str(term), now=elapsed)
            diagnostics.update(
                {
                    "urlAfter": obs["url"],
                    "pageTitle": obs["title"],
                    "soldFilterStateVerified": bool(classified.get("soldFilterStateVerified")),
                    "soldPageHealthVerified": False,
                    "x11SoldStateVerified": False,
                    "marketplacePageClass": classified.get("marketplacePageClass"),
                    "failureStage": PHASE_SOLD_STATE_VERIFICATION,
                    "failureClass": str(term),
                    "phases": [p.to_dict() for p in phases],
                }
            )
            return _fail(str(term), diagnostics, pol)
        if term in {TERMINAL_SOLD_ERROR_PAGE, TERMINAL_SOLD_SORRY}:
            diagnostics.update(
                {
                    "urlAfter": obs["url"],
                    "pageTitle": obs["title"],
                    "soldFilterStateVerified": bool(classified.get("soldFilterStateVerified")),
                    "soldPageHealthVerified": False,
                    "x11SoldStateVerified": False,
                    "marketplacePageClass": classified.get("marketplacePageClass"),
                    "lastUnhealthyTerminal": str(term),
                }
            )
            # Continue polling within verify_deadline.
        if term == TERMINAL_SOLD_UNEXPECTED_FILTER and (elapsed - click_at) >= unexpected_settle_s:
            trans.finish(status="FAIL", reason_code=TERMINAL_SOLD_UNEXPECTED_FILTER, now=elapsed)
            ver.finish(status="FAIL", reason_code=TERMINAL_SOLD_UNEXPECTED_FILTER, now=elapsed)
            diagnostics.update(
                {
                    "urlAfter": obs["url"],
                    "pageTitle": obs["title"],
                    "lhSoldAfter": False,
                    "failureStage": PHASE_SOLD_STATE_TRANSITION,
                    "failureClass": TERMINAL_SOLD_UNEXPECTED_FILTER,
                    "phases": [p.to_dict() for p in phases],
                }
            )
            return _fail(TERMINAL_SOLD_UNEXPECTED_FILTER, diagnostics, pol)
        if page_hijack_at is not None and elapsed >= page_hijack_at:
            trans.finish(status="FAIL", reason_code="PAGE_CHANGED_UNEXPECTEDLY", now=elapsed)
            ver.finish(status="FAIL", reason_code="PAGE_CHANGED_UNEXPECTEDLY", now=elapsed)
            diagnostics.update(
                {
                    "urlAfter": obs["url"],
                    "failureStage": PHASE_SOLD_STATE_TRANSITION,
                    "failureClass": "PAGE_CHANGED_UNEXPECTEDLY",
                    "phases": [p.to_dict() for p in phases],
                }
            )
            return _fail("PAGE_CHANGED_UNEXPECTEDLY", diagnostics, pol)
        elapsed += poll_s

    obs = clock.at(elapsed)
    final_obs = classify_sold_observation(url=obs["url"], title=obs["title"], url_before=url_before)
    # Stable Error/SORRY through the health window → specific marketplace failure (not timeout).
    stable_term = diagnostics.get("lastUnhealthyTerminal") or final_obs.get("terminal")
    if stable_term in {TERMINAL_SOLD_ERROR_PAGE, TERMINAL_SOLD_SORRY}:
        terminal = str(stable_term)
        stage = PHASE_PAGE_HEALTH_VERIFICATION
    else:
        terminal = TERMINAL_SOLD_STATE_VERIFICATION_TIMEOUT
        stage = PHASE_SOLD_STATE_VERIFICATION
    trans.finish(status="FAIL", reason_code=terminal, now=elapsed)
    ver.finish(status="FAIL", reason_code=terminal, now=elapsed)
    diagnostics.update(
        {
            "urlAfter": obs["url"],
            "pageTitle": obs["title"],
            "lhSoldAfter": url_has_lh_sold(obs["url"]),
            "soldFilterStateVerified": bool(final_obs.get("soldFilterStateVerified")),
            "soldPageHealthVerified": False,
            "x11SoldStateVerified": False,
            "marketplacePageClass": final_obs.get("marketplacePageClass") or diagnostics.get("marketplacePageClass"),
            "failureStage": stage,
            "failureClass": terminal,
            "capture": "NOT_RUN",
            "parse": "NOT_RUN",
            "write": "NOT_RUN",
            "phases": [p.to_dict() for p in phases],
        }
    )
    return _fail(terminal, diagnostics, pol)


def _fail(terminal: str, diagnostics: dict[str, Any], pol: SoldTimeoutPolicy) -> dict[str, Any]:
    return {
        "ok": False,
        "terminal": terminal,
        "error": terminal,
        "SOLD_STATE_VERIFIED": False,
        "soldClickSuccess": diagnostics.get("soldClickResult") == "OK",
        "diagnostics": diagnostics,
        "policy": pol.to_dict(),
    }


def build_sold_failure_evidence(
    *,
    runtime_mode: str | None,
    attempt_id: str | None,
    job_id: str | None,
    price_key_id: str | None,
    query: str | None,
    search_submitted: bool,
    ordinary_results_confirmed: bool,
    url: str | None,
    title: str | None,
    ready_state: str | None,
    sold_diagnostics: dict[str, Any] | None,
    child_return_code: int | None = None,
    stderr_summary: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    """Compact failure evidence contract (no secrets / full HTML)."""
    d = sold_diagnostics or {}
    phases = d.get("phases") or []
    return {
        "runtimeMode": runtime_mode,
        "attemptId": attempt_id,
        "jobId": job_id,
        "priceKeyId": price_key_id,
        "query": query,
        "searchSubmitted": bool(search_submitted),
        "ordinaryResultsConfirmed": bool(ordinary_results_confirmed),
        "currentUrl": url,
        "pageTitle": title,
        "documentReadyState": ready_state,
        "soldSelectorCandidates": d.get("soldSelectorCandidates") or ["Sold items", "left_rail_orange_highlight"],
        "soldControlDiscovered": d.get("soldControlDiscovered"),
        "soldClickAttempted": d.get("soldClickAttempted"),
        "soldClickResult": d.get("soldClickResult"),
        "lhSoldBefore": d.get("lhSoldBefore"),
        "lhSoldAfter": d.get("lhSoldAfter"),
        "soldFilterStateVerified": d.get("soldFilterStateVerified"),
        "soldPageHealthVerified": d.get("soldPageHealthVerified"),
        "x11SoldStateVerified": d.get("x11SoldStateVerified"),
        "marketplacePageClass": d.get("marketplacePageClass"),
        "capture": d.get("capture"),
        "parse": d.get("parse"),
        "write": d.get("write"),
        "failureStage": d.get("failureStage"),
        "failureClass": d.get("failureClass") or d.get("terminal") or error_message,
        "errorMessage": error_message or d.get("failureClass"),
        "elapsedPerStage": [
            {"name": p.get("name"), "elapsedMs": p.get("elapsedMs"), "timeoutMs": p.get("timeoutMs"), "status": p.get("status")}
            for p in phases
            if isinstance(p, dict)
        ],
        "childReturnCode": child_return_code,
        "stderrSummary": (stderr_summary or "")[:500],
        "diagnosticScreenshot": "DIAGNOSTIC_ONLY" if d.get("diagnosticScreenshot") else None,
    }


def meowth_historical_replay() -> dict[str, Any]:
    """Offline replay of historical Meowth Sold timeout (does not alter historical verdict)."""
    before = (
        "https://www.ebay.com.au/sch/i.html?_nkw=Meowth+56+jungle+Pokemon"
        "&_sacat=0&_from=R40&_trksid=m570.l1313"
    )
    after = (
        "https://www.ebay.com.au/sch/i.html?_nkw=Meowth+56+jungle+Pokemon"
        "&_sacat=0&_from=R40&rt=nc&LH_PrefLoc=2"
    )
    title = "Meowth 56 Jungle Pokemon for sale | eBay - Google Chrome"
    clock = FixtureClock(
        frames=[
            (0.0, before, title, {}),
            (0.8, before, title, {}),
            (1.2, after, title, {}),  # wrong filter shortly after click
        ],
        sold_control_at=0.5,
        click_at=0.7,
    )
    result = run_sold_fixture(clock, poll_s=0.05)
    return {
        "historicalVerdictUnchanged": "FAIL_NAVIGATION / SOLD_NAVIGATION_TIMEOUT / consumed=true",
        "futurePhaseTerminal": result.get("terminal"),
        "futureFailureStage": (result.get("diagnostics") or {}).get("failureStage"),
        "rootCauseClass": (
            "ROOT_CAUSE_REPRODUCED"
            if result.get("terminal") == TERMINAL_SOLD_UNEXPECTED_FILTER
            else "HISTORICAL_TIMEOUT_ROOT_CAUSE_NOT_FULLY_PROVABLE"
        ),
        "fixtureResult": result,
        "note": (
            "Historical opaque SOLD_NAVIGATION_TIMEOUT preserved. "
            "Corrected diagnostics identify unexpected LH_PrefLoc without LH_Sold."
        ),
    }


__all__ = [
    "DEFAULT_SOLD_TIMEOUT_POLICY",
    "SoldTimeoutPolicy",
    "FixtureClock",
    "PhaseRecord",
    "build_sold_failure_evidence",
    "classify_sold_observation",
    "meowth_historical_replay",
    "run_sold_fixture",
    "url_has_lh_sold",
    "url_has_unexpected_filter",
    "CONTROL_PLANE_PERSISTENCE_FAILURE",
    "TERMINAL_SOLD_STATE_VERIFIED",
    "TERMINAL_SOLD_CONTROL_DISCOVERY_TIMEOUT",
    "TERMINAL_SOLD_CLICK_FAILURE",
    "TERMINAL_SOLD_STATE_VERIFICATION_TIMEOUT",
    "TERMINAL_SOLD_UNEXPECTED_FILTER",
]
