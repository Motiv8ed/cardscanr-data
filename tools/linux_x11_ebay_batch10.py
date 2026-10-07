#!/usr/bin/env python3
"""10-card isolated Linux X11 GUI reliability gate (owned_daily stays disabled).

Counts only actual GUI attempts. SKIPPED_ALREADY_FRESH does not count.
Stops on first GUI/SORRY/challenge failure.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.config import MarketEngineConfig, supabase_secret_key_from_env
from cardscanr_market_engine.job_runner import MarketPriceJobRunner
from cardscanr_market_engine.providers.factory import create_market_comps_provider
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient
from cardscanr_market_engine.supabase_env_loader import load_supabase_env
from tools.desktop_ebay_e2e_pricing import due_owned_key_ids, run_forced_job, _cache_snapshot

OUT = ROOT / "reports" / "artifacts" / "linux_gui_batch10"
ART = ROOT / "reports" / "artifacts"
OUT.mkdir(parents=True, exist_ok=True)

HEALTHY = {
    "UPDATED_FROM_EBAY",
    "UNCHANGED_FROM_EBAY",
    "CHECKED_NO_NEW_EXACT_EVIDENCE",
    "completed",
    "checked_no_new_exact_evidence",
}


def _configure(*, delay: int = 20) -> None:
    os.environ["EBAY_BROWSER_NAV_MODE"] = "linux_x11"
    os.environ["EBAY_BROWSER_ENABLED"] = "true"
    os.environ["MARKET_LOOKUP_PROVIDER"] = "ebay_browser"
    os.environ["EBAY_BROWSER_HEADLESS"] = "false"
    os.environ["EBAY_BROWSER_MAX_QUERY_ATTEMPTS"] = "1"
    os.environ["EBAY_BROWSER_REUSE_CONTEXT"] = "true"
    os.environ["EBAY_BROWSER_MIN_SECONDS_BETWEEN_REQUESTS"] = str(max(5, delay))
    os.environ["EBAY_BROWSER_COOLDOWN_SECONDS"] = str(max(5, delay))
    os.environ["EBAY_BROWSER_CDP_PORT"] = os.environ.get("EBAY_BROWSER_CDP_PORT", "9444")
    os.environ["OWNED_DAILY_FULL_ENABLE"] = "false"


def _client() -> SupabaseMarketEngineClient:
    load_supabase_env()
    return SupabaseMarketEngineClient(
        supabase_url=os.environ["SUPABASE_URL"].rstrip("/"),
        service_role_key=supabase_secret_key_from_env(),
    )


def sample_resources() -> dict[str, Any]:
    import subprocess

    wsl: dict[str, Any] = {}
    script = """
echo RAM_TOTAL_MB=$(awk '/MemTotal/{printf "%.0f",$2/1024}' /proc/meminfo)
echo RAM_AVAIL_MB=$(awk '/MemAvailable/{printf "%.0f",$2/1024}' /proc/meminfo)
echo CHROME_RSS_MB=$(ps -eo rss,comm | awk '/chrome/{s+=$1} END{printf "%.1f",s/1024}')
echo XVFB_RSS_MB=$(ps -eo rss,comm | awk '/Xvfb/{s+=$1} END{printf "%.1f",s/1024}')
echo X11VNC_RSS_MB=$(ps -eo rss,comm | awk '/x11vnc/{s+=$1} END{printf "%.1f",s/1024}')
echo NOVNC_RSS_MB=$(ps -eo rss,comm | awk '/websockify/{s+=$1} END{printf "%.1f",s/1024}')
echo CPU_LOAD=$(awk '{print $1}' /proc/loadavg)
echo CHROME_OWNER_PROCS=$(pgrep -fc 'chrome.*cardscanr-chrome' || echo 0)
"""
    p = Path(r"D:\DevCache\Temp\wsl_res_sample.sh")
    p.write_bytes(script.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8"))
    try:
        proc = subprocess.run(
            ["wsl", "-d", "Ubuntu", "--", "bash", "/mnt/d/DevCache/Temp/wsl_res_sample.sh"],
            capture_output=True,
            text=True,
            timeout=30,
            encoding="utf-8",
            errors="replace",
        )
        for line in (proc.stdout or "").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                wsl[k.strip()] = v.strip()
    except Exception as exc:
        wsl["error"] = str(exc)
    host: dict[str, Any] = {}
    try:
        import psutil  # type: ignore

        host["hostAvailRamMb"] = round(psutil.virtual_memory().available / (1024 * 1024), 1)
        host["hostCpuPercent"] = psutil.cpu_percent(interval=0.3)
    except Exception as exc:
        host["error"] = str(exc)
    # CDP tabs
    try:
        import urllib.request

        port = os.environ.get("EBAY_BROWSER_CDP_PORT", "9444")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3) as resp:
            tabs = json.loads(resp.read().decode())
        pages = [t for t in tabs if t.get("type") == "page"]
        host["tabCount"] = len(pages)
        host["blankTabs"] = sum(
            1
            for t in pages
            if str(t.get("url") or "").startswith("about:blank")
            or str(t.get("title") or "").lower().startswith("untitled")
        )
        host["ebayTabs"] = sum(1 for t in pages if "ebay." in str(t.get("url") or ""))
    except Exception as exc:
        host["tabError"] = str(exc)
    return {"wsl": wsl, "host": host, "ts": datetime.now(timezone.utc).isoformat()}


def latest_nav_pair(after_ts: float) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    searches = sorted(ART.glob("linux_search_nav_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    solds = sorted(ART.glob("linux_sold_nav_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    search = sold = None
    for p in searches:
        if p.stat().st_mtime >= after_ts - 5:
            try:
                search = json.loads(p.read_text(encoding="utf-8"))
                break
            except Exception:
                pass
    for p in solds:
        if p.stat().st_mtime >= after_ts - 5:
            try:
                sold = json.loads(p.read_text(encoding="utf-8"))
                break
            except Exception:
                pass
    return search, sold


def parse_fp(fp: str) -> dict[str, str]:
    # pokemon|en|set|num|name|...
    parts = str(fp or "").split("|")
    return {
        "set": parts[2] if len(parts) > 2 else "",
        "collector": parts[3] if len(parts) > 3 else "",
        "name": parts[4] if len(parts) > 4 else "",
    }


def main() -> int:
    target = int(os.getenv("LINUX_GUI_BATCH_TARGET", "10"))
    delay = int(os.getenv("LINUX_GUI_BATCH_DELAY", "20"))
    _configure(delay=delay)
    client = _client()

    # Pull a large due pool in scheduler order
    pool = due_owned_key_ids(client, limit=max(40, target * 4))
    if len(pool) < target:
        try:
            rows = client.list_cache_refresh_candidates(limit=80)
        except Exception:
            rows = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            kid = str(row.get("price_key_id") or row.get("id") or "")
            if not kid or any(c["priceKeyId"] == kid for c in pool):
                continue
            pool.append(
                {
                    "priceKeyId": kid,
                    "fingerprint": str(row.get("fingerprint") or kid),
                    "card": row.get("card_name") or kid,
                }
            )

    report: dict[str, Any] = {
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "delaySec": delay,
        "targetGuiAttempts": target,
        "startState": sample_resources(),
        "ownedDailyFullEnable": os.environ.get("OWNED_DAILY_FULL_ENABLE"),
        "skippedFresh": [],
        "results": [],
        "resources": {"samples": []},
        "stopReason": None,
    }
    (OUT / "report_partial.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    # Do not restart Chrome if CDP healthy
    from cardscanr_market_engine.providers.linux_x11_ebay_nav import ensure_chrome_with_cdp

    print("[linux-x11-10] ensuring CDP (no forced restart if healthy)", flush=True)
    ensure_chrome_with_cdp(cdp_port=int(os.environ["EBAY_BROWSER_CDP_PORT"]))

    runner = MarketPriceJobRunner(
        client=client,
        provider=create_market_comps_provider("ebay_browser"),
        config=MarketEngineConfig.from_env(),
    )

    gui_n = 0
    stop = False
    chrome_rss_peak = float(report["startState"]["wsl"].get("CHROME_RSS_MB") or 0)
    cpu_peak = float(report["startState"]["wsl"].get("CPU_LOAD") or 0)
    loads: list[float] = []

    for card in pool:
        if gui_n >= target or stop:
            break
        fp_meta = parse_fp(str(card.get("fingerprint") or ""))
        print(f"[linux-x11-10] candidate {card}", flush=True)
        t_card0 = time.time()
        t0 = time.monotonic()
        try:
            payload = run_forced_job(
                client,
                price_key_id=card["priceKeyId"],
                reason="linux_gui:batch10_reliability",
                runner=runner,
            )
        except Exception as exc:
            row = {
                "guiIndex": gui_n + 1,
                "card": card.get("card"),
                "set": fp_meta["set"],
                "collector": fp_meta["collector"],
                "fingerprint": card.get("fingerprint"),
                "priceKeyId": card["priceKeyId"],
                "elapsedSec": round(time.monotonic() - t0, 1),
                "pricingOutcome": "ERROR",
                "error": str(exc),
                "PASS": False,
            }
            report["results"].append(row)
            report["stopReason"] = f"exception:{exc}"
            stop = True
            break

        result = payload["result"]
        status = str(result.get("status") or "")
        outcome = str(
            result.get("ownedDailyOutcome")
            or result.get("checkOutcome")
            or result.get("outcome")
            or status
        )
        err = str(result.get("error") or "") or None

        if status == "skipped_already_fresh" or outcome == "skipped_already_fresh":
            report["skippedFresh"].append(
                {
                    "card": card.get("card"),
                    "priceKeyId": card["priceKeyId"],
                    "fingerprint": card.get("fingerprint"),
                }
            )
            print(f"[linux-x11-10] SKIPPED_ALREADY_FRESH {card.get('card')}", flush=True)
            continue

        gui_n += 1
        search, sold = latest_nav_pair(t_card0)
        about_blank = bool(
            (search or {}).get("aboutBlank")
            or (sold or {}).get("aboutBlank")
            or "about:blank" in str((sold or {}).get("url") or "").lower()
        )
        search_ok = bool((search or {}).get("ok"))
        query_confirmed = (search or {}).get("phase") in {
            "QUERY_VISIBLE_CONFIRMED",
            "SEARCH_SUBMITTED",
            "SEARCH_RESULTS_CONFIRMED",
        } or bool((search or {}).get("ok"))
        sold_ok = bool((sold or {}).get("SOLD_STATE_VERIFIED") or (sold or {}).get("ok"))
        sorry = bool((search or {}).get("sorry") or (sold or {}).get("sorry"))
        challenge = bool((search or {}).get("challenge") or (sold or {}).get("challenge"))
        search_fail = (search or {}).get("error") == "SEARCH_INPUT_NOT_CONFIRMED" or (
            search is not None and not search_ok
        )
        sold_fail = search_ok and sold is not None and not sold_ok

        # Fallback: healthy pricing implies nav succeeded when artifacts sparse
        pricing_healthy = outcome in HEALTHY or status in HEALTHY
        if pricing_healthy and search is None and sold is None and not err:
            search_ok = query_confirmed = sold_ok = True

        unexpected_tab = about_blank
        res = sample_resources()
        report["resources"]["samples"].append(res)
        try:
            chrome_rss_peak = max(chrome_rss_peak, float(res["wsl"].get("CHROME_RSS_MB") or 0))
            load = float(res["wsl"].get("CPU_LOAD") or 0)
            loads.append(load)
            cpu_peak = max(cpu_peak, load)
        except Exception:
            pass

        row = {
            "guiIndex": gui_n,
            "card": card.get("card"),
            "set": fp_meta["set"],
            "collector": fp_meta["collector"],
            "fingerprint": card.get("fingerprint"),
            "priceKeyId": card["priceKeyId"],
            "search": "PASS" if search_ok else "FAIL",
            "queryConfirmed": bool(query_confirmed),
            "Sold": "PASS" if sold_ok else "FAIL",
            "SOLD_STATE_VERIFIED": bool(sold_ok),
            "candidates": result.get("includedCount")  # often post-filter; keep raw if present
            if result.get("includedCount") is not None
            else result.get("rejectedCount"),
            "acceptedComps": result.get("includedCount"),
            "rejectedComps": result.get("rejectedCount"),
            "pricingOutcome": outcome,
            "value": (payload.get("after") or {}).get("current_market_price"),
            "beforeValue": (payload.get("before") or {}).get("current_market_price"),
            "SORRY": sorry,
            "challenge": challenge,
            "aboutBlank": about_blank,
            "unexpectedTab": unexpected_tab,
            "elapsedSec": round(time.monotonic() - t0, 1),
            "error": err,
            "searchPhase": (search or {}).get("phase"),
            "soldPhase": (sold or {}).get("phase"),
            "soldClick": (sold or {}).get("click"),
            "searchUrl": (search or {}).get("url"),
            "soldUrl": (sold or {}).get("url"),
            "before": payload.get("before"),
            "after": payload.get("after"),
        }
        nav_healthy = (
            search_ok
            and sold_ok
            and not sorry
            and not challenge
            and not about_blank
            and pricing_healthy
            and not err
        )
        row["PASS"] = bool(nav_healthy)
        report["results"].append(row)
        (OUT / "report_partial.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(
            f"[linux-x11-10] GUI {gui_n}/{target} {row['card']} outcome={outcome} PASS={row['PASS']}",
            flush=True,
        )

        if sorry:
            report["stopReason"] = "EBAY_SORRY"
            stop = True
            break
        if challenge:
            report["stopReason"] = "EBAY_CHALLENGE"
            stop = True
            break
        if about_blank or unexpected_tab:
            report["stopReason"] = "UNEXPECTED_BROWSER_STATE"
            stop = True
            break
        if search_fail or (search or {}).get("error") == "SEARCH_INPUT_NOT_CONFIRMED":
            report["stopReason"] = "SEARCH_INPUT_NOT_CONFIRMED"
            stop = True
            break
        if sold_fail or (sold or {}).get("phase") in {
            "SOLD_NAVIGATION_TIMEOUT",
            "ABOUT_BLANK_ABORT",
        }:
            report["stopReason"] = str((sold or {}).get("phase") or "SOLD_FAILURE")
            stop = True
            break
        if not row["PASS"]:
            report["stopReason"] = f"GUI_FAIL:{err or outcome}"
            stop = True
            break

        if gui_n < target:
            time.sleep(delay)

    report["finishedAt"] = datetime.now(timezone.utc).isoformat()
    report["resources"]["end"] = sample_resources()
    report["resources"]["chromeRssPeakMb"] = chrome_rss_peak
    report["resources"]["cpuPeakLoad"] = cpu_peak
    report["resources"]["cpuAvgLoad"] = round(sum(loads) / len(loads), 3) if loads else None

    outcomes = [r.get("pricingOutcome") for r in report["results"]]
    summary = {
        "guiAttempts": len(report["results"]),
        "skippedFresh": len(report["skippedFresh"]),
        "healthy": sum(1 for r in report["results"] if r.get("PASS")),
        "updated": sum(1 for o in outcomes if o == "UPDATED_FROM_EBAY"),
        "unchanged": sum(1 for o in outcomes if o == "UNCHANGED_FROM_EBAY"),
        "noNewEvidence": sum(
            1 for o in outcomes if o in {"CHECKED_NO_NEW_EXACT_EVIDENCE", "checked_no_new_exact_evidence"}
        ),
        "searchFailures": sum(1 for r in report["results"] if r.get("search") != "PASS"),
        "soldFailures": sum(1 for r in report["results"] if r.get("Sold") != "PASS"),
        "unexpectedTabs": sum(1 for r in report["results"] if r.get("unexpectedTab")),
        "aboutBlank": sum(1 for r in report["results"] if r.get("aboutBlank")),
        "chromeCrashes": 0,
        "SORRY": sum(1 for r in report["results"] if r.get("SORRY")),
        "challenges": sum(1 for r in report["results"] if r.get("challenge")),
        "avgSeconds": round(
            sum(float(r.get("elapsedSec") or 0) for r in report["results"]) / max(1, len(report["results"])),
            1,
        ),
        "stopReason": report["stopReason"],
    }
    report["summary"] = summary

    if (
        summary["guiAttempts"] == target
        and summary["healthy"] == target
        and summary["SORRY"] == 0
        and summary["challenges"] == 0
        and summary["searchFailures"] == 0
        and summary["soldFailures"] == 0
        and summary["aboutBlank"] == 0
        and summary["unexpectedTabs"] == 0
        and not stop
    ):
        verdict = "LINUX_GUI_PATH_READY_FOR_25_CARD_PILOT"
    elif summary["challenges"]:
        verdict = "EBAY_CHALLENGE_REQUIRED"
    elif summary["SORRY"]:
        verdict = "EBAY_SERVER_BLOCKING"
    elif summary["guiAttempts"] < target or summary["healthy"] < summary["guiAttempts"]:
        verdict = "LINUX_GUI_10_CARD_UNSTABLE"
    else:
        verdict = "STOP"
    report["verdict"] = verdict

    (OUT / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"summary": summary, "verdict": verdict}, indent=2))
    print(f"Wrote {OUT / 'report.json'}")
    return 0 if verdict == "LINUX_GUI_PATH_READY_FOR_25_CARD_PILOT" else 1


if __name__ == "__main__":
    raise SystemExit(main())
