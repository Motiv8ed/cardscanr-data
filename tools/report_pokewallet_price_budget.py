#!/usr/bin/env python3
"""Report local PokeWallet request-budget ledger state."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
IMPORTER_PATH = ROOT / "tools" / "import_pokewallet_set_prices.py"


def load_importer_module() -> Any:
    spec = importlib.util.spec_from_file_location("import_pokewallet_set_prices", IMPORTER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load importer module from {IMPORTER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def parse_args(default_ledger_path: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Report PokeWallet budget ledger details.")
    parser.add_argument(
        "--budget-ledger-path",
        default=default_ledger_path,
        help="Path to the request ledger JSON file.",
    )
    return parser.parse_args()


def main() -> int:
    importer = load_importer_module()
    args = parse_args(str(importer.REQUEST_LEDGER_PATH.relative_to(importer.ROOT)))

    ledger_path = Path(str(args.budget_ledger_path or importer.REQUEST_LEDGER_PATH))
    if not ledger_path.is_absolute():
        ledger_path = (importer.ROOT / ledger_path).resolve()

    key_args = SimpleNamespace(api_key_file="", api_key_env_name="", allow_local_config_api_key_env=False)
    key_resolution = importer.build_key_resolution(key_args)
    current_key_fingerprint = str(key_resolution.get("apiKeyFingerprint") or "")

    ledger = importer.load_request_ledger(ledger_path)
    stored_fingerprint = str(ledger.get("apiKeyFingerprint") or "")

    settings_args = SimpleNamespace(max_requests_per_hour=None, max_requests_per_day=None, request_safety_buffer=None)
    budget_settings = importer.resolve_budget_settings(settings_args)
    snapshot = importer.budget_snapshot(ledger, budget_settings, datetime.now(timezone.utc))

    oldest = None
    newest = None
    rows = ledger.get("requests") if isinstance(ledger.get("requests"), list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        parsed = importer.parse_utc(row.get("timestampUtc"))
        if parsed is None:
            continue
        if oldest is None or parsed < oldest:
            oldest = parsed
        if newest is None or parsed > newest:
            newest = parsed

    fingerprint_match = None
    if current_key_fingerprint and stored_fingerprint:
        fingerprint_match = current_key_fingerprint == stored_fingerprint

    def as_iso(value: datetime | None) -> str | None:
        if value is None:
            return None
        return importer.as_utc_iso(value)

    print("PokeWallet price budget report")
    print(f"  ledger path: {importer.to_root_relative_or_abs(ledger_path)}")
    print(f"  api key fingerprint (current): {current_key_fingerprint or 'n/a'}")
    print(f"  api key fingerprint (ledger): {stored_fingerprint or 'n/a'}")
    print(f"  key fingerprint match: {fingerprint_match}")
    print(
        "  hourly used/remaining: "
        f"{snapshot.get('hourlyUsed', 0)} / {snapshot.get('hourlyRemaining', 0)}"
    )
    print(
        "  daily used/remaining: "
        f"{snapshot.get('dailyUsed', 0)} / {snapshot.get('dailyRemaining', 0)}"
    )
    print(f"  oldest request timestamp: {as_iso(oldest) or 'n/a'}")
    print(f"  newest request timestamp: {as_iso(newest) or 'n/a'}")
    print(f"  request rows tracked: {len(rows)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
