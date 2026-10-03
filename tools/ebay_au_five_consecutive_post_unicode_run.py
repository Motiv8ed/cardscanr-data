#!/usr/bin/env python3
"""Authorised 5-card consecutive reliability run AFTER Unicode capture fix.

Live eBay searches capped at 5 via SEARCH_SUBMISSION_STARTED only.
Does not continue the historical Ceruledge run.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.config import MarketEngineConfig, supabase_secret_key_from_env
from cardscanr_market_engine.ebay_browser_work_gate import evaluate_ebay_browser_work_gate
from cardscanr_market_engine.job_runner import MarketPriceJobRunner
from cardscanr_market_engine.live_navigation_attempt import (
    has_search_submission_started,
    new_attempt_id,
    read_search_submission_event,
)
from cardscanr_market_engine.local_browser_runtime import ensure_xvfb, probe_pre_live_runtime
from cardscanr_market_engine.marketplace_ops_state import get_active_cooldown
from cardscanr_market_engine.owned_daily_outcomes import (
    UNCHANGED_FROM_EBAY,
    UPDATED_FROM_EBAY,
)
from cardscanr_market_engine.owned_daily_pacing import OwnedDailyPacingConfig, OwnedDailyPacingController
from cardscanr_market_engine.owned_daily_scheduler import OwnedDailySchedulerConfig, OwnedPrintingRefreshScheduler
from cardscanr_market_engine.owned_daily_source_policy import classify_owned_price_source
from cardscanr_market_engine.providers.factory import create_market_comps_provider
from cardscanr_market_engine.providers.linux_x11_ebay_nav import ensure_chrome_with_cdp
from cardscanr_market_engine.providers.query_builder import build_provider_search_queries
from cardscanr_market_engine.reliability_harness_classification import (
    classify_reliability_card_verdict,
    is_structured_challenge,
)
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient
from cardscanr_market_engine.supabase_env_loader import load_supabase_env
from cardscanr_market_engine.marketplaces import resolve_marketplace_config
from cardscanr_market_engine.models import ProviderRequest
from tools.desktop_ebay_e2e_pricing import run_forced_job
from tools.ebay_au_fresh_5_card_reliability_run import _select_due_cards

OUT = ROOT / "reports" / "artifacts" / "ebay_five_consecutive_post_unicode"
CARDS_DIR = OUT / "cards"
BOOT = OUT / "bootstrap"
BEFORE = OUT / "before"
AFTER = OUT / "after"
ATTEMPTS = OUT / "attempts"
AUTHORISED_MAX = 5
CAPTURE_MODS = [
    "cardscanr_market_engine/providers/post_sold_capture.py",
    "cardscanr_market_engine/providers/post_sold_capture_worker.py",
    "cardscanr_market_engine/providers/post_sold_capture_process.py",
]
CLOSURE_FILES = ROOT / "reports" / "artifacts" / "post_sold_unicode_capture_closure" / "files"
READINESS_PATH = ROOT / "reports" / "artifacts" / "post_sold_unicode_capture_closure" / "local_capture_readiness.json"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _configure() -> None:
    os.environ["EBAY_BROWSER_NAV_MODE"] = "linux_x11"
    os.environ["EBAY_BROWSER_ENABLED"] = "true"
    os.environ["EBAY_BROWSER_HEADLESS"] = "false"
    os.environ["EBAY_BROWSER_MAX_QUERY_ATTEMPTS"] = "1"
    os.environ["EBAY_BROWSER_REUSE_CONTEXT"] = "true"
    os.environ.setdefault("EBAY_BROWSER_CDP_PORT", "9444")
    os.environ.setdefault("EBAY_BROWSER_MIN_SECONDS_BETWEEN_REQUESTS", "20")
    os.environ.setdefault("EBAY_BROWSER_COOLDOWN_SECONDS", "20")
    os.environ["OWNED_DAILY_FULL_ENABLE"] = "false"
    # Canonical live_nav_attempts directory is the sole authority (shared with WSL).
    os.environ.pop("CARDSCANR_LIVE_NAV_ATTEMPTS_DIR", None)


def _restart_chrome_blank() -> dict[str, Any]:
    """Stop leftover production Chrome and restart on about:blank (not an eBay search)."""
    script = r"""
PREFIX="$HOME/.local/cardscanr-gui"
export PATH="$PREFIX/root/usr/bin:$PATH"
export DISPLAY=:99
unset WAYLAND_DISPLAY
"$PREFIX/bin/cardscanr_chrome_ctl.sh" stop || true
sleep 2
"$PREFIX/bin/cardscanr_chrome_ctl.sh" start \
  --remote-debugging-port=9444 \
  --remote-debugging-address=127.0.0.1 \
  --remote-allow-origins=* \
  --disable-restore-session-state \
  about:blank
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
  if curl -s -m 2 http://127.0.0.1:9444/json/version >/dev/null 2>&1; then
    if curl -s -m 2 http://127.0.0.1:9444/json/list | grep -qi 'ebay\.'; then
      echo RESTART_STILL_HAS_EBAY
      exit 4
    fi
    echo RESTART_CDP_READY
    exit 0
  fi
  sleep 1
done
echo RESTART_CDP_FAIL
exit 1
"""
    tmp = Path(r"D:\DevCache\Temp\wsl_chrome_restart_blank.sh")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(script.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8"))
    proc = subprocess.run(
        ["wsl", "-d", "Ubuntu", "--", "bash", "/mnt/d/DevCache/Temp/wsl_chrome_restart_blank.sh"],
        capture_output=True,
        text=True,
        timeout=90,
        encoding="utf-8",
        errors="replace",
    )
    return {
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "")[-2000:],
        "stderr": (proc.stderr or "")[-1000:],
        "ok": proc.returncode == 0 and "RESTART_CDP_READY" in (proc.stdout or ""),
    }


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_runtime_modules() -> dict[str, Any]:
    import cardscanr_market_engine.providers.post_sold_capture as cap
    import cardscanr_market_engine.providers.post_sold_capture_process as proc
    import cardscanr_market_engine.providers.post_sold_capture_worker as worker

    modules: dict[str, Any] = {}
    all_match = True
    for rel in CAPTURE_MODS:
        p = ROOT / rel
        row = {
            "path": str(p.resolve()),
            "exists": p.is_file(),
            "sha256": _sha256_file(p) if p.is_file() else None,
        }
        copy = CLOSURE_FILES / Path(rel).name
        if copy.is_file():
            row["closureCopySha256"] = _sha256_file(copy)
            row["matchesClosureCopy"] = row["sha256"] == row["closureCopySha256"]
            all_match = all_match and bool(row["matchesClosureCopy"])
        else:
            row["closureCopyMissing"] = True
            all_match = False
        modules[rel] = row
    return {
        "pythonExecutable": sys.executable,
        "sysPrefix": sys.prefix,
        "sysPath0": sys.path[0],
        "repoRoot": str(ROOT.resolve()),
        "captureModuleFile": str(Path(cap.__file__).resolve()),
        "workerModuleFile": str(Path(worker.__file__).resolve()),
        "processModuleFile": str(Path(proc.__file__).resolve()),
        "workerModuleName": "cardscanr_market_engine.providers.post_sold_capture_worker",
        "hasWriteUtf8Atomic": hasattr(cap, "write_utf8_bytes_atomic"),
        "hasCaptureEncodingFailure": hasattr(cap, "CAPTURE_ENCODING_FAILURE"),
        "modules": modules,
        "matchesClosure": all_match,
    }


def capture_readiness_from_closure() -> dict[str, Any]:
    data = json.loads(READINESS_PATH.read_text(encoding="utf-8-sig")) if READINESS_PATH.is_file() else {}
    needed = [
        "cdpProcessBoundaryReady",
        "unicodeCaptureReady",
        "largeHtmlCaptureReady",
        "artifactWriteReady",
        "artifactShaReady",
        "subprocessProtocolUtf8Ready",
        "failureTaxonomyReady",
    ]
    ok = all(bool(data.get(k)) for k in needed)
    return {"ok": ok, "flags": {k: bool(data.get(k)) for k in needed}, "source": str(READINESS_PATH)}


def _gate_ok() -> tuple[bool, dict[str, Any]]:
    try:
        gate = evaluate_ebay_browser_work_gate(market="AU", for_probe=False)
        cd = get_active_cooldown("AU")
        flag_path = ROOT / "reports" / "runtime" / "owned_daily_full_enable.flag"
        flag = flag_path.read_text(encoding="utf-8").strip() if flag_path.is_file() else "false"
        payload = {
            "gate": gate.to_dict(),
            "activeAuChallenges": gate.active_challenge_count,
            "cooldown": None if cd is None else cd.to_dict(),
            "ownedDaily": flag,
            "probeInFlight": gate.to_dict().get("probeInFlight"),
            "stateIntegrityOk": gate.to_dict().get("stateIntegrityOk"),
        }
        ok = (
            bool(gate.allowed)
            and gate.active_challenge_count == 0
            and flag == "false"
            and payload["probeInFlight"] in (False, None)
            and payload["stateIntegrityOk"] in (True, None)
            and cd is None
        )
        return ok, payload
    except (OSError, PermissionError, TimeoutError, Exception) as exc:
        # Fail closed: gate unreadable must not crash stop accounting.
        return False, {
            "gate": {"allowed": False, "error": f"{type(exc).__name__}:{exc}"[:400]},
            "activeAuChallenges": None,
            "cooldown": None,
            "ownedDaily": "unknown",
            "probeInFlight": None,
            "stateIntegrityOk": False,
            "gateReadError": f"{type(exc).__name__}:{exc}"[:400],
        }


def _pre_live_ok(
    *,
    runtime_mode: str = "COLD_START",
    expected_prior: dict[str, Any] | None = None,
) -> tuple[bool, dict[str, Any]]:
    payload = probe_pre_live_runtime(
        cdp_port=9444,
        runtime_mode=runtime_mode,
        expected_prior=expected_prior,
    )
    needed = [
        "xvfbReady",
        "cdpReady",
        "x11NavigationRuntimeReady",
        "chromeWindowReady",
        "windowFocusReady",
        "keyboardInjectionReady",
        "preSubmitGuiReady",
        "searchToolSelfCheck",
        "soldToolSelfCheck",
    ]
    # Mode-aware: INTER_CARD may accept exactly one expected prior marketplace target.
    target_ok = bool((payload.get("targetPolicy") or {}).get("ok", False)) if payload.get("targetPolicy") else (
        not payload.get("ebayTargets")
    )
    ok = bool(payload.get("ready")) and all(payload.get(k) for k in needed) and target_ok
    payload["neededOk"] = {k: bool(payload.get(k)) for k in needed}
    payload["runtimeMode"] = runtime_mode
    return ok, payload


def _cache_snap(row: dict[str, Any] | None) -> dict[str, Any]:
    row = row or {}
    source_class = classify_owned_price_source(
        current_market_price=row.get("current_market_price"),
        display_price_source=row.get("display_price_source"),
        provider=row.get("provider"),
    )
    return {
        "price": row.get("current_market_price"),
        "recommendedPrice": row.get("recommended_price"),
        "source": row.get("display_price_source") or row.get("provider"),
        "provider": row.get("provider"),
        "displayPriceSource": row.get("display_price_source"),
        "freshness": row.get("last_updated_at") or row.get("updated_at"),
        "refreshStatus": row.get("refresh_status"),
        "sampleCount": row.get("sample_count") or row.get("comp_count"),
        "confidence": row.get("confidence"),
        "sourceClass": source_class,
        "verifiedLocal": source_class == "verified_local",
        "referenceOnly": source_class == "reference_only",
    }


def _diag(result: dict[str, Any]) -> dict[str, Any]:
    pd = result.get("providerDiagnostics") or {}
    if isinstance(pd, dict) and isinstance(pd.get("diagnostics"), dict):
        return pd.get("diagnostics") or {}
    return pd if isinstance(pd, dict) else {}


def _extract_capture(
    result: dict[str, Any],
    diag: dict[str, Any],
    *,
    job_id: str | None = None,
    attempt_id: str | None = None,
    price_key_id: str | None = None,
    fingerprint: str | None = None,
) -> dict[str, Any]:
    """Attach capture evidence only when correlated to the current job.

    Never hydrates global post_sold_capture_last into reliability evidence.
    """
    from cardscanr_market_engine.capture_evidence_correlation import (
        correlate_capture_evidence,
        not_run_capture_block,
    )

    corr = correlate_capture_evidence(
        result=result,
        diag=diag,
        expected_job_id=job_id,
        expected_attempt_id=attempt_id,
        expected_price_key_id=price_key_id,
        expected_fingerprint=fingerprint,
        allow_global_last_capture=False,
    )
    if corr.status == "NOT_RUN":
        block = not_run_capture_block()
        block["details"] = corr.details
        return block

    probe = result.get("postSoldCapture") or diag.get("postSoldCapture") or {}
    if not isinstance(probe, dict):
        probe = {}
    pdiag = probe.get("diagnostics") if isinstance(probe.get("diagnostics"), dict) else {}
    cap_proc = pdiag.get("captureProcess") or result.get("captureProcess") or {}
    if not isinstance(cap_proc, dict):
        cap_proc = {}

    html_path = corr.artifact_path
    sha = corr.sha256
    sha_ok = False
    utf8_ok = False
    bytes_len = None
    chars_len = probe.get("documentBodyChars")
    if html_path and Path(str(html_path)).is_file() and "post_sold_capture_last" not in str(html_path).replace("\\", "/"):
        raw = Path(str(html_path)).read_bytes()
        bytes_len = len(raw)
        try:
            text = raw.decode("utf-8")
            utf8_ok = True
            chars_len = len(text)
            recomputed = hashlib.sha256(raw).hexdigest()
            sha_ok = bool(sha) and recomputed == str(sha)
            if not sha:
                sha = recomputed
        except UnicodeDecodeError:
            utf8_ok = False

    return {
        "status": corr.status,
        "phase": corr.phase or result.get("postSoldCapturePhase") or probe.get("capturePhase"),
        "failureClass": probe.get("failureClass") or result.get("failureClass"),
        "failureDetail": probe.get("failureDetail"),
        "targetId": corr.target_id,
        "targetUrl": probe.get("targetUrl"),
        "targetTitle": probe.get("targetTitle"),
        "captureMethod": probe.get("captureMethod"),
        "elapsedMs": probe.get("captureElapsedMs") or cap_proc.get("elapsedMs"),
        "exitCode": cap_proc.get("exitCode"),
        "orphanCount": cap_proc.get("orphanCountAfter"),
        "htmlChars": chars_len,
        "htmlBytes": bytes_len,
        "bodyLength": probe.get("documentBodyChars"),
        "artifactPath": html_path if corr.status == "ATTACHED" else None,
        "sha256": sha if corr.status == "ATTACHED" else None,
        "shaVerified": sha_ok,
        "utf8Verified": utf8_ok,
        "protocolBytesApprox": cap_proc.get("stdoutByteLength"),
        "workerStatus": cap_proc.get("status"),
        "captureOrigin": corr.capture_origin,
        "correlated": corr.correlated,
        "rejectionReasons": corr.rejection_reasons,
        "globalLastCaptureAuthoritative": False,
        "identityHint": {
            "targetUrl": probe.get("targetUrl"),
            "targetTitle": probe.get("targetTitle"),
        },
    }


def _parse_block(result: dict[str, Any], diag: dict[str, Any]) -> dict[str, Any]:
    pd = result.get("providerDiagnostics") if isinstance(result.get("providerDiagnostics"), dict) else {}
    stats = pd.get("pricingStats") or result.get("pricingStats") or {}
    if not isinstance(stats, dict):
        stats = {}
    accepted = stats.get("acceptedPrices") or stats.get("accepted_prices") or result.get("acceptedPrices")
    return {
        "phase": result.get("parsePhase") or diag.get("parsePhase"),
        "fetched": stats.get("fetched") or stats.get("fetchedCount") or pd.get("fetchedCount"),
        "accepted": stats.get("accepted") or stats.get("acceptedCount") or pd.get("acceptedCount"),
        "rejected": stats.get("rejected") or stats.get("rejectedCount") or pd.get("rejectedCount"),
        "rejectionReasons": stats.get("rejectionReasons") or pd.get("rejectionReasons"),
        "acceptedPrices": accepted,
        "median": stats.get("median") or result.get("recommendedPrice"),
        "confidence": stats.get("confidence") or result.get("confidence"),
        "basis": stats.get("priceBasis") or result.get("priceBasis") or pd.get("final_price_basis"),
        "currency": result.get("currency") or "AUD",
        "sampleCount": stats.get("sampleCount") or result.get("sampleCount"),
    }


def _ownership(client: SupabaseMarketEngineClient) -> dict[str, Any]:
    payload = client.list_owned_market_pricing_targets(include_zero_owners=False)
    targets = payload.get("targets") or payload.get("items") or []
    if isinstance(payload, list):
        targets = payload
    qty = sum(int(t.get("total_owned_quantity") or 0) for t in targets if isinstance(t, dict))
    return {"ownedTargets": len(targets), "ownershipQuantitySum": qty}


def _build_job_identity(client: SupabaseMarketEngineClient, sel: dict[str, Any]) -> dict[str, Any]:
    kid = str(sel["priceKeyId"])
    key = client.get_price_key(price_key_id=kid)
    market = resolve_marketplace_config(
        market_country=str(getattr(key, "market_country", None) or "au"),
        currency=str(getattr(key, "currency", None) or sel.get("currency") or "AUD"),
        marketplace="ebay",
    )
    request = ProviderRequest(
        price_key=key,
        market_country=market.market_country,
        currency=market.currency,
        marketplace=market.marketplace,
        provider_marketplace_id=market.provider_marketplace_id,
        provider_domain=market.provider_domain,
        search_locale=market.search_locale,
        display_name=market.display_name,
        market_config=market,
    )
    queries = build_provider_search_queries(request, max_attempts=1)
    if not queries:
        raise RuntimeError("query_construction_failed:empty")
    primary = queries[0]
    return {
        "priceKeyId": kid,
        "fingerprint": str(getattr(key, "fingerprint", None) or sel.get("fingerprint") or ""),
        "card": str(getattr(key, "card_name", None) or sel.get("card") or ""),
        "set": str(getattr(key, "set_name", None) or sel.get("set") or ""),
        "collector": str(getattr(key, "collector_number", None) or sel.get("collector") or ""),
        "language": str(getattr(key, "language", None) or sel.get("language") or "en"),
        "market": "AU",
        "currency": str(getattr(key, "currency", None) or sel.get("currency") or "AUD"),
        "schedulerReason": sel.get("schedulerReason"),
        "priorityBand": sel.get("priorityBand"),
        "query": primary.query_text,
        "querySource": primary.query_source,
        "queryIndex": primary.query_index,
        "queryReady": True,
    }


def preflight_only() -> int:
    _configure()
    load_supabase_env()
    OUT.mkdir(parents=True, exist_ok=True)
    BOOT.mkdir(parents=True, exist_ok=True)
    BEFORE.mkdir(parents=True, exist_ok=True)
    ATTEMPTS.mkdir(parents=True, exist_ok=True)

    mods = verify_runtime_modules()
    (BOOT / "runtime_modules.json").write_text(json.dumps(mods, indent=2) + "\n", encoding="utf-8")
    cap_ready = capture_readiness_from_closure()
    (BOOT / "capture_readiness.json").write_text(json.dumps(cap_ready, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"RUNTIME_MODULES": {"matchesClosure": mods["matchesClosure"], "python": mods["pythonExecutable"]}}, indent=2))
    print(json.dumps({"CAPTURE_READINESS": cap_ready}, indent=2))
    if not mods["matchesClosure"] or not mods["hasWriteUtf8Atomic"] or not mods["hasCaptureEncodingFailure"]:
        print("PREFLIGHT_STOP stale_runtime_modules")
        return 2
    if not cap_ready["ok"]:
        print("PREFLIGHT_STOP capture_readiness")
        return 2

    ok, pre = _gate_ok()
    (BOOT / "phase0_gate.json").write_text(json.dumps(pre, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"PHASE0": {"ok": ok, "allowed": pre["gate"].get("allowed"), "challenges": pre["activeAuChallenges"], "cooldown": pre["cooldown"], "ownedDaily": pre["ownedDaily"]}}, indent=2))
    if not ok:
        print("PREFLIGHT_STOP phase0")
        return 2

    xv = ensure_xvfb()
    (BOOT / "ensure_xvfb.json").write_text(json.dumps(xv, indent=2) + "\n", encoding="utf-8")
    try:
        ensure_chrome_with_cdp(cdp_port=9444, start_url="about:blank")
    except RuntimeError as exc:
        if "CDP_HAS_EBAY_TARGET" not in str(exc) and "CDP_READY_BUT_EBAY_TARGET" not in str(exc):
            raise
        restart = _restart_chrome_blank()
        (BOOT / "chrome_restart_blank.json").write_text(json.dumps(restart, indent=2) + "\n", encoding="utf-8")
        if not restart.get("ok"):
            print("PREFLIGHT_STOP leftover_ebay_target_could_not_restart")
            return 2
        ensure_chrome_with_cdp(cdp_port=9444, start_url="about:blank")
    rt_ok, rt = _pre_live_ok()
    (BOOT / "runtime_gate.json").write_text(json.dumps(rt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"PHASE1": {"ok": rt_ok, "needed": rt.get("neededOk"), "ebayTargets": rt.get("ebayTargets"), "ready": rt.get("ready")}}, indent=2))
    if not rt_ok:
        print("PREFLIGHT_STOP phase1")
        return 2

    cfg = MarketEngineConfig.from_env()
    client = SupabaseMarketEngineClient(supabase_url=cfg.supabase_url, service_role_key=supabase_secret_key_from_env())
    own = _ownership(client)
    (BEFORE / "ownership_before.json").write_text(json.dumps(own, indent=2) + "\n", encoding="utf-8")
    print("OWNERSHIP_BEFORE", own)
    print("PHASE0_PHASE1_PASS")
    return 0


def main() -> int:
    started = _utc()
    _configure()
    load_supabase_env()
    for d in (OUT, CARDS_DIR, BOOT, BEFORE, AFTER, ATTEMPTS):
        d.mkdir(parents=True, exist_ok=True)

    accounting = {
        "reliabilityRunAuthorisedMax": AUTHORISED_MAX,
        "liveNavigationStartedCount": 0,
        "terminalAttemptCount": 0,
        "completedHealthyPathCount": 0,
        "safeNoPriceCount": 0,
        "failedAttemptCount": 0,
        "challengeStopCount": 0,
        "retryCount": 0,
        "attemptIds": [],
        "interCardDelaysSec": [],
    }
    run: dict[str, Any] = {
        "taskId": "CARDSCANR-EBAY-AU-FIVE-CONSECUTIVE-POST-UNICODE-FIX",
        "startedAtUtc": started,
        "historicalCeruledgeUnchanged": True,
        "attemptAccounting": accounting,
        "cards": [],
        "codeFrozenAfterFirstSearch": False,
        "liveAttemptDefinition": "SEARCH_SUBMISSION_STARTED durable event only",
    }

    rc = preflight_only()
    if rc != 0:
        run["verdict"] = "5_CARD_RELIABILITY_STOPPED_SAFE"
        run["stopReason"] = "preflight_denied"
        run["finishedAtUtc"] = _utc()
        (OUT / "RUN_RESULT.json").write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(run, indent=2)[:4000])
        return 2

    cfg = MarketEngineConfig.from_env()
    client = SupabaseMarketEngineClient(supabase_url=cfg.supabase_url, service_role_key=supabase_secret_key_from_env())
    selections = _select_due_cards(client, limit=AUTHORISED_MAX)
    persist_sel = []
    for row in selections:
        slim = {k: v for k, v in row.items() if k != "rawTarget"}
        cache = _cache_snap(client.get_cache_row(price_key_id=str(row["priceKeyId"])) or {})
        slim["beforeCache"] = cache
        persist_sel.append(slim)
    run["schedulerSelection"] = persist_sel
    (OUT / "scheduler_selection.json").write_text(json.dumps(persist_sel, indent=2) + "\n", encoding="utf-8")
    if len(selections) < AUTHORISED_MAX:
        run["verdict"] = "5_CARD_RELIABILITY_STOPPED_SAFE"
        run["stopReason"] = f"insufficient_due_targets:{len(selections)}"
        run["finishedAtUtc"] = _utc()
        (OUT / "RUN_RESULT.json").write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
        print("STOP insufficient scheduler targets", len(selections))
        return 1

    pacing = OwnedDailyPacingController(OwnedDailyPacingConfig.from_env())
    shared_runner = MarketPriceJobRunner(
        client=client,
        provider=create_market_comps_provider("ebay_browser"),
        config=cfg,
    )
    shared_runner._ebay_probe_mode = False  # noqa: SLF001

    card_reports: list[dict[str, Any]] = []
    stop_run = False
    stop_reason = None
    last_card_end: float | None = None

    for idx in range(AUTHORISED_MAX):
        position = idx + 1
        report: dict[str, Any] = {
            "reliabilityPosition": position,
            "selection": persist_sel[idx] if idx < len(persist_sel) else {},
            "cardVerdict": None,
        }
        if stop_run:
            report["cardVerdict"] = "NOT_ATTEMPTED_AFTER_STOP"
            card_reports.append(report)
            (CARDS_DIR / f"card_{position}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            continue

        g_ok, g_payload = _gate_ok()
        r_ok, r_payload = _pre_live_ok()
        report["preflight"] = {"marketplace": g_payload, "runtime": r_payload, "ok": g_ok and r_ok}
        if not g_ok or not r_ok:
            report["cardVerdict"] = "STOP_PREFLIGHT"
            stop_run = True
            stop_reason = f"preflight_denied_before_card_{position}"
            card_reports.append(report)
            (CARDS_DIR / f"card_{position}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            break

        sel = selections[idx]
        kid = str(sel["priceKeyId"])
        before = _cache_snap(client.get_cache_row(price_key_id=kid) or {})
        report["before"] = before
        try:
            identity = _build_job_identity(client, sel)
        except Exception as exc:
            report["cardVerdict"] = "STOP_PREFLIGHT"
            report["identityError"] = f"{type(exc).__name__}:{exc}"
            stop_run = True
            stop_reason = f"query_identity_failed_before_card_{position}"
            card_reports.append(report)
            (CARDS_DIR / f"card_{position}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            break

        attempt_id = new_attempt_id()
        accounting["attemptIds"].append(attempt_id)
        report["identity"] = identity
        report["attemptId"] = attempt_id
        print(
            f"[reliability] CARD {position}/{AUTHORISED_MAX} {identity.get('card')} "
            f"key={kid} attempt={attempt_id} query={identity.get('query')!r}",
            flush=True,
        )

        os.environ["CARDSCANR_LIVE_ATTEMPT_ID"] = attempt_id
        os.environ["CARDSCANR_PRICE_KEY_ID"] = kid
        t0 = time.monotonic()
        payload: dict[str, Any] | None = None
        exc_text = None
        try:
            payload = run_forced_job(
                client,
                price_key_id=kid,
                reason=f"reliability_five_consecutive_post_unicode:{sel.get('schedulerReason') or 'owned_daily'}",
                runner=shared_runner,
            )
        except Exception as exc:
            exc_text = f"{type(exc).__name__}:{exc}"
            payload = {
                "result": {"status": "failed", "error": exc_text},
                "before": before,
                "after": _cache_snap(client.get_cache_row(price_key_id=kid) or {}),
                "durationSec": round(time.monotonic() - t0, 1),
            }
        finally:
            os.environ.pop("CARDSCANR_LIVE_ATTEMPT_ID", None)
            os.environ.pop("CARDSCANR_PRICE_KEY_ID", None)

        result = payload.get("result") if isinstance(payload, dict) else {}
        result = result if isinstance(result, dict) else {}
        diag = _diag(result)
        search_started = has_search_submission_started(attempt_id)
        if search_started:
            accounting["liveNavigationStartedCount"] += 1
            run["codeFrozenAfterFirstSearch"] = True
        event = read_search_submission_event(attempt_id)
        report["searchSubmissionEvent"] = event
        challenge = is_structured_challenge(result, diag)
        accounting["terminalAttemptCount"] += 1 if search_started else 0
        after = payload.get("after") if isinstance(payload, dict) else _cache_snap(client.get_cache_row(price_key_id=kid) or {})
        report["after"] = after
        report["job"] = {
            "jobId": payload.get("jobId") if isinstance(payload, dict) else None,
            "durationSec": payload.get("durationSec") if isinstance(payload, dict) else None,
            "status": result.get("status"),
            "ownedDailyOutcome": result.get("ownedDailyOutcome") or result.get("outcomeClass"),
            "recommendedPrice": result.get("recommendedPrice"),
            "error": result.get("error") or exc_text,
            "postSoldCapturePhase": result.get("postSoldCapturePhase"),
            "x11SoldStateVerified": result.get("x11SoldStateVerified") or diag.get("x11SoldStateVerified"),
            "parsePhase": result.get("parsePhase"),
            "providerDiagnostics": result.get("providerDiagnostics"),
            "liveAttemptNumber": accounting["liveNavigationStartedCount"] if search_started else None,
            "searchSubmitted": search_started,
            "attemptId": attempt_id,
        }
        nav = diag.get("desktopNav") or diag.get("linuxNav") or {}
        report["navigation"] = {
            "liveAttemptNumber": accounting["liveNavigationStartedCount"] if search_started else None,
            "searchSubmitted": search_started,
            "desktopNav": nav,
            "finalUrl": (nav if isinstance(nav, dict) else {}).get("url") or diag.get("finalUrl") or result.get("sourceUrl"),
            "x11SoldStateVerified": result.get("x11SoldStateVerified") or diag.get("x11SoldStateVerified"),
        }
        report["challenge"] = {
            "activeChallenge": challenge,
            "providerOutcome": diag.get("providerOutcome"),
            "operationalStatus": diag.get("operationalStatus"),
            "structuredOnly": True,
        }
        report["capture"] = _extract_capture(
            result,
            diag,
            job_id=str(payload.get("jobId") or "") if isinstance(payload, dict) else None,
            attempt_id=attempt_id,
            price_key_id=kid,
            fingerprint=str(identity.get("fingerprint") or ""),
        )
        if report["capture"].get("status") == "NOT_RUN":
            report["parse"] = {
                "phase": "NOT_RUN",
                "fetched": None,
                "accepted": None,
                "rejected": None,
                "rejectionReasons": None,
                "acceptedPrices": None,
                "median": None,
                "confidence": None,
                "basis": None,
                "currency": identity.get("currency") or "AUD",
                "sampleCount": None,
            }
        else:
            report["parse"] = _parse_block(result, diag)
        report["write"] = {
            "outcome": result.get("ownedDailyOutcome") or result.get("outcomeClass"),
            "snapshot": result.get("snapshotId") or result.get("priceSnapshotId"),
            "priorPrice": before.get("price"),
            "resultingPrice": after.get("price") if isinstance(after, dict) else None,
            "provider": after.get("provider") if isinstance(after, dict) else None,
            "displaySource": after.get("displayPriceSource") if isinstance(after, dict) else None,
            "refreshStatus": after.get("refreshStatus") if isinstance(after, dict) else None,
            "freshness": after.get("freshness") if isinstance(after, dict) else None,
        }

        verdict = classify_reliability_card_verdict(
            result,
            diag=diag,
            search_submission_started=search_started,
            preflight_failed=False,
        )
        if verdict == "PASS_PRICE_UPDATED":
            try:
                bp = before.get("price")
                ap = after.get("price") if isinstance(after, dict) else None
                if bp is not None and ap is not None and float(bp) == float(ap):
                    if str(result.get("ownedDailyOutcome") or "") == UNCHANGED_FROM_EBAY:
                        verdict = "PASS_PRICE_UNCHANGED"
                    elif str(result.get("ownedDailyOutcome") or "") != UPDATED_FROM_EBAY:
                        verdict = "PASS_PRICE_UNCHANGED"
            except (TypeError, ValueError):
                pass
        report["cardVerdict"] = verdict

        if challenge:
            accounting["challengeStopCount"] += 1
            stop_run = True
            stop_reason = f"STOP_CHALLENGE_card_{position}"
        elif verdict in {"PASS_PRICE_UPDATED", "PASS_PRICE_UNCHANGED", "SAFE_NO_NEW_EXACT_EVIDENCE"}:
            accounting["completedHealthyPathCount"] += 1
            if verdict == "SAFE_NO_NEW_EXACT_EVIDENCE":
                accounting["safeNoPriceCount"] += 1
        elif search_started:
            accounting["failedAttemptCount"] += 1
            stop_run = True
            stop_reason = f"{verdict}_card_{position}"
        else:
            stop_run = True
            stop_reason = f"{verdict}_card_{position}"

        _, g_after = _gate_ok()
        report["controlPlaneAfter"] = g_after
        card_reports.append(report)
        (CARDS_DIR / f"card_{position}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"CARD": position, "verdict": verdict, "accounting": accounting}, indent=2), flush=True)

        last_card_end = time.monotonic()
        if stop_run:
            break
        if position < AUTHORISED_MAX:
            outcome = str(result.get("ownedDailyOutcome") or result.get("outcomeClass") or "")
            pacing.observe_outcome(outcome)
            delay = int(pacing.next_delay_seconds()) if hasattr(pacing, "next_delay_seconds") else int(pacing.config.min_inter_job_delay_seconds)
            # Fallback if controller API differs
            if delay <= 0:
                delay = int(pacing.config.min_inter_job_delay_seconds)
            accounting["interCardDelaysSec"].append(delay)
            print(f"[reliability] pacing sleep {delay}s before next card", flush=True)
            time.sleep(delay)

    while len(card_reports) < AUTHORISED_MAX:
        position = len(card_reports) + 1
        report = {
            "reliabilityPosition": position,
            "cardVerdict": "NOT_ATTEMPTED_AFTER_STOP",
            "selection": persist_sel[position - 1] if position - 1 < len(persist_sel) else {},
        }
        card_reports.append(report)
        (CARDS_DIR / f"card_{position}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    own_after = _ownership(client)
    own_before = json.loads((BEFORE / "ownership_before.json").read_text(encoding="utf-8"))
    (AFTER / "ownership_after.json").write_text(json.dumps(own_after, indent=2) + "\n", encoding="utf-8")
    mutations = int(own_after.get("ownershipQuantitySum") or 0) - int(own_before.get("ownershipQuantitySum") or 0)
    run["ownership"] = {"before": own_before, "after": own_after, "mutations": mutations}

    run["cards"] = card_reports
    run["stopReason"] = stop_reason
    run["attemptAccounting"] = accounting
    healthy = accounting["completedHealthyPathCount"]
    if mutations != 0:
        run["verdict"] = "5_CARD_RELIABILITY_FAIL"
        run["stopReason"] = (stop_reason or "") + ";OWNERSHIP_MUTATION"
    elif healthy == AUTHORISED_MAX and accounting["challengeStopCount"] == 0 and accounting["failedAttemptCount"] == 0 and accounting["retryCount"] == 0:
        run["verdict"] = "5_CARD_RELIABILITY_PASS"
    elif accounting["liveNavigationStartedCount"] > AUTHORISED_MAX:
        run["verdict"] = "5_CARD_RELIABILITY_FAIL"
    else:
        run["verdict"] = "5_CARD_RELIABILITY_STOPPED_SAFE"

    _, g_final = _gate_ok()
    run["controlPlaneFinal"] = g_final
    run["runtimeFinal"] = probe_pre_live_runtime(cdp_port=9444)
    run["finishedAtUtc"] = _utc()
    run["ownedDaily"] = {
        "flag": (ROOT / "reports" / "runtime" / "owned_daily_full_enable.flag").read_text(encoding="utf-8").strip(),
        "scheduler": "NOT_RUNNING",
        "worker": "NOT_RUNNING",
    }
    (OUT / "RUN_RESULT.json").write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"VERDICT": run["verdict"], "accounting": accounting, "stopReason": stop_reason, "ownershipMutations": mutations}, indent=2))
    return 0 if run["verdict"] != "5_CARD_RELIABILITY_FAIL" else 3


if __name__ == "__main__":
    if "--preflight-only" in sys.argv:
        raise SystemExit(preflight_only())
    raise SystemExit(main())
