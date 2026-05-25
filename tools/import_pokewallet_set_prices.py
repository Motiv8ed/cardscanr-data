#!/usr/bin/env python3
"""Stage PokeWallet /prices/:setCode data into current price cache files."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import unicodedata
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent.parent
PUBLIC_DIR = ROOT / "public" / "v1"
REPORTS_DIR = ROOT / "reports"
REPORT_JSON_PATH = REPORTS_DIR / "pokewallet_price_import_latest.json"
REPORT_MD_PATH = REPORTS_DIR / "pokewallet_price_import_latest.md"
CONFIG_PATH = ROOT / "data" / "pokewallet_catalog_config.json"
SETS_SUMMARY_PATH = PUBLIC_DIR / "provider-catalog" / "pokewallet" / "sets-summary.json"
CURRENT_PRICE_ROOT = PUBLIC_DIR / "prices" / "current" / "pokemon"
PRICES_STATUS_PATH = PUBLIC_DIR / "prices" / "status.json"
INDEX_PATH = PUBLIC_DIR / "index.json"
REQUEST_LEDGER_PATH = ROOT / "data" / "pokewallet_price_request_ledger.json"

BASE_URL = "https://api.pokewallet.io"
SCHEMA_VERSION = "1.0.0"
USER_AGENT = "CardScanR-PokeWallet-Set-Price-Importer/1.0"
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_MAX_REQUESTS_PER_HOUR = 100
DEFAULT_MAX_REQUESTS_PER_DAY = 1000
DEFAULT_REQUEST_SAFETY_BUFFER = 0.1
DEFAULT_REQUEST_DELAY_SECONDS = 0.25
DEFAULT_STALENESS = {
    "status": "fresh",
    "ageSeconds": 0,
    "freshForSeconds": 86400,
    "staleAfterSeconds": 172800,
}
SOURCE_ID_POKEWALLET = "pokewallet"
SOURCE_ID_POKEMON_TCG_API = "pokemon_tcg_api"
PRICE_SOURCES = ("tcg", "cm")
DEFAULT_SET_PREFERENCES = {
    "jp": ["23599", "23598", "23600", "23601", "23602", "23603"],
    "en": ["604", "609", "610", "1400", "1538", "1401"],
}
STANDARD_API_KEY_ENV_NAMES = ("POKEWALLET_API_KEY", "CARDSCANR_POKEWALLET_API_KEY")
AUTH_OR_PLAN_STATUS_CODES = {401, 403}
AUTH_OR_PLAN_HINTS = (
    "pro plan required",
    "trial has expired",
    "upgrade to pro",
    "forbidden",
    "unauthorized",
)


@dataclass(frozen=True)
class TargetSet:
    language: str
    app_set_id: str
    app_set_name: str
    provider_set_id: str
    provider_set_code: str
    provider_set_name: str


@dataclass(frozen=True)
class AppCard:
    language: str
    set_id: str
    set_name: str
    collector_number: str
    normalized_name: str
    name: str
    provider_card_id: str
    provider_set_id: str
    provider_set_code: str


@dataclass(frozen=True)
class PriceVariant:
    raw_source: str
    raw_variant: str
    variant: str
    currency: str
    market: str
    country: str
    market_price: float | None
    low_price: float | None
    high_price: float | None
    updated_at: str | None


@dataclass(frozen=True)
class SelectionOptions:
    only_missing_set_prices: bool
    skip_existing_price_files: bool
    refresh_existing_price_files: bool
    max_new_sets: int
    start_after_set: str


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_print(message: str) -> None:
    try:
        print(message)
    except UnicodeEncodeError:
        print(message.encode("ascii", "backslashreplace").decode("ascii"))


def load_json(path: Path) -> Any:
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def try_load_json(path: Path) -> Any:
    try:
        return load_json(path)
    except (OSError, json.JSONDecodeError):
        return None


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")


def to_root_relative_or_abs(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path.resolve())


def parse_utc(ts: Any) -> datetime | None:
    text = str(ts or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def as_utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(str(value).strip())
        if parsed > 0:
            return parsed
    except (TypeError, ValueError):
        pass
    return default


def parse_non_negative_float(value: Any, default: float) -> float:
    try:
        parsed = float(str(value).strip())
        if parsed >= 0:
            return parsed
    except (TypeError, ValueError):
        pass
    return default


def resolve_budget_settings(args: argparse.Namespace) -> dict[str, Any]:
    env_hour = os.environ.get("POKEWALLET_PRICE_MAX_REQUESTS_PER_HOUR")
    env_day = os.environ.get("POKEWALLET_PRICE_MAX_REQUESTS_PER_DAY")
    env_buffer = os.environ.get("POKEWALLET_PRICE_REQUEST_SAFETY_BUFFER")

    cli_hour = getattr(args, "max_requests_per_hour", None)
    cli_day = getattr(args, "max_requests_per_day", None)
    cli_buffer = getattr(args, "request_safety_buffer", None)

    max_hour = parse_positive_int(
        cli_hour if cli_hour is not None else (env_hour if env_hour else DEFAULT_MAX_REQUESTS_PER_HOUR),
        DEFAULT_MAX_REQUESTS_PER_HOUR,
    )
    max_day = parse_positive_int(
        cli_day if cli_day is not None else (env_day if env_day else DEFAULT_MAX_REQUESTS_PER_DAY),
        DEFAULT_MAX_REQUESTS_PER_DAY,
    )

    buffer_ratio = parse_non_negative_float(
        cli_buffer if cli_buffer is not None else (env_buffer if env_buffer else DEFAULT_REQUEST_SAFETY_BUFFER),
        DEFAULT_REQUEST_SAFETY_BUFFER,
    )
    # Treat values > 1 as percentage, for example 10 => 10%.
    if buffer_ratio > 1:
        buffer_ratio = buffer_ratio / 100.0
    if buffer_ratio >= 1:
        buffer_ratio = 0.99

    safe_hour = max(1, int(max_hour * (1.0 - buffer_ratio)))
    safe_day = max(1, int(max_day * (1.0 - buffer_ratio)))

    if cli_hour is not None or cli_day is not None or cli_buffer is not None:
        budget_source = "cli"
    elif env_hour or env_day or env_buffer:
        budget_source = "env"
    else:
        budget_source = "default_safe"

    return {
        "maxRequestsPerHour": max_hour,
        "maxRequestsPerDay": max_day,
        "safetyBuffer": buffer_ratio,
        "safeRequestsPerHour": safe_hour,
        "safeRequestsPerDay": safe_day,
        "budgetSource": budget_source,
    }


def empty_request_ledger(*, api_key_fingerprint: str = "") -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAtUtc": now_utc(),
        "apiKeyFingerprint": api_key_fingerprint or None,
        "requests": [],
    }


def load_request_ledger(path: Path) -> dict[str, Any]:
    payload = try_load_json(path)
    if not isinstance(payload, dict):
        payload = empty_request_ledger()
    rows = payload.get("requests")
    requests = [item for item in rows if isinstance(item, dict)] if isinstance(rows, list) else []
    # Keep one week of request rows; window checks only need 24h.
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    pruned: list[dict[str, Any]] = []
    for row in requests:
        parsed = parse_utc(row.get("timestampUtc"))
        if parsed is None:
            continue
        if parsed >= cutoff:
            pruned.append(row)
    return {
        "schemaVersion": str(payload.get("schemaVersion") or SCHEMA_VERSION),
        "generatedAtUtc": str(payload.get("generatedAtUtc") or now_utc()),
        "apiKeyFingerprint": str(payload.get("apiKeyFingerprint") or "") or None,
        "requests": pruned,
    }


def save_request_ledger(path: Path, ledger: dict[str, Any]) -> None:
    ledger["generatedAtUtc"] = now_utc()
    write_json(path, ledger)


def archive_request_ledger(path: Path, *, reason: str) -> Path | None:
    if not path.exists():
        return None
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_reason = re.sub(r"[^a-z0-9_-]+", "_", reason.lower()).strip("_") or "archive"
    base_name = f"{path.stem}.archive.{timestamp}.{safe_reason}"
    candidate = path.with_name(f"{base_name}{path.suffix}")
    counter = 2
    while candidate.exists():
        candidate = path.with_name(f"{base_name}.{counter}{path.suffix}")
        counter += 1
    path.replace(candidate)
    return candidate


def reconcile_request_ledger(
    *,
    ledger_path: Path,
    current_api_key_fingerprint: str,
    reset_budget_ledger: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    existing = load_request_ledger(ledger_path)
    existing_fingerprint = str(existing.get("apiKeyFingerprint") or "")
    actions: dict[str, Any] = {
        "budgetLedgerPath": to_root_relative_or_abs(ledger_path),
        "budgetLedgerArchivedPath": None,
        "budgetLedgerResetApplied": False,
        "budgetLedgerResetReason": None,
        "budgetLedgerStoredApiKeyFingerprint": existing_fingerprint or None,
        "budgetLedgerCurrentApiKeyFingerprint": current_api_key_fingerprint or None,
        "budgetLedgerFingerprintMatch": None,
    }

    if reset_budget_ledger:
        archived = archive_request_ledger(ledger_path, reason="manual_reset")
        ledger = empty_request_ledger(api_key_fingerprint=current_api_key_fingerprint)
        save_request_ledger(ledger_path, ledger)
        actions["budgetLedgerArchivedPath"] = to_root_relative_or_abs(archived) if archived else None
        actions["budgetLedgerResetApplied"] = True
        actions["budgetLedgerResetReason"] = "manual_reset"
        actions["budgetLedgerStoredApiKeyFingerprint"] = None
        actions["budgetLedgerFingerprintMatch"] = True if current_api_key_fingerprint else None
        return ledger, actions

    if current_api_key_fingerprint and existing_fingerprint and current_api_key_fingerprint != existing_fingerprint:
        archived = archive_request_ledger(ledger_path, reason="key_fingerprint_mismatch")
        ledger = empty_request_ledger(api_key_fingerprint=current_api_key_fingerprint)
        save_request_ledger(ledger_path, ledger)
        actions["budgetLedgerArchivedPath"] = to_root_relative_or_abs(archived) if archived else None
        actions["budgetLedgerResetApplied"] = True
        actions["budgetLedgerResetReason"] = "key_fingerprint_mismatch"
        actions["budgetLedgerFingerprintMatch"] = True
        return ledger, actions

    existing["apiKeyFingerprint"] = current_api_key_fingerprint or existing_fingerprint or None
    if current_api_key_fingerprint and existing_fingerprint != current_api_key_fingerprint:
        save_request_ledger(ledger_path, existing)
    actions["budgetLedgerFingerprintMatch"] = (
        (current_api_key_fingerprint == existing_fingerprint) if current_api_key_fingerprint and existing_fingerprint else None
    )
    return existing, actions


def append_request_ledger(
    *,
    ledger: dict[str, Any],
    ledger_path: Path,
    timestamp_utc: str,
    language: str,
    provider_set_id: str,
    source_mode: str,
    dry_run: bool,
    status_code: int | None,
    endpoint_success: bool,
    error: str | None,
) -> None:
    if "apiKeyFingerprint" not in ledger:
        ledger["apiKeyFingerprint"] = None
    rows = ledger.get("requests")
    if not isinstance(rows, list):
        rows = []
        ledger["requests"] = rows
    rows.append(
        {
            "timestampUtc": timestamp_utc,
            "language": language,
            "providerSetId": provider_set_id,
            "sourceMode": source_mode,
            "dryRun": dry_run,
            "statusCode": status_code,
            "endpointSuccess": endpoint_success,
            "error": error,
        }
    )
    save_request_ledger(ledger_path, ledger)


def budget_window_usage(ledger: dict[str, Any], now_dt: datetime) -> tuple[int, int]:
    rows = ledger.get("requests")
    if not isinstance(rows, list):
        return 0, 0
    hour_cutoff = now_dt - timedelta(hours=1)
    day_cutoff = now_dt - timedelta(hours=24)
    hourly = 0
    daily = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        ts = parse_utc(row.get("timestampUtc"))
        if ts is None:
            continue
        if ts >= day_cutoff:
            daily += 1
            if ts >= hour_cutoff:
                hourly += 1
    return hourly, daily


def estimate_next_budget_reset(ledger: dict[str, Any], now_dt: datetime) -> dict[str, str | None]:
    rows = ledger.get("requests")
    if not isinstance(rows, list):
        return {"hourlyResetAtUtc": None, "dailyResetAtUtc": None}
    timestamps: list[datetime] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        parsed = parse_utc(row.get("timestampUtc"))
        if parsed is not None:
            timestamps.append(parsed)
    if not timestamps:
        return {"hourlyResetAtUtc": None, "dailyResetAtUtc": None}
    earliest_hour = min(ts for ts in timestamps if ts >= now_dt - timedelta(hours=1)) if any(
        ts >= now_dt - timedelta(hours=1) for ts in timestamps
    ) else None
    earliest_day = min(ts for ts in timestamps if ts >= now_dt - timedelta(hours=24)) if any(
        ts >= now_dt - timedelta(hours=24) for ts in timestamps
    ) else None
    return {
        "hourlyResetAtUtc": as_utc_iso(earliest_hour + timedelta(hours=1)) if earliest_hour else None,
        "dailyResetAtUtc": as_utc_iso(earliest_day + timedelta(hours=24)) if earliest_day else None,
    }


def budget_snapshot(ledger: dict[str, Any], settings: dict[str, Any], now_dt: datetime) -> dict[str, Any]:
    hourly_used, daily_used = budget_window_usage(ledger, now_dt)
    hour_cap = int(settings.get("safeRequestsPerHour") or DEFAULT_MAX_REQUESTS_PER_HOUR)
    day_cap = int(settings.get("safeRequestsPerDay") or DEFAULT_MAX_REQUESTS_PER_DAY)
    hourly_remaining = max(0, hour_cap - hourly_used)
    daily_remaining = max(0, day_cap - daily_used)
    reset = estimate_next_budget_reset(ledger, now_dt)
    return {
        "hourlyUsed": hourly_used,
        "dailyUsed": daily_used,
        "hourlyRemaining": hourly_remaining,
        "dailyRemaining": daily_remaining,
        "requestsAllowedByBudget": min(hourly_remaining, daily_remaining),
        "hourlyResetAtUtc": reset["hourlyResetAtUtc"],
        "dailyResetAtUtc": reset["dailyResetAtUtc"],
    }


def normalize_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).lower()
    return re.sub(r"\s+", " ", normalized).strip()


def normalize_name_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", normalize_text(value))


def normalize_catalog_name(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    key = re.sub(r"[^a-z0-9]+", "_", ascii_text.lower()).strip("_")
    return key or normalize_name_key(value)


def normalize_set_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def normalize_collector_key(value: Any) -> str:
    raw = str(value or "").strip().upper().replace(" ", "")
    if not raw:
        return ""
    compact = re.sub(r"[^A-Z0-9/]+", "", raw)
    match = re.match(r"^([A-Z]*)(\d+)(?:/(\d+))?$", compact)
    if not match:
        return compact
    prefix, first, second = match.groups()
    first_norm = str(int(first)) if first.isdigit() else first
    if second:
        second_norm = str(int(second)) if second.isdigit() else second
        return f"{prefix}{first_norm}/{second_norm}"
    return f"{prefix}{first_norm}"


def collector_keys(value: Any) -> list[str]:
    text = str(value or "").strip()
    result: list[str] = []
    for candidate in [text, re.split(r"[/#]", text, maxsplit=1)[0]]:
        normalized = normalize_collector_key(candidate)
        if normalized and normalized not in result:
            result.append(normalized)
        no_zero = re.sub(r"(\D*)0+(\d)", r"\1\2", normalized)
        if no_zero and no_zero not in result:
            result.append(no_zero)
    return result


def normalize_variant(raw_value: Any) -> str | None:
    raw = normalize_catalog_name(raw_value).strip("_")
    if not raw:
        return None
    aliases = {
        "normal": "normal",
        "regular": "normal",
        "non_holo": "normal",
        "non_holofoil": "normal",
        "holo": "holo",
        "holofoil": "holo",
        "holo_foil": "holo",
        "reverse": "reverse",
        "reverse_holo": "reverse",
        "reverse_holofoil": "reverse",
        "reverse_foil": "reverse",
        "1st_edition": "first_edition",
        "first_edition": "first_edition",
        "1st_edition_holo": "first_edition_holo",
        "1st_edition_holofoil": "first_edition_holo",
        "first_edition_holo": "first_edition_holo",
        "first_edition_holofoil": "first_edition_holo",
        "1st_edition_normal": "first_edition_normal",
        "first_edition_normal": "first_edition_normal",
        "unlimited": "unlimited",
    }
    return aliases.get(raw, raw)


def secret_fingerprint(value: str) -> str:
    cleaned = str(value or "")
    if not cleaned:
        return ""
    prefix = cleaned[:4]
    suffix = cleaned[-4:] if len(cleaned) > 4 else cleaned
    sha12 = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:12]
    return f"len:{len(cleaned)} {prefix}...{suffix} sha12:{sha12}"


def read_windows_env(name: str, scope: str) -> str:
    if os.name != "nt":
        return ""
    try:
        import winreg  # type: ignore
    except Exception:
        return ""

    if scope == "user":
        root = winreg.HKEY_CURRENT_USER
        path = r"Environment"
    elif scope == "machine":
        root = winreg.HKEY_LOCAL_MACHINE
        path = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
    else:
        return ""

    try:
        with winreg.OpenKey(root, path) as key:  # type: ignore[arg-type]
            value, _typ = winreg.QueryValueEx(key, name)
            return str(value or "").strip()
    except OSError:
        return ""


def read_configured_api_env_name() -> str:
    config = try_load_json(CONFIG_PATH)
    if not isinstance(config, dict):
        return ""
    return str(config.get("apiKeyEnv") or "").strip()


def read_key_from_file(path: str) -> str:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""
    return raw.strip()


def build_key_resolution(args: argparse.Namespace) -> dict[str, Any]:
    warnings: list[str] = []
    names_checked: list[str] = list(STANDARD_API_KEY_ENV_NAMES)
    configured_env_name = read_configured_api_env_name()

    resolved_key = ""
    api_key_source = "unknown"
    api_key_env_used: str | None = None

    key_file = str(getattr(args, "api_key_file", "") or "").strip()
    if key_file:
        file_value = read_key_from_file(key_file)
        if file_value:
            resolved_key = file_value
            api_key_source = "cli_option"
            api_key_env_used = "--api-key-file"

    explicit_env_name = str(getattr(args, "api_key_env_name", "") or "").strip()
    if not resolved_key and explicit_env_name:
        explicit_value = os.environ.get(explicit_env_name, "").strip()
        names_checked.insert(0, explicit_env_name)
        if explicit_value:
            resolved_key = explicit_value
            api_key_source = "process_env"
            api_key_env_used = explicit_env_name

    if not resolved_key:
        for name in STANDARD_API_KEY_ENV_NAMES:
            value = os.environ.get(name, "").strip()
            if not value:
                continue
            resolved_key = value
            api_key_env_used = name
            user_value = read_windows_env(name, "user")
            machine_value = read_windows_env(name, "machine")
            if user_value and value == user_value:
                api_key_source = "user_env"
            elif machine_value and value == machine_value:
                api_key_source = "machine_env"
            else:
                api_key_source = "process_env"
            break

    allow_local_config_env = bool(getattr(args, "allow_local_config_api_key_env", False))
    if configured_env_name and configured_env_name not in names_checked:
        names_checked.append(configured_env_name)
    if not resolved_key and allow_local_config_env and configured_env_name:
        configured_value = os.environ.get(configured_env_name, "").strip()
        if configured_value:
            resolved_key = configured_value
            api_key_source = "local_config"
            api_key_env_used = configured_env_name

    process_primary = os.environ.get("POKEWALLET_API_KEY", "").strip()
    user_primary = read_windows_env("POKEWALLET_API_KEY", "user")
    if process_primary and user_primary and process_primary != user_primary:
        warnings.append("Current process POKEWALLET_API_KEY differs from Windows User POKEWALLET_API_KEY.")

    unique_values: dict[str, str] = {}
    for env_name in names_checked:
        process_value = os.environ.get(env_name, "").strip()
        user_value = read_windows_env(env_name, "user")
        machine_value = read_windows_env(env_name, "machine")
        for value in (process_value, user_value, machine_value):
            if value:
                unique_values.setdefault(secret_fingerprint(value), value)

    multiple_detected = len(unique_values) > 1
    if multiple_detected:
        warnings.append("Multiple different API keys were detected across environment sources.")

    if (
        allow_local_config_env
        and configured_env_name
        and configured_env_name != "POKEWALLET_API_KEY"
        and api_key_env_used == configured_env_name
    ):
        warnings.append("Configured local apiKeyEnv overrode POKEWALLET_API_KEY.")

    return {
        "apiKey": resolved_key,
        "apiKeySource": api_key_source,
        "apiKeyEnvUsed": api_key_env_used,
        "apiKeyEnvNamesChecked": names_checked,
        "apiKeyFingerprint": secret_fingerprint(resolved_key),
        "multipleApiKeysDetected": multiple_detected,
        "keySourceWarning": " | ".join(warnings) if warnings else None,
        "configuredApiKeyEnvName": configured_env_name or None,
    }


def to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def is_positive_price(*values: float | None) -> bool:
    return any(isinstance(value, (int, float)) and not isinstance(value, bool) for value in values)


def fetch_prices(
    *,
    api_key: str,
    set_id: str,
    source: str,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[dict[str, Any] | None, int | None, str | None]:
    query = {}
    if source in {"tcg", "cm"}:
        query["source"] = source
    url = f"{BASE_URL}/prices/{quote(set_id, safe='')}"
    if query:
        url = f"{url}?{urlencode(query)}"
    headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "X-API-Key": api_key,
    }
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8")
            payload = json.loads(body) if body else {}
            return payload if isinstance(payload, dict) else {}, response.status, None
    except HTTPError as exc:
        snippet = exc.read(512).decode("utf-8", errors="replace").replace(api_key, "[redacted]")
        return None, exc.code, snippet[:180]
    except URLError as exc:
        return None, None, str(exc)[:180]
    except Exception as exc:  # noqa: BLE001 - import reports diagnostics instead of crashing.
        return None, None, exc.__class__.__name__


def is_auth_or_plan_failure(status_code: int | None, error_text: str | None) -> bool:
    if status_code not in AUTH_OR_PLAN_STATUS_CODES:
        return False
    text = str(error_text or "").lower()
    if not text:
        return True
    return any(hint in text for hint in AUTH_OR_PLAN_HINTS)


def list_price_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    for key in ("data", "results", "prices", "cards"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def load_sets_summary() -> list[dict[str, Any]]:
    payload = try_load_json(SETS_SUMMARY_PATH)
    if not isinstance(payload, dict):
        return []
    raw_sets = payload.get("sets")
    items = raw_sets if isinstance(raw_sets, list) else []
    return [item for item in items if isinstance(item, dict)]


def app_cards_path(language: str, set_id: str) -> Path:
    return PUBLIC_DIR / "catalog" / "pokemon" / language / "cards" / f"{set_id}.json"


def provider_cards_path(language: str, provider_set_id: str) -> Path:
    return PUBLIC_DIR / "provider-catalog" / "pokewallet" / "cards" / language / f"{provider_set_id}.json"


def price_set_file_path(language: str, app_set_id: str) -> Path:
    return CURRENT_PRICE_ROOT / language / f"{app_set_id}.json"


def has_existing_price_file(language: str, app_set_id: str) -> bool:
    return price_set_file_path(language, app_set_id).exists()


def is_after_anchor(value: str, anchor: str) -> bool:
    value_text = str(value or "").strip()
    anchor_text = str(anchor or "").strip()
    if not anchor_text:
        return True
    if value_text.isdigit() and anchor_text.isdigit():
        return int(value_text) > int(anchor_text)
    return normalize_set_key(value_text) > normalize_set_key(anchor_text)


def load_app_cards(language: str, set_id: str) -> tuple[str, list[AppCard]]:
    path = app_cards_path(language, set_id)
    payload = try_load_json(path)
    if not isinstance(payload, dict):
        return set_id, []
    set_name = str(payload.get("setName") or payload.get("name") or set_id)
    cards: list[AppCard] = []
    raw_cards = payload.get("cards")
    if not isinstance(raw_cards, list):
        return set_name, []
    for raw in raw_cards:
        if not isinstance(raw, dict):
            continue
        provider_ids = raw.get("providerIds") if isinstance(raw.get("providerIds"), dict) else {}
        promotion = raw.get("promotionMetadata") if isinstance(raw.get("promotionMetadata"), dict) else {}
        provider_card_id = str(provider_ids.get("pokewallet") or promotion.get("providerCardId") or "").strip()
        provider_set_id = str(promotion.get("providerSetId") or set_id).strip()
        provider_set_code = str(promotion.get("providerSetCode") or set_id).strip()
        collector = str(raw.get("collectorNumber") or "").strip()
        normalized_name = str(raw.get("normalizedName") or normalize_catalog_name(raw.get("name"))).strip()
        if not collector or not normalized_name:
            continue
        cards.append(
            AppCard(
                language=language,
                set_id=str(raw.get("setId") or set_id),
                set_name=str(raw.get("setName") or set_name),
                collector_number=collector,
                normalized_name=normalized_name,
                name=str(raw.get("name") or raw.get("displayName") or normalized_name),
                provider_card_id=provider_card_id,
                provider_set_id=provider_set_id,
                provider_set_code=provider_set_code,
            )
        )
    return set_name, cards


def build_card_indexes(cards: list[AppCard]) -> dict[str, Any]:
    by_provider_id: dict[str, AppCard] = {}
    by_collector: dict[str, list[AppCard]] = {}
    by_collector_name: dict[tuple[str, str], list[AppCard]] = {}
    for card in cards:
        if card.provider_card_id:
            by_provider_id[card.provider_card_id] = card
        for collector_key in collector_keys(card.collector_number):
            by_collector.setdefault(collector_key, []).append(card)
            by_collector_name.setdefault((collector_key, normalize_name_key(card.normalized_name)), []).append(card)
            by_collector_name.setdefault((collector_key, normalize_name_key(card.name)), []).append(card)
    return {
        "byProviderId": by_provider_id,
        "byCollector": by_collector,
        "byCollectorName": by_collector_name,
    }


def choose_target_sets(
    languages: list[str],
    requested_sets: list[str],
    max_sets: int,
    selection_options: SelectionOptions,
) -> tuple[list[TargetSet], dict[str, Any]]:
    set_rows = load_sets_summary()
    targets: list[TargetSet] = []
    seen: set[tuple[str, str]] = set()

    def app_set_id_for(language: str, provider_id: str, provider_code: str) -> str | None:
        for candidate in [provider_id, provider_code]:
            if candidate and app_cards_path(language, candidate).exists():
                return candidate
        return None

    def add_row(language: str, row: dict[str, Any]) -> None:
        provider_id = str(row.get("providerSetId") or "").strip()
        provider_code = str(row.get("providerSetCode") or "").strip()
        if not provider_id and not provider_code:
            return
        app_set_id = app_set_id_for(language, provider_id, provider_code)
        if not app_set_id:
            return
        key = (language, provider_id or provider_code)
        if key in seen:
            return
        seen.add(key)
        targets.append(
            TargetSet(
                language=language,
                app_set_id=app_set_id,
                app_set_name=str(row.get("providerSetName") or app_set_id),
                provider_set_id=provider_id or provider_code,
                provider_set_code=provider_code or provider_id,
                provider_set_name=str(row.get("providerSetName") or provider_id or provider_code),
            )
        )

    expansion_mode_enabled = (
        selection_options.only_missing_set_prices
        or selection_options.skip_existing_price_files
        or selection_options.refresh_existing_price_files
        or selection_options.max_new_sets > 0
        or bool(str(selection_options.start_after_set or "").strip())
    )

    if requested_sets:
        lookup_values = {normalize_set_key(item) for item in requested_sets if item}
        for language in languages:
            matches = [
                row
                for row in set_rows
                if str(row.get("cardScanRLanguage") or "").lower() == language
                and (
                    normalize_set_key(row.get("providerSetId")) in lookup_values
                    or normalize_set_key(row.get("providerSetCode")) in lookup_values
                    or normalize_set_key(row.get("providerSetName")) in lookup_values
                )
            ]
            for row in matches:
                add_row(language, row)
    else:
        for language in languages:
            language_rows = [
                item
                for item in set_rows
                if str(item.get("cardScanRLanguage") or "").lower() == language
                and str(item.get("providerSetId") or "").strip().isdigit()
            ]
            rows_by_id = {str(row.get("providerSetId") or ""): row for row in language_rows}
            if expansion_mode_enabled:
                for row in sorted(language_rows, key=lambda item: int(str(item.get("providerSetId") or 0))):
                    add_row(language, row)
                continue

            preferred = DEFAULT_SET_PREFERENCES.get(language, [])
            for provider_id in preferred:
                row = rows_by_id.get(provider_id)
                if row:
                    add_row(language, row)
            if len([item for item in targets if item.language == language]) < max(1, max_sets):
                for row in sorted(language_rows, key=lambda item: int(str(item.get("providerSetId") or 0))):
                    add_row(language, row)
                    if len([item for item in targets if item.language == language]) >= max(1, max_sets):
                        break

    if not requested_sets and max_sets <= 0 and not expansion_mode_enabled:
        targets = targets[:1]

    start_after = str(selection_options.start_after_set or "").strip()
    start_filtered: list[TargetSet] = []
    for target in targets:
        if start_after and not (
            is_after_anchor(target.provider_set_id, start_after)
            or is_after_anchor(target.provider_set_code, start_after)
            or is_after_anchor(target.app_set_id, start_after)
        ):
            continue
        start_filtered.append(target)

    existing_by_key: dict[tuple[str, str], bool] = {}
    for target in start_filtered:
        existing_by_key[(target.language, target.app_set_id)] = has_existing_price_file(target.language, target.app_set_id)

    filter_missing = bool(selection_options.only_missing_set_prices)
    filter_skip_existing = bool(selection_options.skip_existing_price_files)
    refresh_existing = bool(selection_options.refresh_existing_price_files)

    filtered: list[TargetSet] = []
    existing_price_files_skipped = 0
    for target in start_filtered:
        has_existing = existing_by_key[(target.language, target.app_set_id)]
        if has_existing and not refresh_existing and (filter_missing or filter_skip_existing):
            existing_price_files_skipped += 1
            continue
        filtered.append(target)

    by_language_count: Counter[str] = Counter()
    by_language_new_count: Counter[str] = Counter()
    selected: list[TargetSet] = []
    for target in filtered:
        language = target.language
        has_existing = existing_by_key[(target.language, target.app_set_id)]
        if max_sets > 0 and by_language_count[language] >= max_sets:
            continue
        if selection_options.max_new_sets > 0 and (not has_existing) and by_language_new_count[language] >= selection_options.max_new_sets:
            continue
        by_language_count[language] += 1
        if not has_existing:
            by_language_new_count[language] += 1
        selected.append(target)

    selected_set_ids = [item.provider_set_id for item in selected]
    missing_price_sets_selected = sum(
        1 for item in selected if not existing_by_key[(item.language, item.app_set_id)]
    )
    selected_missing_by_language: dict[str, int] = {}
    existing_file_counts_by_language: dict[str, int] = {}
    for language in languages:
        existing_count = len(
            [
                path
                for path in (CURRENT_PRICE_ROOT / language).glob("*.json")
                if path.name != "status.json"
            ]
        ) if (CURRENT_PRICE_ROOT / language).exists() else 0
        existing_file_counts_by_language[language] = existing_count
        selected_missing_by_language[language] = sum(
            1
            for item in selected
            if item.language == language and not existing_by_key[(item.language, item.app_set_id)]
        )

    estimated_new_coverage = {
        "byLanguage": {
            language: {
                "currentPriceSetFiles": existing_file_counts_by_language.get(language, 0),
                "selectedMissingSetFiles": selected_missing_by_language.get(language, 0),
                "estimatedSetFilesAfterWrite": existing_file_counts_by_language.get(language, 0)
                + selected_missing_by_language.get(language, 0),
            }
            for language in languages
        }
    }

    selection_meta = {
        "existingPriceFilesSkipped": existing_price_files_skipped,
        "missingPriceSetsSelected": missing_price_sets_selected,
        "selectedSetIds": selected_set_ids,
        "estimatedNewCoverage": estimated_new_coverage,
    }
    return selected, selection_meta


def price_variants_from_row(row: dict[str, Any], source_mode: str) -> list[PriceVariant]:
    variants: list[PriceVariant] = []
    raw_variant = row.get("variant")
    variant = normalize_variant(raw_variant)
    if not variant:
        return variants

    if source_mode in {"both", "tcg"}:
        tcg = row.get("tcgplayer")
        if isinstance(tcg, dict) and tcg:
            market = to_float(tcg.get("market_price") if tcg.get("market_price") is not None else tcg.get("mid_price"))
            low = to_float(tcg.get("low_price") if tcg.get("low_price") is not None else tcg.get("direct_low_price"))
            high = to_float(tcg.get("high_price"))
            if is_positive_price(market, low, high):
                variants.append(
                    PriceVariant(
                        raw_source="tcgplayer",
                        raw_variant=str(raw_variant or ""),
                        variant=variant,
                        currency="USD",
                        market="tcgplayer",
                        country="US",
                        market_price=market,
                        low_price=low,
                        high_price=high,
                        updated_at=str(tcg.get("updated_at")) if tcg.get("updated_at") else None,
                    )
                )

    if source_mode in {"both", "cm"}:
        cm = row.get("cardmarket")
        if isinstance(cm, dict) and cm:
            market = to_float(cm.get("avg") if cm.get("avg") is not None else cm.get("trend"))
            low = to_float(cm.get("low"))
            high = None
            if is_positive_price(market, low, high):
                variants.append(
                    PriceVariant(
                        raw_source="cardmarket",
                        raw_variant=str(raw_variant or ""),
                        variant=variant,
                        currency="EUR",
                        market="cardmarket",
                        country="EU",
                        market_price=market,
                        low_price=low,
                        high_price=high,
                        updated_at=str(cm.get("updated_at")) if cm.get("updated_at") else None,
                    )
                )
    return variants


def match_app_card(row: dict[str, Any], indexes: dict[str, Any]) -> tuple[AppCard | None, str, list[str]]:
    provider_id = str(row.get("id") or row.get("provider_id") or row.get("product_id") or "").strip()
    if provider_id:
        card = indexes["byProviderId"].get(provider_id)
        if card is not None:
            return card, "matched_to_app_card", ["provider_card_id_exact"]

    row_name = str(row.get("name") or row.get("card_name") or "").strip()
    row_name_key = normalize_name_key(row_name)
    row_collector = str(row.get("card_number") or row.get("number") or "").strip()
    collector_matches: list[AppCard] = []
    name_matches: list[AppCard] = []

    for collector_key in collector_keys(row_collector):
        collector_matches.extend(indexes["byCollector"].get(collector_key, []))
        name_matches.extend(indexes["byCollectorName"].get((collector_key, row_name_key), []))

    unique_name_matches = {id(card): card for card in name_matches}
    if len(unique_name_matches) == 1:
        return next(iter(unique_name_matches.values())), "matched_to_app_card", [
            "provider_set_collector_name_exact"
        ]
    if len(unique_name_matches) > 1:
        return None, "ambiguous_match", ["duplicate_provider_set_collector_name"]

    unique_collector_matches = {id(card): card for card in collector_matches}
    if len(unique_collector_matches) == 1:
        card = next(iter(unique_collector_matches.values()))
        if normalize_name_key(card.name) == row_name_key or normalize_name_key(card.normalized_name) == row_name_key:
            return card, "matched_to_app_card", ["app_set_collector_name_exact"]
        return None, "no_app_card_match", ["collector_match_name_mismatch"]
    if len(unique_collector_matches) > 1:
        return None, "ambiguous_match", ["collector_number_matches_multiple_cards"]
    return None, "no_app_card_match", ["no_collector_match"]


def price_sort_key(entry: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    return (
        normalize_collector_key(entry.get("collectorNumber")),
        str(entry.get("collectorNumber") or ""),
        str(entry.get("normalizedName") or ""),
        str(entry.get("variant") or ""),
        str(entry.get("market") or ""),
        str(entry.get("currency") or ""),
    )


def existing_price_index(language: str, set_id: str) -> dict[str, dict[str, Any]]:
    path = CURRENT_PRICE_ROOT / language / f"{set_id}.json"
    payload = try_load_json(path)
    if not isinstance(payload, dict):
        return {}
    prices = payload.get("prices")
    if not isinstance(prices, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for record in prices:
        if isinstance(record, dict):
            for key in [record.get("canonicalId"), record.get("priceIdentityId")]:
                if isinstance(key, str) and key:
                    result[key] = record
    return result


def build_price_record(
    *,
    target: TargetSet,
    card: AppCard,
    row: dict[str, Any],
    price: PriceVariant,
    ts: str,
    signals: list[str],
) -> dict[str, Any]:
    condition = "near_mint"
    canonical_card_id = (
        f"pokemon|{target.language}|{card.set_id}|{card.collector_number}|{card.normalized_name}"
    )
    if target.language == "en":
        canonical_id = f"{canonical_card_id}|{price.variant}|{condition}"
    else:
        canonical_id = (
            f"{canonical_card_id}|{SOURCE_ID_POKEWALLET}|{price.market}|"
            f"{price.currency.lower()}|{price.variant}|{condition}"
        )
    price_identity_id = (
        f"{canonical_card_id}|{price.variant}|{condition}|{price.market}|{price.currency.lower()}"
    )
    provider_card_id = card.provider_card_id or str(row.get("id") or "")
    record = {
        "canonicalId": canonical_id,
        "setId": card.set_id,
        "collectorNumber": card.collector_number,
        "normalizedName": card.normalized_name,
        "variant": price.variant,
        "condition": condition,
        "currency": price.currency,
        "marketPrice": price.market_price,
        "lowPrice": price.low_price,
        "highPrice": price.high_price,
        "source": SOURCE_ID_POKEWALLET,
        "fetchedAtUtc": ts,
        "nextExpectedPriceUpdateAtUtc": None,
        "staleness": dict(DEFAULT_STALENESS),
        "canonicalCardId": canonical_card_id,
        "priceIdentityId": price_identity_id,
        "market": price.market,
        "country": price.country,
        "sourceCurrency": price.currency,
        "targetCurrency": price.currency,
        "conversionPolicy": "none",
        "status": "priced",
        "confidence": "high" if "provider_set_collector_name_exact" in signals else "medium",
        "diagnostics": {
            "sourceRecordStatus": "priced",
            "rawSource": price.raw_source,
            "rawVariant": price.raw_variant,
            "providerPriceUpdatedAt": price.updated_at,
            "matchSignals": signals,
        },
    }
    if target.language == "jp":
        record["providerIds"] = {
            "pokewalletId": provider_card_id,
            "pokewalletSetId": target.provider_set_id,
        }
        record["matchConfidence"] = 1.0 if "provider_set_collector_name_exact" in signals else 0.9
        record["matchSignals"] = signals
    return record


def summarize_current_counts() -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for language_dir in sorted([item for item in CURRENT_PRICE_ROOT.iterdir() if item.is_dir()], key=lambda p: p.name):
        record_count = 0
        file_count = 0
        source_counts: Counter[str] = Counter()
        status_counts: Counter[str] = Counter()
        currency_counts: Counter[str] = Counter()
        for path in sorted(language_dir.glob("*.json")):
            if path.name == "status.json":
                continue
            payload = try_load_json(path)
            if not isinstance(payload, dict):
                continue
            prices = payload.get("prices")
            if not isinstance(prices, list):
                continue
            file_count += 1
            for record in prices:
                if not isinstance(record, dict):
                    continue
                record_count += 1
                source_counts[str(record.get("source") or payload.get("source") or "unknown")] += 1
                status_counts[str(record.get("status") or payload.get("status") or "unknown")] += 1
                currency_counts[str(record.get("currency") or payload.get("currency") or "unknown")] += 1
        result[language_dir.name] = {
            "recordCount": record_count,
            "fileCount": file_count,
            "sourceCounts": dict(sorted(source_counts.items())),
            "statusCounts": dict(sorted(status_counts.items())),
            "currencyCounts": dict(sorted(currency_counts.items())),
        }
    return result


def single_or_mixed(values: Counter[str]) -> str | None:
    positive = [key for key, count in values.items() if count > 0 and key != "unknown"]
    if not positive:
        return None
    return positive[0] if len(positive) == 1 else "mixed"


def build_set_payload(target: TargetSet, records: list[dict[str, Any]], ts: str) -> dict[str, Any]:
    currency_counts = Counter(str(record.get("currency") or "unknown") for record in records)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAtUtc": ts,
        "game": "pokemon",
        "language": target.language,
        "setId": target.app_set_id,
        "setName": target.app_set_name,
        "source": SOURCE_ID_POKEWALLET,
        "currency": single_or_mixed(currency_counts) or "mixed",
        "status": "partial" if target.language == "jp" else "ok",
        "priceCount": len(records),
        "lastSuccessfulPriceUpdateAtUtc": ts,
        "nextExpectedPriceUpdateAtUtc": None,
        "expectedUpdateIntervalMinutes": None if target.language == "jp" else 60,
        "isLivePricing": False,
        "staleness": dict(DEFAULT_STALENESS),
        "prices": sorted(records, key=price_sort_key),
    }


def update_price_status_files(ts: str, written_languages: set[str]) -> None:
    if not written_languages:
        return
    counts = summarize_current_counts()
    prices_status = try_load_json(PRICES_STATUS_PATH)
    if not isinstance(prices_status, dict):
        prices_status = {
            "schemaVersion": SCHEMA_VERSION,
            "cacheVersion": datetime.now(timezone.utc).strftime("%Y.%m.%d.%H%M"),
            "intendedConsumer": "cardscanr_app",
            "priceDataMode": "batched_refresh",
            "notes": [
                "Timestamps are UTC. Apps should convert to the user's local timezone.",
                "Current prices are latest-known cached values, not live market quotes.",
            ],
            "languages": {},
            "status": "ok",
        }
    prices_status["generatedAtUtc"] = ts
    prices_status.setdefault("schemaVersion", SCHEMA_VERSION)
    prices_status.setdefault("cacheVersion", datetime.now(timezone.utc).strftime("%Y.%m.%d.%H%M"))
    languages = prices_status.get("languages")
    if not isinstance(languages, dict):
        languages = {}
        prices_status["languages"] = languages

    for language in ("en", "jp"):
        current = counts.get(language, {"recordCount": 0, "fileCount": 0, "sourceCounts": {}, "statusCounts": {}, "currencyCounts": {}})
        status_path = CURRENT_PRICE_ROOT / language / "status.json"
        previous_status = try_load_json(status_path)
        if not isinstance(previous_status, dict):
            previous_status = {}
        record_count = int(current.get("recordCount") or 0)
        file_count = int(current.get("fileCount") or 0)
        source_counts = current.get("sourceCounts") if isinstance(current.get("sourceCounts"), dict) else {}
        status_counts = current.get("statusCounts") if isinstance(current.get("statusCounts"), dict) else {}
        currency_counts = current.get("currencyCounts") if isinstance(current.get("currencyCounts"), dict) else {}
        primary_source = None
        if source_counts:
            primary_source = sorted(source_counts.items(), key=lambda item: (-int(item[1]), item[0]))[0][0]
        currency = None
        if currency_counts:
            currency = single_or_mixed(Counter({str(k): int(v) for k, v in currency_counts.items()}))

        has_records = record_count > 0
        wrote_language = language in written_languages
        if not wrote_language:
            continue
        status_value = "partial" if language == "jp" and has_records else previous_status.get("status", "ok")
        if not has_records:
            status_value = "not_available" if language == "jp" else "unavailable"
        if status_value == "not_available":
            set_status_value = "unavailable"
        else:
            set_status_value = status_value if status_value in {"ok", "partial", "stale", "very_stale", "unavailable"} else "partial"

        notes = previous_status.get("notes")
        if not isinstance(notes, list):
            notes = []
        if language == "jp" and has_records:
            for note in [
                "JP current prices are partial PokeWallet set-price imports.",
                "Provider currency is stored as-is; no currency conversion is applied.",
                "TCGPlayer USD and CardMarket EUR records remain separate.",
            ]:
                if note not in notes:
                    notes.append(note)

        payload = dict(previous_status)
        if has_records and wrote_language:
            last_batch_set_ids = sorted(
                path.stem
                for path in (CURRENT_PRICE_ROOT / language).glob("*.json")
                if path.name != "status.json"
            )
            staleness = {
                "status": "fresh",
                "ageSeconds": 0,
                "freshForSeconds": 86400,
                "staleAfterSeconds": 259200,
            }
        elif has_records:
            previous_last_batch = previous_status.get("lastBatchSetIds")
            last_batch_set_ids = previous_last_batch if isinstance(previous_last_batch, list) else []
            previous_staleness = previous_status.get("staleness")
            staleness = previous_staleness if isinstance(previous_staleness, dict) else {
                "status": "fresh",
                "ageSeconds": 0,
                "freshForSeconds": 86400,
                "staleAfterSeconds": 259200,
            }
        else:
            last_batch_set_ids = []
            staleness = {
                "status": "unavailable",
                "ageSeconds": None,
                "freshForSeconds": 86400,
                "staleAfterSeconds": 259200,
            }
        payload.update(
            {
                "schemaVersion": SCHEMA_VERSION,
                "generatedAtUtc": ts,
                "game": "pokemon",
                "language": language,
                "status": set_status_value,
                "currentPriceFilesAvailable": has_records,
                "currentPriceSetFileCount": file_count,
                "currentPriceRecordCount": record_count,
                "recordCount": record_count,
                "lastSuccessfulPriceUpdateAtUtc": ts if has_records and wrote_language else previous_status.get("lastSuccessfulPriceUpdateAtUtc"),
                "lastUpdatedAtUtc": ts if has_records and wrote_language else previous_status.get("lastUpdatedAtUtc"),
                "lastSuccessfulPushAtUtc": previous_status.get("lastSuccessfulPushAtUtc"),
                "lastBatchSetIds": last_batch_set_ids,
                "lastBatchSize": file_count if has_records and wrote_language else previous_status.get("lastBatchSize", 0),
                "lastBatchStartedAtUtc": ts if has_records and wrote_language else previous_status.get("lastBatchStartedAtUtc"),
                "lastBatchFinishedAtUtc": ts if has_records and wrote_language else previous_status.get("lastBatchFinishedAtUtc"),
                "lastBatchDurationSeconds": 0 if has_records and wrote_language else previous_status.get("lastBatchDurationSeconds"),
                "nextExpectedPriceUpdateAtUtc": previous_status.get("nextExpectedPriceUpdateAtUtc") if language == "en" else None,
                "expectedUpdateIntervalMinutes": previous_status.get("expectedUpdateIntervalMinutes") if language == "en" else None,
                "fullRotationEstimatedHours": previous_status.get("fullRotationEstimatedHours") if language == "en" else None,
                "currency": currency,
                "isLivePricing": False,
                "source": primary_source,
                "sourceSummary": {
                    "primarySource": primary_source,
                    "sourceCounts": source_counts,
                    "currency": currency,
                    "isLivePricing": False,
                },
                "statusCounts": status_counts,
                "staleness": staleness,
                "notes": notes,
            }
        )
        write_json(status_path, payload)

        language_status = dict(languages.get(language)) if isinstance(languages.get(language), dict) else {}
        language_status.update(
            {
                "game": "pokemon",
                "language": language,
                "status": "partial" if language == "jp" and has_records else payload["status"],
                "currentPriceFilesAvailable": has_records,
                "currentPriceSetFileCount": file_count,
                "currentPriceRecordCount": record_count,
                "recordCount": record_count,
                "lastSuccessfulPriceUpdateAtUtc": payload.get("lastSuccessfulPriceUpdateAtUtc"),
                "lastUpdatedAtUtc": payload.get("lastUpdatedAtUtc"),
                "nextExpectedPriceUpdateAtUtc": payload.get("nextExpectedPriceUpdateAtUtc"),
                "staleness": payload["staleness"],
                "source": primary_source,
                "sourceSummary": payload["sourceSummary"],
                "statusCounts": status_counts,
            }
        )
        if language == "jp":
            language_status["notes"] = notes
        languages[language] = language_status

    write_json(PRICES_STATUS_PATH, prices_status)


def run_subprocess(command: list[str]) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return {
        "command": " ".join(command),
        "returnCode": completed.returncode,
        "stdoutTail": completed.stdout.splitlines()[-20:],
        "stderrTail": completed.stderr.splitlines()[-20:],
    }


def refresh_after_write(languages: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    history_command = [
        sys.executable,
        "tools/build_price_history_snapshots.py",
        "--languages",
        ",".join([language for language in languages if language in {"en", "jp"}]),
    ]
    result["historySnapshots"] = run_subprocess(history_command)
    result["indexRefresh"] = run_subprocess([sys.executable, "tools/refresh_public_index.py"])
    result["validation"] = run_subprocess([sys.executable, "tools/validate_cache.py"])
    return result


def process_set(
    *,
    target: TargetSet,
    api_key: str,
    source_mode: str,
    ts: str,
    write: bool,
    only_missing: bool,
    skip_existing_better: bool,
    request_delay_seconds: float,
    ledger: dict[str, Any],
    ledger_path: Path,
    dry_run: bool,
    report: dict[str, Any],
) -> list[dict[str, Any]]:
    set_report = {
        "language": target.language,
        "appSetId": target.app_set_id,
        "appSetName": target.app_set_name,
        "providerSetId": target.provider_set_id,
        "providerSetCode": target.provider_set_code,
        "endpoint": f"/prices/{target.provider_set_id}",
        "statusCode": None,
        "errorSnippet": None,
        "endpointSuccess": False,
        "providerRowsReceived": 0,
        "priceRecordsReceived": 0,
        "matchedRecords": 0,
        "wouldImportRecords": 0,
        "importedRecords": 0,
        "skippedExistingBetterRecords": 0,
        "ambiguousRecords": 0,
        "unmatchedRecords": 0,
        "unusableRecords": 0,
        "classificationCounts": {},
        "sourceCounts": {},
        "currencyCounts": {},
        "variantCounts": {},
        "error": None,
        "rateLimited": False,
        "samples": [],
    }
    report["setsAttempted"].append(set_report)

    app_set_name, cards = load_app_cards(target.language, target.app_set_id)
    if not cards:
        set_report["error"] = "no_app_catalogue_cards"
        set_report["classificationCounts"] = {"no_app_card_match": 1}
        return []
    if target.app_set_name == target.app_set_id and app_set_name:
        object.__setattr__(target, "app_set_name", app_set_name)

    payload, status_code, error = fetch_prices(api_key=api_key, set_id=target.provider_set_id, source=source_mode)
    report["apiRequestsUsed"] += 1
    set_report["statusCode"] = status_code
    append_request_ledger(
        ledger=ledger,
        ledger_path=ledger_path,
        timestamp_utc=now_utc(),
        language=target.language,
        provider_set_id=target.provider_set_id,
        source_mode=source_mode,
        dry_run=dry_run,
        status_code=status_code,
        endpoint_success=payload is not None,
        error=error,
    )
    if request_delay_seconds > 0:
        time.sleep(request_delay_seconds)
    if payload is None:
        set_report["error"] = error or "request_failed"
        if error:
            set_report["errorSnippet"] = error
        if status_code == 429:
            set_report["rateLimited"] = True
            report["rateLimitDetected"] = True
        report["endpointFailures"] += 1
        return []
    report["endpointSuccesses"] += 1
    set_report["endpointSuccess"] = True
    rows = list_price_rows(payload)
    set_report["providerRowsReceived"] = len(rows)
    indexes = build_card_indexes(cards)
    existing_index = existing_price_index(target.language, target.app_set_id)
    output_records: list[dict[str, Any]] = []
    seen_output_ids: set[str] = set()
    classification_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    currency_counts: Counter[str] = Counter()
    variant_counts: Counter[str] = Counter()

    for row in rows:
        raw_variant = row.get("variant")
        if not normalize_variant(raw_variant):
            classification_counts["missing_variant"] += 1
            set_report["unusableRecords"] += 1
            continue

        card, classification, signals = match_app_card(row, indexes)
        classification_counts[classification] += 1
        if classification == "ambiguous_match":
            set_report["ambiguousRecords"] += 1
            continue
        if classification == "no_app_card_match" or card is None:
            set_report["unmatchedRecords"] += 1
            continue
        set_report["matchedRecords"] += 1

        variants = price_variants_from_row(row, source_mode)
        if not variants:
            classification_counts["unusable_price"] += 1
            set_report["unusableRecords"] += 1
            continue

        for price_variant in variants:
            set_report["priceRecordsReceived"] += 1
            if not price_variant.currency:
                classification_counts["missing_currency"] += 1
                set_report["unusableRecords"] += 1
                continue
            if target.language == "en" and price_variant.currency != "USD":
                classification_counts["unusable_price"] += 1
                set_report["unusableRecords"] += 1
                continue

            record = build_price_record(
                target=target,
                card=card,
                row=row,
                price=price_variant,
                ts=ts,
                signals=signals,
            )
            canonical_id = str(record.get("canonicalId") or "")
            price_identity_id = str(record.get("priceIdentityId") or "")
            existing = existing_index.get(canonical_id) or existing_index.get(price_identity_id)
            if only_missing and existing:
                classification_counts["skipped_existing"] += 1
                set_report["skippedExistingBetterRecords"] += 1
                continue
            if (
                target.language == "en"
                and skip_existing_better
                and existing
                and existing.get("source") == SOURCE_ID_POKEMON_TCG_API
            ):
                classification_counts["skipped_existing_better"] += 1
                set_report["skippedExistingBetterRecords"] += 1
                continue
            if canonical_id in seen_output_ids:
                classification_counts["ambiguous_match"] += 1
                set_report["ambiguousRecords"] += 1
                continue
            seen_output_ids.add(canonical_id)
            output_records.append(record)
            set_report["wouldImportRecords"] += 1
            if write:
                set_report["importedRecords"] += 1
            classification_counts["imported"] += 1
            source_counts[price_variant.raw_source] += 1
            currency_counts[price_variant.currency] += 1
            variant_counts[price_variant.variant] += 1
            if len(set_report["samples"]) < 5:
                set_report["samples"].append(
                    {
                        "collectorNumber": card.collector_number,
                        "normalizedName": card.normalized_name,
                        "variant": price_variant.variant,
                        "rawSource": price_variant.raw_source,
                        "currency": price_variant.currency,
                        "classification": "imported" if write else "would_import",
                    }
                )

    set_report["classificationCounts"] = dict(sorted(classification_counts.items()))
    set_report["sourceCounts"] = dict(sorted(source_counts.items()))
    set_report["currencyCounts"] = dict(sorted(currency_counts.items()))
    set_report["variantCounts"] = dict(sorted(variant_counts.items()))
    return output_records


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    a = lines.append
    a("# PokeWallet Price Import")
    a("")
    a(f"- startedAtUtc: {report.get('startedAtUtc')}")
    a(f"- finishedAtUtc: {report.get('finishedAtUtc')}")
    a(f"- mode: {report.get('mode')}")
    a(f"- languages: {', '.join(report.get('languages', []))}")
    a(f"- source: {report.get('sourceMode')}")
    a(f"- only missing set prices: {'yes' if report.get('onlyMissingSetPrices') else 'no'}")
    a(f"- skip existing price files: {'yes' if report.get('skipExistingPriceFiles') else 'no'}")
    a(f"- refresh existing price files: {'yes' if report.get('refreshExistingPriceFiles') else 'no'}")
    a(f"- max new sets: {report.get('maxNewSets', 0)}")
    a(f"- start after set: {report.get('startAfterSet') or 'n/a'}")
    a(f"- existing price files skipped: {report.get('existingPriceFilesSkipped', 0)}")
    a(f"- missing price sets selected: {report.get('missingPriceSetsSelected', 0)}")
    a(f"- selected set ids: {report.get('selectedSetIds', [])}")
    a(f"- estimated new coverage: {report.get('estimatedNewCoverage', {})}")
    a(f"- status: {report.get('status')}")
    a(f"- planned requests: {report.get('plannedRequests', 0)}")
    a(f"- requests allowed by budget: {report.get('requestsAllowedByBudget', 0)}")
    a(f"- requests skipped due to budget: {report.get('requestsSkippedDueToBudget', 0)}")
    a(f"- budget ledger path: {report.get('budgetLedgerPath')}")
    a(f"- budget ledger reset applied: {report.get('budgetLedgerResetApplied')}")
    a(f"- budget ledger reset reason: {report.get('budgetLedgerResetReason')}")
    a(f"- budget ledger archived path: {report.get('budgetLedgerArchivedPath')}")
    a(f"- budget ledger fingerprint match: {report.get('budgetLedgerFingerprintMatch')}")
    a(f"- budget source: {report.get('budgetSource')}")
    a(f"- budget decision: {report.get('budgetDecision')}")
    a(f"- hourly used/remaining: {report.get('hourlyUsed', 0)} / {report.get('hourlyRemaining', 0)}")
    a(f"- daily used/remaining: {report.get('dailyUsed', 0)} / {report.get('dailyRemaining', 0)}")
    a(f"- next hourly reset estimate: {report.get('hourlyResetAtUtc') or 'n/a'}")
    a(f"- next daily reset estimate: {report.get('dailyResetAtUtc') or 'n/a'}")
    a(f"- rate limit detected: {'yes' if report.get('rateLimitDetected') else 'no'}")
    a(f"- API key present: {'yes' if report.get('apiKeyPresent') else 'no'}")
    a(f"- API key source: {report.get('apiKeySource')}")
    a(f"- API key fingerprint: {report.get('apiKeyFingerprint') or 'n/a'}")
    a(f"- multiple API keys detected: {'yes' if report.get('multipleApiKeysDetected') else 'no'}")
    if report.get("keySourceWarning"):
        a(f"- key source warning: {report.get('keySourceWarning')}")
    a(f"- API requests used: {report.get('apiRequestsUsed', 0)}")
    a(f"- endpoint success/failure: {report.get('endpointSuccesses', 0)} / {report.get('endpointFailures', 0)}")
    a(f"- price records received: {report.get('priceRecordsReceived', 0)}")
    a(f"- matched records: {report.get('matchedRecords', 0)}")
    a(f"- imported records: {report.get('importedRecords', 0)}")
    a(f"- would import records: {report.get('wouldImportRecords', 0)}")
    a(f"- skipped existing better records: {report.get('skippedExistingBetterRecords', 0)}")
    a(f"- ambiguous records: {report.get('ambiguousRecords', 0)}")
    a(f"- unmatched records: {report.get('unmatchedRecords', 0)}")
    a(f"- unusable records: {report.get('unusableRecords', 0)}")
    a(f"- validation result: {report.get('validationResult')}")
    a(f"- next recommended action: {report.get('nextRecommendedAction')}")
    a(f"- next recommended safe command: {report.get('nextRecommendedSafeCommand')}")
    a("")
    a("## Counts")
    a("")
    a(f"- before: {report.get('beforeCurrentPriceCounts')}")
    a(f"- after: {report.get('afterCurrentPriceCounts')}")
    a("")
    a("## By Language")
    for key, value in sorted(report.get("recordsByLanguage", {}).items()):
        a(f"- {key}: {value}")
    a("")
    a("## By Source")
    for key, value in sorted(report.get("recordsBySource", {}).items()):
        a(f"- {key}: {value}")
    a("")
    a("## By Currency")
    for key, value in sorted(report.get("recordsByCurrency", {}).items()):
        a(f"- {key}: {value}")
    a("")
    a("## By Variant")
    for key, value in sorted(report.get("recordsByVariant", {}).items()):
        a(f"- {key}: {value}")
    a("")
    a("## Sets")
    a("")
    a("| Language | Set | HTTP | Rate limited | Rows | Price records | Matched | Imported | Skipped existing | Ambiguous | Unmatched | Unusable | Error |")
    a("|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    for item in report.get("setsAttempted", []):
        error_text = str(item.get("errorSnippet") or item.get("error") or "").replace("|", "/")
        a(
            f"| {item.get('language')} | {item.get('appSetId')} / {item.get('providerSetId')} | "
            f"{item.get('statusCode') or 'n/a'} | {'yes' if item.get('rateLimited') else 'no'} | "
            f"{item.get('providerRowsReceived', 0)} | "
            f"{item.get('priceRecordsReceived', 0)} | {item.get('matchedRecords', 0)} | "
            f"{item.get('importedRecords', 0)} | {item.get('skippedExistingBetterRecords', 0)} | "
            f"{item.get('ambiguousRecords', 0)} | {item.get('unmatchedRecords', 0)} | "
            f"{item.get('unusableRecords', 0)} | {error_text} |"
        )
    a("")
    return "\n".join(lines)


def parse_languages(args: argparse.Namespace) -> list[str]:
    values = [item.strip().lower() for item in str(args.languages or "").split(",") if item.strip()]
    if args.include_en and "en" not in values:
        values.append("en")
    if args.include_jp and "jp" not in values:
        values.append("jp")
    result: list[str] = []
    for value in values or ["jp"]:
        if value == "zh":
            continue
        if value in {"en", "jp"} and value not in result:
            result.append(value)
    return result


def build_report(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    started = now_utc()
    started_dt = parse_utc(started) or datetime.now(timezone.utc)
    key_resolution = build_key_resolution(args)
    api_key = str(key_resolution.get("apiKey") or "")
    api_key_source = str(key_resolution.get("apiKeySource") or "unknown")
    api_key_env_used = key_resolution.get("apiKeyEnvUsed")
    api_key_env_names_checked = key_resolution.get("apiKeyEnvNamesChecked") or []
    languages = parse_languages(args)
    requested_sets = [item.strip() for item in str(args.sets or "").split(",") if item.strip()]
    max_sets = max(0, int(args.max_sets or 0))
    max_new_sets = max(0, int(args.max_new_sets or 0))
    write = bool(args.write)
    only_missing_set_prices = bool(args.only_missing_set_prices)
    refresh_existing_price_files = bool(args.refresh_existing_price_files)
    skip_existing_price_files = bool(args.skip_existing_price_files) or only_missing_set_prices
    if refresh_existing_price_files:
        skip_existing_price_files = False
        only_missing_set_prices = False
    selection_options = SelectionOptions(
        only_missing_set_prices=only_missing_set_prices,
        skip_existing_price_files=skip_existing_price_files,
        refresh_existing_price_files=refresh_existing_price_files,
        max_new_sets=max_new_sets,
        start_after_set=str(args.start_after_set or "").strip(),
    )
    targets, selection_meta = choose_target_sets(languages, requested_sets, max_sets, selection_options)
    budget_settings = resolve_budget_settings(args)
    respect_budget = bool(getattr(args, "respect_budget", True)) and not bool(getattr(args, "ignore_budget", False))
    fit_budget = bool(getattr(args, "fit_budget", False))
    wait_for_budget = bool(getattr(args, "wait_for_budget", False))
    reset_budget_ledger = bool(getattr(args, "reset_budget_ledger", False))
    ledger_path = Path(str(getattr(args, "budget_ledger_path", "") or REQUEST_LEDGER_PATH))
    if not ledger_path.is_absolute():
        ledger_path = (ROOT / ledger_path).resolve()
    current_key_fingerprint = str(key_resolution.get("apiKeyFingerprint") or "")
    ledger, ledger_actions = reconcile_request_ledger(
        ledger_path=ledger_path,
        current_api_key_fingerprint=current_key_fingerprint,
        reset_budget_ledger=reset_budget_ledger,
    )
    snapshot = budget_snapshot(ledger, budget_settings, started_dt)
    before_counts = summarize_current_counts()
    report: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "startedAtUtc": started,
        "finishedAtUtc": None,
        "provider": "pokewallet",
        "status": "pending",
        "mode": "write" if write else "dry-run",
        "dryRun": not write,
        "write": write,
        "commitSafeReport": bool(args.commit_safe_report),
        "languages": languages,
        "setsRequested": requested_sets,
        "setsSelected": [
            {
                "language": item.language,
                "appSetId": item.app_set_id,
                "providerSetId": item.provider_set_id,
                "providerSetCode": item.provider_set_code,
                "providerSetName": item.provider_set_name,
            }
            for item in targets
        ],
        "sourceMode": args.source,
        "onlyMissingSetPrices": selection_options.only_missing_set_prices,
        "skipExistingPriceFiles": selection_options.skip_existing_price_files,
        "refreshExistingPriceFiles": selection_options.refresh_existing_price_files,
        "maxNewSets": selection_options.max_new_sets,
        "startAfterSet": selection_options.start_after_set,
        "existingPriceFilesSkipped": int(selection_meta.get("existingPriceFilesSkipped") or 0),
        "missingPriceSetsSelected": int(selection_meta.get("missingPriceSetsSelected") or 0),
        "selectedSetIds": selection_meta.get("selectedSetIds") if isinstance(selection_meta.get("selectedSetIds"), list) else [],
        "estimatedNewCoverage": selection_meta.get("estimatedNewCoverage") if isinstance(selection_meta.get("estimatedNewCoverage"), dict) else {},
        "plannedRequests": len(targets),
        "requestsAllowedByBudget": snapshot["requestsAllowedByBudget"],
        "requestsRemainingAfterExecution": snapshot["requestsAllowedByBudget"],
        "requestsSkippedDueToBudget": 0,
        "budgetLedgerPath": ledger_actions.get("budgetLedgerPath"),
        "budgetLedgerArchivedPath": ledger_actions.get("budgetLedgerArchivedPath"),
        "budgetLedgerResetApplied": bool(ledger_actions.get("budgetLedgerResetApplied")),
        "budgetLedgerResetReason": ledger_actions.get("budgetLedgerResetReason"),
        "budgetLedgerStoredApiKeyFingerprint": ledger_actions.get("budgetLedgerStoredApiKeyFingerprint"),
        "budgetLedgerCurrentApiKeyFingerprint": ledger_actions.get("budgetLedgerCurrentApiKeyFingerprint"),
        "budgetLedgerFingerprintMatch": ledger_actions.get("budgetLedgerFingerprintMatch"),
        "hourlyUsed": snapshot["hourlyUsed"],
        "hourlyRemaining": snapshot["hourlyRemaining"],
        "dailyUsed": snapshot["dailyUsed"],
        "dailyRemaining": snapshot["dailyRemaining"],
        "hourlyResetAtUtc": snapshot["hourlyResetAtUtc"],
        "dailyResetAtUtc": snapshot["dailyResetAtUtc"],
        "budgetSource": budget_settings["budgetSource"],
        "budgetDecision": "pending" if respect_budget else "ignored_manual_override",
        "rateLimitDetected": False,
        "apiKeyPresent": bool(api_key),
        "apiKeySource": api_key_source,
        "apiKeyFingerprint": key_resolution.get("apiKeyFingerprint") or None,
        "multipleApiKeysDetected": bool(key_resolution.get("multipleApiKeysDetected")),
        "keySourceWarning": key_resolution.get("keySourceWarning"),
        "apiKeyEnvUsed": api_key_env_used,
        "apiKeyEnvNamesChecked": api_key_env_names_checked,
        "apiRequestsUsed": 0,
        "endpointSuccesses": 0,
        "endpointFailures": 0,
        "priceRecordsReceived": 0,
        "matchedRecords": 0,
        "wouldImportRecords": 0,
        "importedRecords": 0,
        "skippedExistingBetterRecords": 0,
        "ambiguousRecords": 0,
        "unmatchedRecords": 0,
        "unusableRecords": 0,
        "recordsByLanguage": {},
        "recordsBySource": {},
        "recordsByCurrency": {},
        "recordsByVariant": {},
        "beforeCurrentPriceCounts": before_counts,
        "afterCurrentPriceCounts": before_counts,
        "validationResult": "not_run",
        "validationDetails": {},
        "derivedRefresh": {},
        "setsAttempted": [],
        "notes": [
            "API keys are resolved from explicit CLI options first, then process environment defaults; full secrets are never written to this report.",
            "No prices are fabricated; rows without usable provider price fields are skipped.",
            "TCGPlayer USD and CardMarket EUR are preserved as separate records where the schema allows it.",
            "ZH is intentionally not processed.",
        ],
        "nextRecommendedAction": "",
        "nextRecommendedSafeCommand": "",
    }
    outputs_by_language_set: dict[str, list[dict[str, Any]]] = {}

    def update_selected_set_summary() -> None:
        selected_set_ids = [item.provider_set_id for item in targets]
        missing_price_sets_selected = sum(
            1 for item in targets if not has_existing_price_file(item.language, item.app_set_id)
        )
        by_language: dict[str, dict[str, int]] = {}
        for language in languages:
            current_files = len(
                [
                    path
                    for path in (CURRENT_PRICE_ROOT / language).glob("*.json")
                    if path.name != "status.json"
                ]
            ) if (CURRENT_PRICE_ROOT / language).exists() else 0
            selected_missing = sum(
                1
                for item in targets
                if item.language == language and not has_existing_price_file(item.language, item.app_set_id)
            )
            by_language[language] = {
                "currentPriceSetFiles": current_files,
                "selectedMissingSetFiles": selected_missing,
                "estimatedSetFilesAfterWrite": current_files + selected_missing,
            }
        report["selectedSetIds"] = selected_set_ids
        report["missingPriceSetsSelected"] = missing_price_sets_selected
        report["estimatedNewCoverage"] = {"byLanguage": by_language}

    update_selected_set_summary()

    def budget_summary_text() -> str:
        return (
            f"hourly {report.get('hourlyUsed', 0)}/{budget_settings['safeRequestsPerHour']} used, "
            f"daily {report.get('dailyUsed', 0)}/{budget_settings['safeRequestsPerDay']} used"
        )

    def print_budget_block_guidance() -> None:
        safe_print(f"[budget] ledger path: {report.get('budgetLedgerPath')}")
        safe_print(f"[budget] api key fingerprint: {report.get('apiKeyFingerprint') or 'n/a'}")
        safe_print(
            "[budget] If dashboard shows available quota, run with --reset-budget-ledger / -ResetBudgetLedger."
        )

    report["nextRecommendedSafeCommand"] = (
        "python tools/import_pokewallet_set_prices.py --languages jp --source both --only-missing-set-prices --max-new-sets 1 --dry-run --fit-budget --respect-budget --commit-safe-report"
    )

    if not api_key:
        report["status"] = "blocked"
        report["nextRecommendedAction"] = "Set POKEWALLET_API_KEY or CARDSCANR_POKEWALLET_API_KEY before importing prices."
        report["budgetDecision"] = "skipped_no_api_key"
        report["finishedAtUtc"] = now_utc()
        return report, outputs_by_language_set

    if respect_budget:
        while True:
            allowed = int(report.get("requestsAllowedByBudget") or 0)
            planned = int(report.get("plannedRequests") or 0)
            if planned <= allowed:
                report["budgetDecision"] = "allowed_full_run"
                break

            if fit_budget:
                if allowed <= 0:
                    report["status"] = "blocked"
                    report["budgetDecision"] = "blocked_no_budget_remaining"
                    report["requestsSkippedDueToBudget"] = max(0, planned)
                    report["nextRecommendedAction"] = (
                        "No safe request budget remains right now. Wait for quota reset or pass --ignore-budget "
                        "only for explicit manual override. "
                        f"Current budget: {budget_summary_text()}."
                    )
                    print_budget_block_guidance()
                    report["finishedAtUtc"] = now_utc()
                    return report, outputs_by_language_set
                report["budgetDecision"] = "trimmed_to_fit_budget"
                report["requestsSkippedDueToBudget"] = max(0, planned - allowed)
                targets = targets[:allowed]
                report["setsSelected"] = [
                    {
                        "language": item.language,
                        "appSetId": item.app_set_id,
                        "providerSetId": item.provider_set_id,
                        "providerSetCode": item.provider_set_code,
                        "providerSetName": item.provider_set_name,
                    }
                    for item in targets
                ]
                report["plannedRequests"] = len(targets)
                update_selected_set_summary()
                break

            if wait_for_budget:
                # Waiting only meaningfully expands hourly budget; daily shortage should fail safely.
                if int(report.get("dailyRemaining") or 0) <= 0:
                    report["status"] = "blocked"
                    report["budgetDecision"] = "blocked_daily_budget_exhausted"
                    report["requestsSkippedDueToBudget"] = max(0, planned)
                    report["nextRecommendedAction"] = (
                        "Daily safe budget is exhausted. Wait for daily reset or lower request volume. "
                        f"Daily reset estimate: {report.get('dailyResetAtUtc') or 'unknown'}."
                    )
                    report["finishedAtUtc"] = now_utc()
                    return report, outputs_by_language_set
                safe_print(
                    "[budget] Waiting for hourly safe budget. "
                    f"Need {planned}, allowed {allowed}. Next hourly reset: {report.get('hourlyResetAtUtc') or 'unknown'}."
                )
                time.sleep(30)
                ledger = load_request_ledger(ledger_path)
                snapshot = budget_snapshot(ledger, budget_settings, datetime.now(timezone.utc))
                report["requestsAllowedByBudget"] = snapshot["requestsAllowedByBudget"]
                report["hourlyUsed"] = snapshot["hourlyUsed"]
                report["hourlyRemaining"] = snapshot["hourlyRemaining"]
                report["dailyUsed"] = snapshot["dailyUsed"]
                report["dailyRemaining"] = snapshot["dailyRemaining"]
                report["hourlyResetAtUtc"] = snapshot["hourlyResetAtUtc"]
                report["dailyResetAtUtc"] = snapshot["dailyResetAtUtc"]
                continue

            report["budgetDecision"] = "blocked_planned_exceeds_budget"
            report["status"] = "blocked"
            report["requestsSkippedDueToBudget"] = max(0, planned - allowed)
            report["nextRecommendedAction"] = (
                "Planned requests exceed remaining safe budget; import not started. "
                f"Planned={planned}, allowed={allowed}, {budget_summary_text()}. "
                "Use --fit-budget to auto-trim or --wait-for-budget to pause until hourly budget is available."
            )
            print_budget_block_guidance()
            report["finishedAtUtc"] = now_utc()
            return report, outputs_by_language_set

    for target in targets:
        records = process_set(
            target=target,
            api_key=api_key,
            source_mode=args.source,
            ts=started,
            write=write,
            only_missing=bool(args.only_missing),
            skip_existing_better=bool(args.skip_existing_better_prices),
            request_delay_seconds=max(0.0, float(args.request_delay_seconds or 0.0)),
            ledger=ledger,
            ledger_path=ledger_path,
            dry_run=not write,
            report=report,
        )
        key = f"{target.language}:{target.app_set_id}"
        outputs_by_language_set[key] = records
        latest_set = report["setsAttempted"][-1] if report.get("setsAttempted") else {}
        if int(report.get("apiRequestsUsed") or 0) == 1 and is_auth_or_plan_failure(
            latest_set.get("statusCode") if isinstance(latest_set, dict) else None,
            latest_set.get("errorSnippet") if isinstance(latest_set, dict) else None,
        ):
            report["status"] = "auth_or_plan_failure"
            report["budgetDecision"] = "stopped_auth_or_plan_failure"
            report["nextRecommendedAction"] = (
                "First API request returned 401/403 auth-or-plan failure. Stopped immediately to avoid burning request budget."
            )
            break

    for set_report in report["setsAttempted"]:
        report["priceRecordsReceived"] += int(set_report.get("priceRecordsReceived") or 0)
        report["matchedRecords"] += int(set_report.get("matchedRecords") or 0)
        report["wouldImportRecords"] += int(set_report.get("wouldImportRecords") or 0)
        report["importedRecords"] += int(set_report.get("importedRecords") or 0)
        report["skippedExistingBetterRecords"] += int(set_report.get("skippedExistingBetterRecords") or 0)
        report["ambiguousRecords"] += int(set_report.get("ambiguousRecords") or 0)
        report["unmatchedRecords"] += int(set_report.get("unmatchedRecords") or 0)
        report["unusableRecords"] += int(set_report.get("unusableRecords") or 0)
        language = str(set_report.get("language") or "unknown")
        report["recordsByLanguage"][language] = int(report["recordsByLanguage"].get(language, 0)) + int(
            set_report.get("wouldImportRecords") or 0
        )
        for source, count in set_report.get("sourceCounts", {}).items():
            report["recordsBySource"][source] = int(report["recordsBySource"].get(source, 0)) + int(count)
        for currency, count in set_report.get("currencyCounts", {}).items():
            report["recordsByCurrency"][currency] = int(report["recordsByCurrency"].get(currency, 0)) + int(count)
        for variant, count in set_report.get("variantCounts", {}).items():
            report["recordsByVariant"][variant] = int(report["recordsByVariant"].get(variant, 0)) + int(count)

    post_snapshot = budget_snapshot(ledger, budget_settings, datetime.now(timezone.utc))
    report["requestsRemainingAfterExecution"] = post_snapshot["requestsAllowedByBudget"]
    report["hourlyUsed"] = post_snapshot["hourlyUsed"]
    report["hourlyRemaining"] = post_snapshot["hourlyRemaining"]
    report["dailyUsed"] = post_snapshot["dailyUsed"]
    report["dailyRemaining"] = post_snapshot["dailyRemaining"]
    report["hourlyResetAtUtc"] = post_snapshot["hourlyResetAtUtc"]
    report["dailyResetAtUtc"] = post_snapshot["dailyResetAtUtc"]

    all_endpoints_failed = bool(report["setsAttempted"]) and report["endpointSuccesses"] == 0
    all_fail_rate_limited = all_endpoints_failed and all(
        int(item.get("statusCode") or 0) == 429 for item in report.get("setsAttempted", [])
    )
    report["allEndpointsFailed"] = all_endpoints_failed

    if write and report.get("status") != "auth_or_plan_failure":
        written_languages: set[str] = set()
        for target in targets:
            records = outputs_by_language_set.get(f"{target.language}:{target.app_set_id}", [])
            if not records:
                continue
            payload = build_set_payload(target, records, started)
            write_json(CURRENT_PRICE_ROOT / target.language / f"{target.app_set_id}.json", payload)
            written_languages.add(target.language)
        if written_languages:
            update_price_status_files(started, written_languages)
            report["afterCurrentPriceCounts"] = summarize_current_counts()
            report["derivedRefresh"] = refresh_after_write(languages)
            validation = report["derivedRefresh"].get("validation", {})
            report["validationResult"] = "passed" if validation.get("returnCode") == 0 else "failed"
            report["validationDetails"] = validation
        else:
            report["afterCurrentPriceCounts"] = before_counts
            report["derivedRefresh"] = {}
            report["validationResult"] = "not_run"
            report["validationDetails"] = {}
    else:
        report["afterCurrentPriceCounts"] = summarize_current_counts()

    if report.get("status") == "auth_or_plan_failure":
        report["allEndpointsFailed"] = True
    elif all_fail_rate_limited:
        report["rateLimitDetected"] = True
        report["status"] = "rate_limited"
        report["nextRecommendedAction"] = (
            "All attempted endpoints were rate-limited (HTTP 429). Wait for the hourly budget reset or rerun with missing-set mode and a smaller --max-new-sets value."
        )
    elif all_endpoints_failed:
        report["status"] = "failed"
        report["nextRecommendedAction"] = (
            "All attempted endpoints failed. Inspect per-set status codes and error snippets before retrying."
        )
    elif write and report["importedRecords"] > 0:
        report["status"] = "ok"
        report["nextRecommendedAction"] = (
            "Run a bounded missing-set expansion dry-run (for example: --languages jp --source both --only-missing-set-prices --max-new-sets 20 --dry-run), "
            "then review diagnostics before the next write pass."
        )
    elif not write and report["wouldImportRecords"] > 0:
        report["status"] = "ok"
        report["nextRecommendedAction"] = (
            "Dry-run found usable mapped records. Re-run with --write for the same bounded set sample."
        )
    else:
        report["status"] = "blocked"
        report["nextRecommendedAction"] = (
            "Do not write prices yet; inspect unmatched, ambiguous, and unusable records in this report."
        )
    report["finishedAtUtc"] = now_utc()
    return report, outputs_by_language_set


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import staged PokeWallet set prices.")
    parser.add_argument("--languages", default="jp", help="Comma-separated languages to process: en,jp")
    parser.add_argument("--sets", default="", help="Comma-separated provider set ids/codes to process.")
    parser.add_argument("--max-sets", type=int, default=0, help="Maximum sets per language.")
    parser.add_argument("--max-new-sets", type=int, default=0, help="Maximum number of missing-set targets per language.")
    parser.add_argument(
        "--api-key-file",
        default="",
        help="Explicit path to a file containing only the API key (highest precedence).",
    )
    parser.add_argument(
        "--api-key-env-name",
        default="",
        help="Explicit process environment variable name to read for API key before defaults.",
    )
    parser.add_argument(
        "--allow-local-config-api-key-env",
        action="store_true",
        help="Allow data/pokewallet_catalog_config.json apiKeyEnv fallback only when explicitly enabled.",
    )
    parser.add_argument("--start-after-set", default="", help="Select sets after this provider/app set id (useful for paged expansion).")
    parser.add_argument("--source", choices=["both", "tcg", "cm"], default="both")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Do not write current price files. Default.")
    mode.add_argument("--write", action="store_true", help="Write validated mapped current price files.")
    parser.add_argument("--commit-safe-report", action="store_true", help="Write only commit-safe diagnostics.")
    parser.add_argument(
        "--request-delay-seconds",
        "--rate-limit-delay",
        dest="request_delay_seconds",
        type=float,
        default=DEFAULT_REQUEST_DELAY_SECONDS,
        help="Delay between API requests in seconds.",
    )
    parser.add_argument("--skip-existing-better-prices", action="store_true", default=True)
    parser.add_argument("--only-missing", action="store_true", help="Skip records whose current identity already exists.")
    parser.add_argument(
        "--only-missing-set-prices",
        action="store_true",
        help="Select only sets without an existing current price file.",
    )
    parser.add_argument(
        "--skip-existing-price-files",
        action="store_true",
        help="Skip set targets that already have current price files.",
    )
    parser.add_argument(
        "--refresh-existing-price-files",
        action="store_true",
        help="Allow selecting sets that already have current price files.",
    )
    parser.add_argument("--include-en", action="store_true", help="Ensure EN is included.")
    parser.add_argument("--include-jp", action="store_true", help="Ensure JP is included.")
    parser.add_argument("--max-requests-per-hour", type=int, default=None, help="Absolute hourly API limit before safety buffer.")
    parser.add_argument("--max-requests-per-day", type=int, default=None, help="Absolute daily API limit before safety buffer.")
    parser.add_argument(
        "--request-safety-buffer",
        type=float,
        default=None,
        help="Safety buffer ratio (0.1 = 10%%) or percent (10 = 10%%).",
    )
    parser.add_argument(
        "--budget-ledger-path",
        default=str(REQUEST_LEDGER_PATH.relative_to(ROOT)),
        help="Path to shared request ledger JSON file.",
    )
    parser.add_argument(
        "--reset-budget-ledger",
        action="store_true",
        help="Archive and reset the local budget ledger before processing requests.",
    )
    parser.add_argument("--fit-budget", action="store_true", help="Trim selected sets to fit remaining safe budget.")
    parser.add_argument("--wait-for-budget", action="store_true", help="Wait for hourly budget when planned requests exceed remaining safe budget.")
    budget_mode = parser.add_mutually_exclusive_group()
    budget_mode.add_argument("--respect-budget", action="store_true", default=True, help="Enforce request budget limits (default).")
    budget_mode.add_argument("--ignore-budget", action="store_true", help="Ignore request budget limits (manual override).")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report, _records = build_report(args)
    write_json(REPORT_JSON_PATH, report)
    REPORT_MD_PATH.write_text(render_markdown(report), encoding="utf-8", newline="\n")

    safe_print("PokeWallet set price import")
    safe_print(f"  mode: {report['mode']}")
    safe_print(f"  status: {report.get('status', 'unknown')}")
    safe_print(f"  languages: {', '.join(report['languages'])}")
    safe_print(f"  sets attempted: {len(report['setsAttempted'])}")
    safe_print(f"  existing price files skipped: {report.get('existingPriceFilesSkipped', 0)}")
    safe_print(f"  missing price sets selected: {report.get('missingPriceSetsSelected', 0)}")
    safe_print(f"  selected set ids: {report.get('selectedSetIds', [])}")
    safe_print(f"  planned requests: {report.get('plannedRequests', 0)}")
    safe_print(f"  requests allowed by budget: {report.get('requestsAllowedByBudget', 0)}")
    safe_print(f"  requests skipped due to budget: {report.get('requestsSkippedDueToBudget', 0)}")
    safe_print(f"  budget ledger path: {report.get('budgetLedgerPath')}")
    safe_print(f"  budget ledger reset applied: {report.get('budgetLedgerResetApplied')}")
    if report.get("budgetLedgerResetReason"):
        safe_print(f"  budget ledger reset reason: {report.get('budgetLedgerResetReason')}")
    if report.get("budgetLedgerArchivedPath"):
        safe_print(f"  budget ledger archived path: {report.get('budgetLedgerArchivedPath')}")
    if report.get("budgetLedgerStoredApiKeyFingerprint"):
        safe_print(f"  budget ledger stored API fingerprint: {report.get('budgetLedgerStoredApiKeyFingerprint')}")
    safe_print(f"  budget ledger fingerprint match: {report.get('budgetLedgerFingerprintMatch')}")
    safe_print(
        f"  budget usage (hour/day): {report.get('hourlyUsed', 0)}/{report.get('hourlyUsed', 0) + report.get('hourlyRemaining', 0)} "
        f"and {report.get('dailyUsed', 0)}/{report.get('dailyUsed', 0) + report.get('dailyRemaining', 0)}"
    )
    safe_print(f"  budget source: {report.get('budgetSource')}, decision: {report.get('budgetDecision')}")
    safe_print(f"  API key present/source: {report.get('apiKeyPresent')} / {report.get('apiKeySource')}")
    safe_print(f"  API key fingerprint: {report.get('apiKeyFingerprint') or 'n/a'}")
    safe_print(f"  multiple API keys detected: {report.get('multipleApiKeysDetected')}")
    if report.get("keySourceWarning"):
        safe_print(f"  key source warning: {report.get('keySourceWarning')}")
    safe_print(f"  API requests: {report['apiRequestsUsed']}")
    safe_print(f"  price records received: {report['priceRecordsReceived']}")
    safe_print(f"  matched records: {report['matchedRecords']}")
    safe_print(f"  would import records: {report['wouldImportRecords']}")
    safe_print(f"  imported records: {report['importedRecords']}")
    safe_print(f"  skipped existing better: {report['skippedExistingBetterRecords']}")
    safe_print(f"  ambiguous/unmatched/unusable: {report['ambiguousRecords']} / {report['unmatchedRecords']} / {report['unusableRecords']}")
    safe_print(f"  validation result: {report['validationResult']}")
    safe_print(f"  wrote: {REPORT_JSON_PATH.relative_to(ROOT)}")
    safe_print(f"  wrote: {REPORT_MD_PATH.relative_to(ROOT)}")
    if report["validationResult"] == "failed":
        return 1
    if report.get("mode") == "write" and report.get("status") in {"failed", "rate_limited", "auth_or_plan_failure"}:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
