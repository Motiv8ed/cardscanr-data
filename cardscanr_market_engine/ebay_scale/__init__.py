"""Offline-first eBay AU scale helpers (cache, queue, set sweeps).

All behaviour is gated by env flags defaulting OFF; see ``ebay_scale.config``.
"""

from .config import EbayScaleFlags, ebay_scale_flags_from_env
from .cross_user_cache import (
    cross_user_cache_key,
    freshness_window_days,
    is_price_fresh_for_value,
    normalize_condition_band,
)
from .queue_priority import QueueWorkItem, cross_user_dedupe_work_items, score_queue_priority
from .set_sweep import SetSweepCatalogueCard, run_set_sweep
from .sold_results_url import build_au_sold_results_url, sold_url_mode_for_query
from .sold_row_html import SoldResultsRow, parse_sold_results_html

__all__ = [
    "EbayScaleFlags",
    "QueueWorkItem",
    "SetSweepCatalogueCard",
    "SoldResultsRow",
    "build_au_sold_results_url",
    "cross_user_cache_key",
    "cross_user_dedupe_work_items",
    "ebay_scale_flags_from_env",
    "freshness_window_days",
    "is_price_fresh_for_value",
    "normalize_condition_band",
    "parse_sold_results_html",
    "run_set_sweep",
    "score_queue_priority",
    "sold_url_mode_for_query",
]