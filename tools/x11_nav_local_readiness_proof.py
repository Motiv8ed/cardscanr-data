#!/usr/bin/env python3
"""Local readiness proof for X11 nav + runtime. ZERO eBay navigations."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.ebay_browser_work_gate import evaluate_ebay_browser_work_gate
from cardscanr_market_engine.local_browser_runtime import ensure_xvfb, probe_pre_live_runtime
from cardscanr_market_engine.providers.linux_x11_ebay_nav import ensure_chrome_with_cdp
from cardscanr_market_engine.x11_navigation_runtime import ensure_x11_navigation_runtime, probe_x11_navigation_runtime


def main() -> int:
    art = ROOT / "reports" / "artifacts" / "x11_nav_reliability_harness_closure"
    art.mkdir(parents=True, exist_ok=True)
    gate = evaluate_ebay_browser_work_gate(market="AU", for_probe=False)
    xv = ensure_xvfb()
    try:
        ensure_chrome_with_cdp(cdp_port=9444, start_url="about:blank")
        chrome_ok = True
        chrome_err = None
    except Exception as exc:
        chrome_ok = False
        chrome_err = f"{type(exc).__name__}:{exc}"
    ensured = ensure_x11_navigation_runtime(cdp_port=9444, require_cdp=True)
    pre = probe_pre_live_runtime(cdp_port=9444)
    x11 = probe_x11_navigation_runtime(cdp_port=9444, require_cdp=True, run_self_check=True)
    owned = (ROOT / "reports" / "runtime" / "owned_daily_full_enable.flag").read_text(encoding="utf-8").strip()
    payload = {
        "provedAtUtc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "marketplaceGateReady": bool(gate.allowed),
        "gate": gate.to_dict(),
        "xvfb": xv,
        "chromeBootstrapOk": chrome_ok,
        "chromeError": chrome_err,
        "ensureX11": ensured,
        "preLive": pre,
        "x11Probe": x11.to_dict(),
        "summary": {
            "marketplaceGateReady": bool(gate.allowed),
            "xvfbReady": bool(pre.get("xvfbReady")),
            "cdpReady": bool(pre.get("cdpReady")),
            "ebayTargets": list(pre.get("ebayTargets") or []),
            "x11NavigationRuntimeReady": bool(pre.get("x11NavigationRuntimeReady")),
            "searchToolSelfCheck": bool(pre.get("searchToolSelfCheck")),
            "soldToolSelfCheck": bool(pre.get("soldToolSelfCheck")),
            "liveNavigationStartedCount": 0,
            "ownedDaily": owned,
        },
    }
    path = art / "local_readiness_proof.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload["summary"], indent=2))
    print(f"WROTE {path}")
    ok = all(
        [
            payload["summary"]["marketplaceGateReady"],
            payload["summary"]["xvfbReady"],
            payload["summary"]["cdpReady"],
            payload["summary"]["ebayTargets"] == [],
            payload["summary"]["x11NavigationRuntimeReady"],
            payload["summary"]["searchToolSelfCheck"],
            payload["summary"]["soldToolSelfCheck"],
            payload["summary"]["ownedDaily"] == "false",
        ]
    )
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
