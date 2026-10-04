"""Cold-start vs inter-card browser lifecycle policy (no eBay contact).

COLD_START: before Card 1 — unexpected live eBay top-level pages fail closed.
INTER_CARD: after a healthy terminal card — the expected prior pricing page may remain.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlparse

RUNTIME_COLD_START = "COLD_START"
RUNTIME_INTER_CARD = "INTER_CARD"

HEALTHY_PRIOR_VERDICTS = frozenset(
    {
        "PASS_PRICE_UPDATED",
        "PASS_PRICE_UNCHANGED",
        "SAFE_NO_NEW_EXACT_EVIDENCE",
    }
)

# Documented production strategy: accept expected prior Sold page at readiness;
# production search may still call go_ebay_home as search-surface prep (existing).
INTER_CARD_STRATEGY = "REUSE_SAME_TAB"

REASON_EXPECTED_PRIOR = "INTER_CARD_EXPECTED_PRIOR_TARGET"
REASON_UNKNOWN_EBAY = "INTER_CARD_UNKNOWN_EBAY_TARGET"
REASON_MULTIPLE_TOP = "INTER_CARD_MULTIPLE_TOP_LEVEL_EBAY_TARGETS"
REASON_NOT_CORRELATED = "INTER_CARD_PRIOR_TARGET_NOT_CORRELATED"
REASON_PRIOR_NOT_TERMINAL = "INTER_CARD_PRIOR_CARD_NOT_TERMINAL"
REASON_CHALLENGE = "INTER_CARD_CHALLENGE_TARGET"
REASON_COLD_UNEXPECTED = "COLD_START_UNEXPECTED_EBAY_TARGET"
REASON_AUXILIARY_OK = "AUXILIARY_TARGET_NON_BLOCKING"


@dataclass
class PriorCardContext:
    job_id: str | None = None
    attempt_id: str | None = None
    price_key_id: str | None = None
    fingerprint: str | None = None
    target_id: str | None = None
    final_url: str | None = None
    query: str | None = None
    x11_sold_state_verified: bool = False
    capture_correlated: bool = False
    card_verdict: str | None = None
    market: str | None = None
    currency: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "jobId": self.job_id,
            "attemptId": self.attempt_id,
            "priceKeyId": self.price_key_id,
            "fingerprint": self.fingerprint,
            "targetId": self.target_id,
            "finalUrl": self.final_url,
            "query": self.query,
            "x11SoldStateVerified": self.x11_sold_state_verified,
            "captureCorrelated": self.capture_correlated,
            "cardVerdict": self.card_verdict,
            "market": self.market,
            "currency": self.currency,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> PriorCardContext | None:
        if not isinstance(data, dict) or not data:
            return None
        return cls(
            job_id=_s(data.get("jobId") or data.get("job_id")),
            attempt_id=_s(data.get("attemptId") or data.get("attempt_id")),
            price_key_id=_s(data.get("priceKeyId") or data.get("price_key_id")),
            fingerprint=_s(data.get("fingerprint")),
            target_id=_s(data.get("targetId") or data.get("target_id")),
            final_url=_s(data.get("finalUrl") or data.get("final_url") or data.get("url")),
            query=_s(data.get("query")),
            x11_sold_state_verified=bool(data.get("x11SoldStateVerified") or data.get("x11_sold_state_verified")),
            capture_correlated=bool(data.get("captureCorrelated") or data.get("capture_correlated")),
            card_verdict=_s(data.get("cardVerdict") or data.get("card_verdict")),
            market=_s(data.get("market")),
            currency=_s(data.get("currency")),
        )


@dataclass
class ClassifiedTarget:
    target_id: str | None
    type: str
    url: str
    title: str
    top_level: bool
    origin_class: str
    belongs_to_expected_previous_card: bool
    blocking: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "targetId": self.target_id,
            "type": self.type,
            "url": self.url,
            "title": self.title,
            "topLevel": self.top_level,
            "originClass": self.origin_class,
            "belongsToExpectedPreviousCard": self.belongs_to_expected_previous_card,
            "blocking": self.blocking,
            "reason": self.reason,
        }


@dataclass
class TargetPolicyResult:
    ok: bool
    mode: str
    reason_codes: list[str] = field(default_factory=list)
    classified: list[ClassifiedTarget] = field(default_factory=list)
    expected_prior_accepted: bool = False
    top_level_ebay_count: int = 0
    strategy: str = INTER_CARD_STRATEGY

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "mode": self.mode,
            "reasonCodes": list(self.reason_codes),
            "classified": [c.to_dict() for c in self.classified],
            "expectedPriorAccepted": self.expected_prior_accepted,
            "topLevelEbayCount": self.top_level_ebay_count,
            "strategy": self.strategy,
        }


def _s(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def hostname_of(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return ""
    return host


def is_ebay_marketplace_host(host: str) -> bool:
    h = (host or "").lower()
    if h.startswith("www."):
        h = h[4:]
    roots = (
        "ebay.com",
        "ebay.com.au",
        "ebay.co.uk",
        "ebay.ca",
        "ebay.de",
        "ebay.fr",
        "ebay.it",
        "ebay.es",
    )
    return any(h == root or h.endswith("." + root) for root in roots)


def is_challenge_url(url: str, title: str = "") -> bool:
    blob = f"{url} {title}".lower()
    return any(
        token in blob
        for token in (
            "splashui",
            "/sorry",
            "captcha",
            "challenge",
            "access denied",
            "403",
        )
    )


def origin_class_for(url: str, *, cdp_type: str) -> str:
    host = hostname_of(url)
    low = (url or "").lower()
    if not url or low.startswith("about:blank") or low.startswith("chrome://") or low.startswith("devtools://"):
        return "blank_or_browser"
    if cdp_type in {"iframe", "worker", "service_worker", "shared_worker", "browser", "other"}:
        if "doubleclick." in host or "googlesyndication." in host or "googleadservices." in host:
            return "advertising"
    if "doubleclick." in host or host.endswith(".fls.doubleclick.net") or "googlesyndication." in host:
        return "advertising"
    if is_challenge_url(url):
        return "ebay_challenge" if is_ebay_marketplace_host(host) or "ebay." in unquote(url).lower() else "challenge"
    if is_ebay_marketplace_host(host):
        return "ebay_marketplace"
    # Query-string mention of ebay.com must NOT make ads into marketplace pages.
    return "auxiliary"


def is_top_level_browser_page(cdp_type: str, url: str, origin_class: str) -> bool:
    t = (cdp_type or "page").lower()
    if t != "page":
        return False
    if origin_class in {"advertising", "blank_or_browser", "auxiliary"}:
        return False
    return True


def prior_context_is_healthy_terminal(prior: PriorCardContext | None) -> bool:
    if prior is None:
        return False
    if str(prior.card_verdict or "") not in HEALTHY_PRIOR_VERDICTS:
        return False
    if not prior.x11_sold_state_verified:
        return False
    if not prior.capture_correlated and not prior.target_id and not prior.final_url:
        return False
    return True


def correlates_to_prior(target: dict[str, Any], prior: PriorCardContext) -> bool:
    tid = _s(target.get("id") or target.get("targetId"))
    url = str(target.get("url") or "")
    if prior.target_id and tid and tid.upper() == prior.target_id.upper():
        return True
    if prior.final_url and url:
        # Same sold/search identity: host + _nkw + LH_Sold when present.
        pu = urlparse(prior.final_url)
        tu = urlparse(url)
        if (pu.hostname or "").lower() == (tu.hostname or "").lower():
            if prior.query:
                q = prior.query.replace(" ", "+").lower()
                if q and q in url.lower().replace("%20", "+"):
                    return True
            if pu.path and pu.path == tu.path and "LH_Sold=1" in url and "LH_Sold=1" in prior.final_url:
                # Require overlapping nkw token if both have it.
                if "_nkw=" in prior.final_url.lower() and "_nkw=" in url.lower():
                    return prior.final_url.split("_nkw=")[-1][:40].lower() in url.lower()
                return True
    return False


def classify_cdp_targets(
    raw_targets: list[dict[str, Any]],
    *,
    mode: str,
    prior: PriorCardContext | None = None,
) -> list[ClassifiedTarget]:
    mode = (mode or RUNTIME_COLD_START).upper()
    prior_ok = prior_context_is_healthy_terminal(prior)
    out: list[ClassifiedTarget] = []
    for item in raw_targets or []:
        if not isinstance(item, dict):
            continue
        cdp_type = str(item.get("type") or "page").lower()
        url = str(item.get("url") or "")
        title = str(item.get("title") or "")
        tid = _s(item.get("id") or item.get("targetId"))
        origin = origin_class_for(url, cdp_type=cdp_type)
        top = is_top_level_browser_page(cdp_type, url, origin)
        belongs = bool(prior and prior_ok and top and origin == "ebay_marketplace" and correlates_to_prior(item, prior))
        blocking = False
        reason = REASON_AUXILIARY_OK
        if origin == "ebay_challenge" or (top and is_challenge_url(url, title)):
            blocking = True
            reason = REASON_CHALLENGE if mode == RUNTIME_INTER_CARD else REASON_COLD_UNEXPECTED
        elif top and origin == "ebay_marketplace":
            if mode == RUNTIME_COLD_START:
                blocking = True
                reason = REASON_COLD_UNEXPECTED
            elif belongs:
                blocking = False
                reason = REASON_EXPECTED_PRIOR
            else:
                blocking = True
                reason = REASON_UNKNOWN_EBAY if prior_ok else REASON_NOT_CORRELATED
        elif origin == "advertising" or not top:
            blocking = False
            reason = REASON_AUXILIARY_OK
        out.append(
            ClassifiedTarget(
                target_id=tid,
                type=cdp_type,
                url=url,
                title=title,
                top_level=top,
                origin_class=origin,
                belongs_to_expected_previous_card=belongs,
                blocking=blocking,
                reason=reason,
            )
        )
    return out


def evaluate_runtime_targets(
    raw_targets: list[dict[str, Any]],
    *,
    mode: str,
    prior: PriorCardContext | None = None,
) -> TargetPolicyResult:
    mode = (mode or RUNTIME_COLD_START).upper()
    if mode not in {RUNTIME_COLD_START, RUNTIME_INTER_CARD}:
        mode = RUNTIME_COLD_START
    classified = classify_cdp_targets(raw_targets, mode=mode, prior=prior)
    reasons: list[str] = []
    top_ebay = [c for c in classified if c.top_level and c.origin_class == "ebay_marketplace"]
    challenges = [c for c in classified if c.reason == REASON_CHALLENGE or c.origin_class == "ebay_challenge"]
    expected = [c for c in classified if c.belongs_to_expected_previous_card]

    if challenges:
        reasons.append(REASON_CHALLENGE)
    if mode == RUNTIME_COLD_START:
        if top_ebay:
            reasons.append(REASON_COLD_UNEXPECTED)
    else:
        if not prior_context_is_healthy_terminal(prior):
            if top_ebay:
                reasons.append(REASON_PRIOR_NOT_TERMINAL)
        elif len(top_ebay) > 1:
            # One expected + extras unknown.
            unknown = [c for c in top_ebay if not c.belongs_to_expected_previous_card]
            if unknown:
                reasons.append(REASON_MULTIPLE_TOP)
            if not expected:
                reasons.append(REASON_NOT_CORRELATED)
        elif len(top_ebay) == 1:
            if expected:
                pass
            else:
                reasons.append(REASON_UNKNOWN_EBAY)
                reasons.append(REASON_NOT_CORRELATED)
        # zero top-level ebay is also OK for INTER_CARD (already reset)

    # Mark blocking flags consistently for multiples.
    if REASON_MULTIPLE_TOP in reasons:
        for c in classified:
            if c.top_level and c.origin_class == "ebay_marketplace" and not c.belongs_to_expected_previous_card:
                c.blocking = True
                c.reason = REASON_UNKNOWN_EBAY

    ok = not reasons
    return TargetPolicyResult(
        ok=ok,
        mode=mode,
        reason_codes=sorted(set(reasons)),
        classified=classified,
        expected_prior_accepted=bool(expected) and ok and mode == RUNTIME_INTER_CARD,
        top_level_ebay_count=len(top_ebay),
        strategy=INTER_CARD_STRATEGY,
    )


def prior_from_card_report(report: dict[str, Any] | None) -> PriorCardContext | None:
    """Build PriorCardContext from a reliability card JSON report."""
    if not isinstance(report, dict):
        return None
    capture = report.get("capture") if isinstance(report.get("capture"), dict) else {}
    nav = report.get("navigation") if isinstance(report.get("navigation"), dict) else {}
    job = report.get("job") if isinstance(report.get("job"), dict) else {}
    identity = report.get("identity") if isinstance(report.get("identity"), dict) else {}
    desktop = nav.get("desktopNav") if isinstance(nav.get("desktopNav"), dict) else {}
    return PriorCardContext(
        job_id=_s(job.get("jobId") or capture.get("jobId")),
        attempt_id=_s(report.get("attemptId") or capture.get("attemptId") or job.get("attemptId")),
        price_key_id=_s(
            (report.get("selection") or {}).get("priceKeyId")
            if isinstance(report.get("selection"), dict)
            else None
        )
        or _s(capture.get("priceKeyId") or identity.get("priceKeyId")),
        fingerprint=_s(capture.get("fingerprint") or identity.get("fingerprint")),
        target_id=_s(capture.get("targetId")),
        final_url=_s(nav.get("finalUrl") or desktop.get("url")),
        query=_s(identity.get("query")),
        x11_sold_state_verified=bool(
            nav.get("x11SoldStateVerified")
            or desktop.get("SOLD_STATE_VERIFIED")
            or job.get("x11SoldStateVerified")
        ),
        capture_correlated=bool(capture.get("correlated")),
        card_verdict=_s(report.get("cardVerdict")),
        market=_s(identity.get("market") or job.get("market") or job.get("marketCountry")),
        currency=_s(identity.get("currency") or job.get("currency")),
    )


def prior_market_of(prior: PriorCardContext | None) -> str | None:
    if prior is None:
        return None
    market = _s(prior.market)
    if market:
        return market.upper()
    host = hostname_of(prior.final_url or "")
    if host.startswith("www."):
        host = host[4:]
    host_to_market = {
        "ebay.com.au": "AU",
        "ebay.com": "US",
        "ebay.co.uk": "GB",
        "ebay.ca": "CA",
    }
    return host_to_market.get(host)


def required_runtime_mode(
    *,
    next_market: str,
    prior: PriorCardContext | None,
) -> str:
    """INTER_CARD only when the prior healthy terminal card is the same market."""
    nxt = str(next_market or "").strip().upper()
    prev = prior_market_of(prior)
    if not nxt:
        return RUNTIME_COLD_START
    if prev is None:
        return RUNTIME_COLD_START
    if prev != nxt:
        return RUNTIME_COLD_START
    if not prior_context_is_healthy_terminal(prior):
        return RUNTIME_COLD_START
    return RUNTIME_INTER_CARD


__all__ = [
    "HEALTHY_PRIOR_VERDICTS",
    "INTER_CARD_STRATEGY",
    "PriorCardContext",
    "RUNTIME_COLD_START",
    "RUNTIME_INTER_CARD",
    "TargetPolicyResult",
    "classify_cdp_targets",
    "correlates_to_prior",
    "evaluate_runtime_targets",
    "hostname_of",
    "required_runtime_mode",
    "prior_market_of",
    "origin_class_for",
    "prior_context_is_healthy_terminal",
    "prior_from_card_report",
]
