from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .config import ebay_scale_value_first_queue_enabled
from .cross_user_cache import cross_user_cache_key, freshness_window_days, is_price_fresh_for_value, normalize_condition_band

# Lower score = higher priority (matches scheduler convention: smaller priority int wins).
PRIORITY_SELL_TRADE = 1
PRIORITY_STALE_HIGH_VALUE = 2
PRIORITY_WIDELY_OWNED = 3
PRIORITY_NEW_SET = 4
PRIORITY_SET_SWEEP = 5
PRIORITY_DEFAULT = 9


@dataclass(frozen=True)
class QueueWorkItem:
    price_key_id: str
    target: dict[str, Any]
    priority_score: int
    lane: str
    cache_key: str


def _bool_flag(target: dict[str, Any], *names: str) -> bool:
    for name in names:
        if bool(target.get(name)):
            return True
    return False


def score_queue_priority(
    *,
    target: dict[str, Any],
    now: datetime,
    prior_cache: dict[str, Any] | None = None,
) -> QueueWorkItem:
    """Value-first queue scoring (used when EBAY_SCALE_VALUE_FIRST_QUEUE is on)."""
    price_key_id = str(target.get("market_price_key_id") or target.get("price_key_id") or "").strip()
    fingerprint = str(target.get("fingerprint") or "").strip()
    language = str(target.get("language") or "en")
    condition = str(target.get("condition") or "raw")
    variant = str(target.get("variant") or "")
    catalogue_card_id = str(target.get("catalogue_card_id") or target.get("catalogueCardId") or "").strip()
    cache_key = cross_user_cache_key(
        catalogue_card_id=catalogue_card_id or None,
        fingerprint=fingerprint,
        language=language,
        condition_band=normalize_condition_band(condition, variant=variant),
        market_country=str(target.get("market_country") or "AU"),
    )
    try:
        aud_value = float((prior_cache or {}).get("current_market_price") or target.get("current_market_price") or 0)
    except (TypeError, ValueError):
        aud_value = 0.0

    last_raw = (prior_cache or {}).get("last_updated_at")
    last_priced = None
    if last_raw:
        try:
            last_priced = datetime.fromisoformat(str(last_raw).replace("Z", "+00:00"))
        except ValueError:
            last_priced = None
    stale_high_value = aud_value >= 50.0 and not is_price_fresh_for_value(
        last_priced_at=last_priced, aud_value=aud_value, now=now
    )

    if _bool_flag(target, "user_is_selling", "user_is_trading", "listed_for_sale", "listed_for_trade"):
        lane = "sell_trade"
        score = PRIORITY_SELL_TRADE
    elif stale_high_value:
        lane = "stale_high_value"
        score = PRIORITY_STALE_HIGH_VALUE
    elif int(target.get("owner_count") or target.get("total_owned_quantity") or 0) >= 10:
        lane = "widely_owned"
        score = PRIORITY_WIDELY_OWNED
    elif bool(target.get("is_new_set") or target.get("new_set")):
        lane = "new_set"
        score = PRIORITY_NEW_SET
    elif bool(target.get("eligible_for_set_sweep")):
        lane = "set_sweep"
        score = PRIORITY_SET_SWEEP
    else:
        lane = "default"
        score = PRIORITY_DEFAULT

    return QueueWorkItem(
        price_key_id=price_key_id,
        target=target,
        priority_score=score,
        lane=lane,
        cache_key=cache_key,
    )


def cross_user_dedupe_work_items(work_items: list[dict[str, Any]], *, now: datetime) -> list[dict[str, Any]]:
    """Keep one pending job per cross-user cache key (highest-priority lane wins)."""
    if not ebay_scale_value_first_queue_enabled():
        return work_items
    best_by_key: dict[str, tuple[int, dict[str, Any]]] = {}
    for item in work_items:
        raw_target = item.get("target")
        target = raw_target if isinstance(raw_target, dict) else item
        prior = item.get("prior_cache") if isinstance(item.get("prior_cache"), dict) else None
        if not target.get("market_price_key_id") and item.get("price_key_id"):
            target = {**target, "market_price_key_id": item.get("price_key_id")}
        scored = score_queue_priority(target=target, now=now, prior_cache=prior)
        existing = best_by_key.get(scored.cache_key)
        if existing is None or scored.priority_score < existing[0]:
            best_by_key[scored.cache_key] = (scored.priority_score, item)
    deduped = [pair[1] for pair in sorted(best_by_key.values(), key=lambda p: p[0])]
    return deduped


def estimate_cards_per_sold_search(*, set_card_count: int, items_per_page: int = 240) -> dict[str, Any]:
    """UNVERIFIED throughput helper for PR / offline replay."""
    pages = max(1, (set_card_count + items_per_page - 1) // items_per_page)
    return {
        "setCardCount": set_card_count,
        "itemsPerPage": items_per_page,
        "estimatedPagesPerSetSweep": pages,
        "estimatedCardsPricedPerSearch": min(set_card_count, items_per_page),
        "verified": False,
        "label": "UNVERIFIED",
    }