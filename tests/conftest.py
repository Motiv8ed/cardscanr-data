"""Pytest-wide runtime isolation for CardScanR control-plane JSON files."""
from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_cardscanr_runtime_reports(tmp_path, monkeypatch):
    """Redirect reports/runtime writes away from the repo tree during tests."""
    if os.environ.get("CARDSCANR_ALLOW_REAL_RUNTIME_REPORTS") == "1":
        yield
        return
    reports = tmp_path / "reports"
    runtime = reports / "runtime"
    runtime.mkdir(parents=True)
    ledger = runtime / "continuous_au_safety_ledger.json"
    status = runtime / "continuous_au_worker_status.json"
    nav = runtime / "nav_runtime_context.json"
    recovery = runtime / "nav_cdp_recovery_state.json"
    os.environ["CARDSCANR_PYTEST"] = "1"
    os.environ["CARDSCANR_TEST_RUNTIME_ROOT"] = str(reports.resolve())
    import cardscanr_market_engine.config as config_mod
    import cardscanr_market_engine.owned_daily_enablement as enablement_mod

    monkeypatch.setattr(config_mod, "REPORTS_DIR", reports)
    monkeypatch.setattr(enablement_mod, "REPORTS_DIR", reports, raising=False)
    monkeypatch.setattr(enablement_mod, "BUDGET_LEDGER_PATH", ledger, raising=False)
    monkeypatch.setattr(enablement_mod, "STATUS_PATH", status, raising=False)
    monkeypatch.setattr(
        "cardscanr_market_engine.continuous_safety.BUDGET_LEDGER_PATH",
        ledger,
        raising=False,
    )
    monkeypatch.setattr(
        "cardscanr_market_engine.ebay_scale.set_sweep_worker_hook.BUDGET_LEDGER_PATH",
        ledger,
        raising=False,
    )
    monkeypatch.setattr(
        "cardscanr_market_engine.inter_card_cdp_recovery.CDP_RECOVERY_STATE_PATH",
        recovery,
        raising=False,
    )
    monkeypatch.setattr(
        "cardscanr_market_engine.navigation_runtime_context.DEFAULT_CONTEXT_PATH",
        nav,
        raising=False,
    )
    yield