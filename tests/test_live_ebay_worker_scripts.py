from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def read_script(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_live_worker_script_chain_exists() -> None:
    for relative_path in (
        "scripts/run_ebay_price_worker.ps1",
        "scripts/check_live_ebay_worker_config.ps1",
        "scripts/check_live_ebay_worker_config.py",
        "scripts/live_ebay_worker_config.ps1",
        "scripts/start_live_ebay_worker.ps1",
        "scripts/load_supabase_env.ps1",
        "scripts/run_market_price_worker.ps1",
        "workers/market_price_worker.py",
    ):
        assert (ROOT / relative_path).exists(), relative_path


def test_live_worker_launcher_preserves_guards() -> None:
    launcher = read_script("scripts/start_live_ebay_worker.ps1")
    shared_config = read_script("scripts/live_ebay_worker_config.ps1")
    runner = read_script("workers/market_price_worker.py")

    assert "Set-LiveEbayWorkerEnvironment" in launcher
    assert "CONFIRM_LIVE_EBAY_WORKER" in shared_config
    assert "ENABLE_EBAY_REAL_LOOKUP" in shared_config
    assert "MARKET_LOOKUP_PROVIDER\", \"ebay_browser\"" in shared_config
    assert "MARKET_WORKER_CONCURRENCY\", \"1\"" in shared_config
    assert "Refusing personal Chrome profile path" in shared_config
    assert "CONFIRM_LIVE_EBAY_WORKER=true" in runner


def test_config_check_is_non_mutating() -> None:
    checker_ps1 = read_script("scripts/check_live_ebay_worker_config.ps1")
    checker_py = read_script("scripts/check_live_ebay_worker_config.py")

    assert "No eBay lookup ran and no Supabase jobs were claimed" in checker_ps1
    assert "market_price_worker.py" in checker_py
    assert "chromium.launch(channel=\"chrome\", headless=True)" in checker_py
    assert "run_worker_loop" not in checker_py
    assert "run_once(" not in checker_py
