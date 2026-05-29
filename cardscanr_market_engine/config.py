from __future__ import annotations


from dataclasses import dataclass
import os
from pathlib import Path

# Load local Supabase config if env vars are not set
try:
    from .supabase_env_loader import load_supabase_env
    load_supabase_env()
except Exception:
    pass  # Safe: never fail if loader missing

ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = ROOT / "reports"
DEFAULT_EBAY_BROWSER_PROFILE_NAME = "cardscanr"
DEFAULT_EBAY_BROWSER_USER_DATA_DIR = ROOT / ".browser_profiles" / DEFAULT_EBAY_BROWSER_PROFILE_NAME


def _parse_positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be > 0")
    return value


def _parse_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "true" if default else "false").strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


def _parse_browser_user_data_dir() -> str:
    raw = os.getenv("EBAY_BROWSER_USER_DATA_DIR", "").strip()
    if not raw:
        return str(DEFAULT_EBAY_BROWSER_USER_DATA_DIR)
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    return str(path)


@dataclass(frozen=True)
class MarketEngineConfig:
    supabase_url: str
    supabase_service_role_key: str
    provider_name: str
    worker_concurrency: int
    poll_seconds: int
    max_jobs_per_run: int
    high_confidence_hours: int
    medium_confidence_hours: int
    low_confidence_hours: int
    no_comps_hours: int
    refresh_default_cooldown_hours: int
    refresh_high_value_cooldown_hours: int
    refresh_popular_cooldown_hours: int
    refresh_hot_card_cooldown_hours: int
    refresh_low_value_cooldown_hours: int
    ebay_browser_headless: bool
    ebay_browser_engine: str
    ebay_browser_channel: str
    ebay_browser_profile_name: str
    ebay_browser_max_results: int
    ebay_browser_timeout_seconds: int
    ebay_browser_cooldown_seconds: int
    ebay_browser_min_seconds_between_requests: int
    ebay_browser_user_data_dir: str | None
    provider_max_requests_per_minute: int
    provider_max_requests_per_day: int
    reports_dir: Path
    latest_report_path: Path
    runs_report_path: Path
    worker_id: str

    @classmethod
    def from_env(cls, *, require_supabase: bool = True) -> "MarketEngineConfig":
        supabase_url = os.getenv("SUPABASE_URL", "").strip()
        supabase_service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
        if require_supabase:
            if not supabase_url:
                raise ValueError("SUPABASE_URL is required")
            if not supabase_service_role_key:
                raise ValueError("SUPABASE_SERVICE_ROLE_KEY is required")
        provider_name = os.getenv("MARKET_LOOKUP_PROVIDER", "mock").strip().lower() or "mock"
        worker_id = os.getenv("MARKET_WORKER_ID", "market-price-worker")
        reports_dir = REPORTS_DIR
        return cls(
            supabase_url=supabase_url.rstrip("/"),
            supabase_service_role_key=supabase_service_role_key,
            provider_name=provider_name,
            worker_concurrency=_parse_positive_int("MARKET_WORKER_CONCURRENCY", 1),
            poll_seconds=_parse_positive_int("MARKET_WORKER_POLL_SECONDS", 5),
            max_jobs_per_run=_parse_positive_int("MARKET_WORKER_MAX_JOBS_PER_RUN", 5),
            high_confidence_hours=_parse_positive_int("MARKET_CACHE_HIGH_CONFIDENCE_HOURS", 24),
            medium_confidence_hours=_parse_positive_int("MARKET_CACHE_MEDIUM_CONFIDENCE_HOURS", 12),
            low_confidence_hours=_parse_positive_int("MARKET_CACHE_LOW_CONFIDENCE_HOURS", 6),
            no_comps_hours=_parse_positive_int("MARKET_CACHE_NO_COMPS_HOURS", 3),
            refresh_default_cooldown_hours=_parse_positive_int("MARKET_REFRESH_DEFAULT_COOLDOWN_HOURS", 6),
            refresh_high_value_cooldown_hours=_parse_positive_int("MARKET_REFRESH_HIGH_VALUE_COOLDOWN_HOURS", 4),
            refresh_popular_cooldown_hours=_parse_positive_int("MARKET_REFRESH_POPULAR_COOLDOWN_HOURS", 4),
            refresh_hot_card_cooldown_hours=_parse_positive_int("MARKET_REFRESH_HOT_CARD_COOLDOWN_HOURS", 2),
            refresh_low_value_cooldown_hours=_parse_positive_int("MARKET_REFRESH_LOW_VALUE_COOLDOWN_HOURS", 12),
            ebay_browser_headless=_parse_bool("EBAY_BROWSER_HEADLESS", True),
            ebay_browser_engine=os.getenv("EBAY_BROWSER_ENGINE", "chrome").strip().lower() or "chrome",
            ebay_browser_channel=os.getenv("EBAY_BROWSER_CHANNEL", "chrome").strip().lower() or "chrome",
            ebay_browser_profile_name=os.getenv(
                "EBAY_BROWSER_PROFILE_NAME", DEFAULT_EBAY_BROWSER_PROFILE_NAME
            ).strip() or DEFAULT_EBAY_BROWSER_PROFILE_NAME,
            ebay_browser_max_results=_parse_positive_int("EBAY_BROWSER_MAX_RESULTS", 30),
            ebay_browser_timeout_seconds=_parse_positive_int("EBAY_BROWSER_TIMEOUT_SECONDS", 45),
            ebay_browser_cooldown_seconds=_parse_positive_int("EBAY_BROWSER_COOLDOWN_SECONDS", 20),
            ebay_browser_min_seconds_between_requests=_parse_positive_int(
                "EBAY_BROWSER_MIN_SECONDS_BETWEEN_REQUESTS", 20
            ),
            ebay_browser_user_data_dir=_parse_browser_user_data_dir(),
            provider_max_requests_per_minute=_parse_positive_int("MARKET_PROVIDER_MAX_REQUESTS_PER_MINUTE", 2),
            provider_max_requests_per_day=_parse_positive_int("MARKET_PROVIDER_MAX_REQUESTS_PER_DAY", 200),
            reports_dir=reports_dir,
            latest_report_path=reports_dir / "market_price_worker_latest.json",
            runs_report_path=reports_dir / "market_price_worker_runs.jsonl",
            worker_id=worker_id.strip() or "market-price-worker",
        )
