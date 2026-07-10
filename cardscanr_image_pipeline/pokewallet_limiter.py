from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from .processing import pokewallet_request_headers
from .thumbnail_rollout import utc_now_iso


class PokewalletHourlyLimitError(RuntimeError):
    def __init__(self, message: str, *, wait_seconds: float, limits: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.wait_seconds = wait_seconds
        self.limits = limits or {}


@dataclass
class RateLimitEvent:
    at_utc: str
    canonical_base_id: str | None
    status: int
    wait_seconds: float
    message: str | None
    limits: dict[str, Any]
    action: str


@dataclass
class PokewalletGlobalLimiter:
    """Process-wide PokeWallet request gate. Concurrency=1; pauses all workers on hourly 429."""

    min_interval_seconds: float = 2.5
    max_hourly_wait_seconds: float = 3600.0
    events: list[RateLimitEvent] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _last_request_monotonic: float = 0.0
    _paused_until_monotonic: float = 0.0
    total_wait_seconds: float = 0.0

    def before_request(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._paused_until_monotonic:
                wait = self._paused_until_monotonic - now
            else:
                wait = 0.0
            since = now - self._last_request_monotonic
            if since < self.min_interval_seconds:
                wait = max(wait, self.min_interval_seconds - since)
            if wait > 0:
                self.total_wait_seconds += wait
        if wait > 0:
            time.sleep(wait)
        with self._lock:
            self._last_request_monotonic = time.monotonic()

    def note_success(self) -> None:
        with self._lock:
            self._last_request_monotonic = time.monotonic()

    def handle_429_response(
        self,
        response: requests.Response,
        *,
        canonical_base_id: str | None = None,
        persist_callback: Any | None = None,
    ) -> float:
        wait_s = 65.0
        message = None
        limits: dict[str, Any] = {}
        try:
            payload = response.json()
            message = str(payload.get("message") or "")
            limits = dict(payload.get("limits") or {})
            hourly = limits.get("hourly") or {}
            remaining = hourly.get("remaining")
            if "Hourly" in message or remaining == 0:
                wait_s = self.max_hourly_wait_seconds
            elif "Daily" in message:
                wait_s = min(7200.0, self.max_hourly_wait_seconds * 2)
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    wait_s = max(wait_s, float(retry_after))
                except ValueError:
                    pass
        except Exception:
            wait_s = 300.0

        event = RateLimitEvent(
            at_utc=utc_now_iso(),
            canonical_base_id=canonical_base_id,
            status=429,
            wait_seconds=wait_s,
            message=message,
            limits=limits,
            action="pause_all_pokewallet_requests",
        )
        with self._lock:
            self.events.append(event)
            self._paused_until_monotonic = time.monotonic() + wait_s
            self.total_wait_seconds += wait_s

        if persist_callback is not None:
            persist_callback(event)

        # Sleep outside the lock so other threads block in before_request.
        time.sleep(wait_s)
        return wait_s

    def report(self) -> dict[str, Any]:
        return {
            "generatedAtUtc": utc_now_iso(),
            "eventCount": len(self.events),
            "totalWaitSeconds": round(self.total_wait_seconds, 1),
            "events": [
                {
                    "atUtc": e.at_utc,
                    "canonicalBaseId": e.canonical_base_id,
                    "status": e.status,
                    "waitSeconds": e.wait_seconds,
                    "message": e.message,
                    "limits": e.limits,
                    "action": e.action,
                }
                for e in self.events
            ],
        }


_GLOBAL_LIMITER = PokewalletGlobalLimiter()


def get_pokewallet_limiter() -> PokewalletGlobalLimiter:
    return _GLOBAL_LIMITER


def download_pokewallet_bytes_rate_limited(
    session: requests.Session,
    url: str,
    *,
    timeout_seconds: int,
    max_attempts: int = 4,
    canonical_base_id: str | None = None,
    persist_callback: Any | None = None,
    limiter: PokewalletGlobalLimiter | None = None,
) -> tuple[bytes, str]:
    """Download with global PokeWallet gating. Does not log credentials."""
    from .config import ALLOWED_IMAGE_CONTENT_TYPES
    from .processing import ImageValidationError

    gate = limiter or get_pokewallet_limiter()
    last_error: Exception | None = None
    for attempt in range(max_attempts):
        gate.before_request()
        response = session.get(url, timeout=timeout_seconds, headers=pokewallet_request_headers(url))
        if response.status_code == 429:
            gate.handle_429_response(
                response,
                canonical_base_id=canonical_base_id,
                persist_callback=persist_callback,
            )
            last_error = PokewalletHourlyLimitError(
                f"HTTP 429 for {canonical_base_id or url}",
                wait_seconds=gate.events[-1].wait_seconds if gate.events else 3600.0,
            )
            continue
        if response.status_code in {408, 425, 500, 502, 503, 504}:
            time.sleep(min(60.0, 2.0 ** attempt))
            last_error = RuntimeError(f"retryable HTTP {response.status_code}")
            continue
        response.raise_for_status()
        content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type and content_type not in ALLOWED_IMAGE_CONTENT_TYPES:
            raise ImageValidationError(f"unsupported content type {content_type!r}")
        data = response.content
        if not data:
            raise ImageValidationError("empty response body")
        gate.note_success()
        return data, content_type or "application/octet-stream"
    if last_error:
        raise last_error
    raise RuntimeError(f"exhausted PokeWallet download attempts for {canonical_base_id or url}")


def write_rate_limit_report(path: Path, limiter: PokewalletGlobalLimiter | None = None) -> Path:
    gate = limiter or get_pokewallet_limiter()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(gate.report(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
