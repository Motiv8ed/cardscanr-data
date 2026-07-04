from __future__ import annotations

import importlib
import os

from cardscanr_market_engine.config import MarketEngineConfig
from cardscanr_market_engine.scheduler import MarketSchedulerConfig
from cardscanr_market_engine.supabase_env_loader import load_supabase_env


def _clear_supabase_env(monkeypatch) -> None:
    for name in ("SUPABASE_URL", "SUPABASE_SECRET_KEY", "SUPABASE_SERVICE_ROLE_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_market_engine_config_prefers_canonical_secret_key(monkeypatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_canonical_fake")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "legacy-fallback")

    config = MarketEngineConfig.from_env(require_supabase=True)

    assert config.supabase_service_role_key == "sb_secret_canonical_fake"


def test_scheduler_config_accepts_legacy_service_role_fallback(monkeypatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "legacy-fallback")

    config = MarketSchedulerConfig.from_env(require_supabase=True)

    assert config.supabase_service_role_key == "legacy-fallback"


def test_loader_maps_canonical_secret_to_legacy_compatibility_env(tmp_path, monkeypatch) -> None:
    _clear_supabase_env(monkeypatch)
    env_file = tmp_path / "supabase_env.local.json"
    env_file.write_text(
        '{"SUPABASE_URL":"https://example.supabase.co","SUPABASE_SECRET_KEY":"sb_secret_loaded_fake"}',
        encoding="utf-8",
    )

    load_supabase_env(str(env_file))

    assert os.environ["SUPABASE_SECRET_KEY"] == "sb_secret_loaded_fake"
    assert os.environ["SUPABASE_SERVICE_ROLE_KEY"] == "sb_secret_loaded_fake"


def test_loader_accepts_legacy_file_field_without_requiring_user_edit(tmp_path, monkeypatch) -> None:
    _clear_supabase_env(monkeypatch)
    env_file = tmp_path / "supabase_env.local.json"
    env_file.write_text(
        '{"SUPABASE_URL":"https://example.supabase.co","SUPABASE_SERVICE_ROLE_KEY":"sb_secret_legacy_fake"}',
        encoding="utf-8",
    )

    load_supabase_env(str(env_file))

    assert os.environ["SUPABASE_SECRET_KEY"] == "sb_secret_legacy_fake"
    assert os.environ["SUPABASE_SERVICE_ROLE_KEY"] == "sb_secret_legacy_fake"


def test_loader_accepts_bom_encoded_local_json(tmp_path, monkeypatch) -> None:
    _clear_supabase_env(monkeypatch)
    env_file = tmp_path / "supabase_env.local.json"
    env_file.write_text(
        '\ufeff{"SUPABASE_URL":"https://example.supabase.co","SUPABASE_SECRET_KEY":"sb_secret_bom_fake"}',
        encoding="utf-8",
    )

    load_supabase_env(str(env_file))

    assert os.environ["SUPABASE_SECRET_KEY"] == "sb_secret_bom_fake"
    assert os.environ["SUPABASE_SERVICE_ROLE_KEY"] == "sb_secret_bom_fake"


def test_check_live_worker_accepts_non_jwt_secret_key(monkeypatch) -> None:
    module = importlib.import_module("scripts.check_live_ebay_worker_config")
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_live_fake")
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)

    module._check_supabase_env()
