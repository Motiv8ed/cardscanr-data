"""CardScanR market price engine package.

Keep imports lazy so lightweight WSL X11 navigation scripts can import
submodules (FSM, live-attempt accounting) without pulling the full job-runner
dependency tree into the durable GUI venv.
"""

from __future__ import annotations

from typing import Any

__all__ = ["MarketEngineConfig", "MarketPriceJobRunner"]


def __getattr__(name: str) -> Any:
    if name == "MarketEngineConfig":
        from .config import MarketEngineConfig

        return MarketEngineConfig
    if name == "MarketPriceJobRunner":
        from .job_runner import MarketPriceJobRunner

        return MarketPriceJobRunner
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
