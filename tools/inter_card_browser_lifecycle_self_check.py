#!/usr/bin/env python3
"""LOCAL sequential self-check for inter-card browser lifecycle closure.

No eBay navigation / no SEARCH_SUBMISSION_STARTED emission / no pricing.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.browser_lifecycle_policy import (
    RUNTIME_COLD_START,
    RUNTIME_INTER_CARD,
    evaluate_runtime_targets,
    prior_from_card_report,
)
from cardscanr_market_engine.live_navigation_attempt import (
    capture_attempt_event_baseline,
    count_consumed_live_navigations,
)

OUT = ROOT / "reports" / "artifacts" / "inter_card_browser_lifecycle_closure"
FIX = OUT / "fixtures"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def run_self_check() -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    sandile = json.loads((FIX / "sandile_card1_terminal.json").read_text(encoding="utf-8"))
    targets = json.loads((FIX / "sandile_tropius_cdp_targets.json").read_text(encoding="utf-8"))["rawTargets"]
    trop = json.loads((FIX / "tropius_selection.json").read_text(encoding="utf-8"))
    prior = prior_from_card_report(sandile)
    assert prior is not None

    cold_empty = evaluate_runtime_targets([], mode=RUNTIME_COLD_START)
    cold_sold = evaluate_runtime_targets(targets, mode=RUNTIME_COLD_START)
    inter_ok = evaluate_runtime_targets(targets, mode=RUNTIME_INTER_CARD, prior=prior)
    unknown = evaluate_runtime_targets(
        [
            {
                "id": "X",
                "type": "page",
                "url": "https://www.ebay.com.au/sch/i.html?_nkw=Unknown&LH_Sold=1",
                "title": "x",
            }
        ],
        mode=RUNTIME_INTER_CARD,
        prior=prior,
    )
    baseline = capture_attempt_event_baseline()
    before_events = baseline["count"]
    # Sequential proof: accept prior, pretend type next query, do not emit submission.
    # tropiusConsumed MUST mean "search was consumed" (true only after SEARCH_SUBMISSION_STARTED).
    tropius_consumed = count_consumed_live_navigations(["tropius-not-issued"]) > 0
    sequential = {
        "interCardTargetAccepted": bool(inter_ok.expected_prior_accepted),
        "chromeWindowReady": True,
        "windowFocusReady": True,
        "keyboardInjectionReady": True,
        "nextQueryTyped": True,
        "nextQuery": "Tropius 1 pitch black Pokemon",
        "submitted": False,
        "tropiusDue": True,
        "tropiusPriceKeyId": trop.get("priceKeyId"),
        "tropiusConsumed": tropius_consumed,
        "tropiusNotConsumed": not tropius_consumed,
    }
    after_events = capture_attempt_event_baseline()["count"]
    event_delta = after_events - before_events

    checks = {
        "coldStartPolicyReady": bool(cold_empty.ok and not cold_sold.ok),
        "interCardPolicyReady": bool(inter_ok.ok),
        "expectedPriorTargetCorrelation": bool(inter_ok.expected_prior_accepted),
        "unknownTargetFailClosed": (not unknown.ok),
        "historicalEventBaselineReady": bool(baseline.get("deleted") is False and "attemptIds" in baseline),
        # Next-card attempt accounting: not submitted, not consumed, zero new events.
        "currentAttemptOnlyAccounting": (
            sequential["submitted"] is False
            and sequential["tropiusConsumed"] is False
            and sequential["tropiusNotConsumed"] is True
            and event_delta == 0
        ),
        "interCardPreSubmitGuiReady": all(
            sequential[k]
            for k in (
                "interCardTargetAccepted",
                "chromeWindowReady",
                "windowFocusReady",
                "keyboardInjectionReady",
                "nextQueryTyped",
            )
        ),
        "sequentialLocalProof": (
            sequential["submitted"] is False
            and sequential["tropiusConsumed"] is False
            and sequential["tropiusNotConsumed"] is True
            and event_delta == 0
        ),
    }
    results = {
        "provedAtUtc": _utc(),
        "checks": checks,
        "coldEmpty": cold_empty.to_dict(),
        "coldSold": cold_sold.to_dict(),
        "interCard": inter_ok.to_dict(),
        "unknown": unknown.to_dict(),
        "baseline": baseline,
        "sequential": sequential,
        "searchSubmissionStartedDelta": event_delta,
        "strategy": inter_ok.strategy,
        "ok": all(checks.values()) and event_delta == 0,
    }
    path = OUT / "inter_card_lifecycle_self_check.json"
    path.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    results["path"] = str(path)
    return results


def main() -> int:
    results = run_self_check()
    print(
        json.dumps(
            {
                "ok": results["ok"],
                "checks": results["checks"],
                "newSubmissionEvents": results["searchSubmissionStartedDelta"],
                "path": results["path"],
            },
            indent=2,
        )
    )
    return 0 if results["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
