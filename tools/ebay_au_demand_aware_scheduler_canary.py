#!/usr/bin/env python3
"""Finite AU demand-aware owned_daily canary. Max 10 SEARCH_SUBMISSION_STARTED.

Does not enable continuous owned_daily. Stops scheduler/worker after the run.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["OWNED_DAILY_FULL_ENABLE"] = "false"
os.environ["CARDSCANR_RELIABILITY_MAX"] = "10"
os.environ["CARDSCANR_RELIABILITY_SELECT_LIMIT"] = "40"
os.environ["CARDSCANR_CONTINUE_ON_FRESH_SKIP"] = "1"
os.environ["EBAY_BROWSER_MAX_QUERY_ATTEMPTS"] = "1"

import tools.ebay_au_final_five_consecutive_e2e as harness
import tools.ebay_au_final_five_sequential_production_proof as seq
from tools.ebay_au_five_card_inter_card_handoff_e2e import (
    _artifact_contract_self_check,
    _enrich_card_artifact_fields,
    _extend_deploy,
    _freshness_self_check,
    _handoff_source_checks,
    phase0_self_checks,
)
from cardscanr_market_engine.navigation_runtime_context import PRE_SUBMIT_ONLY_ENV

OUT = ROOT / "reports" / "artifacts" / "demand_aware_scheduler_canary"
TASK_ID = "CARDSCANR-DEMAND-AWARE-MARKET-FRESHNESS-AND-OWNED-DAILY-CANARY"


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
    harness.AUTHORISED_MAX = 10
    harness.CONTINUE_ON_FRESH_SKIP = True


def _owned_daily_runtime() -> dict:
    os.environ["OWNED_DAILY_FULL_ENABLE"] = "false"
    flag = os.getenv("OWNED_DAILY_FULL_ENABLE", "")
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
        if "owned_daily" in low or "market_price_scheduler" in low:
            procs.append(line.strip()[:300])
    stop_path = ROOT / "reports" / "runtime" / "scheduler_stop_intent.json"
    stop_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "component": "scheduler",
        "reason": "demand_aware_finite_canary_complete",
        "requestedAtUtc": _utc(),
        "OWNED_DAILY_FULL_ENABLE": False,
    }
    stop_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return {
        "OWNED_DAILY_FULL_ENABLE": flag.lower() in {"1", "true", "yes"},
        "flagRaw": flag,
        "matchingProcesses": procs,
        "schedulerRunning": any("scheduler" in p.lower() for p in procs),
        "workerRunning": any("worker" in p.lower() or "job_runner" in p.lower() for p in procs),
        "stopIntent": str(stop_path),
    }


def map_verdict(run: dict) -> str:
    live = int((run.get("attemptAccounting") or {}).get("liveNavigationStartedCount") or 0)
    healthy = int((run.get("attemptAccounting") or {}).get("completedHealthyPathCount") or 0)
    mutations = int((run.get("ownership") or {}).get("mutations") or 0)
    inner = str(run.get("verdict") or "")
    stop = str(run.get("stopReason") or "")
    if mutations != 0 or live > 10:
        return "DEMAND_AWARE_SCHEDULER_AND_CANARY_FAIL"
    if inner.endswith("_FAIL") or "OWNERSHIP" in stop:
        return "DEMAND_AWARE_SCHEDULER_AND_CANARY_FAIL"
    if any(
        tok in stop
        for tok in (
            "CHALLENGE",
            "CAPTCHA",
            "SORRY",
            "403",
            "NAVIGATION",
            "CAPTURE",
            "PARSE",
            "WRITE",
            "ORPHAN",
            "RETRY",
            "DUPLICATE",
            "CONTRADICTION",
        )
    ):
        return "DEMAND_AWARE_SCHEDULER_AND_CANARY_FAIL"
    if live == 10 and healthy == 10 and inner.endswith("_PASS"):
        return "DEMAND_AWARE_SCHEDULER_AND_CANARY_PASS"
    if live == 10 and healthy == 10:
        return "DEMAND_AWARE_SCHEDULER_AND_CANARY_PASS"
    return "DEMAND_AWARE_SCHEDULER_AND_CANARY_STOPPED_SAFE"


def write_report(run: dict) -> Path:
    path = OUT / "DEMAND_AWARE_SCHEDULER_CANARY_REPORT.md"
    lines = [
        "# CARDSCANR_DEMAND_AWARE_SCHEDULER_CANARY_RESULT",
        "",
        f"**Gate:** `{TASK_ID}`",
        f"**Authoritative prior gate:** `FIVE_CARD_INTER_CARD_HANDOFF_GATE_PASSED_AWAIT_OWNER_REVIEW`",
        f"**Verdict:** `{run.get('canaryVerdict')}`",
        f"**Inner harness:** `{run.get('verdict')}`",
        f"**Started:** {run.get('startedAtUtc')}",
        f"**Finished:** {run.get('finishedAtUtc')}",
        f"**Stop reason:** {run.get('stopReason')}",
        "",
        "NOT_COMMITTED. Continuous owned_daily remains disabled.",
        "",
        "## Demand signal audit",
        "",
        "Used existing `market_price_refresh_jobs.requested_at` + user-origin `reason`",
        "(user_refresh / scanner / search / view / lookup). Engine `owned_daily:*` reasons excluded.",
        "No new product telemetry table. Lifetime owner_count is not the main priority signal.",
        "",
        "## Policy",
        "",
        "- AU/AUD first",
        "- verified-local freshness is market-specific",
        "- HIGH demand threshold 12h; otherwise 24h",
        "- FRESH_SKIP never hits eBay and does not consume SEARCH_SUBMISSION_STARTED",
        "- reference-only and never-priced remain due",
        "- lanes ~50/30/20 among DUE work only",
        "",
        "## Offline",
        "",
        "```json",
        json.dumps(run.get("offlineSimulation"), indent=2)[:12000],
        "```",
        "",
        "## Self-check",
        "",
        "```json",
        json.dumps(run.get("phase0SelfCheck"), indent=2)[:12000],
        "```",
        "",
        "## Scheduler selection (demand-aware)",
        "",
        "```json",
        json.dumps(run.get("schedulerSelection"), indent=2)[:20000],
        "```",
        "",
        "## Attempt accounting",
        "",
        "```json",
        json.dumps(run.get("attemptAccounting"), indent=2)[:8000],
        "```",
        "",
        "## Owned daily final",
        "",
        "```json",
        json.dumps(run.get("ownedDaily"), indent=2),
        "```",
        "",
    ]
    for card in run.get("cards") or []:
        lines.extend(
            [
                f"## Card {card.get('reliabilityPosition')}",
                "",
                f"- verdict: `{card.get('cardVerdict')}`",
                f"- runtimeMode: `{card.get('runtimeMode')}`",
                "",
                "```json",
                json.dumps(
                    {
                        "demandAwareBefore": card.get("demandAwareBefore") or (card.get("selection") or {}).get("demandAware"),
                        "identity": card.get("identity"),
                        "runtimeMode": card.get("runtimeMode"),
                        "sourceAwareEligibility": card.get("sourceAwareEligibility"),
                        "navigation": card.get("navigation"),
                        "challenge": card.get("challenge"),
                        "capture": card.get("capture"),
                        "parse": card.get("parse"),
                        "write": card.get("write"),
                        "freshness": card.get("freshness"),
                        "jobError": (card.get("job") or {}).get("error"),
                    },
                    indent=2,
                )[:24000],
                "```",
                "",
            ]
        )
    if run.get("canaryVerdict") == "DEMAND_AWARE_SCHEDULER_AND_CANARY_PASS":
        lines.extend(
            [
                "## Next gate",
                "",
                "`DEMAND_AWARE_CANARY_PASSED_AWAIT_OWNER_REVIEW_FOR_CONTINUOUS_ROLLOUT`",
                "",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_zip(report_path: Path) -> dict:
    utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    zip_path = Path(rf"C:\Users\andyg\Downloads\CARDSCANR_DEMAND_AWARE_SCHEDULER_CANARY_{utc}.zip")
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
    extra = {
        "artifactContract": _artifact_contract_self_check(),
        "freshness": _freshness_self_check(),
        "handoff": _handoff_source_checks(),
    }
    (harness.BOOT / "phase0_demand_aware_self_checks.json").write_text(
        json.dumps({"phase0": self_checks, "extra": extra}, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"PHASE0_SELF_CHECK": {"ok": self_checks.get("ok")}}, indent=2), flush=True)
    if not self_checks.get("ok"):
        print("STOP: five-card reliability self-check failed; no live canary", flush=True)
        return 2
    os.environ.pop(PRE_SUBMIT_ONLY_ENV, None)

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
    sim_path = OUT / "offline_simulation.json"
    run["offlineSimulation"] = json.loads(sim_path.read_text(encoding="utf-8")) if sim_path.is_file() else {}
    run["phase0SelfCheck"] = self_checks
    run["canaryPolicy"] = {
        "market": "AU/AUD",
        "concurrency": 1,
        "maxLivePricingJobs": 10,
        "maxSearchSubmissionStarted": 10,
        "retries": 0,
        "parallelPricing": 0,
        "card1": "COLD_START",
        "cards2to10": "INTER_CARD",
        "freshSkipConsumesSearch": False,
    }
    cards = [_enrich_card_artifact_fields(card) for card in (run.get("cards") or [])]
    run["cards"] = cards
    run["ownedDaily"] = _owned_daily_runtime()
    run["canaryVerdict"] = map_verdict(run)
    if run["ownedDaily"].get("OWNED_DAILY_FULL_ENABLE"):
        run["canaryVerdict"] = "DEMAND_AWARE_SCHEDULER_AND_CANARY_FAIL"
        run["stopReason"] = (run.get("stopReason") or "") + ";OWNED_DAILY_STILL_ENABLED"
    result_path.write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
    report = write_report(run)
    zip_meta = write_zip(report)
    print(
        json.dumps(
            {
                "CARDSCANR_DEMAND_AWARE_SCHEDULER_CANARY_RESULT": run.get("canaryVerdict"),
                "inner": run.get("verdict"),
                "liveSearches": (run.get("attemptAccounting") or {}).get("liveNavigationStartedCount"),
                "REPORT": str(report),
                "ZIP": zip_meta,
                "ownedDaily": run.get("ownedDaily"),
            },
            indent=2,
        ),
        flush=True,
    )
    if run.get("canaryVerdict") == "DEMAND_AWARE_SCHEDULER_AND_CANARY_PASS":
        return 0
    if run.get("canaryVerdict") == "DEMAND_AWARE_SCHEDULER_AND_CANARY_STOPPED_SAFE":
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
