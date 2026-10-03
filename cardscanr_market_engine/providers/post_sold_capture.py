"""Read-only post-Sold CDP capture: target binding + document capture.

Separates X11 SOLD_STATE_VERIFIED from local CDP capture/parse.
Never navigates, clicks, or reloads via CDP.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote_plus, urlparse

_ARTIFACT_ROOT = Path(__file__).resolve().parents[2] / "reports" / "artifacts"
_AUTHORITATIVE_CAPTURE_ROOT = _ARTIFACT_ROOT / "post_sold_captures"


def _safe_path_token(value: str | None, *, fallback: str = "anon", max_len: int = 64) -> str:
    raw = str(value or "").strip() or fallback
    cleaned = "".join(c if c.isalnum() or c in "-_" else "_" for c in raw)
    return (cleaned or fallback)[:max_len]


def resolve_authoritative_capture_html_path(
    *,
    job_id: str | None = None,
    attempt_id: str | None = None,
    price_key_id: str | None = None,
    artifact_root: Path | str | None = None,
    timestamp_utc: str | None = None,
    explicit_path: Path | str | None = None,
) -> Path:
    """Unique same-filesystem destination for the current attempt's HTML capture.

    Preferred shape:
      reports/artifacts/post_sold_captures/<jobId>/<attemptId>/capture_<UTC>_<ns>.html
    """
    if explicit_path is not None and str(explicit_path).strip():
        p = Path(str(explicit_path).strip())
        if p.suffix.lower() == ".json":
            p = p.with_suffix(".html")
        elif not p.suffix:
            p = Path(str(p) + ".html")
        return p
    root = Path(artifact_root) if artifact_root is not None else _AUTHORITATIVE_CAPTURE_ROOT
    ts = timestamp_utc or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    job = _safe_path_token(job_id, fallback="nojob")
    attempt = _safe_path_token(attempt_id, fallback="noattempt")
    # price_key_id reserved for meta/correlation; path identity is job+attempt+time.
    _ = price_key_id
    dest_dir = root / job / attempt
    return dest_dir / f"capture_{ts}_{time.time_ns()}.html"


def write_utf8_bytes_atomic(
    path: Path | str,
    text: str,
    *,
    reject_existing: bool = False,
) -> dict[str, Any]:
    """Write exact UTF-8 bytes atomically (tmp → fsync → replace).

    Returns sha256 hex of the durable artifact bytes. Never uses platform
    default text encoding. On failure removes only this writer's temp file.

    When reject_existing=True (authoritative captures), refuse to overwrite a
    different existing file; same-bytes reuse is idempotent.
    """
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    if reject_existing and dest.exists():
        try:
            existing = dest.read_bytes()
        except OSError:
            existing = b""
        if existing == data:
            return {
                "path": str(dest),
                "sha256": digest,
                "byteLength": len(data),
                "charLength": len(text),
                "encoding": "utf-8",
                "idempotentReuse": True,
            }
        raise FileExistsError(f"authoritative_artifact_collision:{dest}")
    tmp = dest.with_name(f"{dest.name}.tmp.{os.getpid()}.{time.time_ns()}")
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, dest)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return {
        "path": str(dest),
        "sha256": digest,
        "byteLength": len(data),
        "charLength": len(text),
        "encoding": "utf-8",
    }

POST_SOLD_CAPTURE_PENDING = "POST_SOLD_CAPTURE_PENDING"
POST_SOLD_CAPTURE_READY = "POST_SOLD_CAPTURE_READY"
POST_SOLD_CAPTURE_FAILED = "POST_SOLD_CAPTURE_FAILED"
PARSE_PENDING = "PARSE_PENDING"
PARSE_COMPLETE = "PARSE_COMPLETE"
PARSE_FAILED = "PARSE_FAILED"

CDP_CONNECT_FAILURE = "CDP_CONNECT_FAILURE"
CDP_ENDPOINT_UNAVAILABLE = "CDP_ENDPOINT_UNAVAILABLE"
CDP_TARGET_NOT_FOUND = "CDP_TARGET_NOT_FOUND"
CDP_WRONG_TARGET = "CDP_WRONG_TARGET"
CDP_TARGET_URL_MISMATCH = "CDP_TARGET_URL_MISMATCH"
CDP_TARGET_MISMATCH = "CDP_TARGET_MISMATCH"
CDP_TARGET_AMBIGUOUS = "CDP_TARGET_AMBIGUOUS"
CDP_TARGET_STALE = "CDP_TARGET_STALE"
CDP_TARGET_CHANGED = "CDP_TARGET_CHANGED"
CDP_TARGET_FAILURE = "CDP_TARGET_FAILURE"
CDP_DOCUMENT_NOT_READY = "CDP_DOCUMENT_NOT_READY"
CDP_MAIN_FRAME_NOT_READY = "CDP_MAIN_FRAME_NOT_READY"
CDP_EXECUTION_CONTEXT_NOT_READY = "CDP_EXECUTION_CONTEXT_NOT_READY"
CDP_EVALUATION_FAILURE = "CDP_EVALUATION_FAILURE"
CDP_CAPTURE_FAILURE = "CDP_CAPTURE_FAILURE"
CDP_CAPTURE_METHOD_FAILURE = "CDP_CAPTURE_METHOD_FAILURE"
CDP_CAPTURE_PROCESS_TIMEOUT = "CDP_CAPTURE_PROCESS_TIMEOUT"
CDP_CAPTURE_PROCESS_CRASH = "CDP_CAPTURE_PROCESS_CRASH"
CDP_ATTACH_RACE = "CDP_ATTACH_RACE"
CDP_SESSION_FAILURE = "CDP_SESSION_FAILURE"
CDP_SOLD_READBACK_LOGIC_FAILURE = "CDP_SOLD_READBACK_LOGIC_FAILURE"
CDP_TIMEOUT = "CDP_TIMEOUT"
CDP_INTEGRITY_FAILURE = "CDP_INTEGRITY_FAILURE"
CAPTURE_INTEGRITY_FAILURE = "CAPTURE_INTEGRITY_FAILURE"
CAPTURE_ENCODING_FAILURE = "CAPTURE_ENCODING_FAILURE"
CAPTURE_ARTIFACT_WRITE_FAILURE = "CAPTURE_ARTIFACT_WRITE_FAILURE"
CAPTURE_PROTOCOL_FAILURE = "CAPTURE_PROTOCOL_FAILURE"
POST_SOLD_CAPTURE_TIMEOUT = CDP_CAPTURE_PROCESS_TIMEOUT

MIN_PLAUSIBLE_SOLD_BODY_CHARS = 800
MIN_SOLD_EVIDENCE_BODY_CHARS = 30


@dataclass
class PageTargetCandidate:
    target_id: str | None
    target_type: str
    url: str
    title: str
    score: int = 0
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SoldPageCaptureResult:
    success: bool
    x11_sold_state_verified: bool
    capture_phase: str
    target_id: str | None = None
    target_url: str | None = None
    target_title: str | None = None
    capture_method: str | None = None
    html_or_text: str | None = None
    capture_elapsed_ms: int | None = None
    failure_class: str | None = None
    failure_detail: str | None = None
    sold_state: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    retry_used: bool = False
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_probe_dict(self) -> dict[str, Any]:
        """Non-sensitive capture diagnostics for probe reports (no full document)."""
        body = self.html_or_text or ""
        return {
            "success": self.success,
            "x11SoldStateVerified": self.x11_sold_state_verified,
            "capturePhase": self.capture_phase,
            "targetId": self.target_id,
            "targetUrl": self.target_url,
            "targetTitle": self.target_title,
            "captureMethod": self.capture_method,
            "documentBodyChars": len(body),
            "captureElapsedMs": self.capture_elapsed_ms,
            "failureClass": self.failure_class,
            "failureDetail": self.failure_detail,
            "soldState": self.sold_state,
            "candidates": self.candidates,
            "retryUsed": self.retry_used,
            "diagnostics": self.diagnostics,
        }


def persist_sold_capture_artifact(
    *,
    html: str,
    body_text: str | None = None,
    canonical_itm_href_count: int | None = None,
    card_identity: dict[str, Any] | None = None,
    query: str | None = None,
    target_url: str | None = None,
    target_title: str | None = None,
    capture_method: str | None = None,
    market: str | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write diagnostic HTML + metadata immediately after successful capture.

    Diagnostic evidence only — not a pricing data source. Never stores cookies,
    auth tokens, localStorage credentials, or secret request headers.
    """
    html_text = str(html or "")
    if not html_text.strip():
        return {"ok": False, "reason": "empty_html"}
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = _ARTIFACT_ROOT
    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = out_dir / f"ebay_sold_capture_{ts}.html"
    meta_path = out_dir / f"ebay_sold_capture_{ts}.json"
    html_info = write_utf8_bytes_atomic(html_path, html_text)
    sha = str(html_info["sha256"])
    itm_count = canonical_itm_href_count
    if itm_count is None:
        itm_count = len(set(re.findall(r"https?://(?:www\.)?ebay\.[^/\s\"']+/itm/\d+", html_text, flags=re.I)))
    body_chars = len(str(body_text or ""))
    meta: dict[str, Any] = {
        "captureTimestampUtc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "canonicalCardIdentity": card_identity or {},
        "query": query,
        "targetUrl": target_url,
        "targetTitle": target_title,
        "market": market,
        "bodyLength": body_chars,
        "htmlLength": len(html_text),
        "htmlByteSize": int(html_info["byteLength"]),
        "canonicalItmHrefCount": int(itm_count or 0),
        "captureMethod": capture_method,
        "htmlPath": str(html_path),
        "sha256": sha,
        "encoding": "utf-8",
        "diagnosticOnly": True,
        "notAPricingDataSource": True,
    }
    if body_text is not None and str(body_text):
        body_path = out_dir / f"ebay_sold_capture_{ts}_body.txt"
        body_info = write_utf8_bytes_atomic(body_path, str(body_text))
        meta["bodyPath"] = str(body_path)
        meta["bodySha256"] = body_info["sha256"]
    if extra_meta:
        for key, value in extra_meta.items():
            if key in {"cookies", "authorization", "localStorage", "headers"}:
                continue
            meta[key] = value
    write_utf8_bytes_atomic(meta_path, json.dumps(meta, ensure_ascii=False, indent=2))
    # Verify
    exists = html_path.is_file() and html_path.stat().st_size > 0
    recomputed = hashlib.sha256(html_path.read_bytes()).hexdigest() if exists else ""
    has_itm = "/itm/" in html_text
    return {
        "ok": bool(exists and recomputed == sha and has_itm),
        "htmlPath": str(html_path),
        "metaPath": str(meta_path),
        "byteSize": int(html_info["byteLength"]),
        "sha256": sha,
        "sha256Verified": recomputed == sha,
        "fileExists": exists,
        "canonicalItmHrefCount": int(itm_count or 0),
        "hasCanonicalListingStructure": has_itm,
        "encoding": "utf-8",
    }


def _norm_url(url: str | None) -> str:
    return str(url or "").strip()


def _query_tokens(query: str | None) -> set[str]:
    return {t.lower() for t in str(query or "").split() if len(t) > 1}


def score_sold_page_target(
    *,
    url: str,
    title: str,
    target_type: str,
    expected_url: str | None,
    expected_query: str | None,
    require_lh_sold: bool = True,
) -> PageTargetCandidate:
    """Score a CDP page target for post-Sold capture binding (higher is better)."""
    cand = PageTargetCandidate(
        target_id=None,
        target_type=str(target_type or "page"),
        url=_norm_url(url),
        title=str(title or ""),
    )
    u = cand.url.lower()
    t = cand.title.lower()
    if cand.target_type not in {"page", "Page", ""}:
        cand.score = -1000
        cand.reasons.append("non_page_type")
        return cand
    if u.startswith("about:blank") or u.startswith("chrome://") or u.startswith("chrome-extension://"):
        cand.score = -1000
        cand.reasons.append("rejected_non_content")
        return cand
    if "ebaylive" in u:
        cand.score = -500
        cand.reasons.append("ebay_live")
        return cand
    if "error page" in t or "/error" in u:
        cand.score = -500
        cand.reasons.append("sorry_error")
        return cand
    if "splashui/challenge" in u or "captcha" in t:
        cand.score = -500
        cand.reasons.append("challenge")
        return cand
    if "ebay." not in u:
        cand.score = -200
        cand.reasons.append("not_ebay")
        return cand

    score = 10
    cand.reasons.append("ebay_page")
    if "lh_sold=1" in u:
        score += 100
        cand.reasons.append("lh_sold")
    elif require_lh_sold:
        score -= 40
        cand.reasons.append("missing_lh_sold")
    if "/sch/" in u:
        score += 20
        cand.reasons.append("sch_results")
    exp = _norm_url(expected_url).lower()
    if exp and u:
        if exp == u or exp.split("#")[0] == u.split("#")[0]:
            score += 80
            cand.reasons.append("exact_url_match")
        else:
            try:
                exp_nkw = unquote_plus((parse_qs(urlparse(exp).query).get("_nkw") or [""])[0]).lower()
                got_nkw = unquote_plus((parse_qs(urlparse(u).query).get("_nkw") or [""])[0]).lower()
            except Exception:
                exp_nkw = got_nkw = ""
            if exp_nkw and got_nkw and exp_nkw == got_nkw:
                score += 40
                cand.reasons.append("nkw_match")
            elif "lh_sold=1" in exp and "lh_sold=1" not in u:
                score -= 30
                cand.reasons.append("expected_sold_url_mismatch")
    tokens = _query_tokens(expected_query)
    if tokens:
        blob = f"{u} {t}"
        hit = sum(1 for tok in tokens if tok in blob)
        if hit >= max(1, min(2, len(tokens))):
            score += 15
            cand.reasons.append("query_tokens_present")
    cand.score = score
    return cand


def select_sold_page_target(
    candidates: list[dict[str, Any]],
    *,
    expected_url: str | None,
    expected_query: str | None,
    require_lh_sold: bool = True,
) -> tuple[PageTargetCandidate | None, str | None, list[PageTargetCandidate]]:
    """Deterministically pick exactly one Sold page target or fail closed."""
    scored: list[PageTargetCandidate] = []
    for raw in candidates:
        if not isinstance(raw, dict):
            continue
        ttype = str(raw.get("type") or raw.get("targetType") or "page")
        cand = score_sold_page_target(
            url=str(raw.get("url") or ""),
            title=str(raw.get("title") or ""),
            target_type=ttype,
            expected_url=expected_url,
            expected_query=expected_query,
            require_lh_sold=require_lh_sold,
        )
        cand.target_id = str(raw.get("id") or raw.get("targetId") or "") or None
        scored.append(cand)

    viable = [c for c in scored if c.score >= 50 and "lh_sold" in c.reasons]
    if not viable and not require_lh_sold:
        viable = [c for c in scored if c.score >= 30]

    if not viable:
        viable = [c for c in scored if c.score >= 80]
    if not viable:
        return None, CDP_TARGET_NOT_FOUND, scored

    viable.sort(key=lambda c: c.score, reverse=True)
    best = viable[0]
    peers = [c for c in viable[1:] if c.score >= best.score - 15 and "lh_sold" in c.reasons]
    if peers:
        return None, CDP_TARGET_AMBIGUOUS, scored
    if expected_url and "lh_sold=1" in expected_url.lower() and "lh_sold" not in best.reasons:
        return None, CDP_TARGET_URL_MISMATCH, scored
    return best, None, scored


def verify_captured_sold_state(
    *,
    url: str,
    title: str,
    body_text: str,
) -> dict[str, Any]:
    """Pure sold-state check for a captured document (mirrors provider logic)."""
    url_l = str(url or "").lower()
    title_l = str(title or "").lower()
    body = str(body_text or "")
    body_l = body.lower()
    sold_url = "lh_sold=1" in url_l
    complete_url = "lh_complete=1" in url_l
    sold_date_lines = sum(
        1
        for line in body.splitlines()
        if line.strip().lower().startswith("sold ")
        and not line.strip().lower().startswith("sold items")
        and not line.strip().lower().startswith("sold listings")
    )
    sold_listings_label = "sold listings" in body_l or "sold items" in body_l
    result_level_sold = sold_date_lines >= 1
    verified = bool(sold_url and (result_level_sold or (sold_listings_label and complete_url)))
    return {
        "SOLD_STATE_VERIFIED": verified,
        "soldUrlParam": sold_url,
        "completedUrlParam": complete_url,
        "soldDateLines": sold_date_lines,
        "soldListingsLabel": sold_listings_label,
        "resultLevelSoldEvidence": result_level_sold,
        "titleHasSold": "sold" in title_l,
        "activeListingContaminationPossible": bool(
            not verified and ("buy it now" in body_l or "add to cart" in body_l)
        ),
    }


def assess_document_readiness(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """Classify readiness from a read-only document snapshot (no I/O)."""
    snap = snapshot if isinstance(snapshot, dict) else {}
    ready_state = str(snap.get("readyState") or "").lower()
    body_len = int(snap.get("bodyInnerTextLength") or snap.get("bodyLength") or 0)
    outer_len = int(snap.get("outerHTMLLength") or 0)
    content_len = int(snap.get("contentLength") or 0)
    has_doc = bool(snap.get("documentElementPresent"))
    has_body = bool(snap.get("bodyPresent"))
    frame_url = str(snap.get("frameUrl") or snap.get("url") or "")
    listing_nodes = int(snap.get("listingNodeCount") or 0)

    reasons: list[str] = []
    if ready_state in {"interactive", "complete"}:
        reasons.append(f"readyState_{ready_state}")
    if has_doc:
        reasons.append("documentElement")
    if has_body:
        reasons.append("body")
    if body_len >= MIN_PLAUSIBLE_SOLD_BODY_CHARS:
        reasons.append("body_len_ok")
    if outer_len >= MIN_PLAUSIBLE_SOLD_BODY_CHARS:
        reasons.append("outer_html_ok")
    if content_len >= MIN_PLAUSIBLE_SOLD_BODY_CHARS:
        reasons.append("content_ok")
    if listing_nodes >= 1:
        reasons.append("listing_nodes")

    readable = bool(
        ready_state in {"interactive", "complete"}
        and has_doc
        and has_body
        and (body_len >= MIN_PLAUSIBLE_SOLD_BODY_CHARS or outer_len >= MIN_PLAUSIBLE_SOLD_BODY_CHARS or listing_nodes >= 1)
    )
    failure_class = None
    if not has_doc or ready_state in {"", "loading"}:
        failure_class = CDP_MAIN_FRAME_NOT_READY if not has_doc else CDP_DOCUMENT_NOT_READY
    elif snap.get("executionContextError"):
        failure_class = CDP_EXECUTION_CONTEXT_NOT_READY
    elif not readable and body_len == 0 and outer_len == 0 and content_len == 0:
        failure_class = CDP_DOCUMENT_NOT_READY
    return {
        "readable": readable,
        "readyState": ready_state,
        "bodyInnerTextLength": body_len,
        "outerHTMLLength": outer_len,
        "contentLength": content_len,
        "documentElementPresent": has_doc,
        "bodyPresent": has_body,
        "frameUrl": frame_url,
        "listingNodeCount": listing_nodes,
        "reasons": reasons,
        "failureClass": failure_class,
    }


def capture_integrity_ok(
    *,
    url: str,
    title: str,
    body_text: str,
    expected_url: str | None,
    expected_query: str | None,
    expected_origin: str = "ebay.com.au",
) -> dict[str, Any]:
    """Reject SORRY/Live/challenge/garbage before parser handoff."""
    u = str(url or "").lower()
    t = str(title or "").lower()
    body = str(body_text or "")
    body_l = body.lower()
    reasons: list[str] = []
    ok = True
    if expected_origin.lower() not in u and "ebay." not in u:
        ok = False
        reasons.append("origin_mismatch")
    if "ebaylive" in u:
        ok = False
        reasons.append("ebay_live")
    if "error page" in t or "sorry" in t[:80] or "/error" in u:
        ok = False
        reasons.append("sorry_error")
    if "splashui/challenge" in u or "captcha" in t:
        ok = False
        reasons.append("challenge")
    sold = verify_captured_sold_state(url=url, title=title, body_text=body)
    body_len = len(body.strip())
    # Accept either a large marketplace document OR a compact sold-evidence snippet
    # (unit tests / X11 clipboard fragments with sold dates).
    if body_len < MIN_SOLD_EVIDENCE_BODY_CHARS:
        ok = False
        reasons.append("body_too_small")
    elif body_len < MIN_PLAUSIBLE_SOLD_BODY_CHARS and not sold.get("SOLD_STATE_VERIFIED"):
        ok = False
        reasons.append("body_too_small")
    else:
        reasons.append("body_size_ok")
    if not sold.get("SOLD_STATE_VERIFIED"):
        ok = False
        reasons.append("sold_evidence_missing")
    else:
        reasons.append("sold_evidence_ok")
    tokens = _query_tokens(expected_query)
    if tokens:
        blob = f"{u} {t} {body_l[:2000]}"
        if sum(1 for tok in tokens if tok in blob) < max(1, min(2, len(tokens))):
            ok = False
            reasons.append("query_context_missing")
        else:
            reasons.append("query_context_ok")
    if expected_url and "lh_sold=1" in expected_url.lower() and "lh_sold=1" not in u:
        ok = False
        reasons.append("expected_lh_sold_missing")
    # Plausible result structure
    if "sold " not in body_l and "sold items" not in body_l:
        ok = False
        reasons.append("no_sold_structure")
    return {"ok": ok, "reasons": reasons, "soldState": sold}


def classify_empty_capture_failure(read: dict[str, Any] | None) -> str:
    """Map attach/read diagnostics to a precise failure class (no guessing beyond evidence)."""
    read = read if isinstance(read, dict) else {}
    err = str(read.get("error") or "").lower()
    readiness = read.get("readiness") if isinstance(read.get("readiness"), dict) else {}
    methods = read.get("methods") if isinstance(read.get("methods"), dict) else {}
    if "timeout" in err:
        return CDP_TIMEOUT
    if "execution context" in err or readiness.get("failureClass") == CDP_EXECUTION_CONTEXT_NOT_READY:
        return CDP_EXECUTION_CONTEXT_NOT_READY
    if "session" in err or "target closed" in err or "connection" in err:
        return CDP_SESSION_FAILURE
    if readiness.get("failureClass") == CDP_MAIN_FRAME_NOT_READY:
        return CDP_MAIN_FRAME_NOT_READY
    if read.get("urlChanged") or read.get("targetChanged"):
        return CDP_TARGET_CHANGED
    if read.get("targetMissing"):
        return CDP_TARGET_STALE
    # Methods attempted but all empty while readiness claimed document exists → method failure
    attempted = [k for k, v in methods.items() if isinstance(v, dict) and v.get("attempted")]
    nonempty = [k for k, v in methods.items() if isinstance(v, dict) and int(v.get("length") or 0) > 0]
    if attempted and not nonempty and (
        int(readiness.get("outerHTMLLength") or 0) > 0 or int(readiness.get("listingNodeCount") or 0) > 0
    ):
        return CDP_CAPTURE_METHOD_FAILURE
    if attempted and not nonempty:
        return CDP_DOCUMENT_NOT_READY
    if err:
        return CDP_CAPTURE_FAILURE
    return CDP_DOCUMENT_NOT_READY


def capture_verified_sold_page(
    *,
    x11_sold_state_verified: bool,
    expected_url: str | None,
    expected_query: str | None,
    expected_market: str | None = "AU",
    expected_origin: str = "ebay.com.au",
    list_targets: Callable[[], list[dict[str, Any]]],
    attach_and_read: Callable[[PageTargetCandidate], dict[str, Any]],
    disconnect: Callable[[], None] | None = None,
    allow_one_local_retry: bool = True,
    pre_verified_document: str | None = None,
    pre_verified_document_source: str | None = None,
) -> SoldPageCaptureResult:
    """Bind to the X11-verified Sold page and capture document read-only.

    attach_and_read(candidate) must return:
      {ok, url, title, body_text, error?, elapsed_ms?, readiness?, methods?, ...}
    without navigating/clicking.

    If CDP returns empty but pre_verified_document (e.g. X11 clipboard body) passes
    integrity, capture may succeed with that document for sold evidence; DOM parse
    still uses the bound page handle when available.
    """
    started = time.monotonic()
    base = SoldPageCaptureResult(
        success=False,
        x11_sold_state_verified=bool(x11_sold_state_verified),
        capture_phase=POST_SOLD_CAPTURE_PENDING,
        diagnostics={
            "expectedUrl": expected_url,
            "expectedQuery": expected_query,
            "expectedMarket": expected_market,
            "expectedOrigin": expected_origin,
            "preVerifiedDocumentSource": pre_verified_document_source,
            "preVerifiedDocumentChars": len(pre_verified_document or ""),
        },
    )
    if not x11_sold_state_verified:
        base.failure_class = CDP_SOLD_READBACK_LOGIC_FAILURE
        base.failure_detail = "capture_requires_x11_sold_state_verified"
        base.capture_phase = POST_SOLD_CAPTURE_FAILED
        return base

    def _finalize_with_body(
        result: SoldPageCaptureResult,
        *,
        url: str,
        title: str,
        body: str,
        method: str,
        read: dict[str, Any] | None = None,
    ) -> SoldPageCaptureResult:
        integrity = capture_integrity_ok(
            url=url,
            title=title,
            body_text=body,
            expected_url=expected_url,
            expected_query=expected_query,
            expected_origin=expected_origin,
        )
        result.diagnostics["integrity"] = {k: v for k, v in integrity.items() if k != "soldState"}
        result.sold_state = integrity.get("soldState")
        if read:
            result.diagnostics["readiness"] = read.get("readiness")
            result.diagnostics["methods"] = read.get("methods")
            result.diagnostics["attachReadMs"] = read.get("elapsed_ms")
        if not integrity.get("ok"):
            result.failure_class = CDP_INTEGRITY_FAILURE
            result.failure_detail = ",".join(integrity.get("reasons") or []) or "integrity_failed"
            result.capture_phase = POST_SOLD_CAPTURE_FAILED
            result.html_or_text = body
            result.capture_elapsed_ms = int((time.monotonic() - started) * 1000)
            return result
        result.success = True
        result.capture_phase = POST_SOLD_CAPTURE_READY
        result.html_or_text = body
        result.capture_method = method
        result.target_url = url
        result.target_title = title
        result.capture_elapsed_ms = int((time.monotonic() - started) * 1000)
        return result

    def _attempt(*, retry: bool) -> SoldPageCaptureResult:
        result = SoldPageCaptureResult(
            success=False,
            x11_sold_state_verified=True,
            capture_phase=POST_SOLD_CAPTURE_PENDING,
            retry_used=retry,
            diagnostics=dict(base.diagnostics),
        )
        try:
            raw_targets = list_targets()
        except Exception as exc:
            result.failure_class = CDP_CONNECT_FAILURE
            result.failure_detail = f"{type(exc).__name__}:{exc}"
            result.capture_phase = POST_SOLD_CAPTURE_FAILED
            result.capture_elapsed_ms = int((time.monotonic() - started) * 1000)
            return result

        if not isinstance(raw_targets, list):
            raw_targets = []
        # Probe-safe target inventory (no cookies/headers).
        result.diagnostics["targetCount"] = len(raw_targets)
        result.diagnostics["targets"] = [
            {
                "id": t.get("id") or t.get("targetId"),
                "type": t.get("type") or t.get("targetType") or "page",
                "url": t.get("url") or "",
                "title": t.get("title") or "",
            }
            for t in raw_targets
            if isinstance(t, dict)
        ]

        chosen, fail_cls, scored = select_sold_page_target(
            raw_targets,
            expected_url=expected_url,
            expected_query=expected_query,
            require_lh_sold=True,
        )
        result.candidates = [c.to_dict() for c in scored]
        if fail_cls or chosen is None:
            result.failure_class = fail_cls or CDP_TARGET_NOT_FOUND
            result.failure_detail = fail_cls or "no_viable_sold_target"
            result.capture_phase = POST_SOLD_CAPTURE_FAILED
            result.capture_elapsed_ms = int((time.monotonic() - started) * 1000)
            # Do not accept X11 body alone without a bound CDP page — parser needs DOM/hrefs.
            result.diagnostics["preVerifiedSkippedReason"] = "no_bound_cdp_page_for_parser"
            return result

        result.target_id = chosen.target_id
        result.target_url = chosen.url
        result.target_title = chosen.title
        result.diagnostics["selectedScore"] = chosen.score
        result.diagnostics["selectedReasons"] = list(chosen.reasons)
        result.diagnostics["selectedMatchedLhSold"] = "lh_sold" in chosen.reasons
        result.diagnostics["selectedMatchedQuery"] = "query_tokens_present" in chosen.reasons or "nkw_match" in chosen.reasons

        try:
            read = attach_and_read(chosen)
        except Exception as exc:
            result.failure_class = CDP_CAPTURE_FAILURE
            result.failure_detail = f"{type(exc).__name__}:{exc}"
            result.capture_phase = POST_SOLD_CAPTURE_FAILED
            result.capture_elapsed_ms = int((time.monotonic() - started) * 1000)
            return result

        if not isinstance(read, dict):
            read = {"ok": False, "error": "attach_and_read_non_dict"}

        result.diagnostics["attachOk"] = bool(read.get("ok"))
        result.diagnostics["readiness"] = read.get("readiness")
        result.diagnostics["methods"] = read.get("methods")
        result.diagnostics["frameUrl"] = (read.get("readiness") or {}).get("frameUrl") if isinstance(read.get("readiness"), dict) else read.get("url")

        got_url = str(read.get("url") or chosen.url or "")
        got_title = str(read.get("title") or chosen.title or "")
        body = str(read.get("body_text") or "")
        method = str(read.get("capture_method") or "cdp_read_only")

        if expected_url and "lh_sold=1" in expected_url.lower() and "lh_sold=1" not in got_url.lower():
            result.failure_class = CDP_TARGET_URL_MISMATCH
            result.failure_detail = "attached_url_missing_lh_sold"
            result.target_url = got_url
            result.target_title = got_title
            result.capture_phase = POST_SOLD_CAPTURE_FAILED
            result.capture_elapsed_ms = int((time.monotonic() - started) * 1000)
            return result

        if not read.get("ok") or not body.strip():
            # Prefer proven X11 Sold clipboard body when CDP document extraction is empty.
            if pre_verified_document:
                integ = capture_integrity_ok(
                    url=got_url or str(expected_url or chosen.url),
                    title=got_title or str(chosen.title or ""),
                    body_text=pre_verified_document,
                    expected_url=expected_url,
                    expected_query=expected_query,
                    expected_origin=expected_origin,
                )
                if integ.get("ok"):
                    result.diagnostics["fallbackToPreVerified"] = True
                    result.diagnostics["cdpEmptyClass"] = classify_empty_capture_failure(read)
                    result.diagnostics["cdpEmptyDetail"] = read.get("error") or "empty_body"
                    return _finalize_with_body(
                        result,
                        url=got_url or str(expected_url or chosen.url),
                        title=got_title or str(chosen.title or ""),
                        body=pre_verified_document,
                        method=str(pre_verified_document_source or "x11_clipboard_sold_body"),
                        read=read,
                    )
            result.failure_class = classify_empty_capture_failure(read)
            result.failure_detail = str(read.get("error") or "empty_document_body")
            result.target_url = got_url
            result.target_title = got_title
            result.capture_phase = POST_SOLD_CAPTURE_FAILED
            result.capture_elapsed_ms = int((time.monotonic() - started) * 1000)
            return result

        return _finalize_with_body(
            result,
            url=got_url,
            title=got_title,
            body=body,
            method=method,
            read=read,
        )

    first = _attempt(retry=False)
    if first.success:
        return first
    retryable = first.failure_class in {
        CDP_CONNECT_FAILURE,
        CDP_CAPTURE_FAILURE,
        CDP_CAPTURE_METHOD_FAILURE,
        CDP_TIMEOUT,
        CDP_DOCUMENT_NOT_READY,
        CDP_MAIN_FRAME_NOT_READY,
        CDP_EXECUTION_CONTEXT_NOT_READY,
        CDP_ATTACH_RACE,
        CDP_SESSION_FAILURE,
        CDP_TARGET_NOT_FOUND,
        CDP_TARGET_STALE,
    }
    if not allow_one_local_retry or not retryable:
        return first
    if disconnect is not None:
        try:
            disconnect()
        except Exception:
            pass
    second = _attempt(retry=True)
    second.diagnostics["firstFailureClass"] = first.failure_class
    second.diagnostics["firstFailureDetail"] = first.failure_detail
    return second
