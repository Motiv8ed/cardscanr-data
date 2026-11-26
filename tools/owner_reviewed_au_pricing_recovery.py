#!/usr/bin/env python3
"""One-time, owner-reviewed CardScanR AU eBay challenge recovery.

Never bypasses eBay verification. Stops the single worker and its scheduled
launcher BEFORE unlocking the incident; permits exactly one normal guarded
worker --once probe, then separately authorises ordinary paced processing.
Use only on the owner's pinned Windows checkout after actual page review.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.atomic_json_state import locked_json_state, read_json_object
from cardscanr_market_engine.control_plane_incidents import clear_marketplace_cooldown_if_matches
from cardscanr_market_engine.ebay_availability import _mutate_availability, load_availability
from cardscanr_market_engine.marketplace_ops_state import load_ops_state, parse_utc, utc_iso
from cardscanr_market_engine.control_plane_incidents import list_active_incidents

INCIDENT = "cpi_653e9a725dad42e1"
RECORDED = "2026-11-22T04:14:04.146002Z"
UNTIL = "2026-11-22T16:14:04.146002Z"
EXPECTED_BRANCH = "runtime/reconcile-pr40-20261115"
EXPECTED_HEAD = "c4f0de2d2d0a278331cbd8c32c093c0fafb490c5"
PR106_SHA = "8b81e97eed77a2e74432526ca1aee8f18e11847cb1e7df5ab9f8ad9af2a18cc7"
EVIDENCE_SHA = "b3920384b35d1cbecdeedb8d6fa35952483b0ced208fbfb2845d57434cfdf65d"
OPS_SHA = "e5aed0f193d13981e92c6987957246dda74f6d873621479c73c07d7b6be54b60"
TASK = "CardScanR-LiveEbayPricingRuntime"
APPROVAL = "OWNER_REVIEWED_NORMAL_EBAY_PAGE_SINGLE_PROBE"
BACKUPS = ROOT.parent / "CardScanR_Pricing_OwnerReview_Backups"
INC_FILE = ROOT / "reports/runtime/control_plane_incidents.json"
OPS_FILE = ROOT / "reports/runtime/marketplace_ops_state.json"
AV_FILE = ROOT / "reports/runtime/ebay_availability_state.json"
PID_FILE = ROOT / "reports/runtime/live_ebay_worker.pid.json"
RECEIPT_FILE = BACKUPS / "au_owner_review_recovery_receipt.json"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fail(reason: str) -> None:
    raise RuntimeError(reason)


def command(argv: list[str], timeout: int = 15) -> str:
    ret = subprocess.run(argv, cwd=ROOT, shell=False, capture_output=True,
                         text=True, errors="replace", timeout=timeout)
    if ret.returncode:
        fail("fixed_command_failed:" + str(ret.returncode))
    return ret.stdout.strip()


def ps(script: str, timeout: int = 15) -> str:
    return command(["powershell.exe", "-NoProfile", "-NonInteractive",
                    "-Command", script], timeout=timeout)


def source_preflight() -> dict:
    if os.name != "nt":
        fail("windows_host_required")
    if os.path.normcase(str(ROOT)) != os.path.normcase(
            r"D:\CardScanR_Data\cardscanr-data"):
        fail("unexpected_checkout")
    if command(["git", "branch", "--show-current"]) != EXPECTED_BRANCH:
        fail("runtime_branch_changed")
    if command(["git", "rev-parse", "HEAD"]) != EXPECTED_HEAD:
        fail("runtime_head_changed")
    if sha(ROOT / "workers/market_price_worker.py") != PR106_SHA:
        fail("pricing_worker_source_not_pr106")
    if sha(ROOT / "cardscanr_market_engine/challenge_incident_evidence.py") != EVIDENCE_SHA:
        fail("missing_evidence_writer")
    if sha(ROOT / "cardscanr_market_engine/marketplace_ops_state.py") != OPS_SHA:
        fail("marketplace_module_modified")
    flag = ROOT / "reports/runtime/owned_daily_full_enable.flag"
    if flag.is_symlink() or (flag.exists() and flag.read_text().strip().upper() not in
                              {"OFF", "FALSE", "0"}):
        fail("owned_daily_not_off")
    return {"branch": EXPECTED_BRANCH, "head": EXPECTED_HEAD, "owned_daily": "OFF"}


def state_preflight() -> dict:
    for path in (INC_FILE, OPS_FILE, AV_FILE, PID_FILE):
        if (not path.is_file() or path.is_symlink()
                or getattr(path, "is_junction", lambda: False)()
                or path.stat().st_size > 2_000_000):
            fail("missing_or_unsafe_state_" + path.name)
    ledger = read_json_object(INC_FILE)
    incidents = ledger.get("incidents")
    if not isinstance(incidents, dict):
        fail("invalid_incident_ledger")
    row = incidents.get(INCIDENT)
    if (not isinstance(row, dict) or row.get("status") != "active"
            or row.get("incidentType") != "CHALLENGE"
            or row.get("classification") != "CHALLENGE_REQUIRED"
            or row.get("market") != "AU"
            or parse_utc(row.get("recordedAt")) != parse_utc(RECORDED)):
        fail("pinned_incident_mismatch")
    active = [a for a in list_active_incidents(market="AU", path=INC_FILE)
              if a.get("incidentType") in ("CHALLENGE", "AUTH_REQUIRED")]
    if len(active) != 1 or active[0].get("incidentId") != INCIDENT:
        fail("new_or_unrelated_active_challenge_present")
    ops = load_ops_state(path=OPS_FILE).get("markets")
    cooldown = ops.get("AU") if isinstance(ops, dict) else None
    if (not isinstance(cooldown, dict)
            or cooldown.get("incidentId") != INCIDENT
            or cooldown.get("reason") != "CHALLENGE_REQUIRED"
            or parse_utc(cooldown.get("recordedAt")) != parse_utc(RECORDED)
            or parse_utc(cooldown.get("until")) != parse_utc(UNTIL)):
        fail("cooldown_provenance_mismatch")
    if datetime.now(timezone.utc) < parse_utc(UNTIL):
        fail("challenge_cooldown_not_expired")
    avail = load_availability(path=AV_FILE, market="AU")
    if (avail.state != "CHALLENGE_REQUIRED" or avail.last_challenge_incident_id != INCIDENT
            or avail.probe_in_flight):
        fail("availability_reference_mismatch")
    return {"incident": INCIDENT, "cooldown_expired": True,
            "state_hashes": {p.name: sha(p) for p in (INC_FILE, OPS_FILE, AV_FILE)}}


def existing_ebay_page() -> bool:
    opener = build_opener(ProxyHandler({}))
    with opener.open(Request("http://127.0.0.1:9444/json/list",
                             headers={"Accept": "application/json"}), timeout=3) as res:
        if res.status != 200:
            return False
        data = res.read(65537)
    if len(data) > 65536:
        return False
    targets = json.loads(data)
    if not isinstance(targets, list) or len(targets) > 50:
        return False
    pages = [t for t in targets if isinstance(t, dict) and t.get("type") == "page"]
    if len(pages) != 1:
        return False
    target = pages[0]
    url = target.get("url")
    if not isinstance(url, str):
        return False
    parts = urlsplit(url)
    title = str(target.get("title") or "").lower()
    return (parts.scheme == "https"
            and (parts.hostname or "").lower() in ("ebay.com.au", "www.ebay.com.au")
            and (parts.path.lower().startswith("/sch/") or parts.path.lower().startswith("/itm/"))
            and not any(x in title for x in ("captcha", "verify yourself", "security measure",
                                              "pardon our interruption", "sign in")))


def scheduler() -> dict:
    info = json.loads(ps(
        "$t=Get-ScheduledTask -TaskName 'CardScanR-LiveEbayPricingRuntime' -ErrorAction Stop;"
        "$a=@($t.Actions);"
        "[pscustomobject]@{state=[string]$t.State;count=$a.Count;"
        "exe=[string]$a[0].Execute;arguments=[string]$a[0].Arguments;"
        "workdir=[string]$a[0].WorkingDirectory}|ConvertTo-Json -Compress"))
    if (info.get("count") != 1
            or not str(info.get("exe") or "").lower().endswith("wscript.exe")
            or hashlib.sha256(str(info.get("arguments") or "").encode("utf-8")).hexdigest()
                != "39c6fc71d93915abf358e2967fd2ae99b78e9deeebb2936d4d789b635e727b85"
            or os.path.normcase(str(info.get("workdir") or "").rstrip("\\"))
                != os.path.normcase(str(ROOT))):
        fail("unexpected_scheduled_task_action")
    # The owner-approved #45 shadow wrapper is pinned by *bytes*, not just
    # filename: never run another launcher with similar-looking arguments.
    launcher = Path(r"D:\CardScanR_worktrees\data_deploy_pr45_20261117_logs")
    for name, expected in (
        ("ensure_live_ebay_pricing_runtime_shadow45.vbs",
         "4ca04866385ba80efdf45b2cabb5474a90e268bb8434af2485b520d48cd57621"),
        ("ensure_live_ebay_pricing_runtime_shadow45.ps1",
         "738040de9e4c1e43223658bc775b3b6a07d9c3956a6cacd9c23e15486fda8f49"),
        ("apply_ebay_scale_shadow_flags.ps1",
         "4904b01e6b1df708549bd7dd6eb06d03cbc77998c8298b52f65c03133c143ae0"),
    ):
        file = launcher / name
        if not file.is_file() or file.is_symlink() or sha(file) != expected:
            fail("pinned_scheduled_launcher_source_changed")
    return info


def workers() -> list[int]:
    rows = json.loads(ps(
        "$p=@(Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" "
        "| Where-Object {$_.CommandLine -match 'workers[/\\\\]market_price_worker[.]py'}"
        "| ForEach-Object {[pscustomobject]@{pid=[int]$_.ProcessId;"
        "command=[string]$_.CommandLine}});"
        "ConvertTo-Json -InputObject $p -Compress -Depth 3"))
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        fail("bad_worker_process_inventory")
    pids = []
    for row in rows:
        if isinstance(row, dict) and row.get("pid") and (
                "workers/market_price_worker.py" in str(row.get("command") or "")
                or r"workers\market_price_worker.py" in str(row.get("command") or "")):
            pids.append(int(row["pid"]))
    return sorted(set(pids))


def worker_preflight() -> int:
    pids = workers()
    recorded = read_json_object(PID_FILE).get("pid")
    if len(pids) != 1 or type(recorded) is not int or recorded != pids[0]:
        fail("worker_pid_or_record_mismatch")
    return pids[0]


def backup_states() -> str:
    if BACKUPS.is_symlink() or getattr(BACKUPS, "is_junction", lambda: False)():
        fail("unsafe_recovery_backup_root")
    BACKUPS.mkdir(parents=True, exist_ok=True)
    run = BACKUPS / ("recovery_" + uuid.uuid4().hex[:12])
    run.mkdir(mode=0o700)
    for path in (INC_FILE, OPS_FILE, AV_FILE, PID_FILE):
        out = run / path.name
        out.write_bytes(path.read_bytes())
        if sha(out) != sha(path):
            fail("state_backup_hash_mismatch")
    xml = ps("Export-ScheduledTask -TaskName 'CardScanR-LiveEbayPricingRuntime'")
    (run / "scheduled_task_original.xml").write_text(xml, encoding="utf-8")
    return str(run)


def pause_runtime(pid: int) -> None:
    if scheduler().get("state") != "Ready":
        fail("scheduled_task_not_ready")
    ps("Disable-ScheduledTask -TaskName 'CardScanR-LiveEbayPricingRuntime'"
       " -ErrorAction Stop | Out-Null")
    if scheduler().get("state") != "Disabled":
        fail("task_disable_not_verified")
    if worker_preflight() != pid:
        fail("worker_changed_before_stop")
    ps("Stop-Process -Id " + str(pid) + " -ErrorAction Stop")
    for _ in range(18):
        if pid not in workers():
            return
        time.sleep(.5)
    fail("worker_stop_not_verified_task_left_disabled")


def resolve_incident() -> None:
    # All writers are Windows-host and use the same canonical FileLock.
    with locked_json_state(INC_FILE, default={"version": 1, "incidents": {}}) as doc:
        incidents = dict(doc.get("incidents") or {})
        row = incidents.get(INCIDENT)
        if not isinstance(row, dict) or row.get("status") != "active":
            fail("incident_changed_during_reconciliation")
        other = [a for a in incidents.values()
                 if isinstance(a, dict) and a.get("market") == "AU"
                 and a.get("status") == "active"
                 and a.get("incidentId") != INCIDENT
                 and a.get("incidentType") in ("CHALLENGE", "AUTH_REQUIRED")]
        if other:
            fail("new_challenge_found_under_lock")
        entry = dict(row)
        entry["status"] = "resolved"
        entry["resolvedAt"] = utc_iso()
        entry["resolutionReason"] = "OWNER_REVIEWED_PAGE_NORMAL_AFTER_COOLDOWN"
        evidence = dict(entry.get("evidence") or {})
        history = list(evidence.get("reconciliationHistory") or [])
        history.append({"atUtc": utc_iso(),
                        "action": "owner_reviewed_resolution",
                        "source": "owner_submitted_normal_ebay_sold_page_screenshot",
                        "note": "Past challenge retained as historical observation; new challenge still blocks."})
        evidence["reconciliationHistory"] = history
        entry["evidence"] = evidence
        incidents[INCIDENT] = entry
        doc["incidents"] = incidents
        doc["updatedAtUtc"] = utc_iso()
        doc["revision"] = int(doc.get("revision") or 0) + 1

    cleared = clear_marketplace_cooldown_if_matches(
        "AU", expected_reason="CHALLENGE_REQUIRED",
        expected_recorded_at=RECORDED, expected_until=UNTIL,
        expected_incident_id=INCIDENT, path=OPS_FILE)
    if not cleared.get("cleared"):
        fail("cooldown_cas_mismatch_after_incident_resolution")

    def update(snap):
        if snap.market != "AU" or snap.state != "CHALLENGE_REQUIRED" or (
                snap.last_challenge_incident_id != INCIDENT):
            fail("new_or_changed_availability_challenge_under_lock")
        snap.state = "PROBE_REQUIRED"
        snap.probe_in_flight = False
        snap.next_probe_at = datetime.now(timezone.utc)
        snap.confirmed_healthy = False
        snap.recovery_health_count = 0
        snap.last_outcome = "owner_reviewed_challenge_resolved_probe_required"
        snap.last_challenge_incident_id = None
        return snap
    updated = _mutate_availability(update, path=AV_FILE, market="AU")
    if updated.state != "PROBE_REQUIRED":
        fail("probe_gate_not_restored")
    if list_active_incidents(market="AU", path=INC_FILE):
        fail("new_active_incident_after_reconciliation")


def _atomic_receipt(state: dict) -> None:
    BACKUPS.mkdir(parents=True, exist_ok=True)
    tmp = BACKUPS / ("." + uuid.uuid4().hex + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, RECEIPT_FILE)


def _read_receipt() -> dict:
    if not RECEIPT_FILE.is_file():
        return {"status": "NOT_STARTED"}
    return read_json_object(RECEIPT_FILE)


def start_one_probe() -> dict:
    # Fixed, owner-approved existing worker. Not a Chrome bypass or retry loop.
    python = ROOT / ".venv/Scripts/python.exe"
    if not python.is_file():
        fail("pinned_venv_python_missing")
    if workers():
        fail("another_market_price_worker_running")
    pdir = BACKUPS / ("probe_" + uuid.uuid4().hex[:12])
    pdir.mkdir(mode=0o700)
    exit_path = pdir / "exit_code.txt"
    # All command paths are derived solely from the fixed pinned checkout.
    script = (
        "$ErrorActionPreference='Stop';"
        "$repo='" + str(ROOT) + "';Set-Location $repo;"
        ". ($repo+'\\scripts\\live_ebay_worker_config.ps1');"
        ". ($repo+'\\scripts\\load_supabase_env.ps1');"
        "$profile=Assert-SafeLiveEbayProfilePath -ProfilePath ($repo+'\\.browser_profiles\\cardscanr');"
        "Set-LiveEbayWorkerEnvironment -ProfilePath $profile -Headless $false;"
        "$env:OWNED_DAILY_FULL_ENABLE='false';"
        "$env:MARKET_WORKER_ALLOWED_MARKETS='AU';"
        "$env:MARKET_WORKER_CONCURRENCY='1';"
        "$env:MARKET_WORKER_MAX_JOBS_PER_RUN='1';"
        "$env:EBAY_BROWSER_MAX_CONCURRENCY='1';"
        "$env:EBAY_BROWSER_MAX_QUERY_ATTEMPTS='1';"
        "$env:EBAY_BROWSER_NAV_MODE='linux_x11';"
        "$env:EBAY_BROWSER_CDP_PORT='9444';"
        "$env:EBAY_BROWSER_REUSE_CONTEXT='true';"
        "$env:CARDSCANR_OPERATION_MODE='PROBE';"
        "$env:CARDSCANR_STATE_WRITER_DOMAIN='windows_host';"
        "& '" + str(python) + "' -u 'workers/market_price_worker.py'"
        " --once --max-jobs 1 --poll-seconds 10;"
        "$rc=$LASTEXITCODE;"
        "[IO.File]::WriteAllText('" + str(exit_path) + "', [string]$rc)"
    )
    with (pdir / "stdout.log").open("wb") as out, (pdir / "stderr.log").open("wb") as err:
        proc = subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive",
                                 "-Command", script], cwd=ROOT, shell=False,
                                stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    rec = _read_receipt()
    rec.update({"status": "PROBE_STARTED", "probePid": proc.pid,
                "probeDir": str(pdir), "probeStartedUtc": utc_iso(),
                "scheduledTask": "Disabled", "ownedDaily": "OFF"})
    _atomic_receipt(rec)
    return {"status": "PROBE_STARTED", "probe_pid": proc.pid,
            "task": "Disabled", "max_jobs": 1, "owner_reviewed": True}


def inspect() -> dict:
    source_preflight()
    state = state_preflight()
    if not existing_ebay_page():
        fail("no_existing_normal_ebay_au_page")
    task = scheduler()
    pid = worker_preflight()
    if task.get("state") != "Ready":
        fail("scheduler_task_not_ready")
    return {"status": "READY_FOR_OWNER_REVIEWED_SINGLE_PROBE", **state,
            "worker_pid": pid, "task_state": task["state"],
            "browser": "existing_normal_ebay_au_sold_or_item_page",
            "owner_approval_required": True, "owned_daily": "OFF"}


def apply() -> dict:
    ready = inspect()
    if RECEIPT_FILE.exists():
        fail("recovery_receipt_already_exists_stop_before_duplicate")
    backup_dir = backup_states()
    rec = {"status": "BACKED_UP", "incidentId": INCIDENT,
           "originalClassification": "CHALLENGE_REQUIRED", "backupDir": backup_dir,
           "ownerReview": "Screenshot showed normal eBay sold listings, owner authorised recovery",
           "startedUtc": utc_iso(), "workerPidBefore": ready["worker_pid"]}
    _atomic_receipt(rec)
    pause_runtime(ready["worker_pid"])
    rec.update({"status": "WORKER_STOPPED_TASK_DISABLED", "stoppedUtc": utc_iso()})
    _atomic_receipt(rec)
    # Worker and scheduled launcher cannot race the three-file state transition.
    state_preflight()
    if not existing_ebay_page():
        fail("ebay_page_changed_before_reconciliation")
    resolve_incident()
    rec.update({"status": "PROBE_REQUIRED", "reconciledUtc": utc_iso()})
    _atomic_receipt(rec)
    return start_one_probe()


def probe_status() -> dict:
    rec = _read_receipt()
    if rec.get("status") not in {"PROBE_STARTED", "RESUMED"}:
        return {"status": rec.get("status", "NOT_STARTED")}
    dirname = Path(str(rec["probeDir"]))
    if not dirname.is_relative_to(BACKUPS) or dirname.is_symlink():
        fail("untrusted_probe_receipt_dir")
    exit_path = dirname / "exit_code.txt"
    raw_exit = exit_path.read_text(encoding="utf-8").strip() if exit_path.is_file() else None
    state = load_availability(path=AV_FILE, market="AU")
    return {"status": "PROBE_EXITED" if raw_exit is not None else "PROBE_RUNNING",
            "exit_code": int(raw_exit) if raw_exit in ("0", "1", "2") else None,
            "availability_state": state.state,
            "availability_outcome": state.last_outcome,
            "active_challenges": len(list_active_incidents(market="AU", path=INC_FILE)),
            "task": scheduler().get("state"),
            "owned_daily": "OFF", "probe_started_utc": rec.get("probeStartedUtc")}


def resume() -> dict:
    rec = _read_receipt()
    if rec.get("status") != "PROBE_STARTED":
        fail("no_completed_single_probe_to_resume")
    status = probe_status()
    if (status.get("status") != "PROBE_EXITED"
            or status.get("exit_code") != 0
            or status.get("availability_state") != "HEALTHY"
            or status.get("active_challenges") != 0):
        fail("single_probe_not_healthy_leave_task_paused")
    if scheduler().get("state") != "Disabled":
        fail("scheduled_task_not_disabled_during_recovery")
    if not existing_ebay_page():
        fail("ebay_challenge_visible_after_probe")
    source_preflight()
    if workers():
        fail("unexpected_other_pricing_worker")
    ps("Enable-ScheduledTask -TaskName 'CardScanR-LiveEbayPricingRuntime'"
       " -ErrorAction Stop | Out-Null")
    if scheduler().get("state") != "Ready":
        fail("task_enable_not_verified")
    # Trigger only the existing verified scheduled action (the #45 shadow
    # wrapper). Launching the official ensure script directly would drop its
    # approved runtime shadow settings.
    ps("Start-ScheduledTask -TaskName 'CardScanR-LiveEbayPricingRuntime'"
       " -ErrorAction Stop")
    for _ in range(15):
        if len(workers()) == 1:
            break
        time.sleep(1)
    if len(workers()) != 1:
        fail("scheduled_runtime_relaunch_not_verified")
    rec["status"] = "RESUMED"
    rec["resumedUtc"] = utc_iso()
    _atomic_receipt(rec)
    return {"status": "RESUMED", "task_state": "Ready",
            "worker_pids": workers(), "owned_daily": "OFF",
            "queue_processing_unverified": True}


def main() -> int:
    args = argparse.ArgumentParser()
    args.add_argument("--mode", choices=["inspect", "apply", "status", "resume", "native-probe"], required=True)
    args.add_argument("--approval", default="")
    args.add_argument("--review-receipt", type=Path)
    parsed = args.parse_args()
    try:
        if parsed.mode == "native-probe":
            from cardscanr_market_engine.native_au_pricing_recovery import run_native_recovery

            result = run_native_recovery(parsed.review_receipt, parsed.approval)
            print(json.dumps(result, separators=(",", ":")))
            return 0 if result["status"] == "PROBE_HEALTHY_STREAK_PENDING" else 2
        if parsed.mode == "inspect":
            result = inspect()
        elif parsed.mode == "apply":
            if parsed.approval != APPROVAL:
                fail("owner_approval_required")
            result = apply()
        elif parsed.mode == "status":
            result = probe_status()
        else:
            if parsed.approval != APPROVAL:
                fail("owner_approval_required")
            result = resume()
        print(json.dumps(result, separators=(",", ":")))
        return 0
    except Exception as exc:
        # Never expose a secret-loaded environment, stack, URLs or browser text.
        print(json.dumps({"status": "STOPPED_SAFELY", "mode": parsed.mode,
                          "reason_type": type(exc).__name__,
                          "reason": str(exc)[:140]}, separators=(",", ":")))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
