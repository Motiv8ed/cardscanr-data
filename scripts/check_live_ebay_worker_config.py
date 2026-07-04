#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _jwt_payload(token: str) -> dict[str, object] | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(decoded.decode("utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _check_supabase_env() -> None:
    supabase_url = _require_env("SUPABASE_URL").rstrip("/")
    service_role_key = os.getenv("SUPABASE_SECRET_KEY", "").strip() or _require_env("SUPABASE_SERVICE_ROLE_KEY")
    parsed = urlparse(supabase_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise RuntimeError("SUPABASE_URL must be an https URL")
    if "your-project" in supabase_url:
        raise RuntimeError("SUPABASE_URL still contains the example placeholder")
    if "your-local-worker-service-role-key" in service_role_key:
        raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY still contains the example placeholder")
    anon_key = os.getenv("SUPABASE_ANON_KEY", "").strip()
    if anon_key and anon_key == service_role_key:
        raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY must not be the anon key")
    payload = _jwt_payload(service_role_key)
    if payload is not None and payload.get("role") not in {None, "service_role"}:
        raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY JWT role is not service_role")
    print("[live-ebay-config] Supabase URL/secret env: ok")


def _check_worker_env() -> None:
    expected = {
        "MARKET_LOOKUP_PROVIDER": "ebay_browser",
        "ENABLE_EBAY_REAL_LOOKUP": "true",
        "CONFIRM_LIVE_EBAY_WORKER": "true",
        "EBAY_BROWSER_ENGINE": "chrome",
        "EBAY_BROWSER_CHANNEL": "chrome",
        "EBAY_BROWSER_PROFILE_NAME": "cardscanr",
        "EBAY_MARKET_SCOPE": "marketplace",
        "MARKET_WORKER_CONCURRENCY": "1",
    }
    for name, expected_value in expected.items():
        actual = _require_env(name).lower()
        if actual != expected_value:
            raise RuntimeError(f"{name} must be {expected_value!r}")
    _require_env("EBAY_BROWSER_USER_DATA_DIR")
    _require_env("EBAY_BROWSER_HEADLESS")
    print("[live-ebay-config] Live worker guard env: ok")


def _check_entrypoints() -> None:
    required = [
        ROOT / "workers" / "market_price_worker.py",
        ROOT / "scripts" / "run_market_price_worker.ps1",
        ROOT / "scripts" / "start_live_ebay_worker.ps1",
    ]
    for path in required:
        if not path.exists():
            raise RuntimeError(f"Required entrypoint file missing: {path}")
    print("[live-ebay-config] Worker entrypoint files: ok")


def _check_imports_and_browser() -> None:
    import requests  # noqa: F401
    from playwright.sync_api import sync_playwright

    from cardscanr_market_engine.config import MarketEngineConfig
    from cardscanr_market_engine.providers.ebay_browser_provider import EbayBrowserProviderConfig

    MarketEngineConfig.from_env(require_supabase=True)
    EbayBrowserProviderConfig.from_env()
    print("[live-ebay-config] Python deps and provider config: ok")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        browser.close()
    print("[live-ebay-config] Installed Chrome channel launch: ok")


def main() -> int:
    _check_entrypoints()
    _check_supabase_env()
    _check_worker_env()
    _check_imports_and_browser()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
