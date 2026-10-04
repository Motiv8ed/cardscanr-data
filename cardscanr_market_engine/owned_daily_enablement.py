"""Production owned_daily enablement flag + continuous AU env contract."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .config import REPORTS_DIR

FLAG_PATH = REPORTS_DIR / "runtime" / "owned_daily_full_enable.flag"
STATUS_PATH = REPORTS_DIR / "runtime" / "continuous_au_worker_status.json"
METRICS_PATH = REPORTS_DIR / "runtime" / "continuous_au_daily_metrics.jsonl"
BUDGET_LEDGER_PATH = REPORTS_DIR / "runtime" / "continuous_au_safety_ledger.json"

TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off", ""})


def read_owned_daily_flag(path: Path | None = None) -> bool | None:
    target = path or FLAG_PATH
    if not target.is_file():
        return None
    text = target.read_text(encoding="utf-8").strip().lower()
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    return None


def write_owned_daily_flag(enabled: bool, *, path: Path | None = None) -> Path:
    target = path or FLAG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("true\n" if enabled else "false\n", encoding="utf-8")
    return target


def owned_daily_full_enable(*, flag_path: Path | None = None) -> bool:
    """Flag file is authoritative when present; else OWNED_DAILY_FULL_ENABLE env."""
    flagged = read_owned_daily_flag(flag_path)
    if flagged is not None:
        return flagged
    return os.getenv("OWNED_DAILY_FULL_ENABLE", "false").strip().lower() in TRUE_VALUES


def apply_continuous_au_env() -> dict[str, str]:
    """Bind production continuous AU env. Does not start processes."""
    values = {
        "OWNED_DAILY_FULL_ENABLE": "true",
        "OWNED_DAILY_ALLOWED_MARKETS": "AU",
        "OWNED_DAILY_FULL_MAX_ENQUEUE": "1",
        "OWNED_DAILY_MAX_ENQUEUE": "1",
        "OWNED_DAILY_SYNC_KEYS": "false",
        "OWNED_DAILY_DRY_RUN": "false",
        "OWNED_DAILY_POLL_SECONDS": os.getenv("OWNED_DAILY_POLL_SECONDS", "90"),
        "HOT_VERIFIED_TTL_HOURS": "12",
        "NORMAL_VERIFIED_TTL_HOURS": "24",
        "HIGH_DEMAND_REQUESTS_24H": "3",
        "EBAY_BROWSER_MAX_QUERY_ATTEMPTS": "1",
        "EBAY_BROWSER_ENABLED": "true",
        "MARKET_LOOKUP_PROVIDER": "ebay_browser",
        "EBAY_BROWSER_NAV_MODE": "linux_x11",
        "MARKET_WORKER_CONCURRENCY": "1",
        "MARKET_WORKER_MAX_JOBS_PER_RUN": "1",
        "CONFIRM_LIVE_EBAY_WORKER": "true",
        "MAX_LIVE_SUBMISSIONS_PER_HOUR": "20",
        "MAX_LIVE_SUBMISSIONS_PER_DAY": "200",
        "MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES": "3",
        "MAX_TRANSIENT_MARKETPLACE_FAILURES_PER_HOUR": "5",
        "EBAY_TRANSIENT_FAILURE_COOLDOWN_MINUTES": os.getenv(
            "EBAY_TRANSIENT_FAILURE_COOLDOWN_MINUTES", "90"
        ),
        "EBAY_TRANSIENT_FAILURE_SECOND_COOLDOWN_MINUTES": os.getenv(
            "EBAY_TRANSIENT_FAILURE_SECOND_COOLDOWN_MINUTES", "180"
        ),
    }
    for key, value in values.items():
        os.environ[key] = value
    os.environ.pop("PRE_SUBMIT_ONLY", None)
    os.environ.pop("CARDSCANR_PRE_SUBMIT_ONLY", None)
    return values


def enablement_snapshot() -> dict[str, Any]:
    return {
        "flagPath": str(FLAG_PATH),
        "flagEnabled": owned_daily_full_enable(),
        "OWNED_DAILY_FULL_ENABLE": os.getenv("OWNED_DAILY_FULL_ENABLE"),
        "OWNED_DAILY_ALLOWED_MARKETS": os.getenv("OWNED_DAILY_ALLOWED_MARKETS"),
        "OWNED_DAILY_FULL_MAX_ENQUEUE": os.getenv("OWNED_DAILY_FULL_MAX_ENQUEUE"),
        "EBAY_BROWSER_NAV_MODE": os.getenv("EBAY_BROWSER_NAV_MODE"),
        "PRE_SUBMIT_ONLY": os.getenv("PRE_SUBMIT_ONLY"),
    }
