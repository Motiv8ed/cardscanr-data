from __future__ import annotations

import os
from dataclasses import dataclass


def _parse_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "true" if default else "false").strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class EbayScaleFlags:
    """Feature flags for eBay AU cache / queue / set-sweep scale work (default OFF).

    Master switch enables runtime hooks in job_runner, query_builder, and scheduler.
    """

    enabled: bool = False
    cross_user_cache: bool = False
    value_first_queue: bool = False
    direct_sold_results_url: bool = False
    set_sweep_mode: bool = False
    set_sweep_exact_card_aud_threshold: float = 20.0

    @classmethod
    def from_env(cls) -> "EbayScaleFlags":
        master = _parse_bool("EBAY_SCALE_ENABLED", False)
        return cls(
            enabled=master,
            cross_user_cache=master and _parse_bool("EBAY_SCALE_CROSS_USER_CACHE", False),
            value_first_queue=master and _parse_bool("EBAY_SCALE_VALUE_FIRST_QUEUE", False),
            direct_sold_results_url=master and _parse_bool("EBAY_SCALE_DIRECT_SOLD_URL", False),
            set_sweep_mode=master and _parse_bool("EBAY_SCALE_SET_SWEEP", False),
            set_sweep_exact_card_aud_threshold=float(
                os.getenv("EBAY_SCALE_SET_SWEEP_EXACT_AUD", "20").strip() or "20"
            ),
        )

    def to_public_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "crossUserCache": self.cross_user_cache,
            "valueFirstQueue": self.value_first_queue,
            "directSoldResultsUrl": self.direct_sold_results_url,
            "setSweepMode": self.set_sweep_mode,
            "setSweepExactCardAudThreshold": self.set_sweep_exact_card_aud_threshold,
        }


def ebay_scale_flags_from_env() -> EbayScaleFlags:
    return EbayScaleFlags.from_env()


def ebay_scale_direct_sold_url_enabled() -> bool:
    return EbayScaleFlags.from_env().direct_sold_results_url


def ebay_scale_cross_user_cache_enabled() -> bool:
    return EbayScaleFlags.from_env().cross_user_cache


def ebay_scale_value_first_queue_enabled() -> bool:
    return EbayScaleFlags.from_env().value_first_queue


def ebay_scale_set_sweep_enabled() -> bool:
    return EbayScaleFlags.from_env().set_sweep_mode