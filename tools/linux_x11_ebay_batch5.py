#!/usr/bin/env python3
"""5-card isolated Linux X11 GUI eBay pricing proof (no owned_daily enable).

EBAY_BROWSER_NAV_MODE=linux_x11:
  - search/Sold via WSL X11 mouse/keyboard only
  - CDP attach for read-only parse into existing exact-comp pricing engine
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.config import MarketEngineConfig, supabase_secret_key_from_env
from cardscanr_market_engine.job_runner import MarketPriceJobRunner
from cardscanr_market_engine.owned_daily_outcomes import HEALTHY_CHECK_OUTCOMES, summarize_outcome_counts
from cardscanr_market_engine.providers.factory import create_market_comps_provider
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient
from cardscanr_market_engine.supabase_env_loader import load_supabase_env
from tools.desktop_ebay_e2e_pricing import due_owned_key_ids, run_forced_job, _cache_snapshot

OUT = ROOT / "reports" / "artifacts" / "linux_gui_batch5"
OUT.mkdir(parents=True, exist_ok=True)


def _configure_linux_env(*, inter_job_delay: int = 20) -> None:
    os.environ["EBAY_BROWSER_NAV_MODE"] = "linux_x11"
    os.environ["EBAY_BROWSER_ENABLED"] = "true"
    os.environ["MARKET_LOOKUP_PROVIDER"] = "ebay_browser"
    os.environ["EBAY_BROWSER_HEADLESS"] = "false"
    os.environ["EBAY_BROWSER_MAX_QUERY_ATTEMPTS"] = "1"
    os.environ["EBAY_BROWSER_REUSE_CONTEXT"] = "true"
    os.environ["EBAY_BROWSER_MIN_SECONDS_BETWEEN_REQUESTS"] = str(max(5, inter_job_delay))
    os.environ["EBAY_BROWSER_COOLDOWN_SECONDS"] = str(max(5, inter_job_delay))
    os.environ["EBAY_BROWSER_CDP_PORT"] = os.environ.get("EBAY_BROWSER_CDP_PORT", "9444")
    os.environ.setdefault("OWNED_DAILY_FULL_ENABLE", "false")


def _client() -> SupabaseMarketEngineClient:
    load_supabase_env()
    return SupabaseMarketEngineClient(
        supabase_url=os.environ["SUPABASE_URL"].rstrip("/"),
        service_role_key=supabase_secret_key_from_env(),
    )


def sample_resources() -> dict[str, Any]:
    """Best-effort WSL + host resource snapshot."""
    import subprocess

    wsl = {}
    try:
        script = r"""
echo RAM_TOTAL_MB=$(awk '/MemTotal/{printf "%.0f",$2/1024}' /proc/meminfo)
echo RAM_AVAIL_MB=$(awk '/MemAvailable/{printf "%.0f",$2/1024}' /proc/meminfo)
echo CHROME_RSS_MB=$(ps -eo rss,comm | awk '/chrome/{s+=$1} END{printf "%.1f",s/1024}')
echo XVFB_RSS_MB=$(ps -eo rss,comm | awk '/Xvfb/{s+=$1} END{printf "%.1f",s/1024}')
echo X11VNC_RSS_MB=$(ps -eo rss,comm | awk '/x11vnc/{s+=$1} END{printf "%.1f",s/1024}')
echo NOVNC_RSS_MB=$(ps -eo rss,comm | awk '/websockify/{s+=$1} END{printf "%.1f",s/1024}')
echo CPU_LOAD=$(awk '{print $1}' /proc/loadavg)
"""
        p = Path(r"D:\DevCache\Temp\wsl_res_sample.sh")
        p.write_text(script.replace("\r\n", "\n"), encoding="utf-8")
        proc = subprocess.run(
            ["wsl", "-d", "Ubuntu", "--", "bash", "/mnt/d/DevCache/Temp/wsl_res_sample.sh"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        for line in (proc.stdout or "").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                wsl[k.strip()] = v.strip()
    except Exception as exc:
        wsl["error"] = str(exc)
    host = {}
    try:
        import psutil  # type: ignore

        host["hostAvailRamMb"] = round(psutil.virtual_memory().available / (1024 * 1024), 1)
        host["hostCpuPercent"] = psutil.cpu_percent(interval=0.5)
    except Exception:
        try:
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory"],
                text=True,
            ).strip()
            host["hostAvailRamMb"] = round(int(out) / 1024, 1)
        except Exception as exc:
            host["error"] = str(exc)
    return {"wsl": wsl, "host": host, "ts": datetime.now(timezone.utc).isoformat()}


def main() -> int:
    delay = int(os.getenv("LINUX_GUI_BATCH_DELAY", "20"))
    _configure_linux_env(inter_job_delay=delay)
    client = _client()
    cards = due_owned_key_ids(client, limit=5)
    if len(cards) < 5:
        print(f"[linux-x11] only {len(cards)} due; trying cache candidates", flush=True)
        try:
            rows = client.list_cache_refresh_candidates(limit=30)
        except Exception as exc:
            print(f"[linux-x11] cache fallback failed: {exc}", flush=True)
            rows = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            kid = str(row.get("price_key_id") or row.get("id") or "")
            fp = str(row.get("fingerprint") or kid)
            if not kid or any(c["priceKeyId"] == kid for c in cards):
                continue
            cards.append({"priceKeyId": kid, "fingerprint": fp, "card": row.get("card_name") or fp})
            if len(cards) >= 5:
                break

    report: dict[str, Any] = {
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "delaySec": delay,
        "cards": cards[:5],
        "results": [],
        "resources": {"start": sample_resources(), "samples": []},
    }
    if not cards:
        report["error"] = "no_due_cards"
        (OUT / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
        return 2

    # Ensure CDP Chrome once up-front
    from cardscanr_market_engine.providers.linux_x11_ebay_nav import ensure_chrome_with_cdp

    print("[linux-x11] ensuring Chrome+CDP on :99", flush=True)
    ensure_chrome_with_cdp()

    shared_runner = MarketPriceJobRunner(
        client=client,
        provider=create_market_comps_provider("ebay_browser"),
        config=MarketEngineConfig.from_env(),
    )

    stop = False
    for i, card in enumerate(cards[:5]):
        if stop:
            break
        print(f"[linux-x11] BATCH {i+1}/5 {card}", flush=True)
        t0 = time.monotonic()
        row: dict[str, Any] = {
            "index": i + 1,
            "card": card.get("card"),
            "fingerprint": card.get("fingerprint"),
            "priceKeyId": card["priceKeyId"],
            "searchMethod": "visible_ebay_search_input_x11",
        }
        try:
            payload = run_forced_job(
                client,
                price_key_id=card["priceKeyId"],
                reason="linux_gui:batch5_isolated",
                runner=shared_runner,
            )
            result = payload["result"]
            err = str(result.get("error") or "")
            diag = ((result.get("providerDiagnostics") or {}).get("diagnostics") or {})
            if not diag and isinstance(result.get("providerDiagnostics"), dict):
                diag = result.get("providerDiagnostics") or {}
            desktop_nav = diag.get("desktopNav") or {}
            status = result.get("status") or result.get("outcome") or ""
            # Prefer classified owned-daily-style outcome if present
            outcome = (
                result.get("ownedDailyOutcome")
                or result.get("checkOutcome")
                or result.get("outcome")
                or status
            )
            row.update(
                {
                    "elapsedSec": round(time.monotonic() - t0, 1),
                    "durationSec": payload.get("durationSec"),
                    "query": diag.get("queryDiagnostics", {}).get("queryText")
                    or diag.get("query_text")
                    or result.get("queryUsed"),
                    "resultPageReached": bool(desktop_nav.get("searchSuccess")),
                    "soldClick": bool(desktop_nav.get("soldClickSuccess")),
                    "SOLD_STATE_VERIFIED": bool(desktop_nav.get("SOLD_STATE_VERIFIED")),
                    "sorry": bool(desktop_nav.get("sorry")),
                    "challenge": bool(desktop_nav.get("challenge")),
                    "candidates": (diag.get("qualitySummary") or {}).get("candidate_count")
                    or diag.get("resultCount"),
                    "acceptedExactComps": result.get("acceptedExactComps")
                    or result.get("exactCompCount")
                    or (diag.get("qualitySummary") or {}).get("exact_comp_count"),
                    "pricingOutcome": outcome,
                    "status": status,
                    "error": err or None,
                    "before": payload.get("before"),
                    "after": payload.get("after"),
                    "desktopNav": desktop_nav,
                }
            )
            if desktop_nav.get("challenge") or "challenge" in err.lower():
                row["STOP"] = "challenge"
                stop = True
        except Exception as exc:
            row.update(
                {
                    "elapsedSec": round(time.monotonic() - t0, 1),
                    "pricingOutcome": "ERROR",
                    "error": str(exc),
                }
            )
            if "challenge" in str(exc).lower() or "captcha" in str(exc).lower():
                row["STOP"] = "challenge"
                stop = True
        report["results"].append(row)
        report["resources"]["samples"].append(sample_resources())
        (OUT / "report_partial.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        if i < 4 and not stop:
            time.sleep(delay)

    report["finishedAt"] = datetime.now(timezone.utc).isoformat()
    report["resources"]["end"] = sample_resources()
    outcomes = [r.get("pricingOutcome") for r in report["results"]]
    healthy = [o for o in outcomes if o in HEALTHY_CHECK_OUTCOMES or o in {
        "UPDATED_FROM_EBAY",
        "UNCHANGED_FROM_EBAY",
        "CHECKED_NO_NEW_EXACT_EVIDENCE",
        "completed",
        "checked_no_new_exact_evidence",
    }]
    report["summary"] = {
        "attempted": len(report["results"]),
        "healthy": len(healthy),
        "updated": sum(1 for o in outcomes if o == "UPDATED_FROM_EBAY"),
        "unchanged": sum(1 for o in outcomes if o == "UNCHANGED_FROM_EBAY"),
        "noNewEvidence": sum(1 for o in outcomes if o in {"CHECKED_NO_NEW_EXACT_EVIDENCE", "checked_no_new_exact_evidence"}),
        "searchFailures": sum(1 for r in report["results"] if not r.get("resultPageReached")),
        "soldFailures": sum(1 for r in report["results"] if r.get("resultPageReached") and not r.get("SOLD_STATE_VERIFIED")),
        "sorry": sum(1 for r in report["results"] if r.get("sorry")),
        "challenges": sum(1 for r in report["results"] if r.get("challenge") or r.get("STOP") == "challenge"),
        "avgSeconds": round(
            sum(float(r.get("elapsedSec") or 0) for r in report["results"]) / max(1, len(report["results"])),
            1,
        ),
        "stoppedEarly": stop,
    }
    (OUT / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], indent=2))
    print(f"Wrote {OUT / 'report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
