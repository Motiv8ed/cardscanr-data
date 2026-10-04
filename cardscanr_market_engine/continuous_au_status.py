"""Compact continuous AU worker status + daily operating metrics."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .owned_daily_enablement import METRICS_PATH, STATUS_PATH
from .scheduler import utc_iso
from .smoke_utils import append_jsonl, write_json


def empty_status() -> dict[str, Any]:
    return {
        "enabled": False,
        "market": "AU",
        "workerState": "NOT_RUNNING",
        "currentCard": None,
        "currentPriceKeyId": None,
        "currentLane": None,
        "currentReason": None,
        "demandClass": None,
        "verifiedAgeHours": None,
        "TTL": None,
        "submissions1h": 0,
        "submissions24h": 0,
        "healthy1h": 0,
        "healthy24h": 0,
        "transientFailures1h": 0,
        "consecutiveTransientFailures": 0,
        "lastSuccessfulJobAt": None,
        "lastTransientFailureAt": None,
        "cooldownUntil": None,
        "nextProbeAt": None,
        "lastHardStopReason": None,
        "activeChallenges": 0,
        "updatedAtUtc": utc_iso(),
    }


def write_continuous_status(payload: dict[str, Any], *, path: Path | None = None) -> dict[str, Any]:
    status = empty_status()
    status.update(payload)
    status["updatedAtUtc"] = utc_iso()
    write_json(path or STATUS_PATH, status)
    return status


def read_continuous_status(path: Path | None = None) -> dict[str, Any]:
    target = path or STATUS_PATH
    if not target.is_file():
        return empty_status()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return empty_status()
    return data if isinstance(data, dict) else empty_status()


def append_daily_metrics(payload: dict[str, Any], *, path: Path | None = None) -> None:
    row = {
        "recordedAtUtc": utc_iso(),
        "dayUtc": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        **payload,
    }
    append_jsonl(path or METRICS_PATH, row)
