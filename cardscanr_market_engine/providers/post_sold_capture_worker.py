"""Isolated post-Sold capture worker process entrypoint.

Runs read-only direct CDP capture and emits a single JSON object on stdout.
Supports intentional hang injection for offline hard-deadline tests via:
  CARDSCANR_CAPTURE_WORKER_HANG_AT=before_connect|connect|enumerate|readiness|body|html|crash|malformed
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.providers.post_sold_capture import (  # noqa: E402
    CAPTURE_ARTIFACT_WRITE_FAILURE,
    CAPTURE_ENCODING_FAILURE,
    CAPTURE_PROTOCOL_FAILURE,
    CDP_CAPTURE_METHOD_FAILURE,
    CDP_CONNECT_FAILURE,
    CDP_DOCUMENT_NOT_READY,
    CDP_ENDPOINT_UNAVAILABLE,
    CDP_EVALUATION_FAILURE,
    CDP_INTEGRITY_FAILURE,
    CDP_TARGET_AMBIGUOUS,
    CDP_TARGET_ATTACH_FAILURE,
    CDP_TARGET_FAILURE,
    CDP_TARGET_MISMATCH,
    CDP_TARGET_NOT_FOUND,
    CDP_TIMEOUT,
    MARKETPLACE_ERROR_PAGE,
    TARGET_REJECTED_UNHEALTHY_PAGE,
    assess_document_readiness,
    capture_integrity_ok,
    resolve_authoritative_capture_html_path,
    select_sold_page_target,
    write_utf8_bytes_atomic,
)
from cardscanr_market_engine.providers.sold_page_health import (  # noqa: E402
    rejection_evidence_from_scored,
)
from cardscanr_market_engine.providers.post_sold_capture_cdp import (  # noqa: E402
    CHALLENGE_UI_JS,
    COLLECT_CANDIDATES_JS,
    ITM_HREF_COUNT_JS,
    READINESS_JS,
    DirectCdpSession,
    count_itm_hrefs,
    list_page_targets,
    version_info,
)

# Re-export / alias status codes used in worker JSON contract
STATUS_SUCCESS = "SUCCESS"
CDP_CAPTURE_PROCESS_CRASH = "CDP_CAPTURE_PROCESS_CRASH"
_ARTIFACT_PATH: str | None = None
_LAST_DIR = ROOT / "reports" / "artifacts" / "post_sold_capture_last"


def _configure_stdio_utf8() -> None:
    """Best-effort UTF-8 stdio; authoritative protocol still uses stdout.buffer."""
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="strict")  # type: ignore[attr-defined]
        except Exception:
            pass


def _hang_point() -> str:
    return str(os.environ.get("CARDSCANR_CAPTURE_WORKER_HANG_AT") or "").strip().lower()


def _maybe_hang(stage: str) -> None:
    if _hang_point() == stage:
        while True:
            time.sleep(60)


def _write_protocol_line(payload: dict[str, Any]) -> None:
    """Emit compact ASCII-safe JSON metadata as UTF-8 bytes on stdout.

    Never depends on the Windows console/code-page codec. Large HTML must not
    travel through this channel.
    """
    line = json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n"
    data = line.encode("utf-8")
    try:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    except Exception:
        # Last-resort text write with explicit UTF-8 — still no charmap default.
        sys.stdout.write(line)
        sys.stdout.flush()


def _update_diagnostic_mirror(slim: dict[str, Any]) -> tuple[bool, str | None]:
    """Best-effort diagnostic pointer only — never authoritative, never fatal.

    Prefers a small last_capture_meta.json pointer to the unique artifactPath.
    Does not copy multi-megabyte HTML into shared last_capture.html by default.
    """
    try:
        _LAST_DIR.mkdir(parents=True, exist_ok=True)
        pointer = {
            "artifactPath": slim.get("html_path"),
            "bodyPath": slim.get("body_path"),
            "candidatesPath": slim.get("candidates_path"),
            "sha256": slim.get("html_sha256"),
            "bodySha256": slim.get("body_sha256"),
            "capturedAtUtc": slim.get("captured_at_utc"),
            "jobId": slim.get("job_id"),
            "attemptId": slim.get("attempt_id"),
            "priceKeyId": slim.get("price_key_id"),
            "fingerprint": slim.get("fingerprint"),
            "captureOrigin": slim.get("capture_origin"),
            "targetId": slim.get("target_id"),
            "status": slim.get("status"),
            "diagnosticOnly": True,
            "authoritative": False,
            "lastCaptureRole": "developer_diagnostic_pointer_non_authoritative",
        }
        write_utf8_bytes_atomic(
            _LAST_DIR / "last_capture_meta.json",
            json.dumps(pointer, ensure_ascii=False, indent=2),
        )
        # Optional legacy HTML mirror — off by default (avoids Windows replace locks).
        if str(os.environ.get("CARDSCANR_DIAGNOSTIC_MIRROR_HTML") or "").strip() == "1":
            html_path = slim.get("html_path")
            if html_path and Path(str(html_path)).is_file():
                write_utf8_bytes_atomic(
                    _LAST_DIR / "last_capture.html",
                    Path(str(html_path)).read_text(encoding="utf-8"),
                )
        return True, None
    except Exception as exc:
        return False, f"{type(exc).__name__}:{exc}"


def _emit(payload: dict[str, Any], *, artifact_path: str | None = None) -> int:
    """Persist AUTHORITATIVE unique capture artifacts; emit compact metadata.

    Critical path: unique current-job HTML/body/candidates (atomic UTF-8).
    Diagnostic mirror (post_sold_capture_last/*) is best-effort and non-fatal.
    """
    html = payload.get("html")
    body_text = payload.get("body_text")
    slim = {k: v for k, v in payload.items() if k not in {"html", "body_text"}}
    explicit = artifact_path if artifact_path is not None else _ARTIFACT_PATH

    # Correlation fields first so they land in authoritative meta too.
    slim["job_id"] = str(os.environ.get("CARDSCANR_JOB_ID") or "").strip() or None
    slim["attempt_id"] = str(os.environ.get("CARDSCANR_LIVE_ATTEMPT_ID") or "").strip() or None
    slim["price_key_id"] = str(os.environ.get("CARDSCANR_PRICE_KEY_ID") or "").strip() or None
    slim["fingerprint"] = str(os.environ.get("CARDSCANR_FINGERPRINT") or "").strip() or None
    slim["capture_origin"] = (
        str(os.environ.get("CARDSCANR_CAPTURE_ORIGIN") or "").strip().upper() or "UNKNOWN"
    )
    slim["captured_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    slim["authoritativeArtifact"] = True
    slim["sharedLastCaptureCritical"] = False

    # Diagnostics inventory can be large; keep compact scoring summary only.
    diag = slim.get("diagnostics")
    if isinstance(diag, dict) and isinstance(diag.get("scored"), list) and len(diag["scored"]) > 12:
        slim["diagnostics"] = {
            **diag,
            "scored": diag["scored"][:12],
            "scoredTruncated": True,
        }

    try:
        has_document = bool(html is not None and str(html))
        if has_document:
            auth_html = resolve_authoritative_capture_html_path(
                job_id=slim.get("job_id"),
                attempt_id=slim.get("attempt_id"),
                price_key_id=slim.get("price_key_id"),
                explicit_path=explicit,
            )
            html_info = write_utf8_bytes_atomic(auth_html, str(html), reject_existing=True)
            slim["html_path"] = html_info["path"]
            slim["html_sha256"] = html_info["sha256"]
            slim["html_length_bytes"] = html_info["byteLength"]
            slim["html_length"] = html_info["charLength"]
            # Sibling authoritative body + candidates (same directory).
            auth_stem = auth_html.with_suffix("")
            if body_text is not None and str(body_text):
                body_info = write_utf8_bytes_atomic(
                    Path(str(auth_stem) + "_body.txt"),
                    str(body_text),
                    reject_existing=True,
                )
                slim["body_path"] = body_info["path"]
                slim["body_sha256"] = body_info["sha256"]
                slim["body_length_bytes"] = body_info["byteLength"]
                slim["body_text_length"] = body_info["charLength"]
            if isinstance(payload.get("candidates"), list):
                cand_path = Path(str(auth_stem) + "_candidates.json")
                write_utf8_bytes_atomic(
                    cand_path,
                    json.dumps(payload.get("candidates"), ensure_ascii=False, indent=2),
                    reject_existing=True,
                )
                slim["candidates_path"] = str(cand_path)
                slim["candidate_count"] = len(payload.get("candidates") or [])
                slim.pop("candidates", None)
            # Compact authoritative meta next to HTML (not the shared diagnostic dir).
            meta_path = Path(str(auth_stem) + "_meta.json")
            write_utf8_bytes_atomic(
                meta_path,
                json.dumps(slim, ensure_ascii=False, indent=2),
                reject_existing=True,
            )
            slim["meta_path"] = str(meta_path)
        elif body_text is not None and str(body_text) and slim.get("status") == STATUS_SUCCESS:
            # SUCCESS without HTML is unexpected — still persist body uniquely.
            auth_html = resolve_authoritative_capture_html_path(
                job_id=slim.get("job_id"),
                attempt_id=slim.get("attempt_id"),
                price_key_id=slim.get("price_key_id"),
                explicit_path=explicit,
            )
            body_info = write_utf8_bytes_atomic(
                auth_html.with_name(auth_html.stem + "_body.txt"),
                str(body_text),
                reject_existing=True,
            )
            slim["body_path"] = body_info["path"]
            slim["body_sha256"] = body_info["sha256"]
            slim["body_length_bytes"] = body_info["byteLength"]
            slim["body_text_length"] = body_info["charLength"]
    except UnicodeEncodeError as exc:
        slim = {
            "status": CAPTURE_ENCODING_FAILURE,
            "error": f"UnicodeEncodeError:{exc}",
            "elapsed_ms": slim.get("elapsed_ms"),
            "target_id": slim.get("target_id"),
            "target_url": slim.get("target_url"),
        }
        try:
            _write_protocol_line(slim)
        except Exception:
            pass
        return 1
    except (OSError, FileExistsError) as exc:
        slim = {
            "status": CAPTURE_ARTIFACT_WRITE_FAILURE,
            "error": f"{type(exc).__name__}:{exc}",
            "elapsed_ms": slim.get("elapsed_ms"),
            "target_id": slim.get("target_id"),
            "target_url": slim.get("target_url"),
        }
        try:
            _write_protocol_line(slim)
        except Exception:
            pass
        return 1

    # Diagnostic mirror AFTER authoritative success path — never fatal.
    if slim.get("status") == STATUS_SUCCESS and slim.get("html_path"):
        mirror_ok, mirror_warn = _update_diagnostic_mirror(slim)
        slim["diagnosticMirrorUpdated"] = bool(mirror_ok)
        if mirror_warn:
            slim["diagnosticMirrorWarning"] = mirror_warn
        slim["diagnosticOnlyLastCapture"] = True
        slim["notAuthoritativeEvidence"] = True
        slim["lastCaptureRole"] = "developer_diagnostic_pointer_non_authoritative"
    else:
        slim["diagnosticMirrorUpdated"] = False

    try:
        _write_protocol_line(slim)
    except UnicodeEncodeError as exc:
        # Artifacts already durable — protocol failure must not destroy them.
        try:
            _write_protocol_line(
                {
                    "status": CAPTURE_PROTOCOL_FAILURE,
                    "error": f"UnicodeEncodeError:{exc}",
                    "html_path": slim.get("html_path"),
                    "body_path": slim.get("body_path"),
                    "html_sha256": slim.get("html_sha256"),
                    "elapsed_ms": slim.get("elapsed_ms"),
                }
            )
        except Exception:
            pass
        return 1
    except Exception as exc:
        try:
            _write_protocol_line(
                {
                    "status": CAPTURE_PROTOCOL_FAILURE,
                    "error": f"{type(exc).__name__}:{exc}",
                    "html_path": slim.get("html_path"),
                    "body_path": slim.get("body_path"),
                    "elapsed_ms": slim.get("elapsed_ms"),
                }
            )
        except Exception:
            pass
        return 1

    return 0 if slim.get("status") == STATUS_SUCCESS else 1


def _fail(status: str, *, elapsed_ms: float, **extra: Any) -> int:
    payload = {"status": status, "elapsed_ms": int(elapsed_ms), **extra}
    # Failures never include html/body_text.
    payload.pop("html", None)
    payload.pop("body_text", None)
    return _emit(payload)


def run_capture(args: argparse.Namespace) -> int:
    global _ARTIFACT_PATH
    _ARTIFACT_PATH = str(args.artifact_path or "").strip() or None
    started = time.monotonic()
    cdp_base = str(args.cdp_endpoint).rstrip("/")
    expected_url = str(args.expected_url or "")
    expected_query = str(args.expected_query or "")
    expected_origin = str(args.expected_origin or "ebay.com.au")
    max_results = int(args.max_results or 60)
    socket_timeout = float(args.socket_timeout or 4.0)

    if _hang_point() == "crash":
        os._exit(99)

    if _hang_point() == "malformed":
        sys.stdout.write("{not-json\n")
        sys.stdout.flush()
        return 1

    # Offline hard-deadline tests inject hangs by stage name. Hang immediately so
    # the proof does not depend on a live CDP endpoint; mid-stage markers below
    # remain for optional live debugging when CARDSCANR_CAPTURE_WORKER_MID_HANG=1.
    mid = str(os.environ.get("CARDSCANR_CAPTURE_WORKER_MID_HANG") or "").strip() == "1"
    if not mid and _hang_point() in {
        "before_connect",
        "connect",
        "enumerate",
        "readiness",
        "body",
        "html",
    }:
        _maybe_hang(_hang_point())

    _maybe_hang("before_connect")

    try:
        ver, ver_ms = version_info(cdp_base, timeout=min(2.0, socket_timeout))
    except Exception as exc:
        return _fail(
            CDP_ENDPOINT_UNAVAILABLE,
            elapsed_ms=(time.monotonic() - started) * 1000,
            error=f"{type(exc).__name__}:{exc}",
        )

    _maybe_hang("connect")

    try:
        targets, list_ms = list_page_targets(cdp_base, timeout=min(2.0, socket_timeout))
    except Exception as exc:
        return _fail(
            CDP_CONNECT_FAILURE,
            elapsed_ms=(time.monotonic() - started) * 1000,
            error=f"{type(exc).__name__}:{exc}",
            versionLatencyMs=round(ver_ms, 3),
        )

    _maybe_hang("enumerate")

    chosen, fail_cls, scored = select_sold_page_target(
        targets,
        expected_url=expected_url or None,
        expected_query=expected_query or None,
        require_lh_sold=True,
    )
    inventory = [
        {
            "id": t.get("id"),
            "url": t.get("url"),
            "title": t.get("title"),
            "hasWs": bool(t.get("webSocketDebuggerUrl")),
        }
        for t in targets
    ]
    diagnostics: dict[str, Any] = {
        "versionLatencyMs": round(ver_ms, 3),
        "listLatencyMs": round(list_ms, 3),
        "browser": ver.get("Browser"),
        "targetCount": len(targets),
        "targets": inventory,
        "scored": [c.to_dict() for c in scored],
    }

    if fail_cls == CDP_TARGET_AMBIGUOUS:
        return _fail(
            CDP_TARGET_AMBIGUOUS,
            elapsed_ms=(time.monotonic() - started) * 1000,
            diagnostics=diagnostics,
        )
    if fail_cls or chosen is None:
        rej = rejection_evidence_from_scored(
            scored,
            expected_url=expected_url or None,
            expected_query=expected_query or None,
            fail_cls=fail_cls or CDP_TARGET_NOT_FOUND,
        )
        status = str(rej.get("failureClass") or fail_cls or CDP_TARGET_NOT_FOUND)
        diagnostics["rejectionEvidence"] = rej
        diagnostics["expectedTargetFound"] = rej.get("expectedTargetFound")
        diagnostics["healthClassification"] = rej.get("healthClassification")
        diagnostics["marketplacePageClass"] = rej.get("healthClassification")
        if status in {MARKETPLACE_ERROR_PAGE, TARGET_REJECTED_UNHEALTHY_PAGE}:
            diagnostics["capture"] = "NOT_RUN"
            diagnostics["parse"] = "NOT_RUN"
            diagnostics["write"] = "NOT_RUN"
        return _fail(
            status,
            elapsed_ms=(time.monotonic() - started) * 1000,
            diagnostics=diagnostics,
            target_id=rej.get("expectedTargetId"),
            target_url=rej.get("expectedTargetUrl"),
            error=",".join(rej.get("rejectionReasons") or []) or status,
        )

    # Locate websocket URL for chosen target
    ws_url = ""
    for t in targets:
        if str(t.get("id") or "") == str(chosen.target_id or "") or str(t.get("url") or "") == chosen.url:
            ws_url = str(t.get("webSocketDebuggerUrl") or "")
            if ws_url:
                break
    if not ws_url:
        return _fail(
            CDP_CONNECT_FAILURE,
            elapsed_ms=(time.monotonic() - started) * 1000,
            error="missing_websocket_debugger_url",
            diagnostics=diagnostics,
            target_id=chosen.target_id,
            target_url=chosen.url,
        )

    methods: dict[str, Any] = {}
    session: DirectCdpSession | None = None
    try:
        session = DirectCdpSession(ws_url, timeout=socket_timeout)
        session.call("Runtime.enable", timeout=socket_timeout)

        _maybe_hang("readiness")
        readiness_raw: dict[str, Any] = {}
        readiness: dict[str, Any] = {}
        # Large documents (multi-MB HTML) can briefly report about:blank after attach.
        for _attempt in range(8):
            readiness_raw = session.evaluate(f"({READINESS_JS})()", timeout=socket_timeout)
            if not isinstance(readiness_raw, dict):
                readiness_raw = {}
            readiness = assess_document_readiness(readiness_raw)
            diagnostics["readiness"] = readiness
            frame_url = str(readiness_raw.get("frameUrl") or "")
            if frame_url and frame_url not in {"about:blank", "about:blank#blocked"}:
                break
            time.sleep(0.35)

        live_url = str(readiness_raw.get("frameUrl") or chosen.url or "")
        if live_url in {"about:blank", "about:blank#blocked", ""}:
            # Prefer the CDP target inventory URL when the frame has not committed yet.
            live_url = str(chosen.url or "")
        if expected_url and "lh_sold=1" in expected_url.lower() and "lh_sold=1" not in live_url.lower():
            return _fail(
                CDP_TARGET_MISMATCH,
                elapsed_ms=(time.monotonic() - started) * 1000,
                diagnostics=diagnostics,
                target_id=chosen.target_id,
                target_url=live_url,
                expected_url=expected_url,
            )

        body_text = ""
        html = ""
        capture_method = None

        _maybe_hang("body")
        methods["evaluate_inner_text"] = {"attempted": True}
        try:
            body_text = str(
                session.evaluate("(document.body && document.body.innerText) || ''", timeout=socket_timeout) or ""
            )
            methods["evaluate_inner_text"]["length"] = len(body_text)
            if body_text.strip():
                capture_method = "cdp_evaluate_inner_text"
        except Exception as exc:
            methods["evaluate_inner_text"]["error"] = f"{type(exc).__name__}:{exc}"
            methods["evaluate_inner_text"]["length"] = 0

        _maybe_hang("html")
        methods["evaluate_outer_html"] = {"attempted": True}
        try:
            html = str(
                session.evaluate(
                    "(document.documentElement && document.documentElement.outerHTML) || ''",
                    timeout=min(8.0, max(socket_timeout, 5.0)),
                )
                or ""
            )
            methods["evaluate_outer_html"]["length"] = len(html)
            if html.strip() and (not body_text.strip() or len(html) > len(body_text)):
                if not capture_method:
                    capture_method = "cdp_evaluate_outer_html"
        except Exception as exc:
            methods["evaluate_outer_html"]["error"] = f"{type(exc).__name__}:{exc}"
            methods["evaluate_outer_html"]["length"] = 0

        document_for_integrity = html if html.strip() else body_text
        if not document_for_integrity.strip():
            return _fail(
                CDP_DOCUMENT_NOT_READY if not readiness.get("readable") else CDP_CAPTURE_METHOD_FAILURE,
                elapsed_ms=(time.monotonic() - started) * 1000,
                diagnostics={**diagnostics, "methods": methods, "readiness": readiness},
                target_id=chosen.target_id,
                target_url=live_url,
                target_title=chosen.title,
            )

        integrity = capture_integrity_ok(
            url=live_url,
            title=chosen.title,
            body_text=body_text or document_for_integrity[:50000],
            expected_url=expected_url or None,
            expected_query=expected_query or None,
            expected_origin=expected_origin,
        )
        diagnostics["integrity"] = {k: v for k, v in integrity.items() if k != "soldState"}
        diagnostics["methods"] = methods
        if not integrity.get("ok"):
            return _fail(
                CDP_INTEGRITY_FAILURE,
                elapsed_ms=(time.monotonic() - started) * 1000,
                diagnostics=diagnostics,
                target_id=chosen.target_id,
                target_url=live_url,
                target_title=chosen.title,
                body_text_length=len(body_text),
                html_length=len(html),
                failure_detail=",".join(integrity.get("reasons") or []),
            )

        # Listing provenance via DOM evaluate (preferred) + HTML regex fallback.
        candidates: list[dict[str, Any]] = []
        try:
            raw_cands = session.evaluate_function(
                COLLECT_CANDIDATES_JS,
                {"maxResults": max_results},
                timeout=min(8.0, max(socket_timeout, 5.0)),
            )
            if isinstance(raw_cands, list):
                candidates = [c for c in raw_cands if isinstance(c, dict)]
        except Exception as exc:
            diagnostics["candidateExtractError"] = f"{type(exc).__name__}:{exc}"

        itm_meta: dict[str, Any]
        try:
            itm_meta = session.evaluate(f"({ITM_HREF_COUNT_JS})()", timeout=socket_timeout) or {}
            if not isinstance(itm_meta, dict):
                itm_meta = {}
        except Exception:
            itm_meta = count_itm_hrefs(html or body_text)

        canonical_count = int(itm_meta.get("hrefCount") or 0)
        unique_ids = itm_meta.get("uniqueItemIds") or []
        if not isinstance(unique_ids, list):
            unique_ids = []

        title = chosen.title
        try:
            title = str(session.evaluate("document.title || ''", timeout=2.0) or title)
        except Exception:
            pass

        # Read-only challenge UI visibility probe (never clicks / solves CAPTCHA).
        challenge_ui: dict[str, Any] = {}
        try:
            challenge_raw = session.evaluate(f"({CHALLENGE_UI_JS})()", timeout=min(3.0, socket_timeout))
            if isinstance(challenge_raw, dict):
                challenge_ui = challenge_raw
        except Exception as exc:
            challenge_ui = {"error": f"{type(exc).__name__}:{exc}"}
        diagnostics["challengeUi"] = challenge_ui

        elapsed_ms = (time.monotonic() - started) * 1000
        return _emit(
            {
                "status": STATUS_SUCCESS,
                "capture_method": capture_method or "cdp_direct",
                "target_id": chosen.target_id,
                "target_url": live_url,
                "target_title": title,
                "target_match": True,
                "ready_state": readiness.get("readyState"),
                "body_text": body_text,
                "html": html,
                "body_text_length": len(body_text),
                "html_length": len(html),
                "canonical_itm_href_count": canonical_count,
                "unique_item_ids": unique_ids[:200],
                "candidates": candidates,
                "sold_state": integrity.get("soldState"),
                "readiness": readiness,
                "methods": methods,
                "challenge_ui": challenge_ui,
                "diagnostics": diagnostics,
                "elapsed_ms": int(elapsed_ms),
            }
        )
    except TimeoutError as exc:
        return _fail(
            CDP_TIMEOUT,
            elapsed_ms=(time.monotonic() - started) * 1000,
            error=f"TimeoutError:{exc}",
            diagnostics=diagnostics,
            target_id=chosen.target_id,
            target_url=chosen.url,
        )
    except UnicodeEncodeError as exc:
        return _fail(
            CAPTURE_ENCODING_FAILURE,
            elapsed_ms=(time.monotonic() - started) * 1000,
            error=f"UnicodeEncodeError:{exc}",
            diagnostics=diagnostics,
            target_id=chosen.target_id if chosen else None,
            target_url=chosen.url if chosen else None,
        )
    except Exception as exc:
        err = f"{type(exc).__name__}:{exc}"
        status = CDP_EVALUATION_FAILURE
        low = err.lower()
        if "connect" in low or "websocket" in low or "connection refused" in low:
            status = CDP_CONNECT_FAILURE
        elif "encode" in low or "charmap" in low or "codec" in low:
            status = CAPTURE_ENCODING_FAILURE
        return _fail(
            status,
            elapsed_ms=(time.monotonic() - started) * 1000,
            error=err,
            diagnostics=diagnostics,
            target_id=chosen.target_id if chosen else None,
            target_url=chosen.url if chosen else None,
        )
    finally:
        if session is not None:
            session.close()


def main(argv: list[str] | None = None) -> int:
    _configure_stdio_utf8()
    ap = argparse.ArgumentParser(description="Isolated read-only post-Sold CDP capture worker")
    ap.add_argument("--cdp-endpoint", required=True, help="e.g. http://127.0.0.1:9444")
    ap.add_argument("--expected-url", default="")
    ap.add_argument("--expected-query", default="")
    ap.add_argument("--expected-title", default="")
    ap.add_argument("--expected-origin", default="ebay.com.au")
    ap.add_argument("--max-results", type=int, default=60)
    ap.add_argument("--socket-timeout", type=float, default=4.0)
    ap.add_argument("--deadline-seconds", type=float, default=15.0, help="Informational; parent enforces kill.")
    ap.add_argument(
        "--artifact-path",
        default="",
        help="Authoritative unique HTML artifact destination for this attempt",
    )
    args = ap.parse_args(argv)

    # Soft self-budget: exit before parent kill when possible.
    soft = max(1.0, float(args.deadline_seconds) - 0.5)

    result_code = 1
    try:
        # Soft deadline via alarm isn't portable on Windows; parent hard-kills.
        result_code = run_capture(args)
    except UnicodeEncodeError as exc:
        result_code = _fail(
            CAPTURE_ENCODING_FAILURE,
            elapsed_ms=0,
            error=f"UnicodeEncodeError:{exc}",
        )
    except Exception as exc:
        result_code = _fail(
            CDP_CAPTURE_PROCESS_CRASH,
            elapsed_ms=0,
            error=f"{type(exc).__name__}:{exc}",
        )

    # Reference soft budget so linters/static tools see it used.
    _ = soft
    return result_code


if __name__ == "__main__":
    raise SystemExit(main())
