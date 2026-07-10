from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


class RetryableError(RuntimeError):
    def __init__(self, message: str, *, wait_seconds: float | None = None) -> None:
        super().__init__(message)
        self.wait_seconds = wait_seconds


def retry_call(
    operation: Callable[[], T],
    *,
    max_retries: int,
    base_seconds: float,
    retryable: Callable[[Exception], bool] | None = None,
) -> T:
    attempt = 0
    while True:
        try:
            return operation()
        except Exception as exc:
            attempt += 1
            is_retryable = retryable(exc) if retryable else isinstance(exc, (RetryableError, TimeoutError))
            if not is_retryable or attempt > max_retries:
                raise
            if isinstance(exc, RetryableError) and exc.wait_seconds is not None:
                delay = float(exc.wait_seconds)
            else:
                delay = base_seconds * (2 ** (attempt - 1))
                delay += random.uniform(0, min(0.5, delay * 0.1))
            time.sleep(delay)
