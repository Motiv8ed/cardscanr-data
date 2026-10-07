#!/usr/bin/env python3
"""One due-card recovery probe with read-only CDP Network main-document capture.

X11 remains the only navigation/control path. CDP is observe-only.
Redacts cookies/authorization headers.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.ebay_availability import (
    begin_probe,
    browser_work_allowed,
    get_availability,
)
from cardscanr_market_engine.config import MarketEngineConfig, supabase_secret_key_from_env
from cardscanr_market_engine.job_runner import MarketPriceJobRunner
from cardscanr_market_engine.providers.factory import create_market_comps_provider
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient
from cardscanr_market_engine.supabase_env_loader import load_supabase_env
from tools.desktop_ebay_e2e_pricing import due_owned_key_ids, run_forced_job
from tools.linux_x11_ebay_batch10 import sample_resources

OUT = ROOT / "reports" / "artifacts" / "ebay_sorry_root_cause"
OUT.mkdir(parents=True, exist_ok=True)

SENSITIVE_HEADER_KEYS = {
    "cookie",
    "set-cookie",
    "authorization",
    "proxy-authorization",
    "x-ebay-c-enduserctx",
}


def _client() -> SupabaseMarketEngineClient:
    load_supabase_env()
    return SupabaseMarketEngineClient(
        supabase_url=os.environ["SUPABASE_URL"].rstrip("/"),
        service_role_key=supabase_secret_key_from_env(),
    )


def _configure() -> None:
    os.environ["EBAY_BROWSER_NAV_MODE"] = "linux_x11"
    os.environ["EBAY_BROWSER_ENABLED"] = "true"
    os.environ["MARKET_LOOKUP_PROVIDER"] = "ebay_browser"
    os.environ["EBAY_BROWSER_HEADLESS"] = "false"
    os.environ["EBAY_BROWSER_MAX_QUERY_ATTEMPTS"] = "1"
    os.environ["EBAY_BROWSER_REUSE_CONTEXT"] = "true"
    os.environ["EBAY_BROWSER_CDP_PORT"] = os.environ.get("EBAY_BROWSER_CDP_PORT", "9444")
    os.environ["OWNED_DAILY_FULL_ENABLE"] = "false"


def _wsl_python(code: str, *, timeout: int = 120) -> str:
    import subprocess

    tmp = Path(r"D:\DevCache\Temp") / f"wsl_probe_{int(time.time()*1000)}.py"
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
    return (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")


def clear_sorry_to_homepage() -> dict[str, Any]:
    """X11: leave stale SORRY tab via omnibox home (not a search)."""
    code = r'''
import json, os, subprocess, time
os.environ["DISPLAY"]=":99"
PREFIX=os.path.expanduser("~/.local/cardscanr-gui")
os.environ["PATH"]=f"{PREFIX}/root/usr/bin:"+os.environ.get("PATH","")
lib=f"{PREFIX}/root/usr/lib/x86_64-linux-gnu:{PREFIX}/root/lib/x86_64-linux-gnu"
os.environ["LD_LIBRARY_PATH"]=lib+((":"+os.environ["LD_LIBRARY_PATH"]) if os.environ.get("LD_LIBRARY_PATH") else "")
def sh(c):
    subprocess.check_call(c, shell=True)
import urllib.request
tabs=json.loads(urllib.request.urlopen("http://127.0.0.1:9444/json/list", timeout=5).read())
pages=[t for t in tabs if t.get("type")=="page"]
before=[{"title":t.get("title"),"url":(t.get("url") or "")[:160]} for t in pages]
# focus chrome window on :99
ids=subprocess.check_output("xdotool search --onlyvisible --class google-chrome || true", shell=True, text=True).strip().split()
if ids:
    sh(f"xdotool windowactivate --sync {ids[0]}")
    time.sleep(0.4)
sh("xdotool key --clearmodifiers ctrl+l")
time.sleep(0.25)
sh("xdotool key --clearmodifiers ctrl+a")
time.sleep(0.1)
sh("xdotool type --clearmodifiers --delay 8 -- 'https://www.ebay.com.au/'")
time.sleep(0.2)
sh("xdotool key --clearmodifiers Return")
for i in range(15):
    time.sleep(1)
    tabs=json.loads(urllib.request.urlopen("http://127.0.0.1:9444/json/list", timeout=5).read())
    pages=[t for t in tabs if t.get("type")=="page"]
    titles=" | ".join((t.get("title") or "") for t in pages)
    if "Error Page" not in titles and any("ebay" in (t.get("title") or "").lower() for t in pages):
        break
after=[{"title":t.get("title"),"url":(t.get("url") or "")[:160]} for t in pages]
print(json.dumps({"before":before,"after":after,"waitSec":i+1}))
'''
    out = _wsl_python(code, timeout=90)
    start = out.find("{")
    end = out.rfind("}")
    if start >= 0 and end > start:
        return json.loads(out[start : end + 1])
    return {"raw": out[-1000:]}


def start_network_sniffer(stop_event: threading.Event, sink: list[dict[str, Any]]) -> threading.Thread:
    code = r'''
import json, time, urllib.request
try:
    import websocket
except ImportError:
    import subprocess, sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "websocket-client"])
    import websocket

SENSITIVE = {"cookie","set-cookie","authorization","proxy-authorization","x-ebay-c-enduserctx"}
tabs=json.loads(urllib.request.urlopen("http://127.0.0.1:9444/json/list", timeout=5).read())
page=next(t for t in tabs if t.get("type")=="page")
ws=websocket.create_connection(page["webSocketDebuggerUrl"], timeout=15, suppress_origin=True)
msg_id=0
def call(method, params=None):
    global msg_id
    msg_id += 1
    ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
    while True:
        data=json.loads(ws.recv())
        if data.get("id")==msg_id:
            return data

call("Network.enable", {"maxPostDataSize": 0})
call("Network.setCacheDisabled", {"cacheDisabled": False})
deadline=time.time()+240
events=[]
while time.time()<deadline:
    ws.settimeout(1.0)
    try:
        raw=ws.recv()
    except Exception:
        # stop file?
        import os
        if os.path.exists("/tmp/cardscanr_net_stop"):
            break
        continue
    data=json.loads(raw)
    method=data.get("method")
    params=data.get("params") or {}
    if method=="Network.responseReceived":
        resp=params.get("response") or {}
        headers={k:v for k,v in (resp.get("headers") or {}).items() if str(k).lower() not in SENSITIVE}
        typ=params.get("type")
        url=resp.get("url") or ""
        if typ=="Document" or "/sch/" in url or "ebay." in url:
            events.append({
                "ts": time.time(),
                "type": typ,
                "url": url,
                "status": resp.get("status"),
                "statusText": resp.get("statusText"),
                "mimeType": resp.get("mimeType"),
                "protocol": resp.get("protocol"),
                "remoteIPAddress": resp.get("remoteIPAddress"),
                "fromDiskCache": resp.get("fromDiskCache"),
                "fromServiceWorker": resp.get("fromServiceWorker"),
                "headers": headers,
            })
            open("/tmp/cardscanr_net_events.json","w").write(json.dumps(events, indent=2))
ws.close()
print(json.dumps({"captured": len(events)}))
'''
    def runner() -> None:
        import subprocess
        from pathlib import Path as P

        P("/tmp").mkdir(exist_ok=True)
        # stop flag cleared via wsl
        subprocess.run(
            ["wsl", "-d", "Ubuntu", "--", "bash", "-lc", "rm -f /tmp/cardscanr_net_stop /tmp/cardscanr_net_events.json"],
            capture_output=True,
            text=True,
        )
        tmp = P(r"D:\DevCache\Temp\wsl_net_sniff.py")
        tmp.write_bytes(code.replace("\r\n", "\n").encode("utf-8"))
        proc = subprocess.Popen(
            [
                "wsl",
                "-d",
                "Ubuntu",
                "--",
                "bash",
                "-lc",
                f"source /tmp/cardscanr-xlib-venv/bin/activate; python /mnt/d/DevCache/Temp/{tmp.name}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        while not stop_event.is_set() and proc.poll() is None:
            time.sleep(0.5)
        subprocess.run(
            ["wsl", "-d", "Ubuntu", "--", "bash", "-lc", "touch /tmp/cardscanr_net_stop"],
            capture_output=True,
            text=True,
        )
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        # pull events
        pull = subprocess.run(
            ["wsl", "-d", "Ubuntu", "--", "bash", "-lc", "cat /tmp/cardscanr_net_events.json 2>/dev/null || echo []"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            sink.extend(json.loads(pull.stdout or "[]"))
        except Exception:
            sink.append({"parseError": (pull.stdout or "")[:500]})

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    time.sleep(1.5)
    return t


def main() -> int:
    _configure()
    now = datetime.now(timezone.utc)
    snap = get_availability()
    allowed, reason, snap = browser_work_allowed(for_probe=True)
    report: dict[str, Any] = {
        "startedAt": now.isoformat().replace("+00:00", "Z"),
        "breakerBefore": snap.to_dict(),
        "eligible": allowed and snap.state == "PROBE_REQUIRED",
        "reason": reason,
        "ownedDaily": False,
    }
    if not (allowed and snap.state == "PROBE_REQUIRED"):
        report["verdict"] = "EBAY_COOLDOWN_ACTIVE" if snap.state == "COOLDOWN" else "MORE_DIAGNOSTIC_EVIDENCE_REQUIRED"
        report["message"] = "Probe not eligible; no new eBay request."
        (OUT / "phase_h_probe.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
        return 0

    # Pre-probe: leave stale SORRY page (X11 omnibox home only).
    report["homepageRecover"] = clear_sorry_to_homepage()
    # Do NOT begin_probe here — MarketPriceJobRunner begins the single probe slot.

    events: list[dict[str, Any]] = []
    stop = threading.Event()
    sniffer = start_network_sniffer(stop, events)

    client = _client()
    pool = due_owned_key_ids(client, limit=50)
    runner = MarketPriceJobRunner(
        client=client,
        provider=create_market_comps_provider("ebay_browser"),
        config=MarketEngineConfig.from_env(),
    )
    runner._ebay_probe_mode = True  # type: ignore[attr-defined]

    probe_row = None
    for card in pool:
        payload = run_forced_job(
            client,
            price_key_id=card["priceKeyId"],
            reason="ebay_root_cause:recovery_probe",
            runner=runner,
        )
        result = payload["result"]
        status = str(result.get("status") or "")
        outcome = str(result.get("ownedDailyOutcome") or status)
        if status == "skipped_already_fresh" or outcome == "skipped_already_fresh":
            continue
        probe_row = {
            "card": card.get("card"),
            "priceKeyId": card["priceKeyId"],
            "fingerprint": card.get("fingerprint"),
            "outcome": outcome,
            "status": status,
            "error": result.get("error"),
            "before": payload.get("before"),
            "after": payload.get("after"),
            "durationSec": payload.get("durationSec"),
            "SORRY": "TEMPORARY_EBAY_SERVER_FAILURE" in outcome or "sorry" in str(result.get("error") or "").lower(),
            "challenge": "CHALLENGE" in outcome.upper(),
        }
        break

    stop.set()
    sniffer.join(timeout=15)
    time.sleep(1)
    # re-read events from wsl file
    import subprocess

    pull = subprocess.run(
        ["wsl", "-d", "Ubuntu", "--", "bash", "-lc", "cat /tmp/cardscanr_net_events.json 2>/dev/null || echo []"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        events = json.loads(pull.stdout or "[]")
    except Exception:
        pass

    # post page state via performance API
    post = _wsl_python(
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
expr="(() => { const nav=performance.getEntriesByType('navigation')[0]; const body=(document.body&&document.body.innerText||'').slice(0,600); const ref=(body.match(/0\\.[0-9a-f.]+/i)||[null])[0]; return {title:document.title,href:location.href,responseStatus:nav&&nav.responseStatus,nextHopProtocol:nav&&nav.nextHopProtocol,remoteIP:null,bodyHead:body.slice(0,400),errorRef:ref}; })()"
r=call("Runtime.evaluate",{"expression":expr,"returnByValue":True})
print(json.dumps(r.get("result",{}).get("result",{}).get("value",{})))
ws.close()
''',
        timeout=30,
    )
    post_json = None
    try:
        s = post.find("{")
        e = post.rfind("}")
        post_json = json.loads(post[s : e + 1])
    except Exception:
        post_json = {"raw": post[-800:]}

    report["probe"] = probe_row
    report["networkEvents"] = events
    report["pageAfter"] = post_json
    report["resources"] = sample_resources()
    report["breakerAfter"] = get_availability().to_dict()

    if not probe_row:
        report["verdict"] = "MORE_DIAGNOSTIC_EVIDENCE_REQUIRED"
    elif probe_row.get("challenge"):
        report["verdict"] = "EBAY_CHALLENGE_REQUIRED"
    elif probe_row.get("SORRY"):
        report["verdict"] = "EBAY_RECOVERY_PROBE_FAILED"
    elif probe_row.get("outcome") in {
        "UPDATED_FROM_EBAY",
        "UNCHANGED_FROM_EBAY",
        "CHECKED_NO_NEW_EXACT_EVIDENCE",
        "completed",
        "checked_no_new_exact_evidence",
    }:
        report["verdict"] = "ROOT_CAUSE_NARROWED"
    else:
        report["verdict"] = "MORE_DIAGNOSTIC_EVIDENCE_REQUIRED"

    (OUT / "phase_h_probe.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"verdict": report["verdict"], "probe": probe_row, "networkDocCount": len(events), "pageAfter": post_json}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
