"""Single global browser dispatcher with per-market fairness.

Exactly one eBay/browser pricing job may run at a time. Market queues remain
independent. AU must not permanently monopolize the browser.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import REPORTS_DIR
from .region_pricing_registry import (
    GLOBAL_BROWSER_PRICING_CONCURRENCY,
    browser_ready_region_codes,
    is_region_dispatchable,
)
from .scheduler import utc_iso, utc_now


DISPATCHER_STATE_PATH = REPORTS_DIR / "runtime" / "market_dispatcher_state.json"


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


@dataclass
class MarketDispatcherState:
    last_served_at: dict[str, datetime] = field(default_factory=dict)
    starvation_credit: dict[str, float] = field(default_factory=dict)
    active_market: str | None = None
    active_price_key_id: str | None = None
    global_browser_busy: bool = False
    last_pick: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "lastServedAt": {k: utc_iso(v) for k, v in self.last_served_at.items()},
            "starvationCredit": {k: float(v) for k, v in self.starvation_credit.items()},
            "activeMarket": self.active_market,
            "activePriceKeyId": self.active_price_key_id,
            "globalBrowserBusy": self.global_browser_busy,
            "lastPick": self.last_pick,
            "globalBrowserPricingConcurrency": GLOBAL_BROWSER_PRICING_CONCURRENCY,
            "updatedAtUtc": utc_iso(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "MarketDispatcherState":
        data = payload or {}
        last_served: dict[str, datetime] = {}
        raw_served = data.get("lastServedAt") or data.get("last_served_at") or {}
        if isinstance(raw_served, dict):
            for key, value in raw_served.items():
                parsed = _parse_dt(value)
                if parsed is not None:
                    last_served[str(key).upper()] = parsed
        credits_raw = data.get("starvationCredit") or data.get("starvation_credit") or {}
        credits = {
            str(k).upper(): float(v)
            for k, v in (credits_raw.items() if isinstance(credits_raw, dict) else [])
        }
        return cls(
            last_served_at=last_served,
            starvation_credit=credits,
            active_market=str(data.get("activeMarket") or data.get("active_market") or "") or None,
            active_price_key_id=str(data.get("activePriceKeyId") or data.get("active_price_key_id") or "")
            or None,
            global_browser_busy=bool(data.get("globalBrowserBusy") or data.get("global_browser_busy")),
            last_pick=str(data.get("lastPick") or data.get("last_pick") or "") or None,
        )


def load_dispatcher_state(path: Path | None = None) -> MarketDispatcherState:
    target = path or DISPATCHER_STATE_PATH
    if not target.is_file():
        return MarketDispatcherState()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return MarketDispatcherState()
    if not isinstance(payload, dict):
        return MarketDispatcherState()
    return MarketDispatcherState.from_dict(payload)


def save_dispatcher_state(state: MarketDispatcherState, *, path: Path | None = None) -> Path:
    target = path or DISPATCHER_STATE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(state.to_dict(), indent=2) + "\n", encoding="utf-8")
    return target


def global_concurrency() -> int:
    raw = os.getenv("GLOBAL_BROWSER_PRICING_CONCURRENCY", "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            return GLOBAL_BROWSER_PRICING_CONCURRENCY
    return GLOBAL_BROWSER_PRICING_CONCURRENCY


def acquire_browser_slot(
    *,
    market: str,
    price_key_id: str | None = None,
    state: MarketDispatcherState | None = None,
    path: Path | None = None,
) -> tuple[bool, MarketDispatcherState]:
    """Serialize global browser work. Concurrency remains 1."""
    current = state or load_dispatcher_state(path)
    if global_concurrency() <= 1 and current.global_browser_busy:
        return False, current
    current.global_browser_busy = True
    current.active_market = str(market or "").upper() or None
    current.active_price_key_id = str(price_key_id or "") or None
    save_dispatcher_state(current, path=path)
    return True, current


def release_browser_slot(
    *,
    market: str,
    now: datetime | None = None,
    state: MarketDispatcherState | None = None,
    path: Path | None = None,
) -> MarketDispatcherState:
    current = state or load_dispatcher_state(path)
    stamp = now or utc_now()
    code = str(market or "").upper()
    current.global_browser_busy = False
    current.active_market = None
    current.active_price_key_id = None
    if code:
        current.last_served_at[code] = stamp
        current.last_pick = code
        current.starvation_credit[code] = 0.0
        for other, credit in list(current.starvation_credit.items()):
            if other != code:
                current.starvation_credit[other] = credit + 1.0
    save_dispatcher_state(current, path=path)
    return current


def pick_fair_market(
    due_by_market: Mapping[str, int],
    *,
    now: datetime | None = None,
    state: MarketDispatcherState | None = None,
    enabled_markets: Sequence[str] | None = None,
) -> str | None:
    """Choose one eligible market with due work using age + backlog + starvation credit.

    Demand volume cannot starve other enabled markets that have due work.
    """
    current = now or utc_now()
    dispatcher = state or MarketDispatcherState()
    enabled = {
        str(m).upper()
        for m in (enabled_markets if enabled_markets is not None else browser_ready_region_codes())
    }
    candidates: list[str] = []
    for market, count in due_by_market.items():
        code = str(market or "").upper()
        if code not in enabled:
            continue
        if not is_region_dispatchable(code):
            continue
        if int(count or 0) <= 0:
            continue
        candidates.append(code)
        dispatcher.starvation_credit.setdefault(code, 0.0)
    if not candidates:
        return None

    def _score(code: str) -> tuple[float, str]:
        last = dispatcher.last_served_at.get(code)
        age_hours = 24.0 if last is None else max(
            0.0, (current - last).total_seconds() / 3600.0
        )
        due = float(due_by_market.get(code) or due_by_market.get(code.lower()) or 0)
        # Soft backlog term so AU's large queue cannot monopolize forever.
        backlog = min(due, 20.0) ** 0.35
        credit = float(dispatcher.starvation_credit.get(code) or 0.0)
        return (age_hours * 4.0 + backlog + credit * 3.0, code)

    ranked = sorted(candidates, key=_score, reverse=True)
    return ranked[0]


def dispatcher_status(state: MarketDispatcherState | None = None) -> dict[str, Any]:
    current = state or load_dispatcher_state()
    payload = current.to_dict()
    payload["globalConcurrency"] = global_concurrency()
    payload["activeBrowserJobs"] = 1 if current.global_browser_busy else 0
    return payload
