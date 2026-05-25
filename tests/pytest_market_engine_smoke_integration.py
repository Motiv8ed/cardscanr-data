from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from cardscanr_market_engine.smoke_utils import missing_smoke_env_vars

ROOT = Path(__file__).resolve().parent.parent

pytestmark = [pytest.mark.integration]


@pytest.mark.skipif(
    bool(missing_smoke_env_vars(os.environ)),
    reason="Missing required smoke env vars or MARKET_LOOKUP_PROVIDER is not mock",
)
def test_market_price_engine_smoke_script_runs_with_env() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "smoke_market_price_engine.py")],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"smoke script failed\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    assert "[market-engine-smoke] SUCCESS" in result.stdout
