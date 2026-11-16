"""Bounded Playwright persistent-context launch/teardown (mock-friendly, no eBay traffic)."""
from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

CHROME_PROFILE_LOCK_NAMES = ("SingletonLock", "SingletonCookie", "SingletonSocket")

T = TypeVar("T")


def profile_lock_artifacts_present(profile_dir: Path) -> bool:
    return any((profile_dir / name).exists() for name in CHROME_PROFILE_LOCK_NAMES)


def wait_for_profile_lock_release(
    profile_dir: Path,
    *,
    timeout_seconds: float = 15.0,
    poll_seconds: float = 0.1,
) -> bool:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while time.monotonic() < deadline:
        if not profile_lock_artifacts_present(profile_dir):
            return True
        time.sleep(max(0.01, poll_seconds))
    return not profile_lock_artifacts_present(profile_dir)


def is_target_closed_error(exc: BaseException) -> bool:
    if type(exc).__name__ == "TargetClosedError":
        return True
    message = str(exc)
    return "TargetClosedError" in message or "Target page, context or browser has been closed" in message


def _safe_stop_playwright(pw: Any | None) -> None:
    if pw is None:
        return
    try:
        pw.stop()
    except Exception:
        pass


def _safe_close_context(context: Any | None) -> None:
    if context is None:
        return
    try:
        context.close()
    except Exception:
        pass
    browser = getattr(context, "browser", None)
    if browser is not None:
        try:
            browser.close()
        except Exception:
            pass


def teardown_persistent_context(
    *,
    context: Any | None,
    pw: Any | None,
    profile_dir: Path | None,
    wait_profile_lock: bool = True,
    lock_wait_timeout_seconds: float | None = None,
) -> None:
    """Close context + driver and wait for Chrome profile lock artifacts to clear."""
    _safe_close_context(context)
    _safe_stop_playwright(pw)
    if wait_profile_lock and profile_dir is not None:
        timeout = lock_wait_timeout_seconds
        if timeout is None:
            timeout = float(os.getenv("EBAY_BROWSER_PROFILE_LOCK_WAIT_SECONDS", "15") or "15")
        wait_for_profile_lock_release(profile_dir, timeout_seconds=timeout)


def launch_persistent_context_with_retry(
    launch_fn: Callable[[], T],
    *,
    profile_dir: Path,
    max_attempts: int = 2,
    backoff_seconds: float = 0.75,
) -> T:
    """Invoke launch_fn once; on TargetClosedError retry after bounded teardown + backoff."""
    attempts = max(1, int(max_attempts))
    last_exc: BaseException | None = None
    for attempt in range(attempts):
        if attempt > 0:
            teardown_persistent_context(context=None, pw=None, profile_dir=profile_dir)
            time.sleep(backoff_seconds * attempt)
        try:
            return launch_fn()
        except Exception as exc:
            last_exc = exc
            if attempt + 1 >= attempts or not is_target_closed_error(exc):
                raise
    assert last_exc is not None
    raise last_exc


def run_with_target_closed_retry(
    action: Callable[[], T],
    *,
    profile_dir: Path | None,
    on_target_closed: Callable[[], None] | None = None,
    backoff_seconds: float = 0.5,
) -> T:
    """Run action once; on TargetClosedError optionally reset and retry once."""
    try:
        return action()
    except Exception as exc:
        if not is_target_closed_error(exc):
            raise
        if on_target_closed is not None:
            on_target_closed()
        elif profile_dir is not None:
            teardown_persistent_context(context=None, pw=None, profile_dir=profile_dir, wait_profile_lock=True)
        time.sleep(backoff_seconds)
        return action()