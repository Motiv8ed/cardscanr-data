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
    snap = peek_availability(market=definition.region)
    budget = ContinuousSafetyBudget.from_env()
    status = definition.status
    if not definition.browser_capable:
        status = "BLOCKED_NEEDS_PROVIDER"
    elif gate.get("hardStop"):
        status = "HARD_STOP"
    elif gate.get("workerState") == "COOLDOWN":
        status = "COOLDOWN"
    elif definition.region == "AU" and owned_daily_full_enable():
        status = "CONTINUOUS"
    return {
        "region": definition.region,
        "enabled": bool(definition.worker_enable_default and status == "CONTINUOUS"),
        "provider": definition.provider,
        "currency": definition.currency,
        "workerState": gate.get("workerState"),
        "availability": snap.state,
        "cooldownUntil": gate.get("cooldownUntil"),
        "nextProbeAt": gate.get("nextProbeAt") or (snap.next_probe_at.isoformat() if snap.next_probe_at else None),
        "submissions1h": budget.submissions_1h_for_market(definition.region),
        "submissions24h": budget.submissions_24h_for_market(definition.region),
        "transientFailures1h": budget.transients_1h(),
        "lastSuccessAt": snap.last_healthy_at.isoformat() if snap.last_healthy_at else None,
        "lastFailureAt": snap.last_sorry_at.isoformat() if snap.last_sorry_at else None,
        "hardStopReason": gate.get("hardStop"),
        "status": status,
        "browserCapable": definition.browser_capable,
        "marketplaceHost": definition.marketplace_host,
        "reason": definition.reason,
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
