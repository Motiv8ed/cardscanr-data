from __future__ import annotations


from dataclasses import dataclass
import json
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


def supabase_secret_key_from_env() -> str:
    return (
        os.getenv("SUPABASE_SECRET_KEY", "").strip()
        or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    )


def _parse_positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be > 0")
    return value


def _parse_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "true" if default else "false").strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


def _parse_csv(name: str, default: str) -> tuple[str, ...]:
    raw = os.getenv(name, default)
    return tuple(item.strip().upper() for item in raw.split(",") if item.strip())


def _parse_json_object(name: str) -> dict[str, float]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    parsed: dict[str, float] = {}
    for key, rate in value.items():
        parsed[str(key).strip().upper()] = float(rate)
    return parsed


# Mirrors card_scanner_app ExchangeRateService offline fallback rates (last reviewed 2026-05-07).
_DEFAULT_RATES_TO_AUD: dict[str, float] = {
    "AUD": 1.0,
    "USD": 1.55,
    "EUR": 1.72,
    "GBP": 1.98,
    "NZD": 0.91,
    "JPY": 0.0104,
    "CAD": 1.13,
}


def default_currency_pair_rates() -> dict[str, float]:
    """Build FX pair rates for MARKET_CURRENCY_RATES_JSON when env is unset."""
    rates: dict[str, float] = {}
    for source, source_to_aud in _DEFAULT_RATES_TO_AUD.items():
        if source == "AUD":
            continue
        rates[f"{source}:AUD"] = source_to_aud
    return rates


def resolve_currency_rates(env_rates: dict[str, float]) -> dict[str, float]:
    if env_rates:
        return env_rates
    # Prefer shared ECB cache when healthy; static defaults remain only as a
    # non-production fallback for same-currency / bulk paths that do not mint
    # international estimates.
    try:
        from .international.fx_cache import load_production_pair_rates

        rates, _fx, _cache = load_production_pair_rates()
        if rates:
            return rates
    except Exception:
        pass
    return default_currency_pair_rates()


def resolve_currency_rate_source(env_source: str | None = None) -> str:
    explicit = str(env_source or "").strip()
    if explicit and explicit.lower() not in {"configured_static_rates", "static"}:
        return explicit
    try:
        from datetime import datetime, timezone

        from .international.fx_cache import evaluate_ecb_fx_freshness, load_fx_cache

        fx = evaluate_ecb_fx_freshness(cache=load_fx_cache(), now=datetime.now(timezone.utc))
        if fx.allows_conversion and str(fx.source).upper() == "ECB":
            return "ECB"
    except Exception:
        pass
    return explicit or "configured_static_rates"


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
    ebay_fallback_marketplaces: tuple[str, ...]
    currency_rates: dict[str, float]
    currency_rate_source: str
    enable_live_ebay_scheduler: bool
    confirm_live_ebay_scheduler: bool
    live_ebay_scheduler_markets: str
    live_ebay_scheduler_max_enqueues_per_run: int
    live_ebay_scheduler_max_keys_scanned_per_run: int
    live_ebay_scheduler_min_cooldown_hours: int
    live_ebay_scheduler_allow_force_refresh: bool
    live_ebay_scheduler_dry_run: bool
    live_ebay_scheduler_daily_enqueue_cap: int
    reports_dir: Path
    latest_report_path: Path
    runs_report_path: Path
    worker_id: str

    @classmethod
    def from_env(cls, *, require_supabase: bool = True) -> "MarketEngineConfig":
        supabase_url = os.getenv("SUPABASE_URL", "").strip()
        supabase_service_role_key = supabase_secret_key_from_env()
        if require_supabase:
            if not supabase_url:
                raise ValueError("SUPABASE_URL is required")
            if not supabase_service_role_key:
                raise ValueError("SUPABASE_SECRET_KEY is required")
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
            # Home-market only by default. Cross-marketplace comps must never silently
            # become another market's cached value. Opt in only via explicit env list.
            ebay_fallback_marketplaces=_parse_csv(
                "MARKET_EBAY_FALLBACK_MARKETPLACES",
                "",
            ),
            currency_rates=resolve_currency_rates(_parse_json_object("MARKET_CURRENCY_RATES_JSON")),
            currency_rate_source=resolve_currency_rate_source(
                os.getenv("MARKET_CURRENCY_RATE_SOURCE", "").strip() or None
            ),
            enable_live_ebay_scheduler=_parse_bool("ENABLE_LIVE_EBAY_SCHEDULER", False),
            confirm_live_ebay_scheduler=_parse_bool("CONFIRM_LIVE_EBAY_SCHEDULER", False),
            live_ebay_scheduler_markets=os.getenv("LIVE_EBAY_SCHEDULER_MARKETS", "AU").strip() or "AU",
            live_ebay_scheduler_max_enqueues_per_run=_parse_positive_int("LIVE_EBAY_SCHEDULER_MAX_ENQUEUES_PER_RUN", 2),
            live_ebay_scheduler_max_keys_scanned_per_run=_parse_positive_int("LIVE_EBAY_SCHEDULER_MAX_KEYS_SCANNED_PER_RUN", 25),
            live_ebay_scheduler_min_cooldown_hours=_parse_positive_int("LIVE_EBAY_SCHEDULER_MIN_COOLDOWN_HOURS", 6),
            live_ebay_scheduler_allow_force_refresh=_parse_bool("LIVE_EBAY_SCHEDULER_ALLOW_FORCE_REFRESH", False),
            live_ebay_scheduler_dry_run=_parse_bool("LIVE_EBAY_SCHEDULER_DRY_RUN", True),
            live_ebay_scheduler_daily_enqueue_cap=_parse_positive_int("LIVE_EBAY_SCHEDULER_DAILY_ENQUEUE_CAP", 20),
            reports_dir=reports_dir,
            latest_report_path=reports_dir / "market_price_worker_latest.json",
            runs_report_path=reports_dir / "market_price_worker_runs.jsonl",
            worker_id=worker_id.strip() or "market-price-worker",
        )
