#!/usr/bin/env python3
"""Linux X11 owned-card work until natural 403 SORRY (same-moment control) or 25 GUI attempts.

owned_daily stays disabled. Stops immediately on first confirmed SORRY/403 for Andrew's
Windows same-moment control. Does not generate traffic solely to provoke a 403.
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
from cardscanr_market_engine.ebay_availability import browser_work_allowed, get_availability
from cardscanr_market_engine.finalize_deadline import FINALIZE_TIMEOUT_SAFE
from cardscanr_market_engine.job_runner import MarketPriceJobRunner
from cardscanr_market_engine.marketplace_ops_state import utc_now
from cardscanr_market_engine.owned_daily_outcomes import (
    ALTERNATE_EBAY_SURFACE,
    CHECKED_NO_NEW_EXACT_EVIDENCE,
    EBAY_LIVE_RESULTS,
    TEMPORARY_EBAY_SERVER_FAILURE,
    UNCHANGED_FROM_EBAY,
    UPDATED_FROM_EBAY,
)
from cardscanr_market_engine.providers.factory import create_market_comps_provider
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient
from cardscanr_market_engine.supabase_env_loader import load_supabase_env
from tools.desktop_ebay_e2e_pricing import due_owned_key_ids, run_forced_job
from tools.linux_x11_ebay_batch10 import latest_nav_pair, parse_fp, sample_resources
from tools.linux_x11_ebay_batch25 import HEALTHY, _is_local_gui_fail, _is_transient_ebay

OUT = ROOT / "reports" / "artifacts" / "linux_gui_same_moment_pilot"
OUT.mkdir(parents=True, exist_ok=True)
REF_RE = re.compile(r"0\.[0-9a-f.]+", re.I)


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


def _wsl_python(code: str, *, timeout: int = 45) -> str:
    import subprocess

    tmp = Path(r"D:\DevCache\Temp") / f"wsl_sm_{int(time.time() * 1000)}.py"
    tmp.write_bytes(code.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8"))
    proc = subprocess.run(
        [
            "wsl",
            "-d",
            "Ubuntu",
            "--",
            "bash",
            "-lc",
            f"source /tmp/cardscanr-xlib-venv/bin/activate; python /mnt/d/DevCache/Temp/{tmp.name}",
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )
    return (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")


def capture_sorry_http() -> dict[str, Any]:
    """Read-only CDP: main-document status + body ref from the loaded Error Page."""
    out = _wsl_python(
        r'''
import json, urllib.request
try:
 import websocket
except ImportError:
 import subprocess, sys
 subprocess.check_call([sys.executable,"-m","pip","install","-q","websocket-client"])
 import websocket
tabs=json.loads(urllib.request.urlopen("http://127.0.0.1:9444/json/list", timeout=5).read())
page=next(t for t in tabs if t.get("type")=="page")
ws=websocket.create_connection(page["webSocketDebuggerUrl"], timeout=15, suppress_origin=True)
msg_id=0
def call(method, params=None):
 global msg_id
 msg_id+=1
 ws.send(json.dumps({"id":msg_id,"method":method,"params":params or {}}))
 while True:
  data=json.loads(ws.recv())
  if data.get("id")==msg_id: return data
expr="(() => { const nav=performance.getEntriesByType('navigation')[0]; const body=(document.body&&document.body.innerText||''); const ref=(body.match(/0\\.[0-9a-f.]+/i)||[null])[0]; return {title:document.title,href:location.href,responseStatus:nav&&nav.responseStatus,nextHopProtocol:nav&&nav.nextHopProtocol,bodyHead:body.slice(0,500),errorRef:ref}; })()"
r=call("Runtime.evaluate",{"expression":expr,"returnByValue":True})
print(json.dumps(r.get("result",{}).get("result",{}).get("value",{})))
ws.close()
''',
        timeout=30,
    )
    try:
        s, e = out.find("{"), out.rfind("}")
        return json.loads(out[s : e + 1])
    except Exception:
        return {"raw": out[-800:]}


def extract_query_from_url(url: str | None) -> str | None:
    if not url:
        return None
    m = re.search(r"[?&]_nkw=([^&]+)", url)
    if not m:
        return None
    from urllib.parse import unquote_plus

    return unquote_plus(m.group(1))


def main() -> int:
    target = int(os.getenv("LINUX_GUI_BATCH_TARGET", "25"))
    target = max(1, min(25, target))
    delay = int(os.getenv("LINUX_GUI_BATCH_DELAY", "20"))
    _configure(delay=delay)

    now = utc_now()
    snap = get_availability(now=now)
    probe_allowed, probe_reason, snap = browser_work_allowed(for_probe=True, now=now)
    normal_allowed, normal_reason, _ = browser_work_allowed(for_probe=False, now=now)

    gate = {
        "nowUtc": now.isoformat().replace("+00:00", "Z"),
        "state": snap.state,
        "nextProbeAt": snap.next_probe_at.isoformat().replace("+00:00", "Z") if snap.next_probe_at else None,
        "probeInFlight": snap.probe_in_flight,
        "probeEligible": probe_allowed,
        "probeReason": probe_reason,
        "normalEligible": normal_allowed,
        "normalReason": normal_reason,
    }
    (OUT / "breaker_gate.json").write_text(json.dumps(gate, indent=2), encoding="utf-8")
    print(json.dumps({"BREAKER": gate}, indent=2), flush=True)

    if snap.state == "COOLDOWN" or (not probe_allowed and not normal_allowed):
        remaining = None
        if snap.next_probe_at and now < snap.next_probe_at:
            remaining = int((snap.next_probe_at - now).total_seconds())
        report = {
            "verdict": "EBAY_COOLDOWN_ACTIVE",
            "breaker": gate,
            "remainingCooldownSec": remaining,
            "ownedDailyEnabled": False,
        }
        (OUT / "FINAL_REPORT.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
        return 2

    client = _client()
    pool = due_owned_key_ids(client, limit=max(80, target * 5))
    if len(pool) < target:
        try:
            rows = client.list_cache_refresh_candidates(limit=120)
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

    from cardscanr_market_engine.providers.linux_x11_ebay_nav import ensure_chrome_with_cdp

    ensure_chrome_with_cdp(cdp_port=int(os.environ["EBAY_BROWSER_CDP_PORT"]))

    runner = MarketPriceJobRunner(
        client=client,
        provider=create_market_comps_provider("ebay_browser"),
        config=MarketEngineConfig.from_env(),
    )

    report: dict[str, Any] = {
        "startedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "breakerGate": gate,
        "targetGuiAttempts": target,
        "delaySec": delay,
        "ownedDailyFullEnable": False,
        "windowsBaseline": "WINDOWS_BASELINE_OK",
        "sameMomentControl": "WAITING_FOR_SAME_MOMENT_CONTROL",
        "skippedFresh": [],
        "results": [],
        "resources": {"start": sample_resources(), "samples": []},
        "stopReason": None,
        "actionRequired": None,
    }

    gui_n = 0
    stop = False
    idx = 0
    chrome_rss_peak = float(report["resources"]["start"]["wsl"].get("CHROME_RSS_MB") or 0)
    loads: list[float] = []

    while gui_n < target and not stop and idx < len(pool):
        card = pool[idx]
        idx += 1
        fp_meta = parse_fp(str(card.get("fingerprint") or ""))
        # Re-check breaker each attempt; use probe slot only while PROBE_REQUIRED.
        snap_now = get_availability()
        use_probe = snap_now.state == "PROBE_REQUIRED"
        if snap_now.state == "COOLDOWN":
            report["stopReason"] = "EBAY_AVAILABILITY_COOLDOWN"
            stop = True
            break
        if snap_now.state == "CHALLENGE_REQUIRED":
            report["stopReason"] = "EBAY_CHALLENGE_REQUIRED"
            stop = True
            break
        if use_probe:
            runner._ebay_probe_mode = True  # type: ignore[attr-defined]
        else:
            runner._ebay_probe_mode = False  # type: ignore[attr-defined]

        print(f"[same-moment] candidate {card.get('card')} probe={use_probe}", flush=True)
        t_card0 = time.time()
        t0 = time.monotonic()
        try:
            payload = run_forced_job(
                client,
                price_key_id=card["priceKeyId"],
                reason="linux_gui:same_moment_pilot",
                runner=runner,
            )
        except Exception as exc:
            msg = str(exc)
            if "failed_to_claim_job" in msg and "running" in msg.lower():
                print(f"[same-moment] skip reclaim-miss {card.get('card')}", flush=True)
                continue
            if "EBAY_AVAILABILITY" in msg.upper():
                report["stopReason"] = msg.split(":")[0]
                stop = True
                break
            report["results"].append(
                {
                    "guiIndex": gui_n + 1,
                    "card": card.get("card"),
                    "error": msg,
                    "PASS": False,
                    "elapsedSec": round(time.monotonic() - t0, 1),
                }
            )
            report["stopReason"] = f"exception:{exc}"
            stop = True
            break

        result = payload["result"]
        status = str(result.get("status") or "")
        outcome = str(result.get("ownedDailyOutcome") or result.get("outcome") or status)
        err = str(result.get("error") or "") or None

        if status == "skipped_already_fresh" or outcome == "skipped_already_fresh":
            report["skippedFresh"].append({"card": card.get("card"), "priceKeyId": card["priceKeyId"]})
            print(f"[same-moment] SKIPPED_ALREADY_FRESH {card.get('card')}", flush=True)
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
            "TEMPORARY_EBAY_SERVER_FAILURE",
        } or bool((search or {}).get("queryVisibleConfirmed")) or bool((search or {}).get("ok"))
        sold_ok = bool((sold or {}).get("SOLD_STATE_VERIFIED") or (sold or {}).get("ok"))
        sorry = bool((search or {}).get("sorry") or (sold or {}).get("sorry")) or _is_transient_ebay(
            outcome, False, err, search
        )
        challenge = bool((search or {}).get("challenge") or (sold or {}).get("challenge")) or (
            "challenge" in (err or "").lower()
        )
        if outcome == TEMPORARY_EBAY_SERVER_FAILURE:
            sorry = True
        finalize_timeout = FINALIZE_TIMEOUT_SAFE in (outcome or "") or FINALIZE_TIMEOUT_SAFE in (err or "")
        pricing_healthy = outcome in HEALTHY or status in HEALTHY
        if pricing_healthy and search is None and sold is None and not err:
            search_ok = query_confirmed = sold_ok = True

        res = sample_resources()
        report["resources"]["samples"].append(res)
        try:
            chrome_rss_peak = max(chrome_rss_peak, float(res["wsl"].get("CHROME_RSS_MB") or 0))
            loads.append(float(res["wsl"].get("CPU_LOAD") or 0))
        except Exception:
            pass

        url = str((search or {}).get("url") or (sold or {}).get("url") or "")
        query = extract_query_from_url(url) or str((search or {}).get("query") or "")
        row: dict[str, Any] = {
            "guiIndex": gui_n,
            "card": card.get("card"),
            "set": fp_meta["set"],
            "collector": fp_meta["collector"],
            "fingerprint": card.get("fingerprint"),
            "priceKeyId": card["priceKeyId"],
            "query": query,
            "search": "PASS" if (search_ok or (query_confirmed and sorry)) else "FAIL",
            "queryConfirmed": bool(query_confirmed),
            "Sold": "PASS" if sold_ok else ("n/a" if sorry else "FAIL"),
            "SOLD_STATE_VERIFIED": bool(sold_ok),
            "pricingOutcome": TEMPORARY_EBAY_SERVER_FAILURE if sorry else outcome,
            "value": (payload.get("after") or {}).get("current_market_price"),
            "beforeValue": (payload.get("before") or {}).get("current_market_price"),
            "SORRY": bool(sorry),
            "challenge": challenge,
            "finalizeTimeout": finalize_timeout,
            "aboutBlank": about_blank,
            "elapsedSec": round(time.monotonic() - t0, 1),
            "error": err,
            "searchUrl": (search or {}).get("url"),
            "soldUrl": (sold or {}).get("url"),
            "searchPhase": (search or {}).get("phase"),
            "soldPhase": (sold or {}).get("phase"),
        }
        local_fail = _is_local_gui_fail(
            search=search,
            sold=sold,
            about_blank=about_blank,
            challenge=challenge,
            query_confirmed=bool(query_confirmed),
            search_ok=search_ok,
            sold_ok=sold_ok,
            sorry=bool(sorry),
        )
        if finalize_timeout:
            local_fail = local_fail or FINALIZE_TIMEOUT_SAFE
        alternate = (
            outcome in {ALTERNATE_EBAY_SURFACE, EBAY_LIVE_RESULTS, "SOLD_UNAVAILABLE_ON_ALTERNATE_SURFACE"}
            or "ebaylive" in (url or "").lower()
            or "ebaylive" in str((search or {}).get("url") or "").lower()
            or str((search or {}).get("classification") or "")
            in {ALTERNATE_EBAY_SURFACE, EBAY_LIVE_RESULTS, "SOLD_UNAVAILABLE_ON_ALTERNATE_SURFACE"}
            or "ALTERNATE_EBAY_SURFACE" in str(err or "")
            or "EBAY_LIVE" in str(err or "").upper()
        )
        if alternate:
            local_fail = None
        row["alternateSurface"] = bool(alternate)
        row["localGuiFailure"] = local_fail
        row["PASS"] = bool(
            (search_ok or query_confirmed)
            and sold_ok
            and not sorry
            and not challenge
            and not about_blank
            and pricing_healthy
            and not finalize_timeout
            and not alternate
        )
        report["results"].append(row)
        (OUT / "report_partial.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(
            f"[same-moment] GUI {gui_n}/{target} {row['card']} outcome={row['pricingOutcome']} PASS={row['PASS']}",
            flush=True,
        )

        if challenge:
            report["stopReason"] = "EBAY_CHALLENGE_REQUIRED"
            report["verdict"] = "EBAY_CHALLENGE_REQUIRED"
            stop = True
            break

        if alternate and not sorry and not challenge:
            # eBay Live / alternate surface — stop attempt safely; do not trip SORRY breaker.
            report["stopReason"] = "EBAY_LIVE_RESULTS"
            report["verdict"] = "EBAY_LIVE_RESULTS"
            report["alternateEvidence"] = {
                "card": card.get("card"),
                "query": query,
                "url": url or (search or {}).get("url"),
                "classification": (search or {}).get("classification") or outcome,
            }
            stop = True
            break

        if local_fail and not sorry and not alternate:
            report["stopReason"] = f"LOCAL_GUI:{local_fail}"
            report["verdict"] = "LOCAL_GUI_FAILURE"
            stop = True
            break

        if sorry:
            # SAME-MOMENT TRIGGER — stop all Linux eBay work immediately.
            http_diag = capture_sorry_http()
            error_ref = http_diag.get("errorRef")
            if not error_ref and isinstance(http_diag.get("bodyHead"), str):
                m = REF_RE.search(http_diag["bodyHead"])
                error_ref = m.group(0) if m else None
            failing_query = query or extract_query_from_url(str(http_diag.get("href") or "")) or str(card.get("card"))
            evidence = {
                "timestampUtc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "card": card.get("card"),
                "priceKeyId": card["priceKeyId"],
                "fingerprint": card.get("fingerprint"),
                "exactQuery": failing_query,
                "resultUrl": http_diag.get("href") or url,
                "httpStatus": http_diag.get("responseStatus"),
                "title": http_diag.get("title"),
                "errorRef": error_ref,
                "bodyHead": http_diag.get("bodyHead"),
                "nextHopProtocol": http_diag.get("nextHopProtocol"),
                "guiIndex": gui_n,
            }
            report["stopReason"] = "NATURAL_HTTP_403_SORRY_SAME_MOMENT"
            report["verdict"] = "ACTION_REQUIRED_SAME_MOMENT_CONTROL"
            report["sorryEvidence"] = evidence
            report["actionRequired"] = {
                "type": "SAME_MOMENT_CONTROL",
                "exactQuery": failing_query,
                "replyOptions": ["WINDOWS_SAME_MOMENT_OK", "WINDOWS_SAME_MOMENT_SORRY"],
            }
            (OUT / "SORRY_EVENT.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
            (OUT / "ACTION_REQUIRED.json").write_text(
                json.dumps(report["actionRequired"], indent=2), encoding="utf-8"
            )
            stop = True
            break

        if not row["PASS"] and not alternate:
            report["stopReason"] = f"GUI_FAIL:{err or outcome}"
            report["verdict"] = "LOCAL_GUI_FAILURE"
            stop = True
            break

        if gui_n < target:
            time.sleep(delay)

    report["finishedAt"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    report["resources"]["end"] = sample_resources()
    report["resources"]["chromeRssPeakMb"] = chrome_rss_peak
    report["resources"]["cpuAvgLoad"] = round(sum(loads) / len(loads), 3) if loads else None
    report["breakerAfter"] = get_availability().to_dict()

    outcomes = [r.get("pricingOutcome") for r in report["results"]]
    summary = {
        "guiAttempts": len(report["results"]),
        "skippedFresh": len(report["skippedFresh"]),
        "healthy": sum(1 for r in report["results"] if r.get("PASS")),
        "updated": sum(1 for o in outcomes if o == UPDATED_FROM_EBAY),
        "unchanged": sum(1 for o in outcomes if o == UNCHANGED_FROM_EBAY),
        "noNewEvidence": sum(
            1 for o in outcomes if o in {CHECKED_NO_NEW_EXACT_EVIDENCE, "checked_no_new_exact_evidence"}
        ),
        "SORRY": sum(1 for r in report["results"] if r.get("SORRY")),
        "challenges": sum(1 for r in report["results"] if r.get("challenge")),
        "localGuiFailures": sum(1 for r in report["results"] if r.get("localGuiFailure")),
        "finalizeTimeouts": sum(1 for r in report["results"] if r.get("finalizeTimeout")),
        "avgSeconds": round(
            sum(float(r.get("elapsedSec") or 0) for r in report["results"]) / max(1, len(report["results"])),
            1,
        ),
        "stopReason": report.get("stopReason"),
    }
    report["summary"] = summary

    if report.get("verdict") == "ACTION_REQUIRED_SAME_MOMENT_CONTROL":
        pass
    elif report.get("verdict") in {"EBAY_CHALLENGE_REQUIRED", "LOCAL_GUI_FAILURE"}:
        pass
    elif summary["guiAttempts"] >= target and summary["SORRY"] == 0 and summary["challenges"] == 0:
        if summary["localGuiFailures"] == 0:
            report["verdict"] = "25_CARD_PILOT_HEALTHY_NO_403"
        else:
            report["verdict"] = "LOCAL_GUI_FAILURE"
    elif summary["SORRY"] == 0 and summary["challenges"] == 0 and not report.get("stopReason"):
        report["verdict"] = "INCOMPLETE_POOL"
    elif not report.get("verdict"):
        report["verdict"] = report.get("stopReason") or "STOP"

    report["ownedDailyEnabled"] = False
    (OUT / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (OUT / "FINAL_REPORT.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    if report.get("verdict") == "ACTION_REQUIRED_SAME_MOMENT_CONTROL":
        q = report["actionRequired"]["exactQuery"]
        print("\n" + "=" * 72, flush=True)
        print("ACTION REQUIRED — SAME-MOMENT CONTROL", flush=True)
        print("=" * 72, flush=True)
        print(f"\nLinux failing query:\n{q}\n", flush=True)
        print(
            "Please run that exact query NOW in normal Windows Chrome, click Sold listings, then reply:\n",
            flush=True,
        )
        print("WINDOWS_SAME_MOMENT_OK\n\nor\n\nWINDOWS_SAME_MOMENT_SORRY\n", flush=True)
        print("=" * 72, flush=True)
        print(json.dumps({"sorryEvidence": report.get("sorryEvidence"), "summary": summary}, indent=2), flush=True)
        return 3

    print(json.dumps({"summary": summary, "verdict": report.get("verdict"), "breakerAfter": report.get("breakerAfter")}, indent=2))
    return 0 if report.get("verdict") == "25_CARD_PILOT_HEALTHY_NO_403" else 1


if __name__ == "__main__":
    raise SystemExit(main())
