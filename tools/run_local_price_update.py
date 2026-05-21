#!/usr/bin/env python3
"""Run a local-first small-batch CardScanR price cache update."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CATALOG_CONFIG_PATH = ROOT / "data" / "catalog_config.json"
EN_SETS_PATH = ROOT / "public" / "v1" / "catalog" / "pokemon" / "en" / "sets.json"
DEFAULT_STATE_PATH = ROOT / "data" / "scheduled_price_refresh_state.json"
DEFAULT_RESULT_PATH = ROOT / "logs" / "local_price_update_last_result.json"
DEFAULT_DIAGNOSTICS_PATH = ROOT / "public" / "v1" / "diagnostics" / "latest-build.json"
PHASE_PREFIX = "CARDSCANR_PHASE "
PROVIDER_POKEWALLET = "pokewallet"
PROVIDER_POKEMON_TCG_API = "pokemon_tcg_api"
PROVIDER_TCGDEX = "tcgdex"
PROVIDER_OTHER = "other"
KNOWN_REQUEST_PROVIDERS = {
    PROVIDER_POKEWALLET,
    PROVIDER_POKEMON_TCG_API,
    PROVIDER_TCGDEX,
    PROVIDER_OTHER,
}


def load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def write_json_atomic(path: Path, payload: dict) -> None:
    ensure_parent_dir(path)
    tmp_path = path.with_name(f"{path.name}.tmp")
    with open(tmp_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")
    os.replace(tmp_path, path)


def emit_phase(phase: str) -> None:
    print(f"{PHASE_PREFIX}{phase}")


def read_text(path: Path) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def load_config() -> dict:
    if not CATALOG_CONFIG_PATH.exists():
        return {}
    return load_json(CATALOG_CONFIG_PATH)


def resolve_state_path(config: dict) -> Path:
    raw = str(config.get("scheduledCurrentPriceStatePath") or "data/scheduled_price_refresh_state.json").strip()
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    return path


def load_state(path: Path) -> dict:
    default_state = {
        "schemaVersion": "1.0.0",
        "enCurrentPriceCursor": 0,
        "lastUpdatedAtUtc": None,
        "lastBatchSetIds": [],
        "lastProcessedSetIds": [],
        "lastStopReason": None,
        "lastRateLimited": False,
        "requestLedger": [],
    }
    if not path.exists():
        return default_state
    payload = load_json(path)
    merged = {**default_state, **payload}
    try:
        merged["enCurrentPriceCursor"] = max(0, int(merged.get("enCurrentPriceCursor", 0)))
    except (TypeError, ValueError):
        merged["enCurrentPriceCursor"] = 0
    if not isinstance(merged.get("lastBatchSetIds"), list):
        merged["lastBatchSetIds"] = []
    if not isinstance(merged.get("lastProcessedSetIds"), list):
        merged["lastProcessedSetIds"] = []
    if not isinstance(merged.get("requestLedger"), list):
        merged["requestLedger"] = []
    merged["lastRateLimited"] = bool(merged.get("lastRateLimited", False))
    return merged


def get_int_env_with_alias(primary: str, aliases: list[str], default: int) -> int:
    for name in [primary] + aliases:
        raw = os.getenv(name, "").strip()
        if not raw:
            continue
        try:
            parsed = int(raw)
        except ValueError:
            continue
        if parsed > 0:
            return parsed
    return default


def normalize_request_provider(value: object) -> str:
    provider = str(value or "").strip().lower()
    aliases = {
        "pokemon-tcg-api": PROVIDER_POKEMON_TCG_API,
        "pokemon_tcg": PROVIDER_POKEMON_TCG_API,
        "pokemontcg": PROVIDER_POKEMON_TCG_API,
        "pokewallet_api": PROVIDER_POKEWALLET,
    }
    provider = aliases.get(provider, provider)
    return provider if provider in KNOWN_REQUEST_PROVIDERS else PROVIDER_OTHER


def resolve_budget_settings(target_hourly: int | None = None, target_daily: int | None = None) -> dict:
    provider_hour = get_int_env_with_alias(
        "CARDSCANR_PROVIDER_PLAN_REQUESTS_PER_HOUR",
        ["POKEWALLET_PROVIDER_PLAN_REQUESTS_PER_HOUR"],
        100,
    )
    provider_day = get_int_env_with_alias(
        "CARDSCANR_PROVIDER_PLAN_REQUESTS_PER_DAY",
        ["POKEWALLET_PROVIDER_PLAN_REQUESTS_PER_DAY"],
        1000,
    )
    max_hour = get_int_env_with_alias(
        "CARDSCANR_MAX_REQUESTS_PER_HOUR",
        ["CARDSCANR_MAX_PRICE_REQUESTS_PER_HOUR", "POKEWALLET_MAX_REQUESTS_PER_HOUR"],
        90,
    )
    max_day = get_int_env_with_alias(
        "CARDSCANR_MAX_REQUESTS_PER_DAY",
        ["CARDSCANR_MAX_PRICE_REQUESTS_PER_DAY", "POKEWALLET_MAX_REQUESTS_PER_DAY"],
        950,
    )
    safety = get_int_env_with_alias(
        "CARDSCANR_REQUEST_SAFETY_BUFFER",
        ["CARDSCANR_PRICE_REQUEST_SAFETY_BUFFER", "POKEWALLET_REQUEST_SAFETY_BUFFER"],
        10,
    )

    if target_hourly is not None and target_hourly > 0:
        max_hour = int(target_hourly)
    if target_daily is not None and target_daily > 0:
        max_day = int(target_daily)

    provider_hour_safe = max(1, provider_hour - safety)
    provider_day_safe = max(1, provider_day - safety)
    hourly_target = max(1, min(max_hour, provider_hour_safe))
    daily_target = max(1, min(max_day, provider_day_safe))
    return {
        "providerPlanHour": provider_hour,
        "providerPlanDay": provider_day,
        "hourlyTarget": hourly_target,
        "dailyTarget": daily_target,
        "safetyBuffer": safety,
        "authoritativeEnv": [
            "CARDSCANR_MAX_REQUESTS_PER_HOUR",
            "CARDSCANR_MAX_REQUESTS_PER_DAY",
            "CARDSCANR_REQUEST_SAFETY_BUFFER",
        ],
        "aliasEnv": [
            "CARDSCANR_MAX_PRICE_REQUESTS_PER_HOUR",
            "CARDSCANR_MAX_PRICE_REQUESTS_PER_DAY",
            "CARDSCANR_PRICE_REQUEST_SAFETY_BUFFER",
        ],
    }


def resolve_single_provider_budget_settings(
    provider: str,
    *,
    plan_hour_env: str,
    plan_day_env: str,
    max_hour_env: str,
    max_day_env: str,
    safety_env: str,
    plan_hour_aliases: list[str] | None = None,
    plan_day_aliases: list[str] | None = None,
    max_hour_aliases: list[str] | None = None,
    max_day_aliases: list[str] | None = None,
    safety_aliases: list[str] | None = None,
    default_plan_hour: int,
    default_plan_day: int,
    default_max_hour: int,
    default_max_day: int,
    default_safety: int,
    target_hourly: int | None = None,
    target_daily: int | None = None,
) -> dict:
    provider_hour = get_int_env_with_alias(plan_hour_env, plan_hour_aliases or [], default_plan_hour)
    provider_day = get_int_env_with_alias(plan_day_env, plan_day_aliases or [], default_plan_day)
    max_hour = get_int_env_with_alias(max_hour_env, max_hour_aliases or [], default_max_hour)
    max_day = get_int_env_with_alias(max_day_env, max_day_aliases or [], default_max_day)
    safety = get_int_env_with_alias(safety_env, safety_aliases or [], default_safety)

    if target_hourly is not None and target_hourly > 0:
        max_hour = int(target_hourly)
    if target_daily is not None and target_daily > 0:
        max_day = int(target_daily)

    hourly_target = max(1, min(max_hour, max(1, provider_hour - safety)))
    daily_target = max(1, min(max_day, max(1, provider_day - safety)))
    return {
        "provider": normalize_request_provider(provider),
        "providerPlanHour": provider_hour,
        "providerPlanDay": provider_day,
        "hourlyTarget": hourly_target,
        "dailyTarget": daily_target,
        "safetyBuffer": safety,
        "authoritativeEnv": [max_hour_env, max_day_env, safety_env],
        "aliasEnv": list((max_hour_aliases or []) + (max_day_aliases or []) + (safety_aliases or [])),
    }


def resolve_provider_budget_settings(
    target_hourly: int | None = None,
    target_daily: int | None = None,
) -> dict[str, dict]:
    return {
        PROVIDER_POKEWALLET: resolve_single_provider_budget_settings(
            PROVIDER_POKEWALLET,
            plan_hour_env="CARDSCANR_POKEWALLET_PLAN_REQUESTS_PER_HOUR",
            plan_day_env="CARDSCANR_POKEWALLET_PLAN_REQUESTS_PER_DAY",
            max_hour_env="CARDSCANR_POKEWALLET_MAX_REQUESTS_PER_HOUR",
            max_day_env="CARDSCANR_POKEWALLET_MAX_REQUESTS_PER_DAY",
            safety_env="CARDSCANR_POKEWALLET_REQUEST_SAFETY_BUFFER",
            plan_hour_aliases=["CARDSCANR_PROVIDER_PLAN_REQUESTS_PER_HOUR", "POKEWALLET_PROVIDER_PLAN_REQUESTS_PER_HOUR"],
            plan_day_aliases=["CARDSCANR_PROVIDER_PLAN_REQUESTS_PER_DAY", "POKEWALLET_PROVIDER_PLAN_REQUESTS_PER_DAY"],
            max_hour_aliases=["CARDSCANR_MAX_REQUESTS_PER_HOUR", "CARDSCANR_MAX_PRICE_REQUESTS_PER_HOUR", "POKEWALLET_MAX_REQUESTS_PER_HOUR"],
            max_day_aliases=["CARDSCANR_MAX_REQUESTS_PER_DAY", "CARDSCANR_MAX_PRICE_REQUESTS_PER_DAY", "POKEWALLET_MAX_REQUESTS_PER_DAY"],
            safety_aliases=["CARDSCANR_REQUEST_SAFETY_BUFFER", "CARDSCANR_PRICE_REQUEST_SAFETY_BUFFER", "POKEWALLET_REQUEST_SAFETY_BUFFER"],
            default_plan_hour=100,
            default_plan_day=1000,
            default_max_hour=90,
            default_max_day=990,
            default_safety=10,
            target_hourly=target_hourly,
            target_daily=target_daily,
        ),
        PROVIDER_POKEMON_TCG_API: resolve_single_provider_budget_settings(
            PROVIDER_POKEMON_TCG_API,
            plan_hour_env="CARDSCANR_POKEMON_TCG_PLAN_REQUESTS_PER_HOUR",
            plan_day_env="CARDSCANR_POKEMON_TCG_PLAN_REQUESTS_PER_DAY",
            max_hour_env="CARDSCANR_POKEMON_TCG_MAX_REQUESTS_PER_HOUR",
            max_day_env="CARDSCANR_POKEMON_TCG_MAX_REQUESTS_PER_DAY",
            safety_env="CARDSCANR_POKEMON_TCG_REQUEST_SAFETY_BUFFER",
            default_plan_hour=100,
            default_plan_day=1000,
            default_max_hour=90,
            default_max_day=900,
            default_safety=10,
        ),
    }


def calculate_cycle_request_cap(state: dict, budget: dict) -> tuple[int, int, int]:
    usage = estimate_budget_usage(state, datetime.now(timezone.utc))
    safety = max(0, int(budget.get("safetyBuffer", 0) or 0))
    hourly_limit = max(0, int(budget.get("hourlyTarget") or 0))
    daily_limit = max(0, int(budget.get("dailyTarget") or 0))
    hourly_remaining = max(0, hourly_limit - int(usage["hourlyUsed"]) - safety)
    daily_remaining = max(0, daily_limit - int(usage["dailyUsed"]) - safety)
    return min(hourly_remaining, daily_remaining), hourly_remaining, daily_remaining


def calculate_provider_cycle_request_cap(state: dict, budget: dict, provider: str) -> tuple[int, int, int]:
    usage = estimate_budget_usage(state, datetime.now(timezone.utc), provider=provider)
    hourly_limit = max(0, int(budget.get("hourlyTarget") or 0))
    daily_limit = max(0, int(budget.get("dailyTarget") or 0))
    hourly_remaining = max(0, hourly_limit - int(usage["hourlyUsed"]))
    daily_remaining = max(0, daily_limit - int(usage["dailyUsed"]))
    return min(hourly_remaining, daily_remaining), hourly_remaining, daily_remaining


def build_current_price_builder_env(
    base_env: dict[str, str],
    batch_size: int,
    request_cap: int,
    set_id: str | None = None,
    provider_request_caps: dict[str, int] | None = None,
) -> dict[str, str]:
    env = dict(base_env)
    env["CARDSCANR_CURRENT_PRICE_BATCH_SIZE"] = str(max(1, batch_size))
    env["CARDSCANR_CURRENT_PRICE_REQUEST_CAP"] = str(max(0, request_cap))
    provider_request_caps = provider_request_caps or {}
    if PROVIDER_POKEWALLET in provider_request_caps:
        env["CARDSCANR_POKEWALLET_CURRENT_PRICE_REQUEST_CAP"] = str(
            max(0, int(provider_request_caps.get(PROVIDER_POKEWALLET) or 0))
        )
    if PROVIDER_POKEMON_TCG_API in provider_request_caps:
        env["CARDSCANR_POKEMON_TCG_CURRENT_PRICE_REQUEST_CAP"] = str(
            max(0, int(provider_request_caps.get(PROVIDER_POKEMON_TCG_API) or 0))
        )
    if set_id:
        env["CARDSCANR_CURRENT_PRICE_SET_ID"] = str(set_id).strip()
    return env


def should_start_current_price_cycle(request_cap: int) -> bool:
    return int(request_cap) > 0


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def provider_count_from_ledger_item(item: dict, provider: str) -> int:
    requested_provider = normalize_request_provider(provider)
    counts = item.get("providerRequestCounts")
    if isinstance(counts, dict):
        try:
            return max(0, int(counts.get(requested_provider) or 0))
        except (TypeError, ValueError):
            return 0
    legacy_provider = normalize_request_provider(item.get("provider") or item.get("requestProvider"))
    if legacy_provider == requested_provider:
        try:
            return max(0, int(item.get("requests") or 0))
        except (TypeError, ValueError):
            return 0
    return 0


def estimate_budget_usage(state: dict, now: datetime, provider: str | None = None) -> dict:
    hourly_cutoff = now.timestamp() - 3600
    rolling_day_cutoff = now.timestamp() - 86400
    hourly_used = 0
    daily_used = 0
    provider = normalize_request_provider(provider) if provider else None
    for item in state.get("requestLedger", []):
        if not isinstance(item, dict):
            continue
        ts = parse_utc(str(item.get("timestampUtc") or ""))
        if ts is None:
            continue
        if provider:
            requests = provider_count_from_ledger_item(item, provider)
        else:
            requests = 0
            try:
                requests = max(0, int(item.get("requests") or 0))
            except (TypeError, ValueError):
                requests = 0
        ts_seconds = ts.timestamp()
        if ts_seconds >= hourly_cutoff:
            hourly_used += requests
        if ts_seconds >= rolling_day_cutoff:
            daily_used += requests
    return {"hourlyUsed": hourly_used, "dailyUsed": daily_used}


def next_budget_reset_seconds(state: dict, now: datetime, window_seconds: int, provider: str | None = None) -> int:
    timestamps: list[float] = []
    provider = normalize_request_provider(provider) if provider else None
    for item in state.get("requestLedger", []):
        if not isinstance(item, dict):
            continue
        if provider and provider_count_from_ledger_item(item, provider) <= 0:
            continue
        ts = parse_utc(str(item.get("timestampUtc") or ""))
        if ts is None:
            continue
        if ts.timestamp() >= now.timestamp() - window_seconds:
            timestamps.append(ts.timestamp())
    if not timestamps:
        return 0
    oldest_in_window = min(timestamps)
    wake_at = oldest_in_window + window_seconds
    return max(1, int(wake_at - now.timestamp()) + 1)


def build_budget_snapshot(state: dict, budget: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    usage = estimate_budget_usage(state, now)
    hourly_remaining = max(0, int(budget["hourlyTarget"]) - int(usage["hourlyUsed"]))
    daily_remaining = max(0, int(budget["dailyTarget"]) - int(usage["dailyUsed"]))
    return {
        "hourlyUsed": int(usage["hourlyUsed"]),
        "dailyUsed": int(usage["dailyUsed"]),
        "hourlyRemaining": int(hourly_remaining),
        "dailyRemaining": int(daily_remaining),
        "hourlySleepSeconds": next_budget_reset_seconds(state, now, 3600),
        "dailySleepSeconds": next_budget_reset_seconds(state, now, 86400),
    }


def build_provider_budget_snapshot(
    state: dict,
    provider: str,
    budget: dict,
    now: datetime | None = None,
) -> dict:
    provider = normalize_request_provider(provider)
    now = now or datetime.now(timezone.utc)
    usage = estimate_budget_usage(state, now, provider=provider)
    hourly_remaining = max(0, int(budget["hourlyTarget"]) - int(usage["hourlyUsed"]))
    daily_remaining = max(0, int(budget["dailyTarget"]) - int(usage["dailyUsed"]))
    return {
        "provider": provider,
        "hourlyUsed": int(usage["hourlyUsed"]),
        "dailyUsed": int(usage["dailyUsed"]),
        "hourlyRemaining": int(hourly_remaining),
        "dailyRemaining": int(daily_remaining),
        "hourlySleepSeconds": next_budget_reset_seconds(state, now, 3600, provider=provider),
        "dailySleepSeconds": next_budget_reset_seconds(state, now, 86400, provider=provider),
    }


def build_all_provider_budget_snapshots(
    state: dict,
    budgets: dict[str, dict],
    now: datetime | None = None,
) -> dict[str, dict]:
    now = now or datetime.now(timezone.utc)
    return {
        normalize_request_provider(provider): build_provider_budget_snapshot(state, provider, budget, now)
        for provider, budget in budgets.items()
    }


def normalize_provider_request_counts(provider_request_counts: dict | None, requests_used: int = 0) -> dict[str, int]:
    normalized = {provider: 0 for provider in sorted(KNOWN_REQUEST_PROVIDERS)}
    if isinstance(provider_request_counts, dict):
        for provider, count in provider_request_counts.items():
            key = normalize_request_provider(provider)
            try:
                normalized[key] = int(normalized.get(key, 0)) + max(0, int(count or 0))
            except (TypeError, ValueError):
                continue
    elif requests_used:
        normalized[PROVIDER_OTHER] = max(0, int(requests_used))
    return {provider: count for provider, count in normalized.items() if count > 0}


def append_request_ledger(
    state: dict,
    requests_used: int,
    status: str,
    now_iso: str,
    provider_request_counts: dict | None = None,
) -> dict:
    counts = normalize_provider_request_counts(provider_request_counts, requests_used=requests_used)
    total_requests = sum(counts.values()) if counts else max(0, int(requests_used))
    ledger = list(state.get("requestLedger", []))
    ledger.append(
        {
            "timestampUtc": now_iso,
            "requests": max(0, int(total_requests)),
            "providerRequestCounts": counts,
            "status": status,
            "source": "local_price_update",
        }
    )
    cutoff = datetime.now(timezone.utc).timestamp() - (2 * 86400)
    trimmed: list[dict] = []
    for item in ledger:
        ts = parse_utc(str(item.get("timestampUtc") or "")) if isinstance(item, dict) else None
        if ts is None:
            continue
        if ts.timestamp() >= cutoff:
            trimmed.append(item)
    state["requestLedger"] = trimmed
    return state


def read_latest_diagnostics() -> dict:
    if not DEFAULT_DIAGNOSTICS_PATH.exists():
        return {}
    try:
        payload = load_json(DEFAULT_DIAGNOSTICS_PATH)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def detect_rate_limited(diagnostics: dict, error_text: str | None = None) -> bool:
    if str(diagnostics.get("buildStatus") or "").lower() == "rate_limited":
        return True
    if str(diagnostics.get("currentPriceEnStatus") or "").lower() == "rate_limited":
        return True
    if str(diagnostics.get("rateLimitStatus") or "").lower() == "rate_limited":
        return True
    raw = (error_text or "").lower()
    if not raw:
        return False
    markers = ["429", "rate limit", "over limit", "quota", "daily limit", "too many requests"]
    return any(marker in raw for marker in markers)


def should_stop_for_budget(state: dict, budget: dict) -> tuple[bool, str, int, int]:
    snapshot = build_budget_snapshot(state, budget)
    hourly_remaining = int(snapshot["hourlyRemaining"])
    daily_remaining = int(snapshot["dailyRemaining"])
    if daily_remaining <= 0:
        return True, "daily_budget_exhausted", hourly_remaining, daily_remaining
    if hourly_remaining <= 0:
        return True, "hourly_budget_exhausted", hourly_remaining, daily_remaining
    return False, "none", hourly_remaining, daily_remaining


def should_stop_for_provider_budget(state: dict, provider: str, budget: dict) -> tuple[bool, str, int, int]:
    provider = normalize_request_provider(provider)
    snapshot = build_provider_budget_snapshot(state, provider, budget)
    hourly_remaining = int(snapshot["hourlyRemaining"])
    daily_remaining = int(snapshot["dailyRemaining"])
    if daily_remaining <= 0:
        return True, f"{provider}_daily_exhausted", hourly_remaining, daily_remaining
    if hourly_remaining <= 0:
        return True, f"{provider}_hourly_exhausted", hourly_remaining, daily_remaining
    return False, "none", hourly_remaining, daily_remaining


def next_safe_wake_seconds(snapshot: dict) -> tuple[int, str]:
    daily_remaining = int(snapshot.get("dailyRemaining") or 0)
    hourly_remaining = int(snapshot.get("hourlyRemaining") or 0)
    if daily_remaining <= 0:
        return int(snapshot.get("dailySleepSeconds") or 0), "daily_budget_exhausted"
    if hourly_remaining <= 0:
        return int(snapshot.get("hourlySleepSeconds") or 0), "hourly_budget_exhausted"
    return 0, "none"


def next_provider_safe_wake_seconds(snapshot: dict) -> tuple[int, str, str | None]:
    provider = normalize_request_provider(snapshot.get("provider"))
    daily_remaining = int(snapshot.get("dailyRemaining") or 0)
    hourly_remaining = int(snapshot.get("hourlyRemaining") or 0)
    if daily_remaining <= 0:
        return int(snapshot.get("dailySleepSeconds") or 0), f"{provider}_daily_exhausted", provider
    if hourly_remaining <= 0:
        return int(snapshot.get("hourlySleepSeconds") or 0), f"{provider}_hourly_exhausted", provider
    return 0, "none", None


def all_provider_budgets_exhausted(snapshots: dict[str, dict]) -> bool:
    if not snapshots:
        return False
    for snapshot in snapshots.values():
        if int(snapshot.get("hourlyRemaining") or 0) > 0 and int(snapshot.get("dailyRemaining") or 0) > 0:
            return False
    return True


def resolve_local_provider_priority(config: dict) -> list[str]:
    raw_value = os.getenv("CARDSCANR_PRICE_PROVIDER_PRIORITY", "").strip()
    if not raw_value:
        raw_value = str(config.get("priceProviderPriority") or "").strip()
    providers = [normalize_request_provider(item) for item in raw_value.split(",") if item.strip()]
    providers = [item for item in providers if item in {PROVIDER_POKEWALLET, PROVIDER_POKEMON_TCG_API}]
    return providers or [PROVIDER_POKEMON_TCG_API]


def local_pokewallet_pricing_enabled(config: dict) -> bool:
    raw_value = os.getenv("CARDSCANR_USE_POKEWALLET_PRICES", "").strip().lower()
    enabled = raw_value in {"1", "true", "yes", "y", "on"}
    if bool(config.get("usePokewalletPrices", False)):
        enabled = True
    priority = resolve_local_provider_priority(config)
    return enabled or (priority and priority[0] == PROVIDER_POKEWALLET)


def provider_counts_from_diagnostics(diagnostics: dict) -> dict[str, int]:
    counts = diagnostics.get("providerRequestCounts")
    if not isinstance(counts, dict):
        counts = {
            PROVIDER_POKEWALLET: diagnostics.get("pokewalletRequestsAttempted", 0),
            PROVIDER_POKEMON_TCG_API: diagnostics.get("pokemonTcgApiRequestsAttempted", 0),
            PROVIDER_TCGDEX: diagnostics.get("tcgdexRequestsAttempted", 0),
        }
    fallback_total = int(diagnostics.get("providerRequestsAttempted") or 0)
    normalized = normalize_provider_request_counts(counts if isinstance(counts, dict) else None)
    if not normalized and fallback_total:
        normalized[PROVIDER_OTHER] = fallback_total
    return normalized


def planned_batch(batch_size: int, config: dict) -> tuple[int, list[str], Path]:
    sets_payload = load_json(EN_SETS_PATH)
    sets = [item for item in sets_payload.get("sets", []) if isinstance(item, dict) and item.get("id")]
    sets.sort(key=lambda item: str(item.get("id") or ""))
    if not sets:
        return 0, [], resolve_state_path(config)

    state_path = resolve_state_path(config)
    state = load_state(state_path)
    cursor = int(state.get("enCurrentPriceCursor", 0)) % len(sets)
    size = max(1, batch_size)
    selected = [str(sets[(cursor + idx) % len(sets)].get("id")) for idx in range(min(size, len(sets)))]
    return cursor, selected, state_path


def run_cmd(command: list[str], env: dict | None = None) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.stdout:
        print(completed.stdout, end="" if completed.stdout.endswith("\n") else "\n")
    if completed.stderr:
        print(completed.stderr, end="" if completed.stderr.endswith("\n") else "\n")
    if completed.returncode != 0:
        raise RuntimeError(f"Command failed ({completed.returncode}): {' '.join(command)}")
    return completed


def git_changed_files() -> list[str]:
    completed = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("Failed to read git status")
    lines = [line.rstrip() for line in completed.stdout.splitlines() if line.strip()]
    files: list[str] = []
    for line in lines:
        files.append(line[3:])
    return files


def price_set_ids_from_files(paths: list[str]) -> list[str]:
    prefix = "public/v1/prices/current/pokemon/en/"
    set_ids: list[str] = []
    for path in paths:
        normalized = path.replace("\\", "/")
        if normalized.startswith(prefix) and normalized.endswith(".json"):
            set_ids.append(Path(normalized).stem)
    return sorted(dict.fromkeys(set_ids))


def git_head_short_hash() -> str | None:
    completed = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return value or None


def migration_summary_fragment() -> str:
    completed = subprocess.run(
        [sys.executable, "tools/report_en_current_price_migration.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return ""
    migrated_line = ""
    for line in completed.stdout.splitlines():
        if "percentage migrated:" in line.lower():
            migrated_line = line.split(":", 1)[-1].strip()
            break
    if not migrated_line:
        return ""
    return f" | migration {migrated_line}"


def should_commit_changes(commit_enabled: bool, changed_files: list[str], validation_passed: bool) -> bool:
    return bool(commit_enabled and validation_passed and bool(changed_files))


def should_push_changes(push_enabled: bool, commit_created: bool) -> bool:
    return bool(push_enabled and commit_created)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run local CardScanR batch price refresh")
    parser.add_argument("--batch-size", type=int, default=10, help="Number of EN sets to refresh in this run")
    parser.add_argument("--dry-run", action="store_true", help="Show planned batch without writing files")
    parser.add_argument("--commit", action="store_true", help="Commit any generated changes")
    parser.add_argument("--push", action="store_true", help="Push committed changes to origin/main")
    parser.add_argument("--all-day", action="store_true", help="Run cycles all day; sleep when hourly budget is exhausted")
    parser.add_argument("--target-hourly-requests", type=int, default=0, help="Override hourly target request budget")
    parser.add_argument("--target-daily-requests", type=int, default=0, help="Override rolling 24h target request budget")
    parser.add_argument("--set-id", type=str, default="", help="Optional EN set id override for one-set provider debugging")
    parser.add_argument("--until-complete", action="store_true", help="Run consecutive cycles until one full EN rotation completes or a stop condition is reached")
    parser.add_argument("--max-cycles", type=int, default=0, help="Optional hard cap on cycles when --until-complete is used")
    parser.add_argument("--cycle-delay-seconds", type=int, default=20, help="Delay between cycles in until-complete mode")
    args = parser.parse_args()

    started_at = time.monotonic()
    started_at_utc = utc_now_iso()
    result_path = Path(os.environ.get("CARDSCANR_LOCAL_UPDATE_RESULT_PATH") or DEFAULT_RESULT_PATH)
    result: dict = {
        "schemaVersion": "1.0.0",
        "startedAtUtc": started_at_utc,
        "finishedAtUtc": None,
        "durationSeconds": None,
        "batchSize": max(1, args.batch_size),
        "plannedSetIds": [],
        "lastBatchSetIds": [],
        "updatedSetIds": [],
        "changedFiles": [],
        "priceStatusUpdated": False,
        "validationPassed": False,
        "commitCreated": False,
        "commitHash": None,
        "pushSucceeded": False,
        "runsExecuted": 0,
        "requestsUsedThisRun": 0,
        "requestsUsedLastHour": 0,
        "requestsUsedLast24Hours": 0,
        "hourlyBudgetRemaining": None,
        "dailyBudgetRemaining": None,
        "pokewalletRequestsUsedLastHour": 0,
        "pokewalletRequestsUsedLast24Hours": 0,
        "pokewalletHourlyRemaining": None,
        "pokewalletDailyRemaining": None,
        "pokemonTcgApiRequestsUsedLastHour": 0,
        "pokemonTcgApiRequestsUsedLast24Hours": 0,
        "pokemonTcgApiHourlyRemaining": None,
        "pokemonTcgApiDailyRemaining": None,
        "providerRequestCounts": {},
        "sleepReason": None,
        "sleepProvider": None,
        "nextSafeWakeAtUtc": None,
        "nextSafeWakeReason": None,
        "stopReason": None,
        "rateLimitStatus": "not_limited",
        "exitCode": 0,
        "error": None,
    }

    def finalize(exit_code: int, error: str | None = None) -> int:
        result["finishedAtUtc"] = utc_now_iso()
        result["durationSeconds"] = max(0, int(round(time.monotonic() - started_at)))
        result["exitCode"] = exit_code
        result["error"] = error
        write_json_atomic(result_path, result)
        return exit_code

    if args.push and not args.commit:
        print("--push requires --commit", file=sys.stderr)
        return finalize(2, "--push requires --commit")

    config = load_config()
    state_path = resolve_state_path(config)
    state = load_state(state_path)
    set_id_override = str(args.set_id or os.getenv("CARDSCANR_CURRENT_PRICE_SET_ID", "")).strip()
    budget = resolve_budget_settings(
        target_hourly=args.target_hourly_requests if args.target_hourly_requests > 0 else None,
        target_daily=args.target_daily_requests if args.target_daily_requests > 0 else None,
    )
    provider_budgets = resolve_provider_budget_settings(
        target_hourly=args.target_hourly_requests if args.target_hourly_requests > 0 else None,
        target_daily=args.target_daily_requests if args.target_daily_requests > 0 else None,
    )
    pokewallet_active = local_pokewallet_pricing_enabled(config)
    cursor, set_ids, _ = planned_batch(args.batch_size, config)
    if set_id_override:
        set_ids = [set_id_override]
    result["plannedSetIds"] = set_ids
    if args.dry_run:
        print("Local updater dry run")
        print(f"- Batch size: {max(1, args.batch_size)}")
        print(f"- Cursor: {cursor}")
        print(f"- Planned set IDs: {', '.join(set_ids) if set_ids else '(none)'}")
        print(f"- State path: {state_path.relative_to(ROOT)}")
        print(f"- Hourly budget target: {budget['hourlyTarget']}")
        print(f"- Rolling 24h budget target: {budget['dailyTarget']}")
        return finalize(0)

    initial_cursor = int(state.get("enCurrentPriceCursor", 0))
    cycles_executed = 0

    try:
        while True:
            state = load_state(state_path)
            budget_snapshot = build_budget_snapshot(state, budget)
            provider_snapshots = build_all_provider_budget_snapshots(state, provider_budgets)
            pokewallet_snapshot = provider_snapshots[PROVIDER_POKEWALLET]
            pokemon_tcg_snapshot = provider_snapshots[PROVIDER_POKEMON_TCG_API]
            hourly_remaining = int(budget_snapshot["hourlyRemaining"])
            daily_remaining = int(budget_snapshot["dailyRemaining"])
            result["requestsUsedLastHour"] = int(budget_snapshot["hourlyUsed"])
            result["requestsUsedLast24Hours"] = int(budget_snapshot["dailyUsed"])
            result["hourlyBudgetRemaining"] = hourly_remaining
            result["dailyBudgetRemaining"] = daily_remaining
            result["pokewalletRequestsUsedLastHour"] = int(pokewallet_snapshot["hourlyUsed"])
            result["pokewalletRequestsUsedLast24Hours"] = int(pokewallet_snapshot["dailyUsed"])
            result["pokewalletHourlyRemaining"] = int(pokewallet_snapshot["hourlyRemaining"])
            result["pokewalletDailyRemaining"] = int(pokewallet_snapshot["dailyRemaining"])
            result["pokemonTcgApiRequestsUsedLastHour"] = int(pokemon_tcg_snapshot["hourlyUsed"])
            result["pokemonTcgApiRequestsUsedLast24Hours"] = int(pokemon_tcg_snapshot["dailyUsed"])
            result["pokemonTcgApiHourlyRemaining"] = int(pokemon_tcg_snapshot["hourlyRemaining"])
            result["pokemonTcgApiDailyRemaining"] = int(pokemon_tcg_snapshot["dailyRemaining"])

            wake_seconds = 0
            wake_reason = "none"
            wake_provider = None
            if pokewallet_active:
                wake_seconds, wake_reason, wake_provider = next_provider_safe_wake_seconds(pokewallet_snapshot)
            elif all_provider_budgets_exhausted(provider_snapshots):
                wake_reason = "all_provider_budgets_exhausted"
                wake_seconds = max(
                    int(snapshot.get("hourlySleepSeconds") or snapshot.get("dailySleepSeconds") or 0)
                    for snapshot in provider_snapshots.values()
                )

            if wake_reason != "none":
                if args.all_day:
                    sleep_seconds = max(1, wake_seconds)
                    wake_at = datetime.now(timezone.utc) + timedelta(seconds=sleep_seconds)
                    result["nextSafeWakeAtUtc"] = wake_at.replace(microsecond=0).isoformat().replace("+00:00", "Z")
                    result["nextSafeWakeReason"] = wake_reason
                    result["sleepReason"] = wake_reason
                    result["sleepProvider"] = wake_provider
                    print(
                        f"Budget sleep: {wake_reason} "
                        f"(sleepProvider={wake_provider or 'all'}, "
                        f"pokewalletHourlyRemaining={result['pokewalletHourlyRemaining']}, "
                        f"pokewalletDailyRemaining={result['pokewalletDailyRemaining']}, "
                        f"pokemonTcgApiHourlyRemaining={result['pokemonTcgApiHourlyRemaining']}, "
                        f"pokemonTcgApiDailyRemaining={result['pokemonTcgApiDailyRemaining']}, "
                        f"wakeInSeconds={sleep_seconds})"
                    )
                    time.sleep(sleep_seconds)
                    continue
                print(
                    f"Budget stop: {wake_reason} "
                    f"(sleepProvider={wake_provider or 'all'}, "
                    f"pokewalletHourlyRemaining={result['pokewalletHourlyRemaining']}, "
                    f"pokewalletDailyRemaining={result['pokewalletDailyRemaining']}, "
                    f"pokemonTcgApiHourlyRemaining={result['pokemonTcgApiHourlyRemaining']}, "
                    f"pokemonTcgApiDailyRemaining={result['pokemonTcgApiDailyRemaining']})"
                )
                result["stopReason"] = wake_reason
                result["sleepReason"] = wake_reason
                result["sleepProvider"] = wake_provider
                return finalize(0)

            pokewallet_cap, pokewallet_hourly_remaining, pokewallet_daily_remaining = calculate_provider_cycle_request_cap(
                state,
                provider_budgets[PROVIDER_POKEWALLET],
                PROVIDER_POKEWALLET,
            )
            pokemon_tcg_cap, pokemon_tcg_hourly_remaining, pokemon_tcg_daily_remaining = calculate_provider_cycle_request_cap(
                state,
                provider_budgets[PROVIDER_POKEMON_TCG_API],
                PROVIDER_POKEMON_TCG_API,
            )
            cycle_request_cap = pokewallet_cap if pokewallet_active else pokemon_tcg_cap
            result["pokewalletHourlyRemaining"] = pokewallet_hourly_remaining
            result["pokewalletDailyRemaining"] = pokewallet_daily_remaining
            result["pokemonTcgApiHourlyRemaining"] = pokemon_tcg_hourly_remaining
            result["pokemonTcgApiDailyRemaining"] = pokemon_tcg_daily_remaining
            result["hourlyBudgetRemaining"] = pokewallet_hourly_remaining if pokewallet_active else pokemon_tcg_hourly_remaining
            result["dailyBudgetRemaining"] = pokewallet_daily_remaining if pokewallet_active else pokemon_tcg_daily_remaining
            if not should_start_current_price_cycle(cycle_request_cap):
                cap_reason = f"{PROVIDER_POKEWALLET}_hourly_exhausted" if pokewallet_active else "all_provider_budgets_exhausted"
                if pokewallet_active and pokewallet_daily_remaining <= 0:
                    cap_reason = f"{PROVIDER_POKEWALLET}_daily_exhausted"
                if args.all_day:
                    sleep_snapshot = pokewallet_snapshot if pokewallet_active else pokemon_tcg_snapshot
                    sleep_seconds = max(
                        1,
                        int(
                            sleep_snapshot.get("hourlySleepSeconds")
                            or sleep_snapshot.get("dailySleepSeconds")
                            or args.cycle_delay_seconds
                            or 60
                        ),
                    )
                    wake_at = datetime.now(timezone.utc) + timedelta(seconds=sleep_seconds)
                    result["nextSafeWakeAtUtc"] = wake_at.replace(microsecond=0).isoformat().replace("+00:00", "Z")
                    result["nextSafeWakeReason"] = cap_reason
                    result["sleepReason"] = cap_reason
                    result["sleepProvider"] = PROVIDER_POKEWALLET if pokewallet_active else None
                    print(
                        f"Budget sleep: {cap_reason} "
                        f"(sleepProvider={result['sleepProvider'] or 'all'}, "
                        f"pokewalletHourlyRemaining={pokewallet_hourly_remaining}, "
                        f"pokewalletDailyRemaining={pokewallet_daily_remaining}, "
                        f"pokemonTcgApiHourlyRemaining={pokemon_tcg_hourly_remaining}, "
                        f"pokemonTcgApiDailyRemaining={pokemon_tcg_daily_remaining}, "
                        f"wakeInSeconds={sleep_seconds})"
                    )
                    time.sleep(sleep_seconds)
                    continue
                print(
                    f"Budget stop: {cap_reason} "
                    f"(sleepProvider={PROVIDER_POKEWALLET if pokewallet_active else 'all'}, "
                    f"pokewalletHourlyRemaining={pokewallet_hourly_remaining}, "
                    f"pokewalletDailyRemaining={pokewallet_daily_remaining}, "
                    f"pokemonTcgApiHourlyRemaining={pokemon_tcg_hourly_remaining}, "
                    f"pokemonTcgApiDailyRemaining={pokemon_tcg_daily_remaining})"
                )
                result["stopReason"] = cap_reason
                result["sleepReason"] = cap_reason
                result["sleepProvider"] = PROVIDER_POKEWALLET if pokewallet_active else None
                return finalize(0)

            env = build_current_price_builder_env(
                os.environ.copy(),
                args.batch_size,
                cycle_request_cap,
                set_id=set_id_override or None,
                provider_request_caps={
                    PROVIDER_POKEWALLET: pokewallet_cap,
                    PROVIDER_POKEMON_TCG_API: pokemon_tcg_cap,
                },
            )

            emit_phase("updating")
            print("Running batch price refresh...")
            run_cmd([sys.executable, "tools/build_price_cache.py", "current_prices"], env=env)

            diagnostics = read_latest_diagnostics()
            provider_counts = provider_counts_from_diagnostics(diagnostics)
            requests_used = sum(provider_counts.values())
            result["requestsUsedThisRun"] = int(result["requestsUsedThisRun"] or 0) + requests_used
            cumulative_counts = dict(result.get("providerRequestCounts") or {})
            for provider, count in provider_counts.items():
                cumulative_counts[provider] = int(cumulative_counts.get(provider, 0) or 0) + int(count)
            result["providerRequestCounts"] = cumulative_counts

            state = load_state(state_path)
            state = append_request_ledger(
                state,
                requests_used=requests_used,
                status=str(diagnostics.get("currentPriceEnStatus") or "ok"),
                now_iso=utc_now_iso(),
                provider_request_counts=provider_counts,
            )
            write_json_atomic(state_path, state)

            emit_phase("validating")
            print("Running validation...")
            validate_env = os.environ.copy()
            validate_env["CARDSCANR_VALIDATE_QUIET"] = "1"
            run_cmd([sys.executable, "tools/validate_cache.py"], env=validate_env)
            result["validationPassed"] = True

            changed_files = git_changed_files()
            result["changedFiles"] = changed_files
            result["updatedSetIds"] = price_set_ids_from_files(changed_files)
            result["lastBatchSetIds"] = list(result["updatedSetIds"])
            result["priceStatusUpdated"] = any(
                path.replace("\\", "/")
                in {
                    "public/v1/prices/status.json",
                    "public/v1/prices/current/pokemon/en/status.json",
                    "public/v1/prices/current/pokemon/jp/status.json",
                }
                for path in changed_files
            )

            cycles_executed += 1
            result["runsExecuted"] = cycles_executed

            if detect_rate_limited(diagnostics):
                result["rateLimitStatus"] = "rate_limited"
                result["stopReason"] = str(diagnostics.get("stopReason") or "provider_rate_limited")
                print(f"Provider rate limit stop: {result['stopReason']}")
                return finalize(0)

            if changed_files:
                print(f"Changed files: {len(changed_files)}")
                for path in changed_files:
                    print(f"- {path}")

            if should_commit_changes(args.commit, changed_files, bool(result.get("validationPassed"))):
                emit_phase("committing")
                run_cmd(["git", "add", "public/v1", "data/scheduled_price_refresh_state.json"])
                post_add_changes = git_changed_files()
                if post_add_changes:
                    migration_fragment = migration_summary_fragment()
                    commit_message = f"chore: local batch cache refresh ({max(1, args.batch_size)} sets){migration_fragment}"
                    run_cmd(["git", "commit", "-m", commit_message])
                    result["commitCreated"] = True
                    result["commitHash"] = git_head_short_hash()
                    if should_push_changes(args.push, result["commitCreated"]):
                        emit_phase("pushing")
                        try:
                            run_cmd(["git", "push", "origin", "main"])
                            result["pushSucceeded"] = True
                        except Exception as push_exc:
                            result["pushSucceeded"] = False
                            result["stopReason"] = "push_failed"
                            return finalize(1, f"push failed after local commit: {push_exc}")

            if not args.until_complete and not args.all_day:
                if not changed_files:
                    print("No repository changes detected.")
                return finalize(0)

            state_after = load_state(state_path)
            current_cursor = int(state_after.get("enCurrentPriceCursor", 0))
            result["plannedSetIds"] = list(state_after.get("lastBatchSetIds") or result["plannedSetIds"])
            if cycles_executed > 0 and current_cursor == initial_cursor:
                result["stopReason"] = "rotation_complete"
                return finalize(0)

            if args.max_cycles > 0 and cycles_executed >= args.max_cycles:
                result["stopReason"] = "max_cycles_reached"
                return finalize(0)

            if args.all_day or args.until_complete:
                sleep_seconds = max(1, args.cycle_delay_seconds if args.cycle_delay_seconds > 0 else 1)
                wake_at = datetime.now(timezone.utc) + timedelta(seconds=sleep_seconds)
                result["nextSafeWakeAtUtc"] = wake_at.replace(microsecond=0).isoformat().replace("+00:00", "Z")
                result["nextSafeWakeReason"] = "cycle_delay"
                time.sleep(sleep_seconds)
    except Exception as exc:
        return finalize(1, str(exc))


if __name__ == "__main__":
    sys.exit(main())
