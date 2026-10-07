#!/usr/bin/env python3
"""Phase 0/1 preflight for after-focus reliability run. No eBay navigation."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.config import MarketEngineConfig, supabase_secret_key_from_env
from cardscanr_market_engine.ebay_browser_work_gate import evaluate_ebay_browser_work_gate
from cardscanr_market_engine.live_navigation_attempt import count_search_submission_started
from cardscanr_market_engine.local_browser_runtime import ensure_xvfb, probe_pre_live_runtime
from cardscanr_market_engine.marketplace_ops_state import get_active_cooldown
from cardscanr_market_engine.providers.linux_x11_ebay_nav import ensure_chrome_with_cdp
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient
from cardscanr_market_engine.supabase_env_loader import load_supabase_env


def _listener_line() -> str:
    proc = subprocess.run(
        [
            "wsl",
            "-d",
            "Ubuntu",
            "--",
            "bash",
            "-lc",
            "ss -ltnp 2>/dev/null | grep ':9444' || netstat -ltnp 2>/dev/null | grep ':9444' || echo NO_LISTENER_LINE",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    return (proc.stdout or "").strip()


def _cdp_exposed(listener: str) -> bool:
    low = listener.lower()
    if "127.0.0.1:9444" in low or "::1:9444" in low:
        return False
    if "0.0.0.0:9444" in low or "[::]:9444" in low or "*:9444" in low:
        return True
    return False


def main() -> int:
    load_supabase_env()
    os.environ["OWNED_DAILY_FULL_ENABLE"] = "false"
    os.environ["EBAY_BROWSER_NAV_MODE"] = "linux_x11"
    os.environ["EBAY_BROWSER_ENABLED"] = "true"
    os.environ["EBAY_BROWSER_HEADLESS"] = "false"
    os.environ["EBAY_BROWSER_MAX_QUERY_ATTEMPTS"] = "1"
    os.environ.setdefault("EBAY_BROWSER_CDP_PORT", "9444")

    art = ROOT / "reports" / "artifacts" / "ebay_5_card_reliability_after_focus" / "bootstrap"
    before = ROOT / "reports" / "artifacts" / "ebay_5_card_reliability_after_focus" / "before"
    art.mkdir(parents=True, exist_ok=True)
    before.mkdir(parents=True, exist_ok=True)

    out: dict = {"provedAtUtc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}

    listener = _listener_line()
    exposed = _cdp_exposed(listener)
    host_probes = []
    for host in ("127.0.0.1",):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.5)
        try:
            rc = s.connect_ex((host, 9444))
            host_probes.append({"host": host, "connectEx": rc, "reachable": rc == 0})
        except OSError as exc:
            host_probes.append({"host": host, "error": str(exc)})
        finally:
            s.close()
    out["cdpLocalOnly"] = {
        "listenerLine": listener,
        "hostProbes": host_probes,
        "localOnly": not exposed,
        "exposedExternally": exposed,
    }

    gate = evaluate_ebay_browser_work_gate(market="AU", for_probe=False)
    cd = get_active_cooldown("AU")
    flag = ROOT / "reports" / "runtime" / "owned_daily_full_enable.flag"
    owned = flag.read_text(encoding="utf-8").strip() if flag.exists() else "MISSING"
    gd = gate.to_dict()
    phase0 = {
        "gate": gd,
        "activeAuChallenges": gate.active_challenge_count,
        "cooldown": None if cd is None else cd.to_dict(),
        "ownedDailyFlag": owned,
        "probeInFlight": gd.get("probeInFlight"),
        "stateIntegrityOk": gd.get("stateIntegrityOk"),
        "searchSubmissionStartedEventsBefore": count_search_submission_started(),
    }
    out["phase0"] = phase0
    (art / "phase0_gate.json").write_text(json.dumps(phase0, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "PHASE0": {
                    "allowed": gate.allowed,
                    "challenges": gate.active_challenge_count,
                    "cooldown": phase0["cooldown"],
                    "probeInFlight": phase0["probeInFlight"],
                    "stateIntegrityOk": phase0["stateIntegrityOk"],
                    "ownedDaily": owned,
                    "cdpLocalOnly": out["cdpLocalOnly"]["localOnly"],
                    "cdpListener": listener[:300],
                }
            },
            indent=2,
        )
    )

    if (
        not gate.allowed
        or gate.active_challenge_count != 0
        or owned != "false"
        or exposed
    ):
        print("PREFLIGHT_STOP")
        (art / "preflight_stop.json").write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
        return 2

    xv = ensure_xvfb()
    (art / "ensure_xvfb.json").write_text(json.dumps(xv, indent=2) + "\n", encoding="utf-8")
    ensure_chrome_with_cdp(cdp_port=9444, start_url="about:blank")

    listener2 = _listener_line()
    exposed2 = _cdp_exposed(listener2)
    out["cdpLocalOnlyAfterChrome"] = {
        "listenerLine": listener2,
        "localOnly": not exposed2,
        "exposedExternally": exposed2,
    }
    if exposed2:
        print("PREFLIGHT_STOP CDP exposed after chrome ensure")
        (art / "preflight_stop.json").write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
        return 2

    rt = probe_pre_live_runtime(cdp_port=9444)
    (art / "runtime_gate.json").write_text(json.dumps(rt, indent=2) + "\n", encoding="utf-8")
    needed = [
        "xvfbReady",
        "cdpReady",
        "x11NavigationRuntimeReady",
        "chromeWindowReady",
        "windowFocusReady",
        "keyboardInjectionReady",
        "preSubmitGuiReady",
        "searchToolSelfCheck",
        "soldToolSelfCheck",
    ]
    print(json.dumps({"RUNTIME": {k: rt.get(k) for k in needed + ["ebayTargets", "ready"]}}, indent=2))
    if rt.get("ebayTargets"):
        print("PREFLIGHT_STOP ebay targets")
        return 2
    if not rt.get("ready") or not all(rt.get(k) for k in needed):
        print("PREFLIGHT_STOP runtime", rt.get("reasonCodes"))
        return 2

    cfg = MarketEngineConfig.from_env()
    client = SupabaseMarketEngineClient(
        supabase_url=cfg.supabase_url,
        service_role_key=supabase_secret_key_from_env(),
    )
    payload = client.list_owned_market_pricing_targets(include_zero_owners=False)
    targets = payload.get("targets") or []
    qty = sum(int(t.get("total_owned_quantity") or 0) for t in targets)
    own = {"ownedTargets": len(targets), "ownershipQuantitySum": qty}
    (before / "ownership_before.json").write_text(json.dumps(own, indent=2) + "\n", encoding="utf-8")
    out["ownershipBefore"] = own
    out["runtime"] = {k: rt.get(k) for k in needed + ["ebayTargets", "ready", "reasonCodes"]}
    (art / "phase0_phase1_pass.json").write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print("OWNERSHIP_BEFORE", qty)
    print("PHASE0_PHASE1_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
