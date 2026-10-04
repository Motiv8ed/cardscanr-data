"""Admin/status snapshot for multi-region continuous pricing."""
from __future__ import annotations

from typing import Any

from .continuous_safety import ContinuousSafetyBudget
from .continuous_worker_policy import classify_continuous_gate
from .ebay_availability import peek_availability
from .market_dispatcher import dispatcher_status
from .owned_daily_enablement import owned_daily_full_enable
from .region_pricing_registry import CARDSCANR_REGIONS, region_definition


def region_status_row(region: str) -> dict[str, Any]:
    definition = region_definition(region)
    gate = classify_continuous_gate(market=definition.region)
    blocked = bool(gate.get("blocked") or not definition.browser_capable)
    snap = None if blocked else peek_availability(market=definition.region)
    budget = ContinuousSafetyBudget.from_env()
    worker_state = str(gate.get("workerState") or "WAITING")
    status = definition.status
    if blocked:
        status = "BLOCKED_NEEDS_PROVIDER"
        worker_state = "BLOCKED"
    elif gate.get("hardStop"):
        status = "HARD_STOP"
    elif worker_state == "COOLDOWN":
        status = "COOLDOWN"
    elif worker_state == "PROBE_REQUIRED":
        status = "PROBE_REQUIRED"
    elif definition.region == "AU" and owned_daily_full_enable():
        status = "CONTINUOUS"
    return {
        "region": definition.region,
        "enabled": False if blocked else bool(definition.worker_enable_default and status == "CONTINUOUS"),
        "provider": "NONE" if blocked else definition.provider,
        "currency": definition.currency,
        "workerState": worker_state,
        "availability": None if blocked else (snap.state if snap else None),
        "cooldownUntil": None if blocked else gate.get("cooldownUntil"),
        "nextProbeAt": None
        if blocked
        else (
            gate.get("nextProbeAt")
            or (snap.next_probe_at.isoformat() if snap and snap.next_probe_at else None)
        ),
        "submissions1h": 0 if blocked else budget.submissions_1h_for_market(definition.region),
        "submissions24h": 0 if blocked else budget.submissions_24h_for_market(definition.region),
        "transientFailures1h": 0 if blocked else budget.transients_1h(),
        "lastSuccessAt": None
        if blocked
        else (snap.last_healthy_at.isoformat() if snap and snap.last_healthy_at else None),
        "lastFailureAt": None
        if blocked
        else (snap.last_sorry_at.isoformat() if snap and snap.last_sorry_at else None),
        "hardStopReason": None if blocked else gate.get("hardStop"),
        "status": status,
        "browserCapable": False if blocked else definition.browser_capable,
        "marketplaceHost": definition.marketplace_host,
        "reason": definition.reason,
        "activeChallenges": 0 if blocked else gate.get("activeChallenges") or 0,
    }


def multi_region_status() -> dict[str, Any]:
    budget = ContinuousSafetyBudget.from_env()
    dispatch = dispatcher_status()
    return {
        "regions": {code: region_status_row(code) for code in CARDSCANR_REGIONS},
        "global": {
            "activeMarket": dispatch.get("activeMarket"),
            "activePriceKeyId": dispatch.get("activePriceKeyId"),
            "globalBrowserBusy": dispatch.get("globalBrowserBusy"),
            "globalSubmissions1h": budget.submissions_1h(),
            "globalSubmissions24h": budget.submissions_24h(),
            "globalConcurrency": dispatch.get("globalConcurrency"),
            "activeBrowserJobs": dispatch.get("activeBrowserJobs"),
        },
    }
