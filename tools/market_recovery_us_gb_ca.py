#!/usr/bin/env python3
"""AU recovery then serialized US→GB→CA live canaries with market cooldown resume.

Uses the single existing global browser (Xvfb :99 / CDP 9444). Does not start a
second Chrome session. JP/EU are never allowlisted.
"""
from __future__ import annotations

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

from cardscanr_market_engine.canary_control_plane import (
    MAX_PRESUBMIT_TRANSIENT_EPISODES_24H,
    canary_may_continue,
    classify_canary_failure,
    parse_operation_mode,
    record_canary_episode,
    set_operation_mode,
)
from cardscanr_market_engine.gaming_resource_pause import GamingResourcePauseController
from cardscanr_market_engine.continuous_safety import ContinuousSafetyBudget
from cardscanr_market_engine.ebay_availability import (
    get_availability,
    peek_availability,
    record_healthy_browser_check,
)
from cardscanr_market_engine.ebay_browser_work_gate import evaluate_ebay_browser_work_gate
from cardscanr_market_engine.live_navigation_attempt import (
    SEARCH_SUBMISSION_STARTED,
    attempts_dir,
    has_search_submission_started,
)
from cardscanr_market_engine.browser_lifecycle_policy import (
    RUNTIME_INTER_CARD,
    evaluate_runtime_targets,
    required_runtime_mode,
)
from cardscanr_market_engine.local_browser_runtime import ensure_xvfb, probe_local_browser_runtime, probe_pre_live_runtime
from cardscanr_market_engine.navigation_runtime_context import (
    DEFAULT_CONTEXT_PATH,
    RUNTIME_MODE_ENV,
    clear_context_from_environ,
    load_navigation_runtime_context,
    persist_inter_card_from_healthy_result,
)
from cardscanr_market_engine.owned_daily_enablement import (
    apply_continuous_multi_region_env,
    owned_daily_full_enable,
    write_owned_daily_flag,
)
from cardscanr_market_engine.region_pricing_registry import (
    load_continuous_market_overrides,
    region_definition,
    write_continuous_market_overrides,
)
from cardscanr_market_engine.region_pricing_status import multi_region_status


ART = ROOT / "reports" / "artifacts" / "market_recovery_us_gb_ca"
CURRENCY = {"AU": "AUD", "US": "USD", "GB": "GBP", "CA": "CAD"}
HEALTHY_OUTCOMES = {
    "UPDATED_FROM_EBAY",
    "UNCHANGED_FROM_EBAY",
    "CHECKED_NO_NEW_EXACT_EVIDENCE",
}


SKIP_OUTCOMES = {
    "already_fresh_noop",
    "ALREADY_FRESH_NOOP",
    "owned_daily_fresh_noop",
    "NO_PRICE_EVER_FOUND",
}


def next_card_index(market: str) -> int:
    index = 1
    while (ART / f"{market}_card_{index}.json").is_file():
        index += 1
    return index


def live_sold_tab_for_market(market: str) -> dict[str, Any] | None:
    """Return the live top-level Sold results tab for this market host, if present."""
    import urllib.request

    definition = region_definition(market)
    home = str(definition.homepage or "").lower()
    host = home.split("//", 1)[-1].split("/", 1)[0]
    try:
        tabs = json.loads(urllib.request.urlopen("http://127.0.0.1:9444/json/list", timeout=5).read().decode())
    except Exception:
        return None
    for t in tabs:
        if not isinstance(t, dict) or str(t.get("type") or "").lower() != "page":
            continue
        url = str(t.get("url") or "")
        low = url.lower()
        if host and host in low and "lh_sold=1" in low and "signin" not in low:
            return t
    return None


def rebound_result_to_live_sold(result: dict[str, Any], live: dict[str, Any]) -> dict[str, Any]:
    """Rewrite healthy-card prior identity onto the live Sold tab (same Chrome process)."""
    merged = dict(result or {})
    tid = str(live.get("id") or "").strip()
    url = str(live.get("url") or "").strip()
    if not tid:
        return merged
    capture = dict(merged.get("currentJobCapture") or {})
    capture["targetId"] = tid
    merged["currentJobCapture"] = capture
    desktop = dict(merged.get("desktopNav") or {})
    desktop["targetId"] = tid
    if url:
        desktop["url"] = url
        post = dict(desktop.get("postNavigation") or {})
        post["resultingUrl"] = url
        desktop["postNavigation"] = post
        merged["finalUrl"] = url
        merged["url"] = url
    merged["desktopNav"] = desktop
    return merged


def persist_inter_card_for_resume(market: str, result: dict[str, Any]) -> dict[str, Any]:
    """Persist INTER_CARD prior, rebounding onto the live Sold tab when available."""
    live = live_sold_tab_for_market(market)
    payload = rebound_result_to_live_sold(result, live) if live else dict(result or {})
    ctx = persist_inter_card_from_healthy_result(payload)
    return {
        "ok": ctx is not None,
        "runtimeMode": ctx.runtime_mode if ctx else None,
        "targetId": ctx.expected_prior.target_id if ctx and ctx.expected_prior else None,
        "liveSoldTargetId": (live or {}).get("id"),
        "reboundToLiveTab": bool(live and live.get("id")),
    }


def healthy_price_keys_from_files(market: str) -> set[str]:
    keys: set[str] = set()
    index = 1
    while True:
        path = ART / f"{market}_card_{index}.json"
        if not path.is_file():
            break
        try:
            cycle = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            index += 1
            continue
        if cycle.get("healthy"):
            price_key = str((cycle.get("result") or {}).get("priceKeyId") or "").strip()
            if price_key:
                keys.add(price_key)
        index += 1
    return keys


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def bind_env(market: str, *, mode: str) -> dict[str, str]:
    definition = region_definition(market)
    set_operation_mode(mode)  # type: ignore[arg-type]
    values = {
        "CARDSCANR_OPERATION_MODE": mode,
        "OWNED_DAILY_ALLOWED_MARKETS": market,
        "OWNED_DAILY_MAX_ENQUEUE": "1",
        "OWNED_DAILY_FULL_MAX_ENQUEUE": "1",
        "OWNED_DAILY_SYNC_KEYS": "false",
        "OWNED_DAILY_DRY_RUN": "false",
        "MARKET_WORKER_ALLOWED_MARKETS": market,
        "MARKET_WORKER_CONCURRENCY": "1",
        "MARKET_WORKER_MAX_JOBS_PER_RUN": "1",
        "CONFIRM_LIVE_EBAY_WORKER": "true",
        "EBAY_BROWSER_ENABLED": "true",
        "MARKET_LOOKUP_PROVIDER": "ebay_browser",
        "EBAY_BROWSER_NAV_MODE": "linux_x11",
        "EBAY_BROWSER_MAX_QUERY_ATTEMPTS": "1",
        "GLOBAL_BROWSER_PRICING_CONCURRENCY": "1",
        "CARDSCANR_MARKET": market,
        "CARDSCANR_CURRENCY": CURRENCY[market],
        "CARDSCANR_MARKETPLACE_HOME": definition.homepage,
        "MAX_LIVE_SUBMISSIONS_PER_HOUR": "20",
        "MAX_LIVE_SUBMISSIONS_PER_DAY": "200",
    }
    # Region canaries prove browser lifecycle. Prefer non-empty Sold pages when possible,
    # but do not exclude P0 entirely — US due inventory is often never-priced.
    if mode == "CANARY":
        os.environ.pop("OWNED_DAILY_SKIP_BANDS", None)
    else:
        os.environ.pop("OWNED_DAILY_SKIP_BANDS", None)
    os.environ.pop("PRE_SUBMIT_ONLY", None)
    os.environ.pop("CARDSCANR_PRE_SUBMIT_ONLY", None)
    for key, value in values.items():
        os.environ[key] = value
    return values


def cold_start_browser() -> dict[str, Any]:
    """COLD_START = clean nav state in the SAME Chrome process (no process/profile restart).

    Closing leftover eBay tabs and parking about:blank preserves cookies/auth.
    Restarting Chrome was a proven defect that dropped the signed-in US session.
    """
    import urllib.error
    import urllib.request

    from cardscanr_market_engine.browser_lifecycle_policy import (
        RUNTIME_COLD_START,
        evaluate_runtime_targets,
        is_ebay_marketplace_host,
        hostname_of,
    )

    cdp_port = int(os.environ.get("EBAY_BROWSER_CDP_PORT", "9444"))
    before = probe_local_browser_runtime(cdp_port=cdp_port)
    closed: list[str] = []
    errors: list[str] = []
    kept_page_id: str | None = None
    # Keep a blank page alive BEFORE closing marketplace tabs. Closing the last
    # Chrome page has killed the authenticated process (PID 7164 / 3006).
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{cdp_port}/json/new?about:blank",
            method="PUT",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode()
        try:
            created = json.loads(raw)
        except Exception:
            created = {}
        kept_page_id = str(created.get("id") or "").strip() or kept_page_id
        time.sleep(0.3)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        errors.append(f"pre_blank:{exc}")
    before = probe_local_browser_runtime(cdp_port=cdp_port)
    page_ids_alive = [
        str(t.get("id") or "")
        for t in (before.raw_targets or [])
        if isinstance(t, dict) and str(t.get("type") or "").lower() == "page" and t.get("id")
    ]
    for target in before.raw_targets or []:
        if not isinstance(target, dict):
            continue
        tid = str(target.get("id") or "").strip()
        url = str(target.get("url") or "")
        ttype = str(target.get("type") or "page").lower()
        if ttype != "page" or not tid:
            continue
        host = hostname_of(url)
        is_ebay = is_ebay_marketplace_host(host) or "signin.ebay" in url.lower() or "ebay." in url.lower()
        if is_ebay:
            remaining = [i for i in page_ids_alive if i != tid]
            if not remaining:
                errors.append(f"skip_last_page:{tid}")
                kept_page_id = kept_page_id or tid
                continue
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json/close/{tid}", timeout=3) as resp:
                    resp.read()
                closed.append(tid)
                page_ids_alive = remaining
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                errors.append(f"{tid}:{exc}")
        elif kept_page_id is None:
            kept_page_id = tid
    if closed:
        time.sleep(0.4)
    # Ensure at least one blank page remains for COLD_START readiness.
    after = probe_local_browser_runtime(cdp_port=cdp_port)
    page_ids = [
        str(t.get("id") or "")
        for t in (after.raw_targets or [])
        if isinstance(t, dict) and str(t.get("type") or "").lower() == "page" and t.get("id")
    ]
    if not page_ids:
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{cdp_port}/json/new?about:blank",
                method="PUT",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            errors.append(f"new_blank:{exc}")
        time.sleep(0.3)
        after = probe_local_browser_runtime(cdp_port=cdp_port)
    else:
        # Navigate a kept page to about:blank when it still holds non-blank content.
        for target in after.raw_targets or []:
            if not isinstance(target, dict) or str(target.get("type") or "").lower() != "page":
                continue
            tid = str(target.get("id") or "")
            url = str(target.get("url") or "")
            if not tid:
                continue
            if url.startswith("about:blank"):
                break
            try:
                # Activate then navigate via CDP HTTP helpers when available.
                urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json/activate/{tid}", timeout=3).read()
            except Exception:
                pass
            break
    after = probe_local_browser_runtime(cdp_port=cdp_port)
    after_policy = evaluate_runtime_targets(after.raw_targets, mode=RUNTIME_COLD_START)
    # Allow leftover non-ebay pages; fail only when eBay top-level pages remain.
    ebay_left = list(after.ebay_targets or [])
    ok = bool(after.cdp_ready and not ebay_left)
    return {
        "ok": ok,
        "resetMethod": "cdp_close_ebay_tabs_same_process",
        "chromeRestarted": False,
        "closedTargetIds": closed,
        "closeErrors": errors,
        "ebayTargetsAfter": ebay_left,
        "coldStartPolicy": after_policy.to_dict(),
        "cdpReady": after.cdp_ready,
    }


def needs_cold_start(market: str, *, healthy: int) -> bool:
    if healthy <= 0:
        return True
    ctx = load_navigation_runtime_context()
    if required_runtime_mode(next_market=market, prior=ctx.expected_prior) != RUNTIME_INTER_CARD:
        return True
    browser = probe_local_browser_runtime()
    policy = evaluate_runtime_targets(
        browser.raw_targets,
        mode=RUNTIME_INTER_CARD,
        prior=ctx.expected_prior,
    )
    return not any(c.belongs_to_expected_previous_card for c in policy.classified)


def force_cold_start_nav_context() -> None:
    """Drop persisted INTER_CARD prior so card-1 / market-switch workers stay COLD_START."""
    clear_context_from_environ()
    os.environ[RUNTIME_MODE_ENV] = "COLD_START"
    for path in (
        DEFAULT_CONTEXT_PATH,
        DEFAULT_CONTEXT_PATH.with_name(DEFAULT_CONTEXT_PATH.stem + ".prior.json"),
    ):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def local_ready(*, market: str, cold: bool = True) -> dict[str, Any]:
    xvfb = ensure_xvfb()
    if cold:
        force_cold_start_nav_context()
        cold_norm = cold_start_browser()
        prior = None
    else:
        cold_norm = {"ok": True, "resetMethod": "skipped_inter_card"}
        prior = load_navigation_runtime_context().expected_prior
    runtime = probe_pre_live_runtime(
        runtime_mode="COLD_START" if cold else "INTER_CARD",
        expected_prior=prior,
    )
    ready = bool(
        xvfb.get("ok")
        and cold_norm.get("ok")
        and runtime.get("xvfbReady")
        and runtime.get("cdpReady")
        and runtime.get("keyboardInjectionReady") is not False
    )
    return {
        "ok": ready,
        "market": market,
        "xvfb": xvfb,
        "coldStart": {
            "ok": cold_norm.get("ok"),
            "resetMethod": cold_norm.get("resetMethod"),
            "ebayTargetsAfter": cold_norm.get("ebayTargetsAfter"),
        },
        "runtime": {
            k: runtime.get(k)
            for k in (
                "ready",
                "xvfbReady",
                "cdpReady",
                "chromeWindowReady",
                "windowFocusReady",
                "keyboardInjectionReady",
                "reasonCodes",
            )
        },
    }


def attempt_baseline() -> set[str]:
    root = attempts_dir()
    return {p.name.split(".", 1)[0] for p in root.glob(f"*.{SEARCH_SUBMISSION_STARTED}.json")}


def new_submission_ids(before: set[str]) -> list[str]:
    after = attempt_baseline()
    return sorted(after - before)


def run_scheduler_worker(market: str, *, mode: str) -> dict[str, Any]:
    bind_env(market, mode=mode)
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = str(ROOT)
    before = attempt_baseline()
    sched = subprocess.run(
        [sys.executable, "-u", str(ROOT / "workers" / "owned_daily_price_scheduler.py"), "--once", "--no-sync-keys"],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    worker = subprocess.run(
        [sys.executable, "-u", str(ROOT / "workers" / "market_price_worker.py"), "--once"],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=1500,
    )
    report: dict[str, Any] = {}
    report_path = ROOT / "reports" / "market_price_worker_latest.json"
    if report_path.is_file():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception:
            report = {}
    results = report.get("results") or []
    row = results[0] if results else {}
    new_ids = new_submission_ids(before)
    submitted = any(has_search_submission_started(aid) for aid in new_ids) or bool(
        row.get("searchSubmissionStarted")
    )
    outcome = str(row.get("ownedDailyOutcome") or row.get("outcomeClass") or "")
    return {
        "market": market,
        "mode": mode,
        "schedulerExit": sched.returncode,
        "workerExit": worker.returncode,
        "schedulerStdout": (sched.stdout or "")[-3000:],
        "workerStdout": (worker.stdout or "")[-5000:],
        "workerStderr": (worker.stderr or "")[-1500:],
        "result": row,
        "outcome": outcome,
        "error": row.get("error") or row.get("errorMessage"),
        "status": row.get("status"),
        "searchSubmissionStarted": submitted,
        "newAttemptIds": new_ids,
        "healthy": outcome in HEALTHY_OUTCOMES and str(row.get("status") or "") in {
            "completed",
            "checked_no_new_exact_evidence",
        },
        "at": utc_now(),
    }


def wait_for_gaming_clear(*, max_wait_seconds: int = 900) -> dict[str, Any]:
    """Block canary/probe work while Fortnite gaming pause is active. Never bypass."""
    controller = GamingResourcePauseController()
    started = time.time()
    while controller.should_block_new_jobs():
        status = controller.status()
        payload = {
            "blocked": True,
            "reason": controller.block_reason(),
            "status": status,
            "elapsedSec": int(time.time() - started),
        }
        if time.time() - started >= max_wait_seconds:
            payload["timeout"] = True
            return payload
        time.sleep(20)
    return {
        "blocked": False,
        "reason": None,
        "status": controller.status(),
        "elapsedSec": int(time.time() - started),
    }


def wait_for_probe_window(market: str, *, max_wait_seconds: int = 3700) -> dict[str, Any]:
    started = time.time()
    while True:
        snap = peek_availability(market=market)
        gate = evaluate_ebay_browser_work_gate(market=market, for_probe=True)
        cont = canary_may_continue(market)
        payload = {
            "market": market,
            "availability": snap.state,
            "nextProbeAt": snap.next_probe_at.isoformat() if snap.next_probe_at else None,
            "probeAllowed": gate.allowed,
            "canary": cont,
            "elapsedSec": int(time.time() - started),
        }
        if snap.state in {"HEALTHY", "PROBE_REQUIRED"}:
            payload["ready"] = True
            return payload
        if cont.get("hardStop"):
            payload["ready"] = False
            payload["stop"] = True
            return payload
        if time.time() - started >= max_wait_seconds:
            payload["ready"] = False
            payload["timeout"] = True
            return payload
        time.sleep(30)


def recover_au() -> dict[str, Any]:
    ART.mkdir(parents=True, exist_ok=True)
    gaming = wait_for_gaming_clear(max_wait_seconds=900)
    _write(ART / "AU_gaming_wait.json", gaming)
    if gaming.get("blocked"):
        return {"ok": False, "reason": "GAMING_RESOURCE_PAUSE", "gaming": gaming}

    before = peek_availability(market="AU")
    _write(ART / "AU_availability_before.json", before.to_dict())
    if before.state == "HEALTHY":
        # Do not COLD_START/restart Chrome — other markets may have a live INTER_CARD tab.
        budget = ContinuousSafetyBudget.from_env()
        budget.record_healthy()
        budget.persist()
        write_owned_daily_flag(True)
        apply_continuous_multi_region_env(markets="AU")
        return {
            "ok": True,
            "alreadyHealthy": True,
            "enabled": True,
            "availability": before.state,
            "chromeResetSkipped": True,
        }

    ready = local_ready(market="AU", cold=True)
    _write(ART / "AU_local_ready.json", ready)
    if not ready["ok"]:
        return {"ok": False, "reason": "LOCAL_RUNTIME_NOT_READY", "local": ready}

    before = peek_availability(market="AU")
    _write(ART / "AU_availability_before.json", before.to_dict())
    if before.state == "HEALTHY":
        # Clear prior consecutive-transient hard-stop so continuous can resume.
        budget = ContinuousSafetyBudget.from_env()
        budget.record_healthy()
        budget.persist()
        write_owned_daily_flag(True)
        apply_continuous_multi_region_env(markets="AU")
        return {
            "ok": True,
            "alreadyHealthy": True,
            "enabled": True,
            "availability": before.state,
        }

    if before.state == "CHALLENGE_REQUIRED":
        return {"ok": False, "reason": "AU_CHALLENGE_REQUIRED", "availability": before.state}

    # Natural PROBE_REQUIRED / cooldown-expired path.
    if before.state == "COOLDOWN":
        wait = wait_for_probe_window("AU")
        _write(ART / "AU_cooldown_wait.json", wait)
        if wait.get("stop") or not wait.get("ready"):
            return {"ok": False, "reason": "AU_COOLDOWN_WAIT_FAILED", "wait": wait}

    gaming2 = wait_for_gaming_clear(max_wait_seconds=120)
    if gaming2.get("blocked"):
        return {"ok": False, "reason": "GAMING_RESOURCE_PAUSE", "gaming": gaming2}

    probe = run_scheduler_worker("AU", mode="PROBE")
    _write(ART / "AU_probe_cycle.json", {k: v for k, v in probe.items() if "Stdout" not in k and "Stderr" not in k})
    after = get_availability(market="AU", persist_transitions=False)
    _write(ART / "AU_availability_after.json", after.to_dict())

    # Empty worker cycle while gaming pause active is not a marketplace failure.
    gaming_after = GamingResourcePauseController()
    if not probe.get("result") and gaming_after.should_block_new_jobs():
        return {
            "ok": False,
            "reason": "GAMING_RESOURCE_PAUSE",
            "gaming": gaming_after.status(),
            "probe": {k: probe.get(k) for k in ("outcome", "error", "status", "healthy", "searchSubmissionStarted")},
        }

    classification = classify_canary_failure(
        outcome=probe.get("outcome"),
        error_message=str(probe.get("error") or ""),
        search_submission_started=bool(probe.get("searchSubmissionStarted")),
    )
    if classification["kind"] == "HARD_STOP":
        return {"ok": False, "reason": "AU_HARD_STOP", "probe": probe, "classification": classification}

    if after.state != "HEALTHY" and not probe.get("healthy"):
        # Probe may leave PROBE_REQUIRED on local failure; marketplace sorry opens cooldown.
        return {
            "ok": False,
            "reason": "AU_PROBE_DID_NOT_RESTORE_HEALTHY",
            "availability": after.state,
            "probe": {k: probe.get(k) for k in ("outcome", "error", "status", "healthy", "searchSubmissionStarted")},
            "classification": classification,
        }

    budget = ContinuousSafetyBudget.from_env()
    budget.record_healthy()
    budget.persist()
    write_owned_daily_flag(True)
    apply_continuous_multi_region_env(markets="AU")
    return {
        "ok": True,
        "alreadyHealthy": False,
        "enabled": True,
        "availability": after.state,
        "probeHealthy": bool(probe.get("healthy")),
        "probeOutcome": probe.get("outcome"),
    }


def enable_market_continuous(market: str) -> dict[str, Any]:
    current = set(load_continuous_market_overrides())
    current.add("AU")
    current.add(market)
    path = write_continuous_market_overrides(sorted(current))
    # Keep AU continuous flag true once AU recovered; allowlist grows.
    if owned_daily_full_enable():
        apply_continuous_multi_region_env(markets=",".join(sorted(current)))
    return {
        "market": market,
        "enabledMarkets": sorted(current),
        "flagPath": str(path),
        "definitionStatus": region_definition(market).status,
    }


def reset_market_canary_artifacts(market: str) -> dict[str, Any]:
    """Archive prior canary card/summary files so the next run starts at 0/5."""
    stamped = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = ART / "_archive" / f"{market}_{stamped}"
    moved: list[str] = []
    archive.mkdir(parents=True, exist_ok=True)
    for path in sorted(ART.glob(f"{market}_*")):
        if not path.is_file():
            continue
        dest = archive / path.name
        path.replace(dest)
        moved.append(path.name)
    force_cold_start_nav_context()
    return {"market": market, "archivedTo": str(archive), "moved": moved}


def run_market_canary(
    market: str,
    *,
    target_healthy: int = 5,
    fresh: bool = False,
) -> dict[str, Any]:
    if fresh:
        reset_market_canary_artifacts(market)
    cards: list[dict[str, Any]] = []
    submissions = 0
    healthy = 0
    stop_reason = None
    result = "RUNNING"
    runtime_modes: list[str] = []
    healthy_keys = healthy_price_keys_from_files(market)
    healthy = len(healthy_keys)
    prev_path = ART / f"{market}_canary_summary.json"
    if prev_path.is_file() and not fresh:
        try:
            previous = json.loads(prev_path.read_text(encoding="utf-8"))
        except Exception:
            previous = {}
        if isinstance(previous, dict) and previous.get("result") == "PASS":
            return previous
        if isinstance(previous, dict):
            cards = list(previous.get("cards") or [])
            submissions = int(previous.get("submissions") or 0)
            runtime_modes = list(previous.get("runtimeModes") or [])
    last_healthy_path = None
    index = next_card_index(market) - 1
    while index >= 1:
        candidate = ART / f"{market}_card_{index}.json"
        try:
            cycle = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            index -= 1
            continue
        if cycle.get("healthy"):
            last_healthy_path = candidate
            break
        index -= 1
    if last_healthy_path is not None and healthy > 0:
        try:
            persist_inter_card_for_resume(
                market,
                json.loads(last_healthy_path.read_text(encoding="utf-8")).get("result") or {},
            )
        except Exception:
            pass
    elif healthy <= 0:
        force_cold_start_nav_context()

    attempts_this_run = 0
    probe_attempts = 0
    while healthy < target_healthy and submissions < target_healthy:
        if attempts_this_run >= max(target_healthy * 4, 12):
            stop_reason = "MAX_CANARY_ATTEMPTS"
            result = "STOPPED_SAFE"
            break
        cont = canary_may_continue(market)
        if cont.get("hardStop"):
            stop_reason = str(cont.get("reason") or "HARD_STOP")
            result = "STOPPED_SAFE"
            break
        if cont.get("cooldown") or cont.get("needsProbe") or cont.get("reason") == "PROBE_REQUIRED":
            wait = wait_for_probe_window(market)
            _write(ART / f"{market}_wait_{len(cards)}.json", wait)
            if wait.get("stop"):
                stop_reason = str((wait.get("canary") or {}).get("reason") or "HARD_STOP")
                result = "STOPPED_SAFE"
                break
            if not wait.get("ready"):
                stop_reason = "COOLDOWN_WAIT_TIMEOUT"
                result = "STOPPED_SAFE"
                break
            if wait.get("availability") == "PROBE_REQUIRED" or cont.get("needsProbe"):
                # New-market COLD_START must close leftover eBay tabs in-process
                # before the worker's ensure_chrome COLD_START check.
                ready = local_ready(market=market, cold=True)
                _write(ART / f"{market}_probe_local_ready_{len(cards)}.json", ready)
                if not ready["ok"]:
                    stop_reason = "LOCAL_RUNTIME_NOT_READY"
                    result = "STOPPED_SAFE"
                    cards.append({"kind": "LOCAL_READY_FAIL", "ready": ready, "at": utc_now()})
                    break
                if probe_attempts >= 3:
                    stop_reason = "PROBE_LOOP"
                    result = "STOPPED_SAFE"
                    break
                probe_attempts += 1
                probe = run_scheduler_worker(market, mode="PROBE")
                cards.append({"kind": "PROBE", **{k: probe.get(k) for k in (
                    "outcome", "error", "status", "healthy", "searchSubmissionStarted", "at"
                )}})
                _write(ART / f"{market}_probe_{len(cards)}.json", probe)
                snap = peek_availability(market=market)
                classification = classify_canary_failure(
                    outcome=probe.get("outcome"),
                    error_message=str(probe.get("error") or ""),
                    search_submission_started=bool(probe.get("searchSubmissionStarted")),
                )
                if snap.state == "CHALLENGE_REQUIRED" or (
                    classification["kind"] == "HARD_STOP" and not probe.get("healthy")
                ):
                    stop_reason = "CHALLENGE_OR_HARD_STOP"
                    result = "STOPPED_SAFE"
                    break
                if probe.get("healthy"):
                    record_healthy_browser_check(from_probe=True, market=market)
                    # Probe proves marketplace health; 5/5 still uses CANARY cards.
                    continue
                if snap.state != "HEALTHY":
                    continue
                continue

        gaming = wait_for_gaming_clear(max_wait_seconds=900)
        if gaming.get("blocked"):
            stop_reason = "GAMING_RESOURCE_PAUSE"
            result = "STOPPED_SAFE"
            cards.append({"kind": "GAMING_PAUSE", "gaming": gaming, "at": utc_now()})
            break

        last_local = bool(cards) and bool((cards[-1].get("classification") or {}).get("localRuntime"))
        cold = needs_cold_start(market, healthy=healthy) or last_local
        if last_local:
            force_cold_start_nav_context()
        ready = local_ready(market=market, cold=cold)
        if not ready["ok"]:
            stop_reason = "LOCAL_RUNTIME_NOT_READY"
            result = "STOPPED_SAFE"
            cards.append({"kind": "LOCAL_READY_FAIL", "ready": ready, "at": utc_now()})
            break

        expected_mode = "COLD_START" if needs_cold_start(market, healthy=healthy) else "INTER_CARD"
        # After local_ready(cold=True) the prior is cleared; recompute from healthy count.
        if healthy <= 0:
            expected_mode = "COLD_START"
        elif required_runtime_mode(
            next_market=market,
            prior=load_navigation_runtime_context().expected_prior,
        ) == RUNTIME_INTER_CARD:
            expected_mode = "INTER_CARD"
        else:
            expected_mode = "COLD_START"

        cycle = run_scheduler_worker(market, mode="CANARY")
        attempts_this_run += 1
        classification = classify_canary_failure(
            outcome=cycle.get("outcome"),
            error_message=str(cycle.get("error") or ""),
            search_submission_started=bool(cycle.get("searchSubmissionStarted")),
        )
        observed_mode = (
            (cycle.get("result") or {}).get("runtimeMode")
            or cycle.get("runtimeMode")
            or expected_mode
        )
        card = {
            "kind": "CANARY",
            "position": healthy + 1,
            "outcome": cycle.get("outcome"),
            "error": cycle.get("error"),
            "status": cycle.get("status"),
            "healthy": cycle.get("healthy"),
            "searchSubmissionStarted": cycle.get("searchSubmissionStarted"),
            "newAttemptIds": cycle.get("newAttemptIds"),
            "classification": classification,
            "expectedRuntimeMode": expected_mode,
            "observedRuntimeMode": observed_mode,
            "at": cycle.get("at"),
        }
        cards.append(card)
        cycle_out = dict(cycle)
        cycle_out["expectedRuntimeMode"] = expected_mode
        cycle_out["observedRuntimeMode"] = observed_mode
        _write(ART / f"{market}_card_{next_card_index(market)}.json", cycle_out)

        if str(cycle.get("outcome") or "") in SKIP_OUTCOMES or str(cycle.get("status") or "") == "skipped_already_fresh":
            continue
        if not cycle.get("result") and not cycle.get("outcome") and not cycle.get("error"):
            # Scheduler enqueued nothing this cycle — wait briefly and retry.
            time.sleep(5)
            continue
        if classification["kind"] == "HARD_STOP":
            record_canary_episode(market, kind="HARD_STOP", outcome=classification["outcome"])
            outcome = str(classification.get("outcome") or "")
            if outcome == "EBAY_AUTH_REQUIRED" and market == "US":
                stop_reason = "US_AUTH_SESSION_NOT_PERSISTING"
            else:
                stop_reason = outcome
            result = "STOPPED_SAFE"
            break
        if classification["kind"] == "LOCAL_RUNTIME_FAILURE":
            # Local readiness issue — do not consume or cooldown; stop for owner if repeated.
            stop_reason = "LOCAL_RUNTIME_FAILURE"
            result = "STOPPED_SAFE"
            break
        if classification["kind"] == "PRE_SUBMIT_TRANSIENT":
            record_canary_episode(market, kind="PRE_SUBMIT_TRANSIENT", outcome=classification["outcome"])
            if canary_may_continue(market).get("reason") == "MAX_PRESUBMIT_TRANSIENT_EPISODES_24H":
                stop_reason = "MAX_PRESUBMIT_TRANSIENT_EPISODES_24H"
                result = "STOPPED_SAFE"
                break
            # Wait cooldown then continue same canary without consuming a submission.
            continue
        if classification["consumed"] or cycle.get("searchSubmissionStarted"):
            submissions += 1
        if classification["kind"] == "POST_SUBMIT_TRANSIENT":
            record_canary_episode(market, kind="POST_SUBMIT_TRANSIENT", outcome=classification["outcome"])
            stop_reason = "POST_SUBMIT_TRANSIENT_CONSUMED"
            result = "STOPPED_SAFE"
            break
        if cycle.get("healthy"):
            persist_inter_card_for_resume(market, cycle.get("result") or cycle)
            price_key = str((cycle.get("result") or {}).get("priceKeyId") or "").strip()
            if price_key:
                healthy_keys.add(price_key)
            healthy = max(healthy + 1, len(healthy_keys))
            runtime_modes.append(str(expected_mode))
            continue
        # Non-healthy non-transient: stop safely.
        stop_reason = str(cycle.get("outcome") or cycle.get("error") or "NON_HEALTHY")
        result = "STOPPED_SAFE"
        break

    if healthy >= target_healthy and submissions <= target_healthy:
        result = "PASS"
        enablement = enable_market_continuous(market)
    else:
        enablement = None
        if result == "RUNNING":
            result = "STOPPED_SAFE" if stop_reason else "FAIL"

    summary = {
        "market": market,
        "result": result,
        "healthy": healthy,
        "submissions": submissions,
        "targetHealthy": target_healthy,
        "maxPresubmitEpisodes": MAX_PRESUBMIT_TRANSIENT_EPISODES_24H,
        "stopReason": stop_reason,
        "runtimeModes": runtime_modes,
        "interCardSequenceOk": (
            len(runtime_modes) >= 2
            and runtime_modes[0] == "COLD_START"
            and all(mode == "INTER_CARD" for mode in runtime_modes[1:])
        )
        if runtime_modes
        else None,
        "cards": cards,
        "enablement": enablement,
        "availability": peek_availability(market=market).to_dict(),
        "canaryGate": canary_may_continue(market),
        "at": utc_now(),
    }
    _write(ART / f"{market}_canary_summary.json", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    fresh_us = "--fresh-us" in args
    fresh_gb = "--fresh-gb" in args
    fresh_ca = "--fresh-ca" in args
    ART.mkdir(parents=True, exist_ok=True)
    _write(ART / "CONTROL_PLANE_BEFORE.json", multi_region_status())
    stages: dict[str, Any] = {
        "startedAt": utc_now(),
        "operationModeInitial": parse_operation_mode(),
        "freshUs": fresh_us,
        "freshGb": fresh_gb,
        "freshCa": fresh_ca,
    }

    au = recover_au()
    stages["AU"] = au
    _write(ART / "AU_recovery.json", au)
    if not au.get("ok"):
        stages["result"] = "STOPPED_SAFE"
        stages["stopReason"] = au.get("reason")
        _write(ART / "MARKET_RECOVERY_SUMMARY.json", stages)
        print(json.dumps({"RESULT": stages["result"], "AU": au}, indent=2, default=str))
        return 2

    # Pause AU continuous dispatch during regional canaries (scheduling pause only).
    write_owned_daily_flag(False)
    stages["AU"]["dispatchPausedForCanaries"] = True

    for market, fresh in (("US", fresh_us), ("GB", fresh_gb), ("CA", fresh_ca)):
        # When restarting US from 0/5, also force fresh GB/CA so prior PASS skip
        # does not leave stale regional enablement half-applied.
        if fresh_us and market in {"GB", "CA"}:
            fresh = True
        summary = run_market_canary(market, fresh=fresh)
        stages[market] = summary
        if summary.get("result") != "PASS":
            stages["result"] = "STOPPED_SAFE" if summary.get("result") == "STOPPED_SAFE" else "FAIL"
            stages["stopReason"] = summary.get("stopReason")
            # Re-enable AU (+ any markets that already passed) continuous.
            write_owned_daily_flag(True)
            apply_continuous_multi_region_env(
                markets=",".join(["AU"] + sorted(load_continuous_market_overrides() - {"AU"}))
            )
            _write(ART / "CONTROL_PLANE_AFTER.json", multi_region_status())
            _write(ART / "MARKET_RECOVERY_SUMMARY.json", stages)
            print(json.dumps({"RESULT": stages["result"], market: summary}, indent=2, default=str))
            return 3

    # All canaries passed — enable AU+US+GB+CA continuous under concurrency=1.
    enabled = sorted({"AU", "US", "GB", "CA"} | set(load_continuous_market_overrides()))
    write_continuous_market_overrides(enabled)
    write_owned_daily_flag(True)
    apply_continuous_multi_region_env(markets=",".join(enabled))
    stages["result"] = "PASS"
    stages["enabledMarkets"] = enabled
    stages["finishedAt"] = utc_now()
    _write(ART / "CONTROL_PLANE_AFTER.json", multi_region_status())
    _write(ART / "MARKET_RECOVERY_SUMMARY.json", stages)
    print(json.dumps({"RESULT": "PASS", "enabledMarkets": enabled}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
