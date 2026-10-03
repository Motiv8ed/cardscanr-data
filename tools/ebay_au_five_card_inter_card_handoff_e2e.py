#!/usr/bin/env python3
"""Authorised FIVE-CARD INTER-CARD HANDOFF E2E production reliability proof.

Fresh independent live run (max 5 SEARCH_SUBMISSION_STARTED).
Does NOT resume historical Tropius/Ninetales.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tools.ebay_au_final_five_consecutive_e2e as harness
import tools.ebay_au_final_five_sequential_production_proof as seq
from cardscanr_market_engine.navigation_runtime_context import PRE_SUBMIT_ONLY_ENV
from tools.ebay_au_five_card_authoritative_artifact_e2e import (
    _artifact_contract_self_check,
    _enrich_card_artifact_fields,
    _extend_deploy,
    _freshness_self_check,
)
from tools.inter_card_browser_lifecycle_self_check import run_self_check as run_inter_card_self_check
from tools.reliability_harness_truth_self_check import run_self_check as run_harness_truth_self_check

OUT = ROOT / "reports" / "artifacts" / "five_card_inter_card_handoff_e2e"
TASK_ID = "CARDSCANR-FINAL-FIVE-CARD-INTER-CARD-HANDOFF-E2E"
HANDOFF_FIX = ROOT / "reports" / "artifacts" / "inter_card_provider_handoff_closure"

ARTIFACT_MODULES = [
    "cardscanr_market_engine/navigation_runtime_context.py",
    "cardscanr_market_engine/browser_lifecycle_policy.py",
    "cardscanr_market_engine/job_runner.py",
    "cardscanr_market_engine/reliability_harness_classification.py",
    "cardscanr_market_engine/reliability_harness_evidence.py",
    "cardscanr_market_engine/pipeline_phase_diagnostics.py",
    "cardscanr_market_engine/live_navigation_attempt.py",
    "cardscanr_market_engine/owned_verified_local_execution.py",
    "cardscanr_market_engine/providers/ebay_browser_provider.py",
    "cardscanr_market_engine/providers/post_sold_capture.py",
    "cardscanr_market_engine/providers/post_sold_capture_process.py",
    "cardscanr_market_engine/providers/post_sold_capture_worker.py",
    "cardscanr_market_engine/providers/linux_x11_ebay_nav.py",
    "tools/linux_x11_ebay_search.py",
    "tools/linux_x11_ebay_sold.py",
    "tools/ebay_au_final_five_consecutive_e2e.py",
    "tools/ebay_au_five_card_inter_card_handoff_e2e.py",
]


def _bind() -> None:
    seq.OUT = OUT
    seq.TASK_ID = TASK_ID
    harness.OUT = OUT
    harness.CARDS_DIR = OUT / "cards"
    harness.BOOT = OUT / "bootstrap"
    harness.BEFORE = OUT / "before"
    harness.AFTER = OUT / "after"
    harness.ATTEMPTS = OUT / "attempts"
    harness.TASK_ID = TASK_ID
    for rel in ARTIFACT_MODULES:
        if rel not in harness.REQUIRED_RUNTIME_FILES:
            harness.REQUIRED_RUNTIME_FILES.append(rel)


def _handoff_source_checks() -> dict:
    nav = (ROOT / "cardscanr_market_engine/providers/linux_x11_ebay_nav.py").read_text(encoding="utf-8")
    search = (ROOT / "tools/linux_x11_ebay_search.py").read_text(encoding="utf-8")
    provider = (ROOT / "cardscanr_market_engine/providers/ebay_browser_provider.py").read_text(encoding="utf-8")
    jr = (ROOT / "cardscanr_market_engine/job_runner.py").read_text(encoding="utf-8")
    hs = (ROOT / "tools/ebay_au_final_five_consecutive_e2e.py").read_text(encoding="utf-8")
    ctx = (ROOT / "cardscanr_market_engine/navigation_runtime_context.py").read_text(encoding="utf-8")
    replay = {}
    two = {}
    if (HANDOFF_FIX / "tropius_ninetales_replay.json").is_file():
        replay = json.loads((HANDOFF_FIX / "tropius_ninetales_replay.json").read_text(encoding="utf-8"))
    if (HANDOFF_FIX / "local_two_card_proof.json").is_file():
        two = json.loads((HANDOFF_FIX / "local_two_card_proof.json").read_text(encoding="utf-8"))
    checks = {
        "runtimeModePropagationReady": "CARDSCANR_RUNTIME_MODE" in nav and "apply_context_to_environ" in hs,
        "expectedPriorContextPropagationReady": "EXPECTED_PRIOR_PATH_ENV" in ctx
        and "expected_prior" in hs
        and "env_exports" in nav,
        "windowsToWslContextReady": "env_exports" in nav and "/mnt/" in ctx,
        "interCardProviderPreparationReady": "allow_existing_ebay_targets" in nav
        and "CDP_REUSED_EXISTING" in nav
        and "errorMessage" in provider
        and "apply_context_to_environ" in jr,
        "tropiusNinetalesOfflineReplayReady": str(replay.get("result") or "") == "PRE_SUBMIT_QUERY_READY",
        "localTwoCardProof": bool(two.get("cardBInterCardAccepted"))
        and two.get("cardBSubmissionStarted") is False,
        "searchCliHasRuntimeMode": "--runtime-mode" in search,
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "tropiusNinetalesReplay": replay,
        "localTwoCardProof": two,
    }


def phase0_self_checks() -> dict:
    os.environ.pop(PRE_SUBMIT_ONLY_ENV, None)
    inter = run_inter_card_self_check()
    truth = run_harness_truth_self_check()
    artifact = _artifact_contract_self_check()
    freshness = _freshness_self_check()
    handoff = _handoff_source_checks()
    ps = harness._pre_submit_only_safety()  # noqa: SLF001
    checks = dict(inter.get("checks") or {})
    required_lifecycle = (
        "coldStartPolicyReady",
        "interCardPolicyReady",
        "expectedPriorTargetCorrelation",
        "unknownTargetFailClosed",
        "historicalEventBaselineReady",
        "currentAttemptOnlyAccounting",
        "interCardPreSubmitGuiReady",
    )
    lifecycle_ok = bool(inter.get("ok")) and all(bool(checks.get(k)) for k in required_lifecycle)
    seq_block = inter.get("sequential") or {}
    semantics_ok = (
        seq_block.get("submitted") is False
        and seq_block.get("tropiusConsumed") is False
        and int(inter.get("searchSubmissionStartedDelta") or 0) == 0
    )
    tropius_replay = str((handoff.get("tropiusNinetalesReplay") or {}).get("result") or "")
    combined = {
        **{k: bool(checks.get(k)) for k in required_lifecycle},
        **(artifact.get("checks") or {}),
        **(freshness.get("checks") or {}),
        **(handoff.get("checks") or {}),
        "harnessTruthSelfCheck": bool(truth.get("ok")),
        "preSubmitOnlyArmed": bool(ps.get("preSubmitOnlyArmed")),
        "preSubmitOnlySafetyOk": bool(ps.get("ok")),
        "localTwoCardProof": bool(handoff["checks"].get("localTwoCardProof")),
        "tropiusNinetalesOfflineReplay": tropius_replay,
    }
    ok = (
        lifecycle_ok
        and semantics_ok
        and bool(artifact.get("ok"))
        and bool(freshness.get("ok"))
        and bool(truth.get("ok"))
        and bool(handoff.get("ok"))
        and bool(ps.get("ok"))
        and tropius_replay == "PRE_SUBMIT_QUERY_READY"
    )
    return {
        "ok": ok,
        "checks": combined,
        "interCardLifecycle": {
            "ok": lifecycle_ok and semantics_ok,
            "checks": checks,
            "path": inter.get("path"),
        },
        "harnessTruth": {"ok": bool(truth.get("ok")), "checks": truth.get("checks"), "path": truth.get("path")},
        "captureArchitecture": artifact,
        "freshnessSemantics": freshness,
        "interCardProviderHandoff": handoff,
        "preSubmitOnlySafety": ps,
    }


def write_report(run: dict) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "FIVE_CARD_INTER_CARD_HANDOFF_E2E_REPORT.md"
    cards = run.get("cards") or []
    acc = run.get("attemptAccounting") or {}
    lines = [
        "# FIVE CARD INTER-CARD HANDOFF E2E",
        "",
        f"**Task:** `{TASK_ID}`",
        f"**Verdict:** `{run.get('verdict')}`",
        f"**Started:** {run.get('startedAtUtc')}",
        f"**Finished:** {run.get('finishedAtUtc')}",
        f"**Stop reason:** {run.get('stopReason')}",
        "",
        "## Self-check",
        "",
        "```json",
        json.dumps(run.get("phase0SelfCheck"), indent=2)[:14000],
        "```",
        "",
        "## PRE_SUBMIT_ONLY safety",
        "",
        "```json",
        json.dumps(run.get("preSubmitOnlySafety"), indent=2)[:4000],
        "```",
        "",
        "## Cold start",
        "",
        "```json",
        json.dumps(run.get("coldStart"), indent=2)[:8000],
        "```",
        "",
        "## Deployment",
        "",
        "```json",
        json.dumps(run.get("deployment"), indent=2)[:16000],
        "```",
        "",
        "## Event baseline",
        "",
        "```json",
        json.dumps(run.get("attemptEventBaseline"), indent=2)[:8000],
        "```",
        "",
        "## Scheduler selection",
        "",
        "```json",
        json.dumps(run.get("schedulerSelection"), indent=2)[:20000],
        "```",
        "",
        "## Attempt accounting",
        "",
        "```json",
        json.dumps(acc, indent=2)[:8000],
        "```",
        "",
    ]
    for card in cards:
        pos = card.get("reliabilityPosition")
        lines.extend(
            [
                f"## Card {pos}",
                "",
                f"- verdict: `{card.get('cardVerdict')}`",
                f"- runtimeMode: `{card.get('runtimeMode')}`",
                f"- attemptId: `{card.get('attemptId')}`",
                "",
                "```json",
                json.dumps(
                    {
                        "identity": card.get("identity"),
                        "contextPropagation": card.get("contextPropagation"),
                        "preflight": {
                            "ok": (card.get("preflight") or {}).get("ok"),
                            "runtimeMode": card.get("runtimeMode"),
                            "preSubmitOnlyArmed": card.get("preSubmitOnlyArmed"),
                        },
                        "sourceAwareEligibility": card.get("sourceAwareEligibility"),
                        "navigation": card.get("navigation"),
                        "challenge": card.get("challenge"),
                        "capture": card.get("capture"),
                        "authoritativeArtifact": card.get("authoritativeArtifact"),
                        "parse": card.get("parse"),
                        "write": card.get("write"),
                        "freshness": card.get("freshness"),
                        "jobError": (card.get("job") or {}).get("error"),
                        "providerDiagnostics": (card.get("job") or {}).get("providerDiagnostics"),
                    },
                    indent=2,
                )[:28000],
                "```",
                "",
            ]
        )
    lines.extend(
        [
            "## Control plane final",
            "",
            "```json",
            json.dumps(run.get("controlPlaneFinal"), indent=2)[:8000],
            "```",
            "",
            "## Process health",
            "",
            "```json",
            json.dumps(run.get("runtimeFinal"), indent=2)[:8000],
            "```",
            "",
            "## Ownership",
            "",
            "```json",
            json.dumps(run.get("ownership"), indent=2)[:4000],
            "```",
            "",
            "## Owned daily",
            "",
            "```json",
            json.dumps(run.get("ownedDaily"), indent=2),
            "```",
            "",
            "## Historical facts (unchanged)",
            "",
            "- Ceruledge: PASS_PRICE_UPDATED",
            "- Sandile: PASS_PRICE_UPDATED",
            "- Tropius: PASS_PRICE_UPDATED",
            "- Historical Ninetales: FAIL_NAVIGATION consumed=false",
            "",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_zip(run: dict, report_path: Path) -> dict:
    import zipfile

    utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    zip_path = Path(rf"C:\Users\andyg\Downloads\CARDSCANR_FIVE_CARD_INTER_CARD_HANDOFF_E2E_{utc}.zip")
    skip_suffix = {".sqlite", ".gz", ".cookie"}
    skip_names = {"cookies", "credentials", "token", "secret", ".env"}
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(OUT.rglob("*")):
            if not p.is_file():
                continue
            low = p.name.lower()
            if p.suffix.lower() in skip_suffix:
                continue
            if any(s in low for s in skip_names):
                continue
            if p.suffix.lower() == ".html" and p.stat().st_size > 250_000:
                continue
            zf.write(p, arcname=str(p.relative_to(ROOT)).replace("\\", "/"))
        if report_path.is_file():
            zf.write(report_path, arcname=str(report_path.relative_to(ROOT)).replace("\\", "/"))
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    meta = {"zip": str(zip_path), "sha256": digest, "bytes": zip_path.stat().st_size, "utc": utc}
    (OUT / "ZIP_META.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return meta


def main() -> int:
    _bind()
    for d in (OUT, harness.BOOT, harness.BEFORE, harness.AFTER, harness.CARDS_DIR, harness.ATTEMPTS):
        d.mkdir(parents=True, exist_ok=True)

    self_checks = phase0_self_checks()
    (harness.BOOT / "phase0_handoff_self_checks.json").write_text(
        json.dumps(self_checks, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps({"PHASE0_SELF_CHECK": {"ok": self_checks["ok"], "checks": self_checks["checks"]}}, indent=2),
        flush=True,
    )
    if not self_checks["ok"]:
        print("STOP: phase0 self-check failed", flush=True)
        return 2
    if self_checks["checks"].get("preSubmitOnlyArmed"):
        print("STOP: PRE_SUBMIT_ONLY armed", flush=True)
        return 2

    orig_verify = harness.verify_deployed_code

    def _verify() -> dict:
        return _extend_deploy(orig_verify())

    harness.verify_deployed_code = _verify  # type: ignore[assignment]

    rc = seq._bootstrap_guards()
    if rc != 0:
        return rc

    rc = harness.main()

    result_path = OUT / "RUN_RESULT.json"
    run = (
        json.loads(result_path.read_text(encoding="utf-8"))
        if result_path.is_file()
        else {"verdict": "5_CARD_RELIABILITY_FAIL"}
    )
    run["phase0SelfCheck"] = self_checks
    run["preSubmitOnlySafety"] = self_checks.get("preSubmitOnlySafety")
    cold_path = harness.BOOT / "phase0c_cold_start_normalisation.json"
    if cold_path.is_file():
        run["coldStart"] = json.loads(cold_path.read_text(encoding="utf-8"))
    deploy_path = harness.BOOT / "phase0_deployed_code.json"
    if deploy_path.is_file():
        run["deployment"] = json.loads(deploy_path.read_text(encoding="utf-8"))
    cards = [_enrich_card_artifact_fields(card) for card in (run.get("cards") or [])]
    run["cards"] = cards
    result_path.write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
    report = write_report(run)
    zip_meta = write_zip(run, report)
    print(json.dumps({"REPORT": str(report), "ZIP": zip_meta, "VERDICT": run.get("verdict")}, indent=2), flush=True)
    return rc


if __name__ == "__main__":
    if "--self-check-only" in sys.argv:
        _bind()
        OUT.mkdir(parents=True, exist_ok=True)
        payload = phase0_self_checks()
        print(json.dumps({"ok": payload["ok"], "checks": payload["checks"]}, indent=2))
        raise SystemExit(0 if payload["ok"] else 2)
    raise SystemExit(main())
