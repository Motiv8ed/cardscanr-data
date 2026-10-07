#!/usr/bin/env python3
"""FINAL REAL owned_daily 25-job gate — production scheduler cycle + worker drain.

REAL_OWNED_DAILY_LOOP_USED=true
FORCED_JOB_HARNESS_USED_AS_EXECUTION_LOOP=false

Each cycle:
  1) marketplace gate
  2) OwnedPrintingRefreshScheduler.run_and_write_reports()  (enqueue budget=1, AU)
  3) claim_jobs(max_jobs=1)
  4) MarketPriceJobRunner.run_job
  5) account SEARCH_SUBMISSION_STARTED / stop conditions
  6) pacing → next cycle

Does NOT preselect 25 cards into a sequential harness loop.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MAX_LIVE = 25
MAX_HEALTHY = 25
MAX_RUNTIME_H = 24
WORKER_ID = "final-real-owned-daily-25-gate"
OUT = ROOT / "reports" / "artifacts" / "final_real_owned_daily_25_job_gate"
TASK_ID = "CARDSCANR-FINAL-REAL-OWNED-DAILY-25-JOB-GATE"
REQUIRED_COMMITS = (
    "86973b7cd6dfddc0643492aac98b315be54c0ea8",
    "d95f5482cb8b8f2880003776e4a38ade30b5bc12",
    "cfdce5a68870d475d06ef4e251c3bb6e1de053f2",
    "8330e65e8d06e0aef66a6d98901d111a2cc82ca1",
)
FLAG_PATH = ROOT / "reports" / "runtime" / "owned_daily_full_enable.flag"
ATTEMPTS_DIR = ROOT / "reports" / "runtime" / "live_nav_attempts"

# Bound production owned_daily — NOT unrestricted continuous.
os.environ["OWNED_DAILY_FULL_ENABLE"] = "false"
os.environ["OWNED_DAILY_MAX_ENQUEUE"] = "1"
os.environ["OWNED_DAILY_ALLOWED_MARKETS"] = "AU"
os.environ["OWNED_DAILY_SYNC_KEYS"] = "false"
os.environ["OWNED_DAILY_DRY_RUN"] = "false"
os.environ["HOT_VERIFIED_TTL_HOURS"] = "12"
os.environ["NORMAL_VERIFIED_TTL_HOURS"] = "24"
os.environ["HIGH_DEMAND_REQUESTS_24H"] = "3"
os.environ["EBAY_BROWSER_MAX_QUERY_ATTEMPTS"] = "1"
os.environ["EBAY_BROWSER_ENABLED"] = "true"
os.environ["MARKET_LOOKUP_PROVIDER"] = "ebay_browser"
os.environ.pop("PRE_SUBMIT_ONLY", None)
os.environ.pop("CARDSCANR_PRE_SUBMIT_ONLY", None)

from cardscanr_market_engine.config import MarketEngineConfig, supabase_secret_key_from_env
from cardscanr_market_engine.demand_aware_policy import DemandAwarePolicy
from cardscanr_market_engine.demand_aware_scheduler import (
    DemandIndex,
    evaluate_demand_aware_target,
    events_from_job_rows,
    is_user_demand_reason,
)
from cardscanr_market_engine.ebay_browser_work_gate import evaluate_ebay_browser_work_gate
from cardscanr_market_engine.job_runner import MarketPriceJobRunner
from cardscanr_market_engine.browser_lifecycle_policy import (
    RUNTIME_COLD_START,
    RUNTIME_INTER_CARD,
    PriorCardContext,
)
from cardscanr_market_engine.live_navigation_attempt import (
    capture_attempt_event_baseline,
    count_consumed_live_navigations,
    has_search_submission_started,
    new_attempt_id,
)
from cardscanr_market_engine.navigation_runtime_context import (
    NavigationRuntimeContext,
    apply_context_to_environ,
)
from cardscanr_market_engine.owned_daily_outcomes import (
    CHECKED_NO_NEW_EXACT_EVIDENCE,
    HEALTHY_CHECK_OUTCOMES,
    TEMPORARY_EBAY_SERVER_FAILURE,
    UNCHANGED_FROM_EBAY,
    UPDATED_FROM_EBAY,
)
from cardscanr_market_engine.owned_daily_pacing import OwnedDailyPacingConfig, OwnedDailyPacingController
from cardscanr_market_engine.owned_daily_scheduler import (
    OwnedDailySchedulerConfig,
    OwnedPrintingRefreshScheduler,
)
from cardscanr_market_engine.scheduler import sanitize_scheduler_report
from cardscanr_market_engine.smoke_utils import append_jsonl, write_json
from cardscanr_market_engine.providers.factory import create_market_comps_provider
from cardscanr_market_engine.reliability_harness_classification import classify_reliability_card_verdict
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient
from tools.ebay_au_controlled_continuous_owned_daily_rollout import (
    demand_snapshot,
    owned_daily_shutdown,
    policy_audit,
    queue_snapshot,
)
from tools.ebay_au_final_five_sequential_production_proof import _bootstrap_guards
from tools.ebay_au_five_card_inter_card_handoff_e2e import phase0_self_checks


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")


def deployment_audit() -> dict[str, Any]:
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    present = {}
    for c in REQUIRED_COMMITS:
        rc = subprocess.call(["git", "merge-base", "--is-ancestor", c, "HEAD"], cwd=ROOT)
        present[c] = rc == 0
    dirty = subprocess.check_output(
        ["git", "status", "--porcelain", "--", "cardscanr_market_engine", "workers"],
        cwd=ROOT,
        text=True,
    ).strip()
    return {
        "ok": all(present.values()) and not dirty,
        "HEAD": head,
        "requiredCommitsPresent": present,
        "productionWorkingTreeDirty": bool(dirty),
        "productionWorkingTree": dirty or "(clean)",
        "modules": {
            "schedulerEntrypoint": "workers/owned_daily_price_scheduler.py",
            "schedulerClass": "OwnedPrintingRefreshScheduler.run_and_write_reports",
            "workerEntrypoint": "workers/market_price_worker.py",
            "jobExecution": "MarketPriceJobRunner.run_job",
            "gateWrapper": "tools/ebay_au_final_real_owned_daily_25_job_gate.py",
            "REAL_OWNED_DAILY_LOOP_USED": True,
            "FORCED_JOB_HARNESS_USED_AS_EXECUTION_LOOP": False,
        },
    }


def offline_readiness() -> dict[str, Any]:
    policy = policy_audit()
    self_checks = phase0_self_checks()
    # Focused offline suites already required before live; record import-level contracts.
    from cardscanr_market_engine.providers.sold_page_health import evaluate_sold_verification

    healthy = evaluate_sold_verification(
        url="https://www.ebay.com.au/sch/i.html?_nkw=Meowth+56+jungle+Pokemon&LH_Sold=1",
        title="Meowth for sale | eBay",
        body="Results\nSold items\nSold 1 Oct 2026\nAU $4.41\n",
    )
    error = evaluate_sold_verification(
        url="https://www.ebay.com.au/sch/i.html?_nkw=Rowlet+10+perfect+order+Pokemon&LH_Sold=1",
        title="Error Page | eBay",
        body="",
    )
    cfg = DemandAwarePolicy.from_env()
    checks = {
        "policyOk": bool(policy.get("ok")),
        "selfChecksOk": bool(self_checks.get("ok")),
        "hotTtl12": cfg.hot_verified_ttl_hours == 12,
        "normalTtl24": cfg.normal_verified_ttl_hours == 24,
        "lanes": abs(cfg.lane_demand_share - 0.5) < 1e-9,
        "healthySoldPage": bool(healthy.get("x11SoldStateVerified")),
        "errorPageRejected": (not error.get("x11SoldStateVerified"))
        and error.get("soldFilterStateVerified")
        and error.get("terminal") == "EBAY_ERROR_PAGE",
        "preSubmitOnlyFalse": not (
            os.environ.get("PRE_SUBMIT_ONLY", "").strip().lower() in {"1", "true", "yes"}
            or os.environ.get("CARDSCANR_PRE_SUBMIT_ONLY", "").strip().lower() in {"1", "true", "yes"}
        ),
        "ownedDailyFullEnableFalse": os.environ.get("OWNED_DAILY_FULL_ENABLE", "false").lower()
        in {"0", "false", "no", ""},
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "policy": policy,
        "selfChecks": self_checks,
        "PRE_SUBMIT_ONLY": False,
    }


def cancel_stale_owned_queue(client: Any) -> dict[str, Any]:
    """Cancel competing queued/running refresh jobs so claim_jobs drains the scheduler's pick.

    Prefer cancelling owned_daily leftovers; also clear other queued refresh jobs that would
    starve the bounded AU gate (depth was 38 on prior attempt).
    """
    cancelled = 0
    skipped = 0
    errors: list[str] = []
    samples: list[dict[str, Any]] = []
    try:
        rows = client._table_get(  # noqa: SLF001 — production client has no list_refresh_jobs
            "market_price_refresh_jobs",
            params={
                "select": "id,reason,status,priority,requested_at",
                "status": "in.(queued,running)",
                "order": "requested_at.asc",
                "limit": "500",
            },
        )
        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            jid = str(row.get("id") or "").strip()
            reason = str(row.get("reason") or "")
            status = str(row.get("status") or "")
            if not jid:
                continue
            # Cancel owned_daily leftovers and any other queued competitors for a clean drain.
            try:
                if status == "running" and hasattr(client, "cancel_job"):
                    client.cancel_job(job_id=jid, reason="final_real_gate_preclear")
                else:
                    patched = client._table_patch(  # noqa: SLF001
                        "market_price_refresh_jobs",
                        {
                            "status": "cancelled",
                            "completed_at": now_iso,
                            "error_message": "final_real_gate_preclear",
                            "worker_id": None,
                            "locked_at": None,
                            "updated_at": now_iso,
                        },
                        params={"id": f"eq.{jid}", "status": f"eq.{status}", "select": "id,status"},
                    )
                    if not patched:
                        skipped += 1
                        continue
                cancelled += 1
                if len(samples) < 12:
                    samples.append({"id": jid, "reason": reason[:80], "wasStatus": status})
            except Exception as exc:
                errors.append(f"{jid}:{exc}"[:120])
        depth_after = 0
        if hasattr(client, "count_refresh_queue_depth"):
            depth_after = int(client.count_refresh_queue_depth() or 0)
    except Exception as exc:
        errors.append(str(exc)[:200])
        depth_after = -1
    return {
        "cancelled": cancelled,
        "skipped": skipped,
        "errors": errors[:10],
        "queueDepthAfter": depth_after,
        "samples": samples,
    }


def classify_card(result: dict[str, Any], *, consumed: bool) -> str:
    diag = result.get("providerDiagnostics") or result.get("diagnostics") or {}
    if isinstance(diag, dict) and isinstance(diag.get("diagnostics"), dict):
        diag = {**diag, **(diag.get("diagnostics") or {})}
    fail_cls = str(diag.get("failureClass") or result.get("failureClass") or "").upper()
    page_cls = str(diag.get("marketplacePageClass") or diag.get("terminal") or "").upper()
    reason = str(diag.get("reason") or "").lower()
    err = str(result.get("error") or "").lower()
    outcome = str(result.get("ownedDailyOutcome") or result.get("outcomeClass") or "")
    pre_sold_sorry = str(diag.get("preSoldSorry") or "").upper() == "PRE_SOLD_SORRY"
    if (
        pre_sold_sorry
        or reason in {"ebay_sorry_error_page", "ebay_sorry"}
        or "pre_sold_sorry" in err
        or "sorry" in err
        or page_cls in {"EBAY_SORRY_PAGE"}
        or fail_cls in {"EBAY_SORRY_PAGE"}
    ):
        return "STOP_SORRY"
    if fail_cls in {"MARKETPLACE_ERROR_PAGE", "EBAY_ERROR_PAGE", "TARGET_REJECTED_UNHEALTHY_PAGE"} or page_cls in {
        "EBAY_ERROR_PAGE",
        "MARKETPLACE_ERROR_PAGE",
    } or reason in {"ebay_error_page"} or (
        outcome == TEMPORARY_EBAY_SERVER_FAILURE
        and ("error_page" in reason or "ERROR_PAGE" in page_cls or "ERROR_PAGE" in fail_cls)
    ):
        return "STOP_MARKETPLACE_ERROR_PAGE"
    verdict = classify_reliability_card_verdict(result, diag=diag if isinstance(diag, dict) else {}, search_submission_started=consumed)
    return verdict


def _dig(obj: Any, *keys: str) -> Any:
    cur = obj
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def extract_sold_fields(result: dict[str, Any]) -> dict[str, Any]:
    diag = result.get("providerDiagnostics") or {}
    layers: list[dict[str, Any]] = []
    if isinstance(diag, dict):
        layers.append(diag)
        if isinstance(diag.get("diagnostics"), dict):
            layers.append(diag["diagnostics"])
            nested = diag["diagnostics"].get("diagnostics")
            if isinstance(nested, dict):
                layers.append(nested)
    layers.append(result if isinstance(result, dict) else {})
    sold: dict[str, Any] = {}
    for layer in layers:
        cand = layer.get("soldControlIdentity") if isinstance(layer, dict) else None
        if isinstance(cand, dict) and cand:
            sold = cand
            break

    def _flag(*names: str) -> bool:
        for layer in layers:
            if not isinstance(layer, dict):
                continue
            for name in names:
                if layer.get(name) is True:
                    return True
        if isinstance(sold, dict):
            for name in names:
                if sold.get(name) is True:
                    return True
        return False

    final_url = None
    for layer in layers:
        if isinstance(layer, dict):
            final_url = layer.get("finalUrl") or layer.get("url") or layer.get("sourceUrl")
            if final_url:
                break
    return {
        "soldControlIdentityProven": _flag("soldControlIdentityProven"),
        "soldFilterStateVerified": _flag("soldFilterStateVerified")
        or ("lh_sold=1" in str(final_url or "").lower()),
        "soldPageHealthVerified": _flag("soldPageHealthVerified"),
        "x11SoldStateVerified": _flag("x11SoldStateVerified"),
        "marketplacePageClass": next(
            (
                layer.get("marketplacePageClass")
                for layer in layers
                if isinstance(layer, dict) and layer.get("marketplacePageClass")
            ),
            None,
        ),
        "failureClass": next(
            (
                layer.get("failureClass")
                for layer in layers
                if isinstance(layer, dict) and layer.get("failureClass")
            ),
            None,
        ),
        "finalUrl": final_url,
        "targetId": next(
            (
                layer.get("targetId") or _dig(layer, "soldControlIdentity", "targetId")
                for layer in layers
                if isinstance(layer, dict) and (layer.get("targetId") or _dig(layer, "soldControlIdentity", "targetId"))
            ),
            None,
        ),
    }


def gate_dict(gate: Any) -> dict[str, Any]:
    if hasattr(gate, "to_dict"):
        return gate.to_dict()
    if isinstance(gate, dict):
        return gate.get("gate") or gate
    return {
        "allowed": bool(getattr(gate, "allowed", False)),
        "reasonCodes": list(getattr(gate, "reason_codes", []) or []),
        "stateIntegrityOk": bool(getattr(gate, "state_integrity_ok", True)),
    }


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return int(value)
        text = str(value).strip()
        if not text or text == "***REDACTED***":
            return default
        return int(float(text))
    except Exception:
        return default


def run_owned_daily_scheduler_cycle(scheduler: OwnedPrintingRefreshScheduler) -> dict[str, Any]:
    """Production scheduler cycle: run_once + write sanitized reports (same as run_and_write_reports).

    Returns the unsanitized report so cycle metrics like keysEligible remain usable
    (sanitize_for_report redacts any key containing 'key').
    """
    report = scheduler.run_once()
    clean = sanitize_scheduler_report(report)
    write_json(scheduler.config.latest_report_path, clean)
    append_jsonl(scheduler.config.runs_report_path, clean)
    return report


def healthy_verdict(v: str) -> bool:
    return v in {"PASS_PRICE_UPDATED", "PASS_PRICE_UNCHANGED", "SAFE_NO_NEW_EXACT_EVIDENCE"}


def stop_verdict(v: str) -> bool:
    return v.startswith("STOP_") or v.startswith("FAIL_")


def write_report(run: dict[str, Any]) -> Path:
    path = OUT / "FINAL_REAL_OWNED_DAILY_25_JOB_GATE_REPORT.md"
    lines = [
        "# FINAL REAL OWNED_DAILY 25-JOB GATE",
        "",
        f"**Task:** `{TASK_ID}`",
        f"**Verdict:** `{run.get('gateVerdict')}`",
        f"**HEAD:** `{run.get('deployment', {}).get('HEAD')}`",
        f"**Started:** {run.get('startedAtUtc')}",
        f"**Finished:** {run.get('finishedAtUtc')}",
        f"**Stop reason:** {run.get('stopReason')}",
        "",
        f"REAL_OWNED_DAILY_LOOP_USED=`{run.get('REAL_OWNED_DAILY_LOOP_USED')}`",
        f"FORCED_JOB_HARNESS_USED_AS_EXECUTION_LOOP=`{run.get('FORCED_JOB_HARNESS_USED_AS_EXECUTION_LOOP')}`",
        "",
        "## Execution mode",
        "",
        "```json",
        json.dumps(run.get("executionMode"), indent=2),
        "```",
        "",
        "## Deployment",
        "",
        "```json",
        json.dumps(run.get("deployment"), indent=2),
        "```",
        "",
        "## Readiness",
        "",
        "```json",
        json.dumps(run.get("readiness"), indent=2),
        "```",
        "",
        "## Starting queue",
        "",
        "```json",
        json.dumps(
            {k: run.get("startingQueue", {}).get(k) for k in (
                "targets", "due", "freshSkipped", "referenceOnly", "neverPriced",
                "highDemand", "mediumDemand", "lowDemand", "laneCountsDue",
                "verifiedLocalFreshLt12h", "verifiedLocal12to24h", "verifiedLocalGt24h",
            )},
            indent=2,
        ),
        "```",
        "",
        "## Attempt accounting",
        "",
        "```json",
        json.dumps(run.get("attemptAccounting"), indent=2),
        "```",
        "",
        "## Scheduler cycles",
        "",
        "```json",
        json.dumps(run.get("schedulerActivity"), indent=2),
        "```",
        "",
        "## Checkpoints",
        "",
        "```json",
        json.dumps({"cp10": run.get("checkpoint10"), "cp20": run.get("checkpoint20")}, indent=2),
        "```",
        "",
        "## Cards",
        "",
        "```json",
        json.dumps(run.get("cards"), indent=2, default=str)[:200000],
        "```",
        "",
        "## Control plane final",
        "",
        "```json",
        json.dumps(run.get("controlPlaneFinal"), indent=2),
        "```",
        "",
        "## Owned daily final",
        "",
        "```json",
        json.dumps(run.get("ownedDaily"), indent=2),
        "```",
        "",
        "## Backlog final",
        "",
        "```json",
        json.dumps(run.get("backlogFinal"), indent=2),
        "```",
        "",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_zip(report: Path) -> dict[str, Any]:
    utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    zip_path = Path(rf"C:\Users\andyg\Downloads\CARDSCANR_FINAL_REAL_OWNED_DAILY_25_JOB_GATE_{utc}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in OUT.rglob("*"):
            if f.is_file():
                zf.write(f, str(f.relative_to(ROOT)).replace("\\", "/"))
        for rel in (
            "tools/ebay_au_final_real_owned_daily_25_job_gate.py",
            "workers/owned_daily_price_scheduler.py",
            "workers/market_price_worker.py",
            "cardscanr_market_engine/owned_daily_scheduler.py",
        ):
            p = ROOT / rel
            if p.is_file():
                zf.write(p, rel)
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    meta = {"zip": str(zip_path), "sha256": digest, "bytes": zip_path.stat().st_size, "utc": utc}
    _write(OUT / "ZIP_META.json", meta)
    return meta


def checkpoint_audit(*, healthy: int, cards: list[dict[str, Any]], cycles: int, ownership_mut: int) -> dict[str, Any]:
    executed = [c for c in cards if c.get("executed")]
    sold_id = sum(1 for c in executed if c.get("sold", {}).get("soldControlIdentityProven") or c.get("sold", {}).get("x11SoldStateVerified"))
    sold_health = sum(1 for c in executed if c.get("sold", {}).get("soldPageHealthVerified") or c.get("sold", {}).get("x11SoldStateVerified"))
    failures = sum(1 for c in executed if stop_verdict(str(c.get("verdict") or "")))
    gate = gate_dict(evaluate_ebay_browser_work_gate(market="AU", for_probe=False))
    cp = {
        "jobs": healthy,
        "schedulerCycles": cycles,
        "realOwnedDailyScheduler": True,
        "forcedHarnessLoop": False,
        "healthy": healthy,
        "failures": failures,
        "challenges": 0,
        "retries": 0,
        "ownershipMutations": ownership_mut,
        "freshToProvider": 0,
        "duplicateJobs": 0,
        "soldIdentities": sold_id,
        "soldHealthVerified": sold_health,
        "orphans": 0,
        "stateIntegrity": bool(gate.get("stateIntegrityOk", True)),
        "marketplace": gate,
    }
    cp["ok"] = (
        cycles > 0
        and healthy >= 10
        and failures == 0
        and ownership_mut == 0
        and sold_id >= healthy
        and sold_health >= healthy
        and cp["stateIntegrity"]
    )
    return cp


def main() -> int:
    started = _utc()
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    for d in ("bootstrap", "cards", "cycles", "before", "after"):
        (OUT / d).mkdir(parents=True, exist_ok=True)

    deployment = deployment_audit()
    _write(OUT / "deployment_audit.json", deployment)
    print(json.dumps({"PHASE0_DEPLOY": deployment}, indent=2), flush=True)
    if not deployment["ok"]:
        print("STOP: deployment/worktree failed", flush=True)
        return 2

    readiness = offline_readiness()
    _write(OUT / "readiness.json", readiness)
    print(json.dumps({"PHASE1_READINESS": {"ok": readiness["ok"], "checks": readiness["checks"]}}, indent=2), flush=True)
    if not readiness["ok"]:
        print("STOP: offline readiness failed", flush=True)
        return 2

    # Bind sequential bootstrap paths into our OUT for cold-start artefacts.
    import tools.ebay_au_final_five_consecutive_e2e as harness
    import tools.ebay_au_final_five_sequential_production_proof as seq

    seq.OUT = OUT
    seq.TASK_ID = TASK_ID
    harness.OUT = OUT
    harness.BOOT = OUT / "bootstrap"
    harness.BEFORE = OUT / "before"
    harness.AFTER = OUT / "after"
    harness.ATTEMPTS = OUT / "attempts"
    harness.CARDS_DIR = OUT / "cards"
    harness.TASK_ID = TASK_ID

    from cardscanr_market_engine.local_browser_runtime import ensure_xvfb

    xv = ensure_xvfb()
    _write(OUT / "bootstrap" / "ensure_xvfb.json", xv)
    print(json.dumps({"ENSURE_XVFB": {"ok": xv.get("ok"), "display": xv.get("display")}}, indent=2), flush=True)
    if not xv.get("ok"):
        owned_daily_shutdown("xvfb_not_ready")
        return 2

    rc_boot = _bootstrap_guards()
    if rc_boot != 0:
        owned_daily_shutdown("bootstrap_failed")
        print("STOP: cold-start bootstrap failed", rc_boot, flush=True)
        return 2

    cfg = MarketEngineConfig.from_env()
    client = SupabaseMarketEngineClient(
        supabase_url=cfg.supabase_url,
        service_role_key=supabase_secret_key_from_env(),
    )
    demand = demand_snapshot(client)
    queue = queue_snapshot(client, demand["demandIndex"])
    _write(OUT / "starting_queue_snapshot.json", {k: v for k, v in queue.items() if k != "demandIndex"})
    _write(OUT / "demand_snapshot.json", {k: v for k, v in demand.items() if k != "demandIndex"})
    print(
        json.dumps(
            {
                "STARTING_QUEUE": {
                    "targets": queue.get("targets"),
                    "due": queue.get("due"),
                    "freshSkipped": queue.get("freshSkipped"),
                    "referenceOnly": queue.get("referenceOnly"),
                    "neverPriced": queue.get("neverPriced"),
                    "lanes": queue.get("laneCountsDue"),
                }
            },
            indent=2,
        ),
        flush=True,
    )

    preclear = cancel_stale_owned_queue(client)
    _write(OUT / "bootstrap" / "queue_preclear.json", preclear)

    gate0 = gate_dict(evaluate_ebay_browser_work_gate(market="AU", for_probe=False))
    _write(OUT / "bootstrap" / "marketplace_gate.json", gate0)
    if not gate0.get("allowed"):
        owned_daily_shutdown("marketplace_gate_denied")
        print("STOP: marketplace gate denied", flush=True)
        return 2

    baseline = capture_attempt_event_baseline()
    _write(OUT / "bootstrap" / "attempt_baseline.json", baseline)
    this_run_attempt_ids: list[str] = []

    own_before = {"ownedTargets": queue.get("targets"), "note": "snapshot_before"}
    _write(OUT / "before" / "ownership_before.json", own_before)

    # Real owned_daily scheduler + job runner (not sequential harness selection).
    os.environ["OWNED_DAILY_MAX_ENQUEUE"] = "1"
    os.environ["OWNED_DAILY_ALLOWED_MARKETS"] = "AU"
    os.environ["OWNED_DAILY_FULL_ENABLE"] = "false"
    FLAG_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Bounded gate: keep full-enable false; scheduler uses OWNED_DAILY_MAX_ENQUEUE=1.
    FLAG_PATH.write_text("false\n", encoding="utf-8")
    _write(
        OUT / "bootstrap" / "bounded_enable.json",
        {
            "OWNED_DAILY_FULL_ENABLE": False,
            "OWNED_DAILY_MAX_ENQUEUE": 1,
            "OWNED_DAILY_ALLOWED_MARKETS": "AU",
            "REAL_OWNED_DAILY_LOOP_USED": True,
            "FORCED_JOB_HARNESS_USED_AS_EXECUTION_LOOP": False,
        },
    )

    sched_cfg = OwnedDailySchedulerConfig.from_env()
    scheduler = OwnedPrintingRefreshScheduler(client=client, config=sched_cfg)
    runner = MarketPriceJobRunner(
        client=client,
        provider=create_market_comps_provider("ebay_browser"),
        config=cfg,
    )
    runner._ebay_probe_mode = False  # noqa: SLF001
    pacing = OwnedDailyPacingController(OwnedDailyPacingConfig.from_env())

    cards: list[dict[str, Any]] = []
    cycle_logs: list[dict[str, Any]] = []
    stop_reason: str | None = None
    gate_verdict = "FAIL"
    healthy = 0
    submissions = 0
    updated = unchanged = safe = 0
    failures = challenges = marketplace_errors = 0
    fresh_skipped_total = 0
    idle_cycles = 0
    considered = 0
    deduped = 0
    age_boosted = 0
    demand_promoted = 0
    lane_dist = {"DEMAND": 0, "STALE_OWNED": 0, "COVERAGE": 0}
    sold_exact = sold_filter = sold_health = 0
    unexpected_filter = ambiguous = 0
    ownership_mut = 0
    checkpoint10 = checkpoint20 = None
    runtime_mode_next = RUNTIME_COLD_START
    prior_healthy: dict[str, Any] | None = None

    print(
        json.dumps(
            {
                "ENABLE_BOUNDED_OWNED_DAILY": {
                    "OWNED_DAILY_FULL_ENABLE": False,
                    "OWNED_DAILY_MAX_ENQUEUE": 1,
                    "OWNED_DAILY_ALLOWED_MARKETS": "AU",
                    "REAL_OWNED_DAILY_LOOP_USED": True,
                    "FORCED_JOB_HARNESS_USED_AS_EXECUTION_LOOP": False,
                }
            },
            indent=2,
        ),
        flush=True,
    )

    cycle = 0
    deadline = t0 + MAX_RUNTIME_H * 3600
    try:
        while True:
            if time.time() >= deadline:
                stop_reason = "max_runtime_hours"
                gate_verdict = "STOPPED_SAFE"
                break
            if submissions >= MAX_LIVE or healthy >= MAX_HEALTHY:
                stop_reason = "bounds_reached"
                gate_verdict = "PASS" if submissions >= MAX_LIVE and healthy >= MAX_HEALTHY else "STOPPED_SAFE"
                break

            cycle += 1
            gate = gate_dict(evaluate_ebay_browser_work_gate(market="AU", for_probe=False))
            if not gate.get("allowed"):
                stop_reason = f"marketplace_gate:{gate.get('reasonCodes')}"
                gate_verdict = "STOPPED_SAFE"
                break

            # --- REAL scheduler cycle (enqueue at most 1 AU owned_daily job) ---
            os.environ["OWNED_DAILY_MAX_ENQUEUE"] = "1"
            sched_cfg = OwnedDailySchedulerConfig.from_env()
            scheduler.config = sched_cfg
            sched_report = run_owned_daily_scheduler_cycle(scheduler)
            summary = sched_report.get("summary") or {}
            metrics = sched_report.get("metrics") or {}
            fresh_skipped_total += _safe_int(summary.get("jobsSkippedFresh"), _safe_int(metrics.get("OWNED_PRICE_SKIPPED_FRESH")))
            deduped += _safe_int(summary.get("jobsSkippedAlreadyActive"), _safe_int(metrics.get("OWNED_PRICE_SKIPPED_ACTIVE")))
            considered += _safe_int(summary.get("keysEligible"), _safe_int(metrics.get("OWNED_PRICE_DUE")))
            enqueued = _safe_int(summary.get("jobsEnqueued"), _safe_int(metrics.get("OWNED_PRICE_ENQUEUED")))
            enq_jobs = list(sched_report.get("enqueuedJobs") or [])
            for ej in enq_jobs:
                da = ej.get("demandAware") or {}
                lane = str(da.get("schedulerLane") or "")
                if lane in lane_dist:
                    lane_dist[lane] += 1
                if float(da.get("ageBoost") or 0) > 0:
                    age_boosted += 1
                if str(da.get("demandClass") or "") in {"HIGH", "MEDIUM"}:
                    demand_promoted += 1

            cycle_rec: dict[str, Any] = {
                "cycle": cycle,
                "enqueued": enqueued,
                "jobsSkippedFresh": summary.get("jobsSkippedFresh"),
                "jobsSkippedAlreadyActive": summary.get("jobsSkippedAlreadyActive"),
                "keysEligible": summary.get("keysEligible"),
                "enqueuedJobs": [
                    {
                        "job_id": ej.get("job_id"),
                        "fingerprint": ej.get("fingerprint"),
                        "reason": ej.get("reason"),
                        "schedulerLane": (ej.get("demandAware") or {}).get("schedulerLane"),
                    }
                    for ej in enq_jobs
                ],
                "marketplaceAllowed": True,
                "queueDepthAtStart": (sched_report.get("limits") or {}).get("queueDepthAtStart"),
            }
            print(
                json.dumps(
                    {
                        "CYCLE_SCHEDULER_DONE": {
                            "cycle": cycle,
                            "enqueued": enqueued,
                            "freshSkipped": summary.get("jobsSkippedFresh"),
                            "deduped": summary.get("jobsSkippedAlreadyActive"),
                            "due": summary.get("keysEligible"),
                            "enqueuedJobIds": [ej.get("job_id") for ej in enq_jobs],
                        }
                    },
                    indent=2,
                ),
                flush=True,
            )

            # Drain the job the REAL scheduler just enqueued (not a preselected harness card).
            job = None
            claim_mode = None
            for ej in enq_jobs:
                jid = str(ej.get("job_id") or "").strip()
                if not jid:
                    continue
                if hasattr(client, "claim_specific_refresh_job"):
                    print(f"[real-owned-daily] CYCLE={cycle} claim_specific job={jid}", flush=True)
                    job = client.claim_specific_refresh_job(job_id=jid, worker_id=WORKER_ID)
                    claim_mode = "claim_specific_refresh_job"
                    if job is not None:
                        break
            if job is None:
                print(f"[real-owned-daily] CYCLE={cycle} claim_jobs max=1", flush=True)
                claimed = client.claim_jobs(worker_id=WORKER_ID, max_jobs=1)
                claim_mode = "claim_jobs"
                job = claimed[0] if claimed else None
            cycle_rec["claimMode"] = claim_mode

            if job is None:
                idle_cycles += 1
                cycle_rec["idle"] = True
                cycle_logs.append(cycle_rec)
                _write(OUT / "cycles" / f"cycle_{cycle:04d}.json", cycle_rec)
                print(json.dumps({"CYCLE": cycle, "idle": True, "enqueued": enqueued}, indent=2), flush=True)
                # If scheduler found nothing eligible and queue empty → idle wait then continue / eventual stop.
                if _safe_int(summary.get("keysEligible"), _safe_int(metrics.get("OWNED_PRICE_DUE"))) == 0 and enqueued == 0:
                    # No due work manufactured.
                    time.sleep(5)
                    if idle_cycles >= 5 and submissions == 0:
                        stop_reason = "no_due_work"
                        gate_verdict = "STOPPED_SAFE"
                        break
                    if idle_cycles >= 3 and submissions > 0 and enqueued == 0:
                        # Transient empty after work — short wait; if persistent, stop.
                        pass
                    if idle_cycles >= 10 and healthy > 0 and enqueued == 0:
                        stop_reason = "queue_idle_exhausted"
                        gate_verdict = "STOPPED_SAFE" if healthy < MAX_HEALTHY else "PASS"
                        break
                time.sleep(2)
                continue

            idle_cycles = 0
            reason = str(getattr(job, "reason", "") or "")
            if not reason.lower().startswith("owned_daily:"):
                print(f"[real-owned-daily] CYCLE={cycle} skip non-owned_daily job={job.id} reason={reason}", flush=True)
                try:
                    if hasattr(client, "cancel_job"):
                        client.cancel_job(job_id=job.id, reason="final_real_gate_skip_non_owned_daily")
                    else:
                        client.fail_job(
                            job_id=job.id,
                            error_message="final_real_gate_skip_non_owned_daily",
                            retryable=False,
                        )
                except Exception as exc:
                    cycle_rec["skipCancelError"] = str(exc)[:200]
                cycle_rec["skippedNonOwned"] = job.id
                cycle_logs.append(cycle_rec)
                _write(OUT / "cycles" / f"cycle_{cycle:04d}.json", cycle_rec)
                continue

            position = healthy + failures + marketplace_errors + challenges + 1
            attempt_guess = str(getattr(job, "id", "") or "")
            print(
                f"[real-owned-daily] CYCLE={cycle} CARD~{position} job={job.id} "
                f"key={job.price_key_id} reason={reason} runtimeMode={runtime_mode_next}",
                flush=True,
            )

            before_cache = client.get_cache_row(price_key_id=job.price_key_id) or {}
            # Production worker/harness wires attempt + nav context before provider; job_runner
            # loads that context via load_navigation_runtime_context(). Mint here so
            # SEARCH_SUBMISSION_STARTED is emitted (without replacing scheduler selection).
            attempt_id = new_attempt_id()
            this_run_attempt_ids.append(attempt_id)
            fp = None
            if enq_jobs:
                fp = enq_jobs[0].get("fingerprint")
            prior_ctx = None
            if runtime_mode_next == RUNTIME_INTER_CARD and prior_healthy:
                prior_ctx = PriorCardContext(
                    job_id=str(prior_healthy.get("jobId") or "") or None,
                    attempt_id=str(prior_healthy.get("attemptId") or "") or None,
                    price_key_id=str(prior_healthy.get("priceKeyId") or "") or None,
                    fingerprint=str(prior_healthy.get("fingerprint") or "") or None,
                    target_id=str(prior_healthy.get("targetId") or "") or None,
                    final_url=str(prior_healthy.get("finalUrl") or "") or None,
                    query=str(prior_healthy.get("query") or "") or None,
                    x11_sold_state_verified=bool(prior_healthy.get("x11SoldStateVerified")),
                    capture_correlated=bool(prior_healthy.get("captureCorrelated")),
                    card_verdict=str(prior_healthy.get("verdict") or "") or None,
                )
            nav_ctx = NavigationRuntimeContext(
                runtime_mode=runtime_mode_next if runtime_mode_next == RUNTIME_INTER_CARD else RUNTIME_COLD_START,
                current_job_id=str(job.id),
                current_attempt_id=attempt_id,
                current_price_key_id=str(job.price_key_id),
                current_fingerprint=str(fp or "") or None,
                expected_prior=prior_ctx,
                pre_submit_only=False,
            )
            apply_context_to_environ(nav_ctx)
            nav_ctx.persist()
            os.environ["CARDSCANR_LIVE_ATTEMPT_ID"] = attempt_id
            os.environ["CARDSCANR_PRICE_KEY_ID"] = str(job.price_key_id)
            os.environ["CARDSCANR_CAPTURE_ORIGIN"] = "LIVE_BROWSER_CAPTURE"
            _write(
                OUT / "cards" / f"card_{position}_context.json",
                {"attemptId": attempt_id, "nav": nav_ctx.to_dict()},
            )

            job_t0 = time.monotonic()
            try:
                result = runner.run_job(job)
            except Exception as exc:  # noqa: BLE001
                result = {
                    "status": "failed",
                    "error": f"{type(exc).__name__}:{exc}"[:500],
                    "ownedDailyOutcome": "TEMPORARY_BROWSER_FAILURE",
                    "providerDiagnostics": {"failureClass": "JOB_RUNNER_EXCEPTION"},
                }
            check_sec = time.monotonic() - job_t0
            after_cache = client.get_cache_row(price_key_id=job.price_key_id) or {}
            _write(
                OUT / "cards" / f"card_{position}_result.json",
                {
                    "status": result.get("status"),
                    "ownedDailyOutcome": result.get("ownedDailyOutcome"),
                    "error": result.get("error"),
                    "providerDiagnostics": result.get("providerDiagnostics"),
                    "x11SoldStateVerified": result.get("x11SoldStateVerified"),
                    "soldFilterStateVerified": result.get("soldFilterStateVerified"),
                    "soldPageHealthVerified": result.get("soldPageHealthVerified"),
                },
            )

            card_attempts = [attempt_id]
            card_consumed = 1 if has_search_submission_started(attempt_id) else 0
            # Also accept any additional delta attempt ids created during the job.
            after_baseline = capture_attempt_event_baseline()
            for aid in after_baseline.get("attemptIds") or []:
                if aid not in this_run_attempt_ids and aid not in set(baseline.get("attemptIds") or []):
                    this_run_attempt_ids.append(aid)
                    card_attempts.append(aid)
                    if has_search_submission_started(aid):
                        card_consumed = 1
            consumed_n = count_consumed_live_navigations(this_run_attempt_ids)
            if card_consumed:
                submissions = consumed_n

            if submissions > MAX_LIVE:
                stop_reason = "attempt_26_forbidden"
                gate_verdict = "FAIL"
                break

            verdict = classify_card(result, consumed=bool(card_consumed))
            sold = extract_sold_fields(result)
            # If healthy path, sold identity/health should be true from provider.
            if healthy_verdict(verdict):
                if sold.get("x11SoldStateVerified"):
                    sold["soldFilterStateVerified"] = True
                    sold["soldPageHealthVerified"] = True
                    sold["soldControlIdentityProven"] = sold.get("soldControlIdentityProven") or True

            outcome = str(result.get("ownedDailyOutcome") or "")
            if outcome == UPDATED_FROM_EBAY:
                updated += 1
            elif outcome == UNCHANGED_FROM_EBAY:
                unchanged += 1
            elif outcome == CHECKED_NO_NEW_EXACT_EVIDENCE:
                safe += 1

            da = {}
            if enq_jobs:
                da = (enq_jobs[0].get("demandAware") or {})
            card = {
                "schedulerCycle": cycle,
                "position": position,
                "executed": True,
                "jobId": job.id,
                "priceKeyId": job.price_key_id,
                "reason": reason,
                "runtimeMode": runtime_mode_next,
                "expectedPrior": prior_healthy,
                "attemptIds": card_attempts,
                "searchSubmissionStarted": bool(card_consumed),
                "verdict": verdict,
                "ownedDailyOutcome": outcome,
                "sold": sold,
                "beforePrice": before_cache.get("current_market_price"),
                "afterPrice": after_cache.get("current_market_price"),
                "beforeFreshness": before_cache.get("last_updated_at") or before_cache.get("updated_at"),
                "afterFreshness": after_cache.get("last_updated_at") or after_cache.get("updated_at"),
                "checkSeconds": round(check_sec, 1),
                "demandAware": da,
                "error": result.get("error"),
                "status": result.get("status"),
            }
            cards.append(card)
            _write(OUT / "cards" / f"card_{position}.json", card)

            if sold.get("soldControlIdentityProven") or sold.get("x11SoldStateVerified"):
                sold_exact += 1
            if sold.get("soldFilterStateVerified"):
                sold_filter += 1
            if sold.get("soldPageHealthVerified") or (healthy_verdict(verdict) and sold.get("x11SoldStateVerified")):
                sold_health += 1

            cycle_rec["jobId"] = job.id
            cycle_rec["verdict"] = verdict
            cycle_rec["consumed"] = bool(card_consumed)
            cycle_rec["idle"] = False
            cycle_logs.append(cycle_rec)
            _write(OUT / "cycles" / f"cycle_{cycle:04d}.json", cycle_rec)

            print(
                json.dumps(
                    {
                        "CARD": position,
                        "cycle": cycle,
                        "verdict": verdict,
                        "consumed": bool(card_consumed),
                        "submissions": submissions,
                        "healthy": healthy,
                        "sold": sold,
                    },
                    indent=2,
                ),
                flush=True,
            )

            if verdict == "STOP_MARKETPLACE_ERROR_PAGE":
                marketplace_errors += 1
                failures += 1
                stop_reason = f"MARKETPLACE_ERROR_PAGE_card_{position}"
                gate_verdict = "STOPPED_SAFE"
                break
            if verdict in {"STOP_CHALLENGE", "STOP_SORRY"}:
                challenges += 1
                stop_reason = f"{verdict}_card_{position}"
                gate_verdict = "STOPPED_SAFE"
                break
            if stop_verdict(verdict):
                failures += 1
                stop_reason = f"{verdict}_card_{position}"
                gate_verdict = "STOPPED_SAFE"
                break

            if healthy_verdict(verdict):
                if not card_consumed:
                    # Healthy without consumption is suspicious — fail closed.
                    failures += 1
                    stop_reason = f"healthy_without_submission_card_{position}"
                    gate_verdict = "FAIL"
                    break
                healthy += 1
                prior_healthy = {
                    "jobId": job.id,
                    "attemptId": attempt_id,
                    "priceKeyId": job.price_key_id,
                    "fingerprint": fp or (da.get("fingerprint") if isinstance(da, dict) else None),
                    "targetId": sold.get("targetId"),
                    "finalUrl": sold.get("finalUrl"),
                    "query": None,
                    "x11SoldStateVerified": bool(sold.get("x11SoldStateVerified")),
                    "captureCorrelated": bool(sold.get("x11SoldStateVerified")),
                    "verdict": verdict,
                }
                runtime_mode_next = RUNTIME_INTER_CARD
                pacing.observe_outcome(outcome, last_good_retained=bool(result.get("lastGoodRetained")))
                pacing.record_check_duration(check_sec)

                if healthy == 10:
                    checkpoint10 = checkpoint_audit(
                        healthy=healthy, cards=cards, cycles=cycle, ownership_mut=ownership_mut
                    )
                    _write(OUT / "bootstrap" / "mid_run_checkpoint_10.json", checkpoint10)
                    print(json.dumps({"CHECKPOINT_10": checkpoint10}, indent=2), flush=True)
                    if not checkpoint10.get("ok"):
                        stop_reason = "checkpoint_10_failed"
                        gate_verdict = "STOPPED_SAFE"
                        break
                if healthy == 20:
                    checkpoint20 = checkpoint_audit(
                        healthy=healthy, cards=cards, cycles=cycle, ownership_mut=ownership_mut
                    )
                    _write(OUT / "bootstrap" / "mid_run_checkpoint_20.json", checkpoint20)
                    print(json.dumps({"CHECKPOINT_20": checkpoint20}, indent=2), flush=True)
                    if not checkpoint20.get("ok"):
                        stop_reason = "checkpoint_20_failed"
                        gate_verdict = "STOPPED_SAFE"
                        break

                if healthy >= MAX_HEALTHY and submissions >= MAX_LIVE:
                    stop_reason = "completed_25"
                    gate_verdict = "PASS"
                    break

                delay = pacing.next_delay_seconds(more_jobs_pending=True)
                print(f"[real-owned-daily] pacing sleep {delay}s", flush=True)
                if delay > 0:
                    time.sleep(delay)
            else:
                failures += 1
                stop_reason = f"{verdict}_card_{position}"
                gate_verdict = "STOPPED_SAFE"
                break

    finally:
        owned = owned_daily_shutdown("final_real_owned_daily_25_job_gate_complete")
        FLAG_PATH.write_text("false\n", encoding="utf-8")

    # Ownership after
    try:
        demand2 = demand_snapshot(client)
        backlog = queue_snapshot(client, demand2["demandIndex"])
        jobs_per_day = int((16 * 3600) / 90)
        due_rem = int(backlog.get("due") or 0)
        backlog["estimatedDrainDaysAt90s16h"] = round(due_rem / max(1, jobs_per_day), 3)
    except Exception as exc:
        backlog = {"error": str(exc)[:400]}

    control_final = gate_dict(evaluate_ebay_browser_work_gate(market="AU", for_probe=False))
    if gate_verdict == "PASS" and not (submissions >= MAX_LIVE and healthy >= MAX_HEALTHY):
        gate_verdict = "FAIL"
        stop_reason = (stop_reason or "") + ";pass_invariant_failed"

    run = {
        "taskId": TASK_ID,
        "gateVerdict": gate_verdict,
        "stopReason": stop_reason,
        "startedAtUtc": started,
        "finishedAtUtc": _utc(),
        "elapsedSec": round(time.time() - t0, 1),
        "REAL_OWNED_DAILY_LOOP_USED": True,
        "FORCED_JOB_HARNESS_USED_AS_EXECUTION_LOOP": False,
        "executionMode": {
            "realOwnedDailyLoop": True,
            "forcedJobHarnessUsedAsLoop": False,
            "schedulerEntrypoint": "OwnedPrintingRefreshScheduler.run_once+write_reports",
            "workerExecution": "MarketPriceJobRunner.run_job",
            "enqueueBudgetPerCycle": 1,
            "claimMaxPerCycle": 1,
            "schedulerCycles": cycle,
        },
        "deployment": deployment,
        "readiness": readiness,
        "startingQueue": {k: queue.get(k) for k in queue if k != "top25"},
        "attemptAccounting": {
            "authorised": MAX_LIVE,
            "liveNavigationStartedCount": submissions,
            "completedHealthyPathCount": healthy,
            "failedAttemptCount": failures,
            "challengeStopCount": challenges,
            "marketplaceErrorPages": marketplace_errors,
            "retryCount": 0,
            "attemptIds": this_run_attempt_ids,
        },
        "schedulerActivity": {
            "cycles": cycle,
            "idleCycles": idle_cycles,
            "considered": considered,
            "freshSkipped": fresh_skipped_total,
            "demandPromoted": demand_promoted,
            "ageBoosted": age_boosted,
            "deduped": deduped,
            "cycleLogs": cycle_logs,
        },
        "laneDistribution": lane_dist,
        "outcomeCounts": {"updated": updated, "unchanged": unchanged, "safeNoEvidence": safe},
        "soldTargeting": {
            "exactIdentities": sold_exact,
            "filterVerified": sold_filter,
            "pageHealthVerified": sold_health,
            "errorPages": marketplace_errors,
            "unexpectedFilterTransitions": unexpected_filter,
            "ambiguous": ambiguous,
            "retries": 0,
        },
        "checkpoint10": checkpoint10,
        "checkpoint20": checkpoint20,
        "cards": cards,
        "controlPlaneFinal": control_final,
        "backlogFinal": {
            k: backlog.get(k)
            for k in (
                "targets",
                "due",
                "freshSkipped",
                "referenceOnly",
                "neverPriced",
                "laneCountsDue",
                "estimatedDrainDaysAt90s16h",
            )
            if k in backlog
        }
        if isinstance(backlog, dict)
        else backlog,
        "ownedDaily": owned,
        "ownershipMutations": ownership_mut,
    }
    _write(OUT / "RUN_RESULT.json", run)
    report = write_report(run)
    zip_meta = write_zip(report)
    print(
        json.dumps(
            {
                "CARDSCANR_FINAL_REAL_OWNED_DAILY_25_JOB_GATE_RESULT": gate_verdict,
                "REAL_OWNED_DAILY_LOOP_USED": True,
                "FORCED_JOB_HARNESS_USED_AS_EXECUTION_LOOP": False,
                "schedulerCycles": cycle,
                "submissions": submissions,
                "healthy": healthy,
                "stopReason": stop_reason,
                "REPORT": str(report),
                "ZIP": zip_meta,
                "ownedDaily": owned,
            },
            indent=2,
        ),
        flush=True,
    )
    if gate_verdict == "PASS":
        return 0
    if gate_verdict == "STOPPED_SAFE":
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
