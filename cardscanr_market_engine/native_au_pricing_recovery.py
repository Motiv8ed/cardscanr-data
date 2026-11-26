"""One owner-reviewed Windows AU probe after separate incident reconciliation."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
from urllib.parse import urlsplit

from .control_plane_incidents import get_incident, incidents_path
from .ebay_availability import (
    begin_probe, record_challenge, record_healthy_browser_check, record_sorry,
    release_probe_local_failure, state_path as availability_path,
)
from .ebay_browser_work_gate import (
    evaluate_ebay_browser_work_gate, evaluate_native_browser_work_gate, native_probe_scope,
)
from .live_navigation_attempt import lookup_search_submission_event
from .marketplace_ops_state import maybe_record_failure_cooldown, parse_utc, state_path as ops_path
from .providers.errors import ProviderError

ROOT = Path(__file__).resolve().parents[1]
APPROVAL = "OWNER_REVIEWED_NORMAL_EBAY_PAGE_SINGLE_PROBE"
REVIEW_OUTCOMES = {
    "owner_reviewed_challenge_resolved_probe_required",
    "manual_challenge_cleared_probe_required",
    "probe_finalize_timeout_safe",
    "cooldown_expired_probe_required",
}


def _sha(path: Path) -> str:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 2_000_000:
        raise RuntimeError("RECOVERY_STATE_FILE_UNAVAILABLE")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _reviewed_state() -> dict[str, str]:
    return {name: _sha(path) for name, path in (
        ("availability", availability_path()), ("operations", ops_path()), ("incidents", incidents_path()),
    )}


def _release() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, timeout=5).strip()


def _assert_windows_runtime_idle() -> None:
    from .native_windows_pricing_browser import require_active_console_session

    if os.name != "nt":
        raise RuntimeError("WINDOWS_RECOVERY_REQUIRES_WINDOWS")
    require_active_console_session()
    flag = ROOT / "reports/runtime/owned_daily_full_enable.flag"
    if flag.is_symlink() or not flag.is_file() or flag.read_text().strip().lower() not in {"false", "off", "0"}:
        raise RuntimeError("OWNED_DAILY_NOT_EXPLICITLY_DISABLED")
    power = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    script = r"""
$ErrorActionPreference = 'Stop'
$task = Get-ScheduledTask -TaskName 'CardScanR-LiveEbayPricingRuntime'
$running = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object CommandLine -match 'workers[/\\](market_price_worker|market_price_scheduler|owned_daily_price_scheduler)\.py')
if ($task.State -ne 'Disabled' -or $running.Count) { exit 2 }
"""
    subprocess.run([str(power), "-NoProfile", "-NonInteractive", "-Command", script],
                   check=True, timeout=12, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)


def reviewed_browser_identity() -> dict:
    """Collect identity only. The owner separately reviews the page and receipt."""
    from .native_windows_pricing_browser import cdp_json, listener_owner, require_active_console_session, valid_socket
    from .providers.desktop_win32_ebay_nav import _page_socket, _window

    owner = listener_owner()
    if owner is None:
        raise RuntimeError("REVIEWED_NATIVE_BROWSER_NOT_RUNNING")
    session = require_active_console_session()
    _window(owner)
    meta = cdp_json("/json/version")
    socket = meta.get("webSocketDebuggerUrl", "") if isinstance(meta, dict) else ""
    if not isinstance(socket, str) or not valid_socket(socket, "/devtools/browser/"):
        raise RuntimeError("REVIEWED_BROWSER_IDENTITY_UNVERIFIED")
    target, _ = _page_socket()
    if listener_owner() != owner or require_active_console_session() != session:
        raise RuntimeError("REVIEWED_BROWSER_IDENTITY_CHANGED")
    return {"pid": owner, "sessionId": session, "targetId": target,
            "browserId": urlsplit(socket).path.rsplit("/", 1)[-1]}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _verify_browser(expected: dict, expires: datetime, target: str | None = None) -> None:
    if _now() >= expires:
        raise RuntimeError("OWNER_REVIEW_EXPIRED")
    if (target is not None and target != expected.get("targetId")) or reviewed_browser_identity() != expected:
        raise RuntimeError("REVIEWED_BROWSER_IDENTITY_CHANGED")
    if _now() >= expires:
        raise RuntimeError("OWNER_REVIEW_EXPIRED")


def _assert_reviewed_browser(expected: dict, expires: datetime) -> None:
    from .providers.desktop_win32_ebay_nav import _check_page, _read

    _verify_browser(expected, expires)
    target, page = _read(expected["targetId"])
    _check_page(page)
    if not urlsplit(page["url"]).path.startswith(("/sch/", "/itm/")):
        raise RuntimeError("REVIEWED_NORMAL_AU_PAGE_REQUIRED")
    _verify_browser(expected, expires, target)


class _NoAdditionalSubmission:
    def budget_exhausted(self):
        return "OWNER_RECOVERY_LIMIT_ONE"


def _fetch(price_key_id: str, attempt_id: str):
    from .config import MarketEngineConfig, supabase_secret_key_from_env
    from .continuous_safety import ContinuousSafetyBudget
    from .marketplaces import resolve_marketplace_config
    from .models import ProviderRequest
    from .navigation_runtime_context import NavigationRuntimeContext, apply_context_to_environ
    from .providers.ebay_browser_provider import EbayBrowserSoldCompsProvider
    from .supabase_client import SupabaseMarketEngineClient
    from .supabase_env_loader import load_supabase_env

    os.environ.update(EBAY_BROWSER_NAV_MODE="desktop_win32", EBAY_BROWSER_ENABLED="true",
                      EBAY_BROWSER_MAX_QUERY_ATTEMPTS="1", MARKET_LOOKUP_PROVIDER="ebay_browser")
    if os.getenv("CARDSCANR_PRE_SUBMIT_ONLY") or os.getenv("PRE_SUBMIT_ONLY"):
        raise RuntimeError("RECOVERY_PRE_SUBMIT_ONLY_ARMED")
    load_supabase_env()
    config = MarketEngineConfig.from_env()
    client = SupabaseMarketEngineClient(supabase_url=config.supabase_url,
                                        service_role_key=supabase_secret_key_from_env())
    key = client.get_price_key(price_key_id)
    if key.id != price_key_id or key.market_country.upper() != "AU" or key.currency.upper() != "AUD":
        raise RuntimeError("REVIEWED_AU_PRICE_KEY_REQUIRED")
    market = resolve_marketplace_config(market_country="AU", currency="AUD", marketplace="ebay")
    request = ProviderRequest(
        price_key=key, market_country="AU", currency="AUD", marketplace=market.marketplace,
        provider_marketplace_id=market.provider_marketplace_id, provider_domain=market.provider_domain,
        search_locale=market.search_locale, display_name=market.display_name, market_config=market,
    )
    apply_context_to_environ(NavigationRuntimeContext(
        current_attempt_id=attempt_id, current_price_key_id=key.id, current_fingerprint=key.fingerprint,
        current_market="AU", current_currency="AUD",
    ))
    budget = ContinuousSafetyBudget.from_env()
    if not evaluate_native_browser_work_gate().allowed:
        raise RuntimeError("RECOVERY_GATE_CHANGED")
    if budget.reserve_live_submission(market="AU"):
        raise RuntimeError("RECOVERY_BUDGET_BLOCKED")
    provider = EbayBrowserSoldCompsProvider()
    provider.bind_live_submission_budget(_NoAdditionalSubmission(), first_slot_pre_reserved=True)
    return provider.fetch_comps(request)


def run_native_recovery(review_file: Path | None, approval: str) -> dict:
    if approval != APPROVAL or review_file is None:
        raise RuntimeError("OWNER_APPROVAL_REQUIRED")
    _assert_windows_runtime_idle()
    _sha(review_file)
    review = json.loads(review_file.read_text(encoding="utf-8"))
    if not isinstance(review, dict):
        raise RuntimeError("OWNER_REVIEW_INVALID_OR_EXPIRED")
    expires = parse_utc(review.get("expiresAtUtc"))
    now = _now()
    price_key = review.get("priceKeyId")
    incident_id = review.get("incidentId")
    evidence = review.get("marketplaceAuthorizationReference")
    if (review.get("schema") != "cardscanr-owner-native-au-probe-v1"
            or review.get("release") != _release()
            or not expires or not now < expires <= now + timedelta(minutes=30)
            or not isinstance(incident_id, str) or not incident_id
            or not isinstance(price_key, str) or not price_key
            or not isinstance(evidence, str) or not evidence.strip()):
        raise RuntimeError("OWNER_REVIEW_INVALID_OR_EXPIRED")
    reviewed = _reviewed_state()
    if review.get("stateSha256") != reviewed:
        raise RuntimeError("OWNER_REVIEW_STATE_CHANGED")
    incident = get_incident(incident_id) or {}
    if (incident.get("market") != "AU" or incident.get("incidentType") != "CHALLENGE"
            or incident.get("status") not in {"resolved", "invalidated"}):
        raise RuntimeError("REVIEWED_INCIDENT_NOT_RECONCILED")
    gate = evaluate_ebay_browser_work_gate(market="AU", for_probe=True)
    availability = gate.availability or {}
    if (not gate.allowed or gate.availability_state != "PROBE_REQUIRED" or gate.probe_in_flight
            or availability.get("lastOutcome") not in REVIEW_OUTCOMES):
        raise RuntimeError("RECOVERY_GATE_NOT_OPEN")
    browser = review.get("browserIdentity")
    if not isinstance(browser, dict) or set(browser) != {"pid", "sessionId", "targetId", "browserId"}:
        raise RuntimeError("OWNER_REVIEW_BROWSER_IDENTITY_REQUIRED")
    _assert_reviewed_browser(browser, expires)
    if _reviewed_state() != reviewed:
        raise RuntimeError("OWNER_REVIEW_STATE_CHANGED")

    # Claim one immutable receipt for this reviewed state before changing the gate.
    identity = hashlib.sha256(json.dumps(reviewed, sort_keys=True).encode()).hexdigest()
    receipt_dir = availability_path().parent / "native_recovery_receipts"
    receipt_dir.mkdir(exist_ok=True)
    receipt = receipt_dir / (identity + ".jsonl")
    attempt_id = "native-recovery-" + identity
    try:
        stream = receipt.open("x", encoding="utf-8")
    except FileExistsError:
        raise RuntimeError("OWNER_REVIEW_ALREADY_CONSUMED") from None
    with stream:
        def write(status):
            stream.write(json.dumps({"status": status, "attemptId": attempt_id, "incidentId": incident_id}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

        write("CLAIMED")
        started = begin_probe(market="AU", expected_updated_at=availability["updatedAtUtc"]).to_dict()["updatedAtUtc"]
        from .providers.desktop_win32_ebay_nav import recovery_browser_scope

        with native_probe_scope(started), recovery_browser_scope(
            lambda target: _verify_browser(browser, expires, target),
        ):
            try:
                if not evaluate_native_browser_work_gate().allowed:
                    raise RuntimeError("RECOVERY_GATE_CHANGED")
                result = _fetch(price_key, attempt_id)
                meta = result.raw_metadata
                capture = meta.get("currentJobCapture") or {}
                valid = lookup_search_submission_event(attempt_id, expected_price_key_id=price_key)
                if (not valid["valid"] or valid["event"].get("priceKeyId") != price_key
                        or meta.get("postSoldCapturePhase") != "POST_SOLD_CAPTURE_READY"
                        or capture.get("attemptId") != attempt_id or capture.get("priceKeyId") != price_key
                        or not evaluate_native_browser_work_gate().allowed):
                    raise RuntimeError("RECOVERY_EVIDENCE_UNVERIFIED")
                _verify_browser(browser, expires)
                record_healthy_browser_check(market="AU", from_probe=True, probe_started_at=started)
                status = "PROBE_HEALTHY_STREAK_PENDING"
            except ProviderError as exc:
                from .owned_daily_outcomes import classify_exception_outcome

                if not evaluate_native_browser_work_gate().allowed:
                    write("PROBE_GATE_CHANGED")
                    raise RuntimeError("RECOVERY_GATE_CHANGED") from None
                outcome = classify_exception_outcome(exc, diagnostics=exc.diagnostics)
                if outcome in {"CHALLENGE_REQUIRED", "EBAY_CHALLENGE_REQUIRED", "EBAY_AUTH_REQUIRED"}:
                    hold = maybe_record_failure_cooldown(market="AU", message=str(exc), diagnostics=exc.diagnostics,
                                                         source_attempt_id=attempt_id)
                    record_challenge(market="AU", probe_started_at=started,
                                     incident_id=hold.incident_id if hold is not None else None)
                    status = "PROBE_CHALLENGE"
                elif outcome in {"TEMPORARY_EBAY_SERVER_FAILURE", "EBAY_RATE_LIMITED", "EBAY_ACCESS_DENIED_403"}:
                    maybe_record_failure_cooldown(market="AU", message=str(exc), diagnostics=exc.diagnostics,
                                                         source_attempt_id=attempt_id)
                    record_sorry(market="AU", from_probe=True, probe_started_at=started)
                    status = "PROBE_COOLDOWN"
                else:
                    release_probe_local_failure(market="AU", probe_started_at=started)
                    status = "PROBE_FAILED_SAFE"
            except Exception:
                try:
                    release_probe_local_failure(market="AU", probe_started_at=started)
                    status = "PROBE_FAILED_SAFE"
                except RuntimeError:
                    status = "PROBE_OWNERSHIP_LOST"
        write(status)
    return {"status": status, "attemptId": attempt_id}
