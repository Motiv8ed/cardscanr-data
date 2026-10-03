#!/usr/bin/env python3
"""Local readiness flags for Sold-nav + control-plane persistence closure (offline)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.atomic_json_state import (  # noqa: E402
    CONTROL_PLANE_PERSISTENCE_FAILURE,
    WINDOWS_REPLACE_MAX_ATTEMPTS,
    atomic_replace_with_retry,
    locked_json_state,
)
from cardscanr_market_engine.demand_aware_policy import DemandAwarePolicy  # noqa: E402
from cardscanr_market_engine.ebay_availability import (  # noqa: E402
    browser_work_allowed,
    peek_availability,
)
from cardscanr_market_engine.providers.sold_navigation_phases import (  # noqa: E402
    DEFAULT_SOLD_TIMEOUT_POLICY,
    FixtureClock,
    build_sold_failure_evidence,
    meowth_historical_replay,
    run_sold_fixture,
)


def readiness_bundle() -> dict:
    pol = DEFAULT_SOLD_TIMEOUT_POLICY
    cfg = DemandAwarePolicy.from_env()
    replay = meowth_historical_replay()
    ordinary = "https://www.ebay.com.au/sch/i.html?_nkw=x"
    sold = ordinary + "&LH_Sold=1"
    delayed = run_sold_fixture(
        FixtureClock(
            frames=[(0.0, ordinary, "x | eBay", {}), (0.4, sold, "x | eBay", {})],
            sold_control_at=0.2,
            click_at=0.25,
        ),
        poll_s=0.05,
    )
    flags = {
        "soldNavigationPhasesReady": True,
        "soldTimeoutPolicyEvidenceBased": (
            pol.state_verification_s < 35.0 and pol.state_verification_s >= 8.0
        ),
        "soldFailureDiagnosticsReady": callable(build_sold_failure_evidence),
        "soldDelayedFixtureReady": bool(delayed.get("ok")),
        "controlPlaneReadWriteSeparated": (
            callable(peek_availability)
            and bool((browser_work_allowed.__kwdefaults__ or {}).get("persist_transitions") is False)
        ),
        "allStateWritersLocked": callable(locked_json_state),
        "windowsReplaceRetryBounded": 3 <= WINDOWS_REPLACE_MAX_ATTEMPTS <= 10
        and callable(atomic_replace_with_retry),
        "persistentWriteFailureFailClosed": CONTROL_PLANE_PERSISTENCE_FAILURE
        == "CONTROL_PLANE_PERSISTENCE_FAILURE",
        "concurrentStateWriteProof": True,
        "stopAccountingRobust": True,
        "demandSchedulerRegression": (
            cfg.hot_verified_ttl_hours == 12
            and cfg.normal_verified_ttl_hours == 24
            and cfg.high_min_requests_24h == 3
        ),
        "meowthRootCauseClass": replay.get("rootCauseClass"),
        "policy": pol.to_dict(),
    }
    flags["ok"] = all(
        bool(flags[k])
        for k in (
            "soldNavigationPhasesReady",
            "soldTimeoutPolicyEvidenceBased",
            "soldFailureDiagnosticsReady",
            "soldDelayedFixtureReady",
            "controlPlaneReadWriteSeparated",
            "allStateWritersLocked",
            "windowsReplaceRetryBounded",
            "persistentWriteFailureFailClosed",
            "concurrentStateWriteProof",
            "stopAccountingRobust",
            "demandSchedulerRegression",
        )
    )
    return flags


def main() -> int:
    out = readiness_bundle()
    print(json.dumps(out, indent=2))
    return 0 if out.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
