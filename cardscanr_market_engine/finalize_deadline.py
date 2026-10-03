"""Bounded deadline helper for post-Sold finalize (parse/price after capture).

IMPORTANT: This is NOT a hard OS deadline for Playwright/CDP sync calls.
`run_with_finalize_deadline` runs ``fn`` on the caller thread; a daemon watchdog
may invoke ``on_timeout`` (e.g. browser.close) but cannot forcibly interrupt a
wedged Playwright driver read. Post-Sold *capture* must use
`post_sold_capture_process.run_capture_worker_process` (process kill boundary).
This module remains for the broader finalize budget after capture returns.
"""
from __future__ import annotations

import os
import threading
from typing import Any, Callable, TypeVar

from .providers.errors import ProviderTemporaryError

T = TypeVar("T")

FINALIZE_SUCCESS = "FINALIZE_SUCCESS"
FINALIZE_TIMEOUT_SAFE = "FINALIZE_TIMEOUT_SAFE"


def finalize_timeout_seconds(default: int = 90) -> int:
    raw = os.getenv("EBAY_BROWSER_FINALIZE_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return max(15, int(default))
    try:
        return max(15, int(raw))
    except ValueError:
        return max(15, int(default))


def run_with_finalize_deadline(
    fn: Callable[[], T],
    *,
    timeout_seconds: float | None = None,
    on_timeout: Callable[[], None] | None = None,
    stage: str = "post_sold_cdp_finalize",
) -> T:
    """Run ``fn`` on the caller thread with a wall-clock watchdog.

    Not sufficient isolation for Playwright sync CDP. Use process-boundary
    capture for connect/read. On timeout, invoke ``on_timeout`` then raise
    ProviderTemporaryError FINALIZE_TIMEOUT_SAFE if/when ``fn`` returns/raises.
    """
    budget = float(timeout_seconds if timeout_seconds is not None else finalize_timeout_seconds())
    done = threading.Event()
    timed_out = threading.Event()

    def _watchdog() -> None:
        if not done.wait(budget):
            timed_out.set()
            if on_timeout is not None:
                try:
                    on_timeout()
                except Exception:
                    pass

    threading.Thread(target=_watchdog, name="finalize-deadline", daemon=True).start()
    try:
        result = fn()
        if timed_out.is_set():
            raise ProviderTemporaryError(
                f"{FINALIZE_TIMEOUT_SAFE}: {stage} exceeded {int(budget)}s after completion race",
                diagnostics=_timeout_diagnostics(stage=stage, timeout_seconds=budget),
            )
        return result
    except ProviderTemporaryError:
        raise
    except Exception as exc:
        if timed_out.is_set():
            raise ProviderTemporaryError(
                f"{FINALIZE_TIMEOUT_SAFE}: {stage} exceeded {int(budget)}s",
                diagnostics={
                    **_timeout_diagnostics(stage=stage, timeout_seconds=budget),
                    "underlyingErrorType": type(exc).__name__,
                    "underlyingError": str(exc)[:300],
                },
            ) from exc
        raise
    finally:
        done.set()


def _timeout_diagnostics(*, stage: str, timeout_seconds: float) -> dict[str, Any]:
    return {
        "ownedDailyOutcome": FINALIZE_TIMEOUT_SAFE,
        "terminal": FINALIZE_TIMEOUT_SAFE,
        "reason": "post_sold_finalize_timeout",
        "stage": stage,
        "timeoutSeconds": int(timeout_seconds),
        "retryable": True,
        "lastGoodRetained": True,
        "falseFreshness": False,
    }


def is_finalize_timeout(exc: BaseException | str | None, *, diagnostics: dict[str, Any] | None = None) -> bool:
    text = str(exc or "").upper()
    diag = diagnostics or {}
    if str(diag.get("ownedDailyOutcome") or "") == FINALIZE_TIMEOUT_SAFE:
        return True
    if str(diag.get("terminal") or "") == FINALIZE_TIMEOUT_SAFE:
        return True
    if str(diag.get("reason") or "") == "post_sold_finalize_timeout":
        return True
    return FINALIZE_TIMEOUT_SAFE in text
