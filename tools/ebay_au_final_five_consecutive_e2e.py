#!/usr/bin/env python3
"""Authorised FINAL 5-card consecutive eBay AU E2E reliability proof.

Fresh run against the corrected codebase (execution eligibility + capture correlation).
Live eBay searches capped at 5 via SEARCH_SUBMISSION_STARTED only.
Does NOT resume any previous run.
After first SEARCH_SUBMISSION_STARTED: treat production code as frozen (harness stops on defect).
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
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.capture_evidence_correlation import (
    correlate_capture_evidence,
    not_run_capture_block,
)
from cardscanr_market_engine.config import MarketEngineConfig, supabase_secret_key_from_env
from cardscanr_market_engine.ebay_browser_work_gate import evaluate_ebay_browser_work_gate
from cardscanr_market_engine.job_runner import MarketPriceJobRunner
from cardscanr_market_engine.browser_lifecycle_policy import (
    RUNTIME_COLD_START,
    RUNTIME_INTER_CARD,
    prior_from_card_report,
)
from cardscanr_market_engine.navigation_runtime_context import (
    PRE_SUBMIT_ONLY_ENV,
    RUNTIME_MODE_ENV,
    NavigationRuntimeContext,
    apply_context_to_environ,
    clear_context_from_environ,
    load_navigation_runtime_context,
    pre_submit_only_requested,
)
from cardscanr_market_engine.live_navigation_attempt import (
    attempts_dir,
    capture_attempt_event_baseline,
    count_consumed_live_navigations,
    count_search_submission_started,
    new_attempt_id,
)
from cardscanr_market_engine.reliability_harness_evidence import classify_card_from_job_result
from cardscanr_market_engine.local_browser_runtime import ensure_xvfb, probe_pre_live_runtime
from cardscanr_market_engine.marketplace_ops_state import get_active_cooldown
from cardscanr_market_engine.models import MarketPriceRefreshJob, ProviderRequest
from cardscanr_market_engine.owned_daily_outcomes import UNCHANGED_FROM_EBAY, UPDATED_FROM_EBAY
from cardscanr_market_engine.owned_daily_pacing import OwnedDailyPacingConfig, OwnedDailyPacingController
from cardscanr_market_engine.owned_daily_source_policy import classify_owned_daily_band, classify_owned_price_source
from cardscanr_market_engine.owned_verified_local_execution import (
    evaluate_owned_verified_local_execution,
    scheduler_jobrunner_agreement,
)
from cardscanr_market_engine.providers.factory import create_market_comps_provider
from cardscanr_market_engine.providers.linux_x11_ebay_nav import ensure_chrome_with_cdp
from cardscanr_market_engine.providers.query_builder import build_provider_search_queries
from cardscanr_market_engine.reliability_harness_classification import is_structured_challenge
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient
from cardscanr_market_engine.supabase_env_loader import load_supabase_env
from cardscanr_market_engine.marketplaces import resolve_marketplace_config
from tests.test_market_price_job_runner_cache_states import _FakeClient, _StaticProvider, _sold_comp
from tools.desktop_ebay_e2e_pricing import run_forced_job
from tools.ebay_au_fresh_5_card_reliability_run import _select_due_cards
from tools.ebay_au_five_consecutive_post_unicode_run import (
    _build_job_identity,
    _cache_snap,
    _diag,
    _extract_capture,
    _gate_ok,
    _ownership,
    _parse_block,
    _pre_live_ok,
    _restart_chrome_blank,
    _sha256_file,
    capture_readiness_from_closure,
)

OUT = ROOT / "reports" / "artifacts" / "final_five_consecutive_e2e"
CARDS_DIR = OUT / "cards"
BOOT = OUT / "bootstrap"
BEFORE = OUT / "before"
AFTER = OUT / "after"
ATTEMPTS = OUT / "attempts"
AUTHORISED_MAX = int(os.getenv("CARDSCANR_RELIABILITY_MAX") or "5")
CONTINUE_ON_FRESH_SKIP = os.getenv("CARDSCANR_CONTINUE_ON_FRESH_SKIP", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
TASK_ID = "CARDSCANR-EBAY-AU-FINAL-FIVE-CONSECUTIVE-E2E-PROOF"

REQUIRED_RUNTIME_FILES = [
    "cardscanr_market_engine/job_runner.py",
    "cardscanr_market_engine/owned_verified_local_execution.py",
    "cardscanr_market_engine/capture_evidence_correlation.py",
    "cardscanr_market_engine/providers/ebay_browser_provider.py",
    "cardscanr_market_engine/providers/post_sold_capture.py",
    "cardscanr_market_engine/providers/post_sold_capture_worker.py",
    "cardscanr_market_engine/providers/post_sold_capture_process.py",
    "cardscanr_market_engine/providers/linux_x11_ebay_nav.py",
    "cardscanr_market_engine/navigation_runtime_context.py",
    "cardscanr_market_engine/browser_lifecycle_policy.py",
    "cardscanr_market_engine/live_navigation_attempt.py",
    "tools/linux_x11_ebay_search.py",
    "tools/linux_x11_ebay_sold.py",
]


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
    # Live production must never inherit the offline PRE_SUBMIT_ONLY arming flag.
    os.environ.pop(PRE_SUBMIT_ONLY_ENV, None)
    # Canonical production attempts directory is authoritative (Windows + WSL).
    # Do NOT point CARDSCANR_LIVE_NAV_ATTEMPTS_DIR at a per-run folder — that
    # previously split Windows harness lookup from WSL emission.
    os.environ.pop("CARDSCANR_LIVE_NAV_ATTEMPTS_DIR", None)
    os.environ["CARDSCANR_CAPTURE_ORIGIN"] = "LIVE_BROWSER_CAPTURE"


def _pre_submit_only_safety() -> dict[str, Any]:
    env_raw = os.environ.get(PRE_SUBMIT_ONLY_ENV)
    armed = pre_submit_only_requested(flag=True)
    return {
        "envValue": env_raw,
        "envAbsentOrFalse": env_raw not in {"1", "true", "TRUE", "yes"},
        "preSubmitOnlyArmed": bool(armed),
        "productionMode": True,
        "submissionEnabled": not armed,
        "cliWouldPassPreSubmitOnly": False,
        "ok": (not armed) and env_raw not in {"1", "true", "TRUE", "yes"},
    }


def snapshot_nav_context_boundaries(*, expected_mode: str) -> dict[str, Any]:
    """Record effective live context without launching WSL or submitting search."""
    ctx = load_navigation_runtime_context()
    exports = ctx.env_exports()
    go_home = not ctx.is_inter_card()
    armed = pre_submit_only_requested(flag=False)
    cli = ["linux_x11_ebay_search.py", "--submit", "enter", "--runtime-mode", ctx.runtime_mode]
    if go_home:
        cli.append("--home")
    if pre_submit_only_requested(flag=True):
        cli.append("--pre-submit-only")
    harness_mode = str(os.environ.get(RUNTIME_MODE_ENV) or "")
    layers = {
        "harness": harness_mode or ctx.runtime_mode,
        "jobRunner": harness_mode or ctx.runtime_mode,
        "provider": ctx.runtime_mode,
        "nav": ctx.runtime_mode,
        "wslEnv": str(exports.get(RUNTIME_MODE_ENV) or ""),
        "wslCli": ctx.runtime_mode,
    }
    expected = (expected_mode or "").upper()
    mismatched = [name for name, mode in layers.items() if str(mode).upper() != expected]
    prior = ctx.expected_prior.to_dict() if ctx.expected_prior is not None else None
    ok = (not mismatched) and (not armed) and ("--pre-submit-only" not in cli)
    if expected == RUNTIME_INTER_CARD:
        ok = bool(ok and prior and prior.get("targetId"))
        if go_home or "--home" in cli:
            ok = False
            mismatched.append("homeReset")
    if expected == RUNTIME_COLD_START and prior:
        ok = False
        mismatched.append("unexpectedPrior")
    return {
        "ok": ok,
        "expectedMode": expected,
        "runtimeMode": ctx.runtime_mode,
        "layers": layers,
        "mismatchedLayers": mismatched,
        "expectedPrior": prior,
        "expectedPriorPath": exports.get("CARDSCANR_EXPECTED_PRIOR_JSON_PATH"),
        "navContextPath": exports.get("CARDSCANR_NAV_CONTEXT_JSON_PATH"),
        "wouldPassHome": "--home" in cli,
        "wouldPassPreSubmitOnly": "--pre-submit-only" in cli,
        "preSubmitOnlyArmed": armed,
        "wslCli": cli,
        "wslEnvRuntimeMode": exports.get(RUNTIME_MODE_ENV),
    }


def verify_deployed_code() -> dict[str, Any]:
    import cardscanr_market_engine.capture_evidence_correlation as cec
    import cardscanr_market_engine.job_runner as jr
    import cardscanr_market_engine.owned_verified_local_execution as ove
    import cardscanr_market_engine.providers.ebay_browser_provider as ebp
    import cardscanr_market_engine.providers.linux_x11_ebay_nav as x11
    import cardscanr_market_engine.providers.post_sold_capture as psc
    import cardscanr_market_engine.providers.post_sold_capture_process as psp
    import cardscanr_market_engine.providers.post_sold_capture_worker as psw

    import cardscanr_market_engine.navigation_runtime_context as nrc

    loaded = {
        "job_runner": Path(jr.__file__).resolve(),
        "owned_verified_local_execution": Path(ove.__file__).resolve(),
        "capture_evidence_correlation": Path(cec.__file__).resolve(),
        "ebay_browser_provider": Path(ebp.__file__).resolve(),
        "post_sold_capture": Path(psc.__file__).resolve(),
        "post_sold_capture_worker": Path(psw.__file__).resolve(),
        "post_sold_capture_process": Path(psp.__file__).resolve(),
        "linux_x11_ebay_nav": Path(x11.__file__).resolve(),
        "navigation_runtime_context": Path(nrc.__file__).resolve(),
    }
    modules: dict[str, Any] = {}
    all_ok = True
    for rel in REQUIRED_RUNTIME_FILES:
        p = (ROOT / rel).resolve()
        under_root = str(p).startswith(str(ROOT.resolve()))
        exists = p.is_file()
        sha = _sha256_file(p) if exists else None
        modules[rel] = {"path": str(p), "exists": exists, "underRepoRoot": under_root, "sha256": sha}
        all_ok = all_ok and exists and under_root

    ebp_src = Path(ebp.__file__).read_text(encoding="utf-8")
    jr_src = Path(jr.__file__).read_text(encoding="utf-8")
    nav_src = Path(x11.__file__).read_text(encoding="utf-8")
    search_src = (ROOT / "tools" / "linux_x11_ebay_search.py").read_text(encoding="utf-8")
    markers = {
        "noStaleLastCaptureFallback": 'post_sold_capture_last" / "last_capture.html"' not in ebp_src
        and "post_sold_capture_last' / 'last_capture.html'" not in ebp_src,
        "jobRunnerOwnsOwnedDailySubstring": '"owned_daily:" in' in jr_src or "owned_daily:" in jr_src,
        "hasEvaluateOwnedVerifiedLocal": hasattr(ove, "evaluate_owned_verified_local_execution"),
        "hasCorrelateCaptureEvidence": hasattr(cec, "correlate_capture_evidence"),
        "hasWriteUtf8Atomic": hasattr(psc, "write_utf8_bytes_atomic"),
        "hasCaptureEncodingFailure": hasattr(psc, "CAPTURE_ENCODING_FAILURE"),
        "hydrateSuccessOnlyComment": "Only hydrates on SUCCESS" in Path(psp.__file__).read_text(encoding="utf-8"),
        "runtimeModePropagationReady": "CARDSCANR_RUNTIME_MODE" in nav_src and "load_navigation_runtime_context" in nav_src,
        "interCardAllowsExistingEbay": "CDP_REUSED_EXISTING" in nav_src and "INTER_CARD" in nav_src,
        "searchCliRuntimeMode": "--runtime-mode" in search_src,
        "jobRunnerAppliesNavContext": "apply_context_to_environ" in jr_src,
        "providerPreservesErrorMessage": "errorMessage" in ebp_src and "navigationFailureClass" in ebp_src,
    }
    all_ok = all_ok and all(markers.values())
    # Loaded modules must resolve into this repo tree.
    for name, path in loaded.items():
        if not str(path).startswith(str(ROOT.resolve())):
            all_ok = False
            markers[f"loaded_{name}_under_root"] = False
        else:
            markers[f"loaded_{name}_under_root"] = True

    return {
        "pythonExecutable": sys.executable,
        "sysPrefix": sys.prefix,
        "sysPath0": sys.path[0],
        "repoRoot": str(ROOT.resolve()),
        "loadedModules": {k: str(v) for k, v in loaded.items()},
        "modules": modules,
        "markers": markers,
        "ok": all_ok,
    }


def _cache_row_for_eligibility(client: SupabaseMarketEngineClient, kid: str) -> dict[str, Any]:
    row = client.get_cache_row(price_key_id=kid) or {}
    return {
        "current_market_price": row.get("current_market_price"),
        "display_price_source": row.get("display_price_source"),
        "provider": row.get("provider"),
        "last_updated_at": row.get("last_updated_at"),
        "next_refresh_due_at": row.get("next_refresh_due_at"),
        "stale_after": row.get("stale_after"),
        "refresh_status": row.get("refresh_status"),
        "last_error_message": row.get("last_error_message"),
    }


def eligibility_dry_check(
    client: SupabaseMarketEngineClient,
    selections: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Phase 3: scheduler + source-aware pre-provider eligibility only. No provider."""
    now = now or datetime.now(timezone.utc)
    cards = []
    all_ok = True
    for idx, sel in enumerate(selections[:AUTHORISED_MAX], start=1):
        kid = str(sel["priceKeyId"])
        cache = _cache_row_for_eligibility(client, kid)
        fresh_hours = int((sel.get("demandAware") or {}).get("freshnessThresholdHours") or 24)
        band, due, reason_suffix, _view, _ex = classify_owned_daily_band(
            cache, now=now, success_fresh_hours=fresh_hours
        )
        execution = evaluate_owned_verified_local_execution(
            cache, now=now, success_fresh_hours=fresh_hours
        )
        agr = scheduler_jobrunner_agreement(scheduler_due=due, execution=execution)
        identity = _build_job_identity(client, sel)

        # Job-runner path stop-before-provider using FakeClient — no production writes.
        key = client.get_price_key(price_key_id=kid)
        cfg = MarketEngineConfig.from_env(require_supabase=False)
        fake = _FakeClient(key)
        fake._cache_row = cache
        demand_class = str((sel.get("demandAware") or {}).get("demandClass") or "LOW")
        if demand_class == "HIGH":
            fake.list_recent_user_demand_jobs = lambda hours=168, _kid=kid, _now=now: [
                {
                    "price_key_id": _kid,
                    "reason": "user_refresh",
                    "requested_at": _now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                }
            ]
        else:
            fake.list_recent_user_demand_jobs = lambda hours=168: []
        runner = MarketPriceJobRunner(
            client=fake,
            provider=_StaticProvider([_sold_comp(title=f"{getattr(key, 'card_name', 'card')} Pokemon")]),
            config=cfg,
            now_func=lambda: now,
            logger=lambda _m: None,
        )
        provider_reached = {"called": False}

        def _stop(**_kwargs):
            provider_reached["called"] = True
            raise RuntimeError("ELIGIBILITY_DRY_STOP_BEFORE_PROVIDER")

        would_skip = False
        with mock.patch.object(runner, "_assert_market_allowed_for_worker", return_value=None):
            with mock.patch.object(runner, "fetch_fallback_result", side_effect=_stop):
                job = MarketPriceRefreshJob(
                    id=f"elig-dry-{kid[:8]}",
                    price_key_id=kid,
                    reason=f"final_e2e_elig_dry:owned_daily:{reason_suffix}",
                    priority=1,
                    status="running",
                    attempt_count=1,
                )
                result = runner.run_job(job)
                would_skip = result.get("status") == "skipped_already_fresh"

        capture = not_run_capture_block()
        card_ok = bool(
            due
            and execution.should_execute
            and not would_skip
            and provider_reached["called"]
            and identity.get("queryReady")
            and agr["agrees"]
            and capture["status"] == "NOT_RUN"
        )
        if due and would_skip and not execution.verified_local:
            # Regression of the just-fixed policy.
            card_ok = False
        all_ok = all_ok and card_ok
        cards.append(
            {
                "position": idx,
                "priceKeyId": kid,
                "fingerprint": identity.get("fingerprint"),
                "card": identity.get("card"),
                "set": identity.get("set"),
                "collector": identity.get("collector"),
                "language": identity.get("language"),
                "market": identity.get("market"),
                "currency": identity.get("currency"),
                "price": cache.get("current_market_price"),
                "source": cache.get("display_price_source"),
                "sourceClass": execution.source_class,
                "verifiedLocal": execution.verified_local,
                "referenceOnly": execution.reference_only,
                "schedulerBand": band,
                "schedulerReason": f"owned_daily:{reason_suffix}",
                "executionReasonCode": execution.reason_code,
                "schedulerDue": due,
                "executionEligible": execution.should_execute,
                "wouldSkipFresh": would_skip,
                "query": identity.get("query"),
                "queryReady": identity.get("queryReady"),
                "agreement": agr,
                "captureEvidence": capture,
                "ok": card_ok,
            }
        )
    return {
        "ok": all_ok and len(cards) == AUTHORISED_MAX,
        "candidates": len(cards),
        "allSchedulerDue": all(c["schedulerDue"] for c in cards),
        "allExecutionEligible": all(c["executionEligible"] for c in cards),
        "allWouldSkipFreshFalse": all(not c["wouldSkipFresh"] for c in cards),
        "allQueryReady": all(c["queryReady"] for c in cards),
        "cards": cards,
        "searchSubmissionStartedInRunDir": count_search_submission_started(),
    }


def preflight_only() -> int:
    _configure()
    load_supabase_env()
    for d in (OUT, BOOT, BEFORE, ATTEMPTS):
        d.mkdir(parents=True, exist_ok=True)

    deploy = verify_deployed_code()
    (BOOT / "phase0_deployed_code.json").write_text(json.dumps(deploy, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"PHASE0_DEPLOY": {"ok": deploy["ok"], "python": deploy["pythonExecutable"]}}, indent=2))
    if not deploy["ok"]:
        print("PREFLIGHT_STOP deployed_code_mismatch")
        return 2

    ps = _pre_submit_only_safety()
    (BOOT / "pre_submit_only_safety.json").write_text(json.dumps(ps, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"PRE_SUBMIT_ONLY_SAFETY": ps}, indent=2), flush=True)
    if not ps["ok"]:
        print("PREFLIGHT_STOP pre_submit_only_armed")
        return 2

    cap_ready = capture_readiness_from_closure()
    (BOOT / "capture_readiness.json").write_text(json.dumps(cap_ready, indent=2) + "\n", encoding="utf-8")
    if not cap_ready["ok"]:
        print("PREFLIGHT_STOP capture_readiness")
        return 2

    ok, pre = _gate_ok()
    (BOOT / "phase1_marketplace.json").write_text(json.dumps(pre, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "PHASE1": {
                    "ok": ok,
                    "allowed": pre["gate"].get("allowed"),
                    "challenges": pre["activeAuChallenges"],
                    "cooldown": pre["cooldown"],
                    "ownedDaily": pre["ownedDaily"],
                }
            },
            indent=2,
        )
    )
    if not ok:
        print("PREFLIGHT_STOP phase1_marketplace")
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
    (BOOT / "phase2_gui_capture.json").write_text(json.dumps(rt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"PHASE2": {"ok": rt_ok, "needed": rt.get("neededOk"), "ebayTargets": rt.get("ebayTargets")}}, indent=2))
    if not rt_ok:
        print("PREFLIGHT_STOP phase2_gui")
        return 2

    cfg = MarketEngineConfig.from_env()
    client = SupabaseMarketEngineClient(
        supabase_url=cfg.supabase_url,
        service_role_key=supabase_secret_key_from_env(),
    )
    own = _ownership(client)
    (BEFORE / "ownership_before.json").write_text(json.dumps(own, indent=2) + "\n", encoding="utf-8")
    print("OWNERSHIP_BEFORE", own)
    print("PHASE0_PHASE1_PHASE2_PASS")
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
        "taskId": TASK_ID,
        "startedAtUtc": started,
        "freshRunNotResume": True,
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
        print(json.dumps({"VERDICT": run["verdict"], "stopReason": run["stopReason"]}, indent=2))
        return 2

    cfg = MarketEngineConfig.from_env()
    client = SupabaseMarketEngineClient(
        supabase_url=cfg.supabase_url,
        service_role_key=supabase_secret_key_from_env(),
    )
    select_limit = int(os.getenv("CARDSCANR_RELIABILITY_SELECT_LIMIT") or str(AUTHORISED_MAX))
    selections = _select_due_cards(client, limit=max(AUTHORISED_MAX, select_limit))
    persist_sel = []
    for row in selections:
        slim = {k: v for k, v in row.items() if k != "rawTarget"}
        cache = _cache_snap(client.get_cache_row(price_key_id=str(row["priceKeyId"])) or {})
        slim["beforeCache"] = cache
        persist_sel.append(slim)
    run["schedulerSelection"] = persist_sel
    (OUT / "scheduler_selection.json").write_text(json.dumps(persist_sel, indent=2) + "\n", encoding="utf-8")
    if len(selections) < AUTHORISED_MAX and not CONTINUE_ON_FRESH_SKIP:
        run["verdict"] = "5_CARD_RELIABILITY_STOPPED_SAFE"
        run["stopReason"] = f"insufficient_due_targets:{len(selections)}"
        run["finishedAtUtc"] = _utc()
        (OUT / "RUN_RESULT.json").write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
        print("STOP insufficient scheduler targets", len(selections))
        return 1
    if CONTINUE_ON_FRESH_SKIP and not selections:
        run["verdict"] = "5_CARD_RELIABILITY_STOPPED_SAFE"
        run["stopReason"] = "insufficient_due_targets:0"
        run["finishedAtUtc"] = _utc()
        (OUT / "RUN_RESULT.json").write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
        print("STOP insufficient scheduler targets", 0)
        return 1

    # Phase 3 — eligibility dry check (no provider / no search).
    elig = eligibility_dry_check(client, selections)
    (BOOT / "phase3_execution_eligibility.json").write_text(json.dumps(elig, indent=2) + "\n", encoding="utf-8")
    run["executionEligibilityDryCheck"] = elig
    print(
        json.dumps(
            {
                "PHASE3": {
                    "ok": elig["ok"],
                    "candidates": elig["candidates"],
                    "allSchedulerDue": elig["allSchedulerDue"],
                    "allExecutionEligible": elig["allExecutionEligible"],
                    "allWouldSkipFreshFalse": elig["allWouldSkipFreshFalse"],
                    "searchEvents": elig["searchSubmissionStartedInRunDir"],
                }
            },
            indent=2,
        )
    )
    if not elig["ok"]:
        run["verdict"] = "5_CARD_RELIABILITY_STOPPED_SAFE"
        run["stopReason"] = "execution_eligibility_dry_check_failed"
        run["finishedAtUtc"] = _utc()
        (OUT / "RUN_RESULT.json").write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
        print("PREFLIGHT_STOP phase3_eligibility")
        return 2
    # Canonical attempts dir retains historical SEARCH_SUBMISSION_STARTED files.
    # That is expected; consumption is by this-run attemptId only. Never delete.
    run["attemptEventBaseline"] = capture_attempt_event_baseline()
    (BOOT / "attempt_event_baseline.json").write_text(
        json.dumps(run["attemptEventBaseline"], indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "ATTEMPT_BASELINE": {
                    "historicalEvents": run["attemptEventBaseline"]["count"],
                    "ok": True,
                    "deleted": False,
                }
            },
            indent=2,
        ),
        flush=True,
    )

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

    HEALTHY_VERDICTS = {
        "PASS_PRICE_UPDATED",
        "PASS_PRICE_UNCHANGED",
        "SAFE_NO_NEW_EXACT_EVIDENCE",
    }
    pool_i = 0
    executed_live = 0
    while executed_live < AUTHORISED_MAX and pool_i < len(selections):
        idx = pool_i
        pool_i += 1
        position = executed_live + 1
        report: dict[str, Any] = {
            "reliabilityPosition": position,
            "selection": persist_sel[idx] if idx < len(persist_sel) else {},
            "cardVerdict": None,
        }
        if stop_run:
            break

        g_ok, g_payload = _gate_ok()
        # Card 1 = COLD_START; after a healthy terminal card = INTER_CARD with expected prior.
        prior_ctx = None
        runtime_mode = RUNTIME_COLD_START
        healthy_prior = [
            c
            for c in card_reports
            if str(c.get("cardVerdict") or "") in HEALTHY_VERDICTS
        ]
        if healthy_prior:
            last = healthy_prior[-1]
            last_verdict = str(last.get("cardVerdict") or "")
            if last_verdict in HEALTHY_VERDICTS:
                prior_obj = prior_from_card_report(last)
                prior_ctx = prior_obj.to_dict() if prior_obj else None
                runtime_mode = RUNTIME_INTER_CARD
                if prior_ctx is None:
                    report["cardVerdict"] = "STOP_PREFLIGHT"
                    report["contextError"] = "INTER_CARD_PRIOR_CONTEXT_MISSING"
                    stop_run = True
                    stop_reason = f"inter_card_prior_missing_before_card_{position}"
                    card_reports.append(report)
                    (CARDS_DIR / f"card_{position}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
                    break
        r_ok, r_payload = _pre_live_ok(runtime_mode=runtime_mode, expected_prior=prior_ctx)
        report["runtimeMode"] = runtime_mode
        report["expectedPrior"] = prior_ctx
        # Per-card source-aware eligibility (no provider).
        kid = str(selections[idx]["priceKeyId"])
        demand_meta = (selections[idx].get("demandAware") or {}) if isinstance(selections[idx], dict) else {}
        fresh_hours = int(demand_meta.get("freshnessThresholdHours") or 24)
        cache_now = _cache_row_for_eligibility(client, kid)
        band, due, reason_suffix, _v, _e = classify_owned_daily_band(
            cache_now, now=datetime.now(timezone.utc), success_fresh_hours=fresh_hours
        )
        execution = evaluate_owned_verified_local_execution(
            cache_now, now=datetime.now(timezone.utc), success_fresh_hours=fresh_hours
        )
        report["demandAwareBefore"] = demand_meta
        report["freshnessThresholdHours"] = fresh_hours
        report["sourceAwareEligibility"] = {
            "schedulerDue": due,
            "schedulerBand": band,
            "executionEligible": execution.should_execute,
            "wouldSkipFresh": execution.would_skip_fresh,
            "reasonCode": execution.reason_code,
            "sourceClass": execution.source_class,
            "verifiedLocal": execution.verified_local,
        }
        report["preflight"] = {"marketplace": g_payload, "runtime": r_payload, "ok": g_ok and r_ok}
        if not g_ok or not r_ok:
            report["cardVerdict"] = "STOP_PREFLIGHT"
            stop_run = True
            stop_reason = f"preflight_denied_before_card_{position}"
            card_reports.append(report)
            (CARDS_DIR / f"card_{position}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            break

        if due and execution.would_skip_fresh and not execution.verified_local:
            report["cardVerdict"] = "STOP_PREFLIGHT"
            report["eligibilityRegression"] = True
            stop_run = True
            stop_reason = f"eligibility_regression_skip_already_fresh_card_{position}"
            card_reports.append(report)
            (CARDS_DIR / f"card_{position}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            break

        if not due and execution.verified_local and execution.would_skip_fresh:
            report["cardVerdict"] = "FRESH_SKIP_NO_SEARCH"
            report["legitimateStateChange"] = True
            card_reports.append(report)
            (CARDS_DIR / f"card_skip_{idx + 1}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            if CONTINUE_ON_FRESH_SKIP:
                print(f"[final-e2e] FRESH_SKIP no search consumed idx={idx} key={kid}", flush=True)
                continue
            report["cardVerdict"] = "STOP_STATE_CHANGED_NOW_VERIFIED_FRESH"
            stop_run = True
            stop_reason = f"state_changed_verified_fresh_card_{position}"
            (CARDS_DIR / f"card_{position}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            break

        if not execution.should_execute:
            report["cardVerdict"] = "STOP_PREFLIGHT"
            stop_run = True
            stop_reason = f"not_execution_eligible_card_{position}"
            card_reports.append(report)
            (CARDS_DIR / f"card_{position}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            break

        sel = selections[idx]
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
            f"[final-e2e] CARD {position}/{AUTHORISED_MAX} {identity.get('card')} "
            f"key={kid} attempt={attempt_id} query={identity.get('query')!r}",
            flush=True,
        )

        os.environ["CARDSCANR_LIVE_ATTEMPT_ID"] = attempt_id
        os.environ["CARDSCANR_PRICE_KEY_ID"] = kid
        os.environ["CARDSCANR_CAPTURE_ORIGIN"] = "LIVE_BROWSER_CAPTURE"
        if identity.get("fingerprint"):
            os.environ["CARDSCANR_FINGERPRINT"] = str(identity["fingerprint"])
        apply_context_to_environ(
            NavigationRuntimeContext(
                runtime_mode=runtime_mode,
                current_attempt_id=attempt_id,
                current_price_key_id=kid,
                current_fingerprint=str(identity.get("fingerprint") or "") or None,
                current_query=str(identity.get("query") or "") or None,
                expected_prior=prior_from_card_report(card_reports[-1]) if card_reports else None,
            )
        )
        ctx_snap = snapshot_nav_context_boundaries(expected_mode=runtime_mode)
        report["contextPropagation"] = ctx_snap
        report["preSubmitOnlyArmed"] = bool(ctx_snap.get("preSubmitOnlyArmed"))
        (CARDS_DIR / f"card_{position}_context.json").write_text(json.dumps(ctx_snap, indent=2) + "\n", encoding="utf-8")
        print(
            json.dumps(
                {
                    "CONTEXT_PROPAGATION": {
                        "card": position,
                        "ok": ctx_snap.get("ok"),
                        "layers": ctx_snap.get("layers"),
                        "wouldPassHome": ctx_snap.get("wouldPassHome"),
                        "preSubmitOnlyArmed": ctx_snap.get("preSubmitOnlyArmed"),
                    }
                },
                indent=2,
            ),
            flush=True,
        )
        if not ctx_snap.get("ok"):
            report["cardVerdict"] = "STOP_PREFLIGHT"
            stop_run = True
            stop_reason = f"nav_context_not_propagated_before_card_{position}"
            card_reports.append(report)
            (CARDS_DIR / f"card_{position}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            clear_context_from_environ()
            break
        t0 = time.monotonic()
        payload: dict[str, Any] | None = None
        exc_text = None
        try:
            payload = run_forced_job(
                client,
                price_key_id=kid,
                reason=f"reliability_final_five_consecutive_e2e:{sel.get('schedulerReason') or 'owned_daily'}",
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
            os.environ.pop("CARDSCANR_FINGERPRINT", None)
            clear_context_from_environ()

        result = payload.get("result") if isinstance(payload, dict) else {}
        result = result if isinstance(result, dict) else {}
        diag = _diag(result)
        after_raw = payload.get("after") if isinstance(payload, dict) else (client.get_cache_row(price_key_id=kid) or {})
        after = _cache_snap(after_raw if isinstance(after_raw, dict) else {})
        # If cache snap missed raw fields, enrich from raw row.
        if isinstance(after_raw, dict):
            after.setdefault("provider", after_raw.get("provider"))
            after.setdefault("displayPriceSource", after_raw.get("display_price_source"))
            after.setdefault("freshness", after_raw.get("last_updated_at"))
            after.setdefault("refreshStatus", after_raw.get("refresh_status"))
            if after.get("price") is None:
                after["price"] = after_raw.get("current_market_price")
        report["after"] = after
        job_id = str(payload.get("jobId") or result.get("jobId") or "") if isinstance(payload, dict) else str(result.get("jobId") or "")
        classified = classify_card_from_job_result(
            result,
            attempt_id=attempt_id,
            price_key_id=kid,
            job_id=job_id or None,
            fingerprint=str(identity.get("fingerprint") or ""),
            before=before,
            after=after,
            preflight_failed=False,
        )
        search_started = bool(classified.get("searchSubmitted"))
        if search_started:
            accounting["liveNavigationStartedCount"] += 1
            run["codeFrozenAfterFirstSearch"] = True
        report["searchSubmissionEvent"] = classified.get("searchSubmissionEvent")
        report["eventLookup"] = classified.get("eventLookup")
        challenge = bool(classified.get("challenge")) or is_structured_challenge(result, diag)
        accounting["terminalAttemptCount"] += 1 if search_started else 0
        report["job"] = {
            "jobId": job_id,
            "durationSec": payload.get("durationSec") if isinstance(payload, dict) else None,
            "status": result.get("status"),
            "ownedDailyOutcome": result.get("ownedDailyOutcome") or result.get("outcomeClass"),
            "recommendedPrice": result.get("recommendedPrice"),
            "error": result.get("error") or exc_text,
            "postSoldCapturePhase": classified.get("postSoldCapturePhase"),
            "x11SoldStateVerified": classified.get("x11SoldStateVerified"),
            "parsePhase": classified.get("parsePhase"),
            "providerDiagnostics": result.get("providerDiagnostics"),
            "liveAttemptNumber": accounting["liveNavigationStartedCount"] if search_started else None,
            "searchSubmitted": search_started,
            "attemptId": attempt_id,
            "executionEligibility": result.get("executionEligibility"),
            "currentJobCapture": result.get("currentJobCapture"),
        }
        # Hard stop if skipped_already_fresh without verified-local (policy regression).
        if result.get("status") == "skipped_already_fresh" and not execution.verified_local:
            report["cardVerdict"] = "STOP_PREFLIGHT"
            report["eligibilityRegression"] = True
            stop_run = True
            stop_reason = f"skipped_already_fresh_reference_only_card_{position}"
            card_reports.append(report)
            (CARDS_DIR / f"card_{position}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            break
        if result.get("status") == "skipped_already_fresh" and execution.verified_local and CONTINUE_ON_FRESH_SKIP:
            report["cardVerdict"] = "FRESH_SKIP_NO_SEARCH"
            card_reports.append(report)
            (CARDS_DIR / f"card_skip_{idx + 1}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            continue

        nav = (
            result.get("desktopNav")
            or diag.get("desktopNav")
            or diag.get("linuxNav")
            or {}
        )
        report["navigation"] = {
            "liveAttemptNumber": accounting["liveNavigationStartedCount"] if search_started else None,
            "searchSubmitted": search_started,
            "desktopNav": nav,
            "finalUrl": (nav if isinstance(nav, dict) else {}).get("url") or diag.get("finalUrl") or result.get("sourceUrl"),
            "x11SoldStateVerified": classified.get("x11SoldStateVerified"),
        }
        report["challenge"] = {
            "activeChallenge": challenge,
            "providerOutcome": diag.get("providerOutcome"),
            "operationalStatus": diag.get("operationalStatus"),
            "structuredOnly": True,
        }
        report["capture"] = classified.get("capture") or {}
        report["parse"] = classified.get("parse") or {}
        report["write"] = classified.get("write") or {}
        # Correlation mismatch after consumed search => STOP.
        if search_started and report["capture"].get("status") == "REJECTED_MISMATCH":
            report["cardVerdict"] = "FAIL_CAPTURE_CORRELATION"
            accounting["failedAttemptCount"] += 1
            stop_run = True
            stop_reason = f"capture_correlation_mismatch_card_{position}"
            card_reports.append(report)
            (CARDS_DIR / f"card_{position}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            break

        verdict = str(classified.get("cardVerdict") or "FAIL_LOCAL")
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
        if classified.get("consistencyError"):
            report["consistencyError"] = classified.get("consistencyError")
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

        if search_started:
            executed_live += 1

        if stop_run:
            break
        if executed_live < AUTHORISED_MAX:
            outcome = str(result.get("ownedDailyOutcome") or result.get("outcomeClass") or "")
            pacing.observe_outcome(outcome)
            delay = (
                int(pacing.next_delay_seconds())
                if hasattr(pacing, "next_delay_seconds")
                else int(pacing.config.min_inter_job_delay_seconds)
            )
            if delay <= 0:
                delay = int(pacing.config.min_inter_job_delay_seconds)
            accounting["interCardDelaysSec"].append(delay)
            print(f"[final-e2e] pacing sleep {delay}s before next card", flush=True)
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
    elif (
        healthy == AUTHORISED_MAX
        and accounting["challengeStopCount"] == 0
        and accounting["failedAttemptCount"] == 0
        and accounting["retryCount"] == 0
        and accounting["liveNavigationStartedCount"] == AUTHORISED_MAX
    ):
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
    run["searchSubmissionStartedTotalCanonical"] = count_search_submission_started()
    run["searchSubmissionStartedThisRun"] = count_consumed_live_navigations(
        list(accounting.get("attemptIds") or [])
    )
    (OUT / "RUN_RESULT.json").write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "VERDICT": run["verdict"],
                "accounting": accounting,
                "stopReason": stop_reason,
                "ownershipMutations": mutations,
                "searches": accounting["liveNavigationStartedCount"],
            },
            indent=2,
        )
    )
    return 0 if run["verdict"] != "5_CARD_RELIABILITY_FAIL" else 3


if __name__ == "__main__":
    if "--preflight-only" in sys.argv:
        raise SystemExit(preflight_only())
    if "--eligibility-dry-only" in sys.argv:
        _configure()
        load_supabase_env()
        for d in (OUT, BOOT, ATTEMPTS):
            d.mkdir(parents=True, exist_ok=True)
        deploy = verify_deployed_code()
        (BOOT / "phase0_deployed_code.json").write_text(json.dumps(deploy, indent=2) + "\n", encoding="utf-8")
        if not deploy["ok"]:
            print("STOP deploy")
            raise SystemExit(2)
        cfg = MarketEngineConfig.from_env()
        client = SupabaseMarketEngineClient(
            supabase_url=cfg.supabase_url,
            service_role_key=supabase_secret_key_from_env(),
        )
        selections = _select_due_cards(client, limit=AUTHORISED_MAX)
        elig = eligibility_dry_check(client, selections)
        (BOOT / "phase3_execution_eligibility.json").write_text(json.dumps(elig, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(elig, indent=2)[:8000])
        raise SystemExit(0 if elig["ok"] else 2)
    raise SystemExit(main())
