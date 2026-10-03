#!/usr/bin/env python3
"""Authorised FIVE-CONSECUTIVE PRODUCTION RELIABILITY FINAL run.

Fresh independent live run (max 5 SEARCH_SUBMISSION_STARTED).
Reuses the corrected final-e2e harness with task-specific output paths.
Does NOT resume any previous reliability batch.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tools.ebay_au_final_five_consecutive_e2e as harness
from cardscanr_market_engine.live_navigation_attempt import (
    attempts_dir,
    attempts_dir_wsl,
    canonical_attempts_dir,
)
from cardscanr_market_engine.wsl_path import same_physical_path
from tools.reliability_harness_truth_self_check import run_self_check

OUT = ROOT / "reports" / "artifacts" / "five_consecutive_production_reliability_final"
TASK_ID = "CARDSCANR-FIVE-CONSECUTIVE-PRODUCTION-RELIABILITY-FINAL"

EXTRA_MODULES = [
    "cardscanr_market_engine/reliability_harness_classification.py",
    "cardscanr_market_engine/reliability_harness_evidence.py",
    "cardscanr_market_engine/pipeline_phase_diagnostics.py",
    "cardscanr_market_engine/live_navigation_attempt.py",
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


def phase0_self_check() -> dict:
    result = run_self_check()
    required = [
        "attemptEventDirectoryShared",
        "attemptLookupById",
        "successfulFixtureClassification",
        "successfulFixtureCaptureEvidence",
        "successfulFixtureParseEvidence",
        "successfulFixtureWriteEvidence",
        "preSubmitFixtureClassification",
        "captureFailureFixtureClassification",
    ]
    checks = result.get("checks") or {}
    ok = bool(result.get("ok")) and all(bool(checks.get(k)) for k in required)
    return {"ok": ok, "checks": checks, "path": result.get("path")}


def _bootstrap_guards() -> int:
    _bind_paths()
    for d in (OUT, harness.BOOT, harness.BEFORE, harness.ATTEMPTS):
        d.mkdir(parents=True, exist_ok=True)

    self_check = phase0_self_check()
    (harness.BOOT / "phase0_harness_self_check.json").write_text(
        json.dumps(self_check, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"PHASE0_HARNESS_SELF_CHECK": self_check}, indent=2), flush=True)
    if not self_check["ok"]:
        print("STOP: harness self-check failed", flush=True)
        return 2

    event_dirs = _event_dir_contract()
    (harness.BOOT / "phase1_event_directory_contract.json").write_text(
        json.dumps(event_dirs, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"PHASE1_EVENT_DIR": event_dirs}, indent=2), flush=True)
    if not event_dirs["ok"]:
        print("STOP: event directory contract failed", flush=True)
        return 2
    return 0


if __name__ == "__main__":
    rc = _bootstrap_guards()
    if rc != 0:
        raise SystemExit(rc)

    if "--preflight-only" in sys.argv:
        raise SystemExit(harness.preflight_only())

    if "--eligibility-dry-only" in sys.argv:
        harness._configure()
        from cardscanr_market_engine.config import MarketEngineConfig, supabase_secret_key_from_env
        from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient
        from cardscanr_market_engine.supabase_env_loader import load_supabase_env
        from tools.ebay_au_fresh_5_card_reliability_run import _select_due_cards

        load_supabase_env()
        deploy = harness.verify_deployed_code()
        (harness.BOOT / "phase0_deployed_code.json").write_text(
            json.dumps(deploy, indent=2) + "\n", encoding="utf-8"
        )
        if not deploy["ok"]:
            print("STOP deploy")
            raise SystemExit(2)
        cfg = MarketEngineConfig.from_env()
        client = SupabaseMarketEngineClient(
            supabase_url=cfg.supabase_url,
            service_role_key=supabase_secret_key_from_env(),
        )
        selections = _select_due_cards(client, limit=harness.AUTHORISED_MAX)
        elig = harness.eligibility_dry_check(client, selections)
        (harness.BOOT / "phase3_execution_eligibility.json").write_text(
            json.dumps(elig, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(elig, indent=2)[:8000])
        raise SystemExit(0 if elig["ok"] else 2)

    raise SystemExit(harness.main())
