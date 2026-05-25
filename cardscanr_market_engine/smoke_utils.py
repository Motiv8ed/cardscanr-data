from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

REQUIRED_SMOKE_ENV_VARS = ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY")
SMOKE_REPORT_LATEST = "market_price_engine_smoke_latest.json"
SMOKE_REPORT_RUNS = "market_price_engine_smoke_runs.jsonl"
REDACTED = "***REDACTED***"
SENSITIVE_KEY_TERMS = ("key", "token", "secret", "authorization", "apikey", "password")


def missing_smoke_env_vars(env: Mapping[str, str] | None = None) -> list[str]:
    source = env if env is not None else os.environ
    missing = [name for name in REQUIRED_SMOKE_ENV_VARS if not str(source.get(name, "")).strip()]
    provider = str(source.get("MARKET_LOOKUP_PROVIDER", "mock")).strip().lower()
    if provider != "mock":
        missing.append("MARKET_LOOKUP_PROVIDER=mock")
    return missing


def sanitize_for_report(payload: Any) -> Any:
    if isinstance(payload, dict):
        sanitized: dict[str, Any] = {}
        for key, value in payload.items():
            lowered = str(key).lower()
            if any(term in lowered for term in SENSITIVE_KEY_TERMS):
                sanitized[str(key)] = REDACTED
            else:
                sanitized[str(key)] = sanitize_for_report(value)
        return sanitized
    if isinstance(payload, list):
        return [sanitize_for_report(item) for item in payload]
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
