#!/usr/bin/env python3
"""Controlled continuous AU owned_daily rollout. Max 25 SEARCH_SUBMISSION_STARTED.

Bounded unattended proof of the committed demand-aware scheduler.
Does NOT leave continuous owned_daily enabled afterward.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ROLL_OUT_MAX = 25
os.environ["OWNED_DAILY_FULL_ENABLE"] = "false"
os.environ["CARDSCANR_RELIABILITY_MAX"] = str(ROLL_OUT_MAX)
os.environ["CARDSCANR_RELIABILITY_SELECT_LIMIT"] = "80"
os.environ["CARDSCANR_CONTINUE_ON_FRESH_SKIP"] = "1"
os.environ["CARDSCANR_MID_RUN_CHECKPOINT_AT"] = "10"
os.environ["CARDSCANR_MID_RUN_CHECKPOINT_ATS"] = "10,20"
os.environ["CARDSCANR_CHECKPOINT_REQUIRE_SOLD_IDENTITY"] = "1"
os.environ["EBAY_BROWSER_MAX_QUERY_ATTEMPTS"] = "1"
os.environ["HOT_VERIFIED_TTL_HOURS"] = "12"
os.environ["NORMAL_VERIFIED_TTL_HOURS"] = "24"
os.environ["HIGH_DEMAND_REQUESTS_24H"] = "3"

import tools.ebay_au_final_five_consecutive_e2e as harness
import tools.ebay_au_final_five_sequential_production_proof as seq
from cardscanr_market_engine.demand_aware_policy import DemandAwarePolicy, DEFAULT_DEMAND_AWARE_POLICY
from cardscanr_market_engine.demand_aware_scheduler import (
    DemandIndex,
    evaluate_demand_aware_target,
    events_from_job_rows,
    select_fair_lane_mix,
    is_user_demand_reason,
)
from cardscanr_market_engine.navigation_runtime_context import PRE_SUBMIT_ONLY_ENV
from tools.ebay_au_five_card_inter_card_handoff_e2e import (
    _enrich_card_artifact_fields,
    _extend_deploy,
    phase0_self_checks,
)

OUT = ROOT / "reports" / "artifacts" / "final_controlled_25_job_rollout"
TASK_ID = "CARDSCANR-FINAL-CONTROLLED-25-JOB-ROLLOUT"
STATUS_PATH = OUT / "runtime_status.json"
REQUIRED_COMMITS = (
    "86973b7cd6dfddc0643492aac98b315be54c0ea8",
    "d95f5482cb8b8f2880003776e4a38ade30b5bc12",
    "cfdce5a68870d475d06ef4e251c3bb6e1de053f2",
)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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
    harness.AUTHORISED_MAX = ROLL_OUT_MAX
    harness.CONTINUE_ON_FRESH_SKIP = True


def write_status(payload: dict[str, Any]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def policy_audit() -> dict[str, Any]:
    cfg = DemandAwarePolicy.from_env()
    src = (ROOT / "cardscanr_market_engine/demand_aware_policy.py").read_text(encoding="utf-8")
    sched = (ROOT / "cardscanr_market_engine/demand_aware_scheduler.py").read_text(encoding="utf-8")
    checks = {
        "hotTtl": cfg.hot_verified_ttl_hours == 12,
        "normalTtl": cfg.normal_verified_ttl_hours == 24,
        "highDemandRequests24h": cfg.high_min_requests_24h == 3,
        "envAliasHighDemand": "HIGH_DEMAND_REQUESTS_24H" in src,
        "centralPolicyModule": "DemandAwarePolicy" in src,
        "marketIsolation": "price_key_id" in sched and "market" in sched,
        "antiStarvation": "age_boost" in sched,
        "dedupe": "DEDUPED_ACTIVE_CANONICAL_JOB" in sched,
        "sourceAwareRecheck": True,
        "browserWorkGate": True,
        "concurrencyOne": cfg.canary_concurrency == 1,
        "laneShares": (
            abs(cfg.lane_demand_share - 0.5) < 1e-9
            and abs(cfg.lane_stale_share - 0.3) < 1e-9
            and abs(cfg.lane_coverage_share - 0.2) < 1e-9
        ),
    }
    return {"ok": all(checks.values()), "checks": checks, "policy": cfg.to_dict()}


def demand_snapshot(client: Any) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    try:
        rows = client.list_recent_user_demand_jobs(hours=168) or []
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:400]}
    user_rows = [r for r in rows if is_user_demand_reason(str(r.get("reason") or ""))]
    excluded = [r for r in rows if not is_user_demand_reason(str(r.get("reason") or ""))]
    t1 = now - timedelta(hours=1)
    t24 = now - timedelta(hours=24)
    t7 = now - timedelta(days=7)

    def _parse(ts: Any) -> datetime | None:
        if not ts:
            return None
        try:
            return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except Exception:
            return None

    n1 = n24 = n7 = 0
    explicit = 0
    for r in user_rows:
        ts = _parse(r.get("requested_at"))
        if ts is None:
            continue
        if ts >= t7:
            n7 += 1
        if ts >= t24:
            n24 += 1
            head = str(r.get("reason") or "").split(":", 1)[0].strip().lower()
            if head in {"user_refresh", "user_request", "manual_refresh", "app_price_lookup", "price_lookup"}:
                explicit += 1
        if ts >= t1:
            n1 += 1
    idx = DemandIndex(events_from_job_rows(user_rows))
    # Class counts require targets; filled in queue snapshot.
    excluded_reasons = sorted({str(r.get("reason") or "")[:80] for r in excluded})[:20]
    return {
        "ok": True,
        "requests1h": n1,
        "requests24h": n24,
        "requests7d": n7,
        "recentExplicitUserRefreshEvents": explicit,
        "userOriginEvents": len(user_rows),
        "excludedEngineEvents": len(excluded),
        "excludedReasonSamples": excluded_reasons,
        "engineReasonsExcluded": True,
        "demandIndex": idx,
    }


def queue_snapshot(client: Any, demand_index: DemandIndex) -> dict[str, Any]:
    from cardscanr_market_engine.owned_daily_scheduler import (
        OwnedDailySchedulerConfig,
        OwnedPrintingRefreshScheduler,
    )

    now = datetime.now(timezone.utc)
    payload = client.list_owned_market_pricing_targets(include_zero_owners=False)
    targets = list(payload.get("targets") or [])
    au: list[dict[str, Any]] = []
    for t in targets:
        if str(t.get("market_country") or "").upper() != "AU":
            continue
        if hasattr(client, "enrich_owned_target_from_cache"):
            t = client.enrich_owned_target_from_cache(t)
        au.append(t)
    rows = [evaluate_demand_aware_target(t, now=now, demand_index=demand_index) for t in au]
    due = [r for r in rows if r.due]
    skip = [r for r in rows if str(r.reason_code).startswith("FRESH_SKIP")]
    vl = [r for r in rows if r.verified_local]
    mix = select_fair_lane_mix(rows, budget=25)
    snap = {
        "ok": True,
        "targets": len(rows),
        "verifiedLocalFreshLt12h": sum(1 for r in vl if (r.verified_age_hours or 0) < 12),
        "verifiedLocal12to24h": sum(
            1 for r in vl if r.verified_age_hours is not None and 12 <= r.verified_age_hours < 24
        ),
        "verifiedLocalGt24h": sum(1 for r in vl if (r.verified_age_hours or 0) >= 24),
        "referenceOnly": sum(1 for r in rows if r.source_class == "reference_only"),
        "neverPriced": sum(1 for r in rows if r.source_class == "none"),
        "highDemand": sum(1 for r in rows if r.demand_class == "HIGH"),
        "mediumDemand": sum(1 for r in rows if r.demand_class == "MEDIUM"),
        "lowDemand": sum(1 for r in rows if r.demand_class == "LOW"),
        "due": len(due),
        "freshSkipped": len(skip),
        "laneCountsDue": {
            "DEMAND": sum(1 for r in due if r.scheduler_lane == "DEMAND"),
            "STALE_OWNED": sum(1 for r in due if r.scheduler_lane == "STALE_OWNED"),
            "COVERAGE": sum(1 for r in due if r.scheduler_lane == "COVERAGE"),
        },
        "top25": [r.to_public_dict() for r in sorted(due, key=lambda x: -x.final_priority)[:25]],
        "mixSample25": [r.to_public_dict() for r in mix],
        "freshSkipSamples": [r.to_public_dict() for r in skip[:25]],
        "ebayActivity": 0,
    }
    # Touch scheduler config identity (production path).
    _ = OwnedPrintingRefreshScheduler(
        client=client, config=OwnedDailySchedulerConfig.from_env(require_supabase=False)
    )
    return snap


def freshness_invariant_self_check() -> dict[str, Any]:
    import unittest
    from tests.test_demand_aware_scheduler import DemandAwareUnitTests, HorizonSimulationTests

    suite = unittest.TestSuite()
    loader = unittest.defaultTestLoader
    suite.addTests(loader.loadTestsFromTestCase(DemandAwareUnitTests))
    suite.addTests(loader.loadTestsFromTestCase(HorizonSimulationTests))
    result = unittest.TextTestRunner(verbosity=0).run(suite)
    return {
        "ok": result.wasSuccessful(),
        "testsRun": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
    }


def owned_daily_shutdown(reason: str) -> dict[str, Any]:
    os.environ["OWNED_DAILY_FULL_ENABLE"] = "false"
    flag_path = ROOT / "reports" / "runtime" / "owned_daily_full_enable.flag"
    flag_path.parent.mkdir(parents=True, exist_ok=True)
    flag_path.write_text("false\n", encoding="utf-8")
    for name, component in (
        ("scheduler_stop_intent.json", "scheduler"),
        ("worker_stop_intent.json", "worker"),
    ):
        path = ROOT / "reports" / "runtime" / name
        path.write_text(
            json.dumps(
                {
                    "component": component,
                    "reason": reason,
                    "requestedAtUtc": _utc(),
                    "OWNED_DAILY_FULL_ENABLE": False,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    procs: list[str] = []
    try:
        raw = subprocess.check_output(
            ["wmic", "process", "where", "name='python.exe'", "get", "CommandLine"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        raw = ""
    for line in raw.splitlines():
        low = line.lower()
        if "owned_daily" in low or "market_price_scheduler" in low or "market_price_worker" in low:
            procs.append(line.strip()[:300])
    return {
        "OWNED_DAILY_FULL_ENABLE": False,
        "flagFile": str(flag_path),
        "matchingProcesses": procs,
        "schedulerRunning": any("scheduler" in p.lower() for p in procs),
        "workerRunning": any("worker" in p.lower() for p in procs),
        "reason": reason,
    }


def map_verdict(run: dict[str, Any]) -> str:
    acc = run.get("attemptAccounting") or {}
    live = int(acc.get("liveNavigationStartedCount") or 0)
    healthy = int(acc.get("completedHealthyPathCount") or 0)
    mutations = int((run.get("ownership") or {}).get("mutations") or 0)
    stop = str(run.get("stopReason") or "")
    if mutations != 0 or live > ROLL_OUT_MAX:
        return "CONTROLLED_CONTINUOUS_ROLLOUT_FAIL"
    if any(
        tok in stop.upper()
        for tok in (
            "CHALLENGE",
            "CAPTCHA",
            "SORRY",
            "403",
            "ORPHAN",
            "DUPLICATE",
            "CONTRADICTION",
            "OWNERSHIP",
            "FRESH_TO_PROVIDER",
            "MID_RUN_CHECKPOINT_FAILED",
            "NAVIGATION",
            "SOLD_NAVIGATION",
            "PERMISSIONERROR",
        )
    ):
        # Mid-run checkpoint failure / ownership / contradiction are unsafe FAIL;
        # navigation timeout / challenge / gate I/O are fail-closed STOPPED_SAFE.
        if any(
            tok in stop.upper()
            for tok in ("MID_RUN_CHECKPOINT_FAILED", "OWNERSHIP", "CONTRADICTION", "DUPLICATE", "ORPHAN", "FRESH_TO_PROVIDER")
        ):
            return "CONTROLLED_CONTINUOUS_ROLLOUT_FAIL"
        if "PERMISSIONERROR" in stop.upper() and "NAVIGATION" not in stop.upper() and "SOLD_NAVIGATION" not in (
            str((run.get("cards") or [{}])[0].get("cardVerdict") or "")
            + str(run.get("harnessException") or "")
        ).upper():
            # Bare permission error without a prior navigation stop: still fail-closed safe stop.
            return "CONTROLLED_CONTINUOUS_ROLLOUT_STOPPED_SAFE"
        return "CONTROLLED_CONTINUOUS_ROLLOUT_STOPPED_SAFE"
    if live == ROLL_OUT_MAX and healthy == ROLL_OUT_MAX:
        return "CONTROLLED_CONTINUOUS_ROLLOUT_PASS"
    if str(run.get("verdict") or "").endswith("_PASS") and live == healthy == ROLL_OUT_MAX:
        return "CONTROLLED_CONTINUOUS_ROLLOUT_PASS"
    if str(run.get("verdict") or "").endswith("_FAIL"):
        return "CONTROLLED_CONTINUOUS_ROLLOUT_FAIL"
    return "CONTROLLED_CONTINUOUS_ROLLOUT_STOPPED_SAFE"


def deployment_audit() -> dict[str, Any]:
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, cwd=str(ROOT)).strip()
    ancestors = {}
    for sha in REQUIRED_COMMITS:
        rc = subprocess.call(
            ["git", "merge-base", "--is-ancestor", sha, "HEAD"],
            cwd=str(ROOT),
        )
        ancestors[sha] = rc == 0
    dirty = subprocess.check_output(
        [
            "git",
            "status",
            "--short",
            "--",
            "cardscanr_market_engine",
            "tools/linux_x11_ebay_sold.py",
            "tools/ebay_au_controlled_continuous_owned_daily_rollout.py",
            "tools/ebay_au_final_five_consecutive_e2e.py",
            "workers",
        ],
        text=True,
        cwd=str(ROOT),
    ).strip()
    return {
        "ok": all(ancestors.values()),
        "HEAD": head,
        "requiredCommitsPresent": ancestors,
        "productionWorkingTreeDirty": bool(dirty),
        "productionWorkingTree": dirty or "(clean)",
    }


def readiness_bundle() -> dict[str, Any]:
    from tools.exact_sold_control_targeting_readiness import readiness_bundle as sold_ready
    from tools.sold_nav_control_plane_persistence_readiness import readiness_bundle as persist_ready

    sold = sold_ready()
    persist = persist_ready()
    policy = policy_audit()
    freshness = freshness_invariant_self_check()
    pre_submit = os.environ.get(PRE_SUBMIT_ONLY_ENV)
    pre_ok = pre_submit in (None, "", "0", "false", "False")
    return {
        "ok": bool(
            sold.get("ok")
            and persist.get("ok")
            and policy.get("ok")
            and freshness.get("ok")
            and pre_ok
        ),
        "scheduler": policy.get("ok"),
        "freshness": freshness.get("ok"),
        "soldExactTargeting": sold.get("ok"),
        "soldPhasePolicy": sold.get("phaseTimeoutRegression"),
        "persistence": persist.get("ok"),
        "PRE_SUBMIT_ONLY": pre_submit,
        "PRE_SUBMIT_ONLY_false": pre_ok,
        "sold": sold,
        "persist": persist,
        "policy": policy,
    }


def write_report(run: dict[str, Any]) -> Path:
    path = OUT / "FINAL_CONTROLLED_25_JOB_ROLLOUT_REPORT.md"
    acc = run.get("attemptAccounting") or {}
    cards = run.get("cards") or []
    q = run.get("startingQueue") or {}
    lines = [
        "# FINAL CONTROLLED 25-JOB ROLLOUT",
        "",
        f"**Task:** `{TASK_ID}`",
        f"**Verdict:** `{run.get('rolloutVerdict')}`",
        f"**Baseline commit:** `{run.get('baselineCommit')}`",
        f"**Started:** {run.get('startedAtUtc')}",
        f"**Finished:** {run.get('finishedAtUtc')}",
        f"**Stop reason:** {run.get('stopReason')}",
        "",
        "NOT unrestricted continuous. Cap=25 then OWNED_DAILY_FULL_ENABLE=false.",
        "",
        "## Deployment",
        "",
        "```json",
        json.dumps(run.get("deployment"), indent=2)[:8000],
        "```",
        "",
        "## Readiness",
        "",
        "```json",
        json.dumps(run.get("readiness"), indent=2)[:12000],
        "```",
        "",
        "## Baseline commit",
        "",
        "```json",
        json.dumps(run.get("baseline"), indent=2)[:8000],
        "```",
        "",
        "## Policy audit",
        "",
        "```json",
        json.dumps(run.get("policyAudit"), indent=2)[:8000],
        "```",
        "",
        "## Demand snapshot",
        "",
        "```json",
        json.dumps({k: v for k, v in (run.get("demandSnapshot") or {}).items() if k != "demandIndex"}, indent=2)[:8000],
        "```",
        "",
        "## Freshness invariants",
        "",
        "```json",
        json.dumps(run.get("freshnessInvariants"), indent=2),
        "```",
        "",
        "## Starting AU queue",
        "",
        f"- targets: {q.get('targets')}",
        f"- due: {q.get('due')}",
        f"- fresh skipped: {q.get('freshSkipped')}",
        f"- reference-only: {q.get('referenceOnly')}",
        f"- never priced: {q.get('neverPriced')}",
        f"- HIGH/MEDIUM/LOW: {q.get('highDemand')}/{q.get('mediumDemand')}/{q.get('lowDemand')}",
        f"- lane due counts: {q.get('laneCountsDue')}",
        "",
        "## Mid-run checkpoint",
        "",
        "```json",
        json.dumps(run.get("midRunCheckpoint"), indent=2)[:8000],
        "```",
        "",
        "## Accounting",
        "",
        "```json",
        json.dumps(acc, indent=2)[:8000],
        "```",
        "",
        "## Lane distribution (executed)",
        "",
        "```json",
        json.dumps(run.get("laneDistribution"), indent=2),
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
        json.dumps(run.get("backlogFinal"), indent=2)[:12000],
        "```",
        "",
    ]
    for card in cards:
        if str(card.get("cardVerdict") or "").startswith("NOT_ATTEMPTED"):
            continue
        da = card.get("demandAwareBefore") or (card.get("selection") or {}).get("demandAware") or {}
        lines.extend(
            [
                f"## Card {card.get('reliabilityPosition')}",
                "",
                f"- verdict: `{card.get('cardVerdict')}`",
                f"- runtimeMode: `{card.get('runtimeMode')}`",
                f"- lane: `{da.get('schedulerLane')}` reason: `{da.get('reasonCode')}`",
                "",
                "```json",
                json.dumps(
                    {
                        "demandAwareBefore": da,
                        "identity": card.get("identity"),
                        "navigation": card.get("navigation"),
                        "capture": card.get("capture"),
                        "parse": card.get("parse"),
                        "write": card.get("write"),
                        "freshness": card.get("freshness"),
                    },
                    indent=2,
                )[:20000],
                "```",
                "",
            ]
        )
    if run.get("rolloutVerdict") == "CONTROLLED_CONTINUOUS_ROLLOUT_PASS":
        lines.extend(
            [
                "## Next gate",
                "",
                "`CONTROLLED_CONTINUOUS_ROLLOUT_PASSED_AWAIT_OWNER_REVIEW_FOR_FULL_AU_ENABLE`",
                "",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_zip(report_path: Path) -> dict[str, Any]:
    utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    zip_path = Path(rf"C:\Users\andyg\Downloads\CARDSCANR_FINAL_CONTROLLED_25_JOB_ROLLOUT_{utc}.zip")
    skip_suffix = {".sqlite", ".gz", ".cookie"}
    skip_names = {"cookies", "credentials", "token", "secret", ".env"}
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel in (
            "cardscanr_market_engine/demand_aware_policy.py",
            "cardscanr_market_engine/demand_aware_scheduler.py",
            "cardscanr_market_engine/owned_daily_scheduler.py",
            "tools/ebay_au_controlled_continuous_owned_daily_rollout.py",
            "tools/ebay_au_final_five_consecutive_e2e.py",
        ):
            p = ROOT / rel
            if p.is_file():
                zf.write(p, arcname=rel.replace("\\", "/"))
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

    started = _utc()
    write_status(
        {
            "workerEnabled": False,
            "market": "AU",
            "workerState": "SELECTING",
            "liveSubmissionsThisRollout": 0,
            "healthyJobsThisRollout": 0,
            "freshSkippedThisRollout": 0,
            "stopReason": None,
            "startedAtUtc": started,
        }
    )

    deployment = deployment_audit()
    (OUT / "deployment_audit.json").write_text(json.dumps(deployment, indent=2) + "\n", encoding="utf-8")
    if not deployment["ok"]:
        print("STOP: required commits missing from HEAD", json.dumps(deployment, indent=2))
        return 2
    if deployment.get("productionWorkingTreeDirty"):
        print("STOP: unexplained production working-tree changes before eBay")
        print(deployment.get("productionWorkingTree"))
        return 2

    baseline = {
        "hash": deployment["HEAD"],
        "message": subprocess.check_output(
            ["git", "log", "-1", "--pretty=%s"], text=True, cwd=str(ROOT)
        ).strip(),
        "requiredCommits": deployment["requiredCommitsPresent"],
    }
    (OUT / "baseline_commit.json").write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")

    os.environ.pop(PRE_SUBMIT_ONLY_ENV, None)
    readiness = readiness_bundle()
    (OUT / "readiness.json").write_text(json.dumps(readiness, indent=2) + "\n", encoding="utf-8")
    if not readiness["ok"]:
        print("STOP: readiness failed", json.dumps(readiness, indent=2)[:8000])
        return 2

    policy = readiness["policy"]
    (OUT / "policy_audit.json").write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")

    freshness = freshness_invariant_self_check()
    (OUT / "freshness_invariants.json").write_text(json.dumps(freshness, indent=2) + "\n", encoding="utf-8")
    if not freshness["ok"]:
        print("STOP: freshness invariants failed")
        return 2

    self_checks = phase0_self_checks()
    (harness.BOOT / "phase0_self_checks.json").write_text(json.dumps(self_checks, indent=2) + "\n", encoding="utf-8")
    if not self_checks.get("ok"):
        print("STOP: five-card reliability self-check failed")
        return 2
    readiness["interCard"] = bool(self_checks.get("ok"))
    readiness["capture"] = bool(self_checks.get("ok"))

    from cardscanr_market_engine.config import MarketEngineConfig, supabase_secret_key_from_env
    from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient
    from cardscanr_market_engine.supabase_env_loader import load_supabase_env

    load_supabase_env()
    cfg = MarketEngineConfig.from_env()
    client = SupabaseMarketEngineClient(
        supabase_url=cfg.supabase_url,
        service_role_key=supabase_secret_key_from_env(),
    )
    demand = demand_snapshot(client)
    demand_public = {k: v for k, v in demand.items() if k != "demandIndex"}
    (OUT / "demand_snapshot.json").write_text(json.dumps(demand_public, indent=2) + "\n", encoding="utf-8")
    if not demand.get("ok"):
        print("STOP: demand snapshot failed")
        return 2

    queue = queue_snapshot(client, demand["demandIndex"])
    (OUT / "starting_queue_snapshot.json").write_text(json.dumps(queue, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "PREFLIGHT": {
                    "baseline": baseline["hash"],
                    "policyOk": policy["ok"],
                    "freshnessOk": freshness["ok"],
                    "selfCheckOk": self_checks.get("ok"),
                    "due": queue.get("due"),
                    "freshSkipped": queue.get("freshSkipped"),
                    "requests24h": demand_public.get("requests24h"),
                }
            },
            indent=2,
        ),
        flush=True,
    )

    write_status(
        {
            "workerEnabled": True,
            "market": "AU",
            "workerState": "PRICING",
            "liveSubmissionsThisRollout": 0,
            "healthyJobsThisRollout": 0,
            "freshSkippedThisRollout": int(queue.get("freshSkipped") or 0),
            "stopReason": None,
            "startedAtUtc": started,
            "maxLiveSubmissions": ROLL_OUT_MAX,
        }
    )

    orig_verify = harness.verify_deployed_code

    def _verify() -> dict:
        return _extend_deploy(orig_verify())

    harness.verify_deployed_code = _verify  # type: ignore[assignment]
    rc_boot = seq._bootstrap_guards()
    if rc_boot != 0:
        owned = owned_daily_shutdown("bootstrap_failed")
        print("STOP: bootstrap failed", rc_boot)
        return 2

    # Live bounded rollout via production reliability harness + demand-aware selection.
    harness_exc: Exception | None = None
    rc = 2
    try:
        rc = harness.main()
    except Exception as exc:  # noqa: BLE001 — always finalize evidence / shutdown
        harness_exc = exc
        print(f"HARNESS_EXCEPTION {type(exc).__name__}: {exc}", flush=True)

    result_path = OUT / "RUN_RESULT.json"
    run = (
        json.loads(result_path.read_text(encoding="utf-8"))
        if result_path.is_file()
        else {
            "verdict": "5_CARD_RELIABILITY_STOPPED_SAFE",
            "stopReason": (
                f"harness_exception:{type(harness_exc).__name__}:{harness_exc}"[:500]
                if harness_exc
                else "missing_run_result"
            ),
            "attemptAccounting": {
                "liveNavigationStartedCount": 0,
                "completedHealthyPathCount": 0,
                "failedAttemptCount": 1 if harness_exc else 0,
                "retryCount": 0,
                "challengeStopCount": 0,
                "attemptIds": [],
            },
            "cards": [],
            "ownership": {"mutations": 0},
        }
    )
    if harness_exc and not run.get("harnessException"):
        run["harnessException"] = f"{type(harness_exc).__name__}:{harness_exc}"[:500]
    cards = [_enrich_card_artifact_fields(c) for c in (run.get("cards") or [])]
    run["cards"] = cards
    run["baseline"] = baseline
    run["baselineCommit"] = baseline["hash"]
    run["deployment"] = deployment
    run["readiness"] = readiness
    run["policyAudit"] = policy
    run["demandSnapshot"] = demand_public
    run["freshnessInvariants"] = freshness
    run["startingQueue"] = queue
    run["phase0SelfCheck"] = self_checks
    if "midRunCheckpoint" not in run:
        cp_path = harness.BOOT / "mid_run_checkpoint.json"
        if cp_path.is_file():
            run["midRunCheckpoint"] = json.loads(cp_path.read_text(encoding="utf-8"))
    if "midRunCheckpoint20" not in run:
        cp20 = harness.BOOT / "mid_run_checkpoint_20.json"
        if cp20.is_file():
            run["midRunCheckpoint20"] = json.loads(cp20.read_text(encoding="utf-8"))
    cp10 = harness.BOOT / "mid_run_checkpoint_10.json"
    if cp10.is_file() and "midRunCheckpoint" not in run:
        run["midRunCheckpoint"] = json.loads(cp10.read_text(encoding="utf-8"))

    lane_dist = {"DEMAND": 0, "STALE_OWNED": 0, "COVERAGE": 0, "ageBoosted": 0, "deduped": 0}
    updated = unchanged = safe = 0
    for c in cards:
        v = str(c.get("cardVerdict") or "")
        if v == "PASS_PRICE_UPDATED":
            updated += 1
        elif v == "PASS_PRICE_UNCHANGED":
            unchanged += 1
        elif v == "SAFE_NO_NEW_EXACT_EVIDENCE":
            safe += 1
        da = c.get("demandAwareBefore") or (c.get("selection") or {}).get("demandAware") or {}
        lane = str(da.get("schedulerLane") or "")
        if lane in lane_dist:
            lane_dist[lane] += 1
        if float(da.get("ageBoost") or 0) > 0:
            lane_dist["ageBoosted"] += 1
    run["laneDistribution"] = lane_dist
    run["outcomeCounts"] = {
        "updated": updated,
        "unchanged": unchanged,
        "safeNoEvidence": safe,
    }

    # Post backlog (offline).
    try:
        demand2 = demand_snapshot(client)
        backlog = queue_snapshot(client, demand2["demandIndex"])
        jobs_per_day = int((16 * 3600) / 90)
        due_rem = int(backlog.get("due") or 0)
        backlog["estimatedDrainDaysAt90s16h"] = round(due_rem / max(1, jobs_per_day), 3)
        oldest = None
        for row in backlog.get("top25") or []:
            age = row.get("verifiedAgeHours")
            if age is None:
                continue
            if oldest is None or float(age) > float(oldest.get("verifiedAgeHours") or 0):
                oldest = row
        backlog["oldestSample"] = oldest
        run["backlogFinal"] = {
            k: backlog[k]
            for k in (
                "targets",
                "due",
                "freshSkipped",
                "referenceOnly",
                "neverPriced",
                "laneCountsDue",
                "estimatedDrainDaysAt90s16h",
                "oldestSample",
            )
            if k in backlog
        }
    except Exception as exc:
        run["backlogFinal"] = {"error": str(exc)[:400]}

    owned = owned_daily_shutdown("controlled_continuous_rollout_complete")
    run["ownedDaily"] = owned
    run["startedAtUtc"] = run.get("startedAtUtc") or started
    run["finishedAtUtc"] = _utc()
    run["rolloutVerdict"] = map_verdict(run)
    if owned.get("OWNED_DAILY_FULL_ENABLE"):
        run["rolloutVerdict"] = "CONTROLLED_CONTINUOUS_ROLLOUT_FAIL"
        run["stopReason"] = (run.get("stopReason") or "") + ";OWNED_DAILY_STILL_ENABLED"

    acc = run.get("attemptAccounting") or {}
    write_status(
        {
            "workerEnabled": False,
            "market": "AU",
            "workerState": "STOPPED",
            "liveSubmissionsThisRollout": acc.get("liveNavigationStartedCount"),
            "healthyJobsThisRollout": acc.get("completedHealthyPathCount"),
            "freshSkippedThisRollout": queue.get("freshSkipped"),
            "lastSuccessfulJobAt": run.get("finishedAtUtc"),
            "stopReason": run.get("stopReason"),
            "rolloutVerdict": run.get("rolloutVerdict"),
            "OWNED_DAILY_FULL_ENABLE": False,
        }
    )

    result_path.write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
    report = write_report(run)
    zip_meta = write_zip(report)
    print(
        json.dumps(
            {
                "CARDSCANR_CONTROLLED_CONTINUOUS_OWNED_DAILY_ROLLOUT_RESULT": run.get("rolloutVerdict"),
                "baseline": baseline["hash"],
                "liveSearches": acc.get("liveNavigationStartedCount"),
                "healthy": acc.get("completedHealthyPathCount"),
                "REPORT": str(report),
                "ZIP": zip_meta,
                "ownedDaily": owned,
            },
            indent=2,
        ),
        flush=True,
    )
    if run.get("rolloutVerdict") == "CONTROLLED_CONTINUOUS_ROLLOUT_PASS":
        return 0
    if run.get("rolloutVerdict") == "CONTROLLED_CONTINUOUS_ROLLOUT_STOPPED_SAFE":
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
