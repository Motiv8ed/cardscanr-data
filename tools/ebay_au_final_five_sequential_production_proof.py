#!/usr/bin/env python3
"""Authorised FINAL 5-CARD SEQUENTIAL PRODUCTION PROOF.

Fresh independent live run (max 5 SEARCH_SUBMISSION_STARTED).
Card 1 = COLD_START after local Chrome blank reset.
Cards 2–5 = INTER_CARD with expected prior target correlation.
Does NOT resume the Sandile run.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tools.ebay_au_final_five_consecutive_e2e as harness
from cardscanr_market_engine.browser_lifecycle_policy import (
    RUNTIME_COLD_START,
    evaluate_runtime_targets,
)
from cardscanr_market_engine.live_navigation_attempt import (
    attempts_dir,
    attempts_dir_wsl,
    capture_attempt_event_baseline,
    canonical_attempts_dir,
)
from cardscanr_market_engine.local_browser_runtime import probe_local_browser_runtime
from cardscanr_market_engine.wsl_path import same_physical_path
from tools.ebay_au_five_consecutive_post_unicode_run import _restart_chrome_blank
from tools.inter_card_browser_lifecycle_self_check import run_self_check as run_inter_card_self_check
from tools.reliability_harness_truth_self_check import run_self_check as run_harness_truth_self_check

OUT = ROOT / "reports" / "artifacts" / "final_five_sequential_proof"
TASK_ID = "CARDSCANR-FINAL-5-CARD-SEQUENTIAL-PRODUCTION-PROOF"

EXTRA_MODULES = [
    "cardscanr_market_engine/reliability_harness_classification.py",
    "cardscanr_market_engine/reliability_harness_evidence.py",
    "cardscanr_market_engine/pipeline_phase_diagnostics.py",
    "cardscanr_market_engine/live_navigation_attempt.py",
    "cardscanr_market_engine/browser_lifecycle_policy.py",
    "cardscanr_market_engine/local_browser_runtime.py",
    "cardscanr_market_engine/x11_navigation_runtime.py",
    "tools/linux_x11_ebay_search.py",
    "tools/linux_x11_ebay_sold.py",
]


def _bind_paths() -> None:
    harness.OUT = OUT
    harness.CARDS_DIR = OUT / "cards"
    harness.BOOT = OUT / "bootstrap"
    harness.BEFORE = OUT / "before"
    harness.AFTER = OUT / "after"
    harness.ATTEMPTS = OUT / "attempts"
    harness.TASK_ID = TASK_ID
    for rel in EXTRA_MODULES:
        if rel not in harness.REQUIRED_RUNTIME_FILES:
            harness.REQUIRED_RUNTIME_FILES.append(rel)


def _event_dir_contract() -> dict:
    win = str(canonical_attempts_dir())
    wsl = attempts_dir_wsl()
    expected_win = str((ROOT / "reports" / "runtime" / "live_nav_attempts").resolve())
    expected_wsl = "/mnt/d/CardScanR_Data/cardscanr-data/reports/runtime/live_nav_attempts"
    return {
        "attemptsDirWindows": win,
        "attemptsDirWsl": wsl,
        "attemptsDirResolved": str(attempts_dir().resolve()),
        "expectedWindows": expected_win,
        "expectedWsl": expected_wsl,
        "samePhysicalDirectory": same_physical_path(win, wsl),
        "ok": (
            Path(win).resolve() == Path(expected_win).resolve()
            and wsl.replace("\\", "/") == expected_wsl
            and same_physical_path(win, wsl)
        ),
    }


def phase0_self_checks() -> dict:
    inter = run_inter_card_self_check()
    truth = run_harness_truth_self_check()
    seq = inter.get("sequential") or {}
    inter_ok = bool(inter.get("ok")) and all(
        bool((inter.get("checks") or {}).get(k))
        for k in (
            "coldStartPolicyReady",
            "interCardPolicyReady",
            "expectedPriorTargetCorrelation",
            "unknownTargetFailClosed",
            "historicalEventBaselineReady",
            "currentAttemptOnlyAccounting",
            "interCardPreSubmitGuiReady",
            "sequentialLocalProof",
        )
    )
    semantics_ok = (
        seq.get("submitted") is False
        and seq.get("tropiusConsumed") is False
        and seq.get("tropiusNotConsumed") is True
        and int(inter.get("searchSubmissionStartedDelta") or 0) == 0
    )
    return {
        "ok": inter_ok and semantics_ok and bool(truth.get("ok")),
        "interCardLifecycle": {
            "ok": inter_ok and semantics_ok,
            "checks": inter.get("checks"),
            "submitted": seq.get("submitted"),
            "tropiusConsumed": seq.get("tropiusConsumed"),
            "tropiusNotConsumed": seq.get("tropiusNotConsumed"),
            "newEventDelta": inter.get("searchSubmissionStartedDelta"),
            "path": inter.get("path"),
        },
        "harnessTruth": {
            "ok": bool(truth.get("ok")),
            "checks": truth.get("checks"),
            "path": truth.get("path"),
        },
        "semanticNamingCorrected": True,
    }


def cold_start_normalise() -> dict:
    """Force COLD_START browser state via about:blank restart if needed. No eBay navigation."""
    before = probe_local_browser_runtime(cdp_port=9444)
    before_policy = evaluate_runtime_targets(before.raw_targets, mode=RUNTIME_COLD_START)
    had_ebay = bool(before.ebay_targets) or (not before_policy.ok)
    restart = None
    if had_ebay or not before.cdp_ready:
        restart = _restart_chrome_blank()
    after = probe_local_browser_runtime(cdp_port=9444)
    after_policy = evaluate_runtime_targets(after.raw_targets, mode=RUNTIME_COLD_START)
    ok = bool(after.cdp_ready and after_policy.ok and not after.ebay_targets)
    return {
        "historicalSoldTargetBeforeReset": list(before.ebay_targets),
        "rawTargetsBefore": before.raw_targets,
        "coldStartBeforeOk": before_policy.ok,
        "resetMethod": "_restart_chrome_blank" if restart is not None else "already_cold_clean",
        "restart": restart,
        "ebayNavigationPerformed": False,
        "ebayTargetsAfter": list(after.ebay_targets),
        "rawTargetsAfter": after.raw_targets,
        "coldStartPolicy": after_policy.to_dict(),
        "ok": ok,
    }


def _bootstrap_guards() -> int:
    _bind_paths()
    for d in (OUT, harness.BOOT, harness.BEFORE, harness.ATTEMPTS):
        d.mkdir(parents=True, exist_ok=True)

    self_checks = phase0_self_checks()
    (harness.BOOT / "phase0_self_checks.json").write_text(
        json.dumps(self_checks, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"PHASE0_SELF_CHECK": self_checks}, indent=2), flush=True)
    if not self_checks["ok"]:
        print("STOP: phase0 self-check failed", flush=True)
        return 2

    event_dirs = _event_dir_contract()
    (harness.BOOT / "phase1_event_directory_contract.json").write_text(
        json.dumps(event_dirs, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"PHASE1_EVENT_DIR": event_dirs}, indent=2), flush=True)
    if not event_dirs["ok"]:
        print("STOP: event directory contract failed", flush=True)
        return 2

    baseline = capture_attempt_event_baseline()
    (harness.BOOT / "attempt_event_baseline_pre_run.json").write_text(
        json.dumps(baseline, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "ATTEMPT_BASELINE": {
                    "historicalEvents": baseline["count"],
                    "deleted": False,
                    "ok": True,
                }
            },
            indent=2,
        ),
        flush=True,
    )

    cold = cold_start_normalise()
    (harness.BOOT / "phase0c_cold_start_normalisation.json").write_text(
        json.dumps(cold, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "PHASE0C_COLD_START": {
                    "historicalSoldTargetBeforeReset": cold["historicalSoldTargetBeforeReset"],
                    "resetMethod": cold["resetMethod"],
                    "ebayNavigationPerformed": cold["ebayNavigationPerformed"],
                    "ebayTargetsAfter": cold["ebayTargetsAfter"],
                    "coldStartPolicyOk": cold["coldStartPolicy"].get("ok"),
                    "ok": cold["ok"],
                }
            },
            indent=2,
        ),
        flush=True,
    )
    if not cold["ok"]:
        print("STOP: cold-start normalisation failed", flush=True)
        return 2
    return 0


if __name__ == "__main__":
    rc = _bootstrap_guards()
    if rc != 0:
        raise SystemExit(rc)
    if "--preflight-only" in sys.argv:
        raise SystemExit(harness.preflight_only())
    raise SystemExit(harness.main())
