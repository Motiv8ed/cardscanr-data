"""Queue capacity / watermark helpers for pricing scheduler throughput."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QueueWatermarks:
    """Bounded queue target so workers stay busy without unbounded enqueue."""

    low: int
    high: int

    def __post_init__(self) -> None:
        if self.low < 0 or self.high < 0:
            raise ValueError("queue watermarks must be >= 0")
        if self.high and self.low > self.high:
            raise ValueError("queue low watermark must be <= high watermark")

    @property
    def enabled(self) -> bool:
        return self.high > 0


def enqueue_budget(
    *,
    queue_depth: int,
    watermarks: QueueWatermarks,
    max_enqueues_per_run: int,
) -> int:
    """How many jobs to enqueue this pass given current depth and caps."""
    max_enqueues = max(0, int(max_enqueues_per_run))
    depth = max(0, int(queue_depth))
    if max_enqueues == 0:
        return 0
    if not watermarks.enabled:
        return max_enqueues
    if depth >= watermarks.high:
        return 0
    # Fill toward high watermark whenever below high; prefer filling when below low.
    room = watermarks.high - depth
    if depth >= watermarks.low:
        # Gentle top-up only — keep queue from draining without large bursts.
        room = min(room, max(1, watermarks.low // 2 or 1))
    return max(0, min(max_enqueues, room))


def projected_cards_per_hour(*, median_job_seconds: float, workers: int = 1) -> float:
    if median_job_seconds <= 0:
        return 0.0
    return (3600.0 / float(median_job_seconds)) * max(1, int(workers))


def hours_for_keys(key_count: int, cards_per_hour: float) -> float | None:
    if cards_per_hour <= 0:
        return None
    return float(key_count) / float(cards_per_hour)
