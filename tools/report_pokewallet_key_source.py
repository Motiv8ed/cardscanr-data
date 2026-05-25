#!/usr/bin/env python3
"""Report safe PokeWallet API key source diagnostics without exposing secrets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

STANDARD_ENV_NAMES = ("POKEWALLET_API_KEY", "CARDSCANR_POKEWALLET_API_KEY")
CONFIG_FILES = (
    ROOT / "data" / "pokewallet_catalog_config.json",
    ROOT / "data" / "pokewallet_jp_price_config.json",
    ROOT / "data" / "pokewallet_pro_price_config.json",
    ROOT / "data" / "provider_probe_config.json",
)
LOCAL_SECRET_CANDIDATES = (
    ROOT / ".env",
    ROOT / ".env.local",
    ROOT / "pokewallet_env.json",
    ROOT / "pokewallet_env.local.json",
)


def try_read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def secret_fingerprint(value: str) -> str:
    clean = str(value or "")
    if not clean:
        return ""
    prefix = clean[:4]
    suffix = clean[-4:] if len(clean) > 4 else clean
    sha12 = hashlib.sha256(clean.encode("utf-8")).hexdigest()[:12]
    return f"len:{len(clean)} {prefix}...{suffix} sha12:{sha12}"


def read_windows_env(name: str, scope: str) -> str:
    if os.name != "nt":
        return ""
    try:
        import winreg  # type: ignore
    except Exception:
        return ""

    if scope == "user":
        root = winreg.HKEY_CURRENT_USER
        path = r"Environment"
    elif scope == "machine":
        root = winreg.HKEY_LOCAL_MACHINE
        path = r"SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Environment"
    else:
        return ""

    try:
        with winreg.OpenKey(root, path) as key:  # type: ignore[arg-type]
            value, _typ = winreg.QueryValueEx(key, name)
            return str(value or "").strip()
    except OSError:
        return ""


def parse_env_file_value(path: Path, names: set[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return result
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in names:
            continue
        cleaned = value.strip().strip("\"").strip("'")
        if cleaned:
            result[key] = cleaned
    return result


def discover_local_config_env_names() -> list[str]:
    discovered: list[str] = []
    for path in CONFIG_FILES:
        payload = try_read_json(path)
        if not isinstance(payload, dict):
            continue
        configured = str(payload.get("apiKeyEnv") or "").strip()
        if configured and configured not in discovered:
            discovered.append(configured)
    return discovered


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    warnings: list[str] = []
    all_values: dict[str, str] = {}
    sources: list[dict[str, Any]] = []

    configured_env_names = discover_local_config_env_names()
    known_env_names: list[str] = list(STANDARD_ENV_NAMES)
    for name in configured_env_names:
        if name not in known_env_names:
            known_env_names.append(name)

    if args.api_key_env_name and args.api_key_env_name not in known_env_names:
        known_env_names.insert(0, args.api_key_env_name)

    def add_source(source: str, name: str, value: str) -> None:
        present = bool(value)
        fingerprint = secret_fingerprint(value) if present else None
        sources.append(
            {
                "source": source,
                "name": name,
                "present": present,
                "fingerprint": fingerprint,
            }
        )
        if present and fingerprint:
            all_values.setdefault(fingerprint, value)

    for name in known_env_names:
        add_source("process_env", name, os.environ.get(name, "").strip())
        add_source("user_env", name, read_windows_env(name, "user"))
        add_source("machine_env", name, read_windows_env(name, "machine"))

    local_files_checked: list[str] = []
    env_name_set = set(known_env_names)
    env_name_set.update({"POKE_WALLET_API_KEY", "POKEWALLET_KEY"})

    for path in LOCAL_SECRET_CANDIDATES:
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        local_files_checked.append(rel)
        if not path.exists():
            sources.append(
                {
                    "source": "local_config_file",
                    "name": rel,
                    "present": False,
                    "fingerprint": None,
                }
            )
            continue

        if path.suffix.lower() == ".json":
            payload = try_read_json(path)
            found = ""
            if isinstance(payload, dict):
                for key in ("POKEWALLET_API_KEY", "CARDSCANR_POKEWALLET_API_KEY", "apiKey", "api_key"):
                    candidate = str(payload.get(key) or "").strip()
                    if candidate:
                        found = candidate
                        break
            add_source("local_config_file", rel, found)
        else:
            parsed = parse_env_file_value(path, env_name_set)
            if parsed:
                # Pick the first present key value for high-level diagnostics.
                first_key = sorted(parsed.keys())[0]
                add_source("local_config_file", f"{rel}:{first_key}", parsed[first_key])
            else:
                add_source("local_config_file", rel, "")

    process_primary = os.environ.get("POKEWALLET_API_KEY", "").strip()
    user_primary = read_windows_env("POKEWALLET_API_KEY", "user")
    if process_primary and user_primary and process_primary != user_primary:
        warnings.append("Current process POKEWALLET_API_KEY differs from Windows User POKEWALLET_API_KEY.")

    multiple_detected = len(all_values) > 1
    if multiple_detected:
        warnings.append("Multiple different API keys were detected across sources.")

    resolved_key = ""
    resolved_source = "unknown"
    resolved_hint = None

    if args.api_key_file:
        try:
            resolved_key = Path(args.api_key_file).read_text(encoding="utf-8").strip()
        except OSError:
            resolved_key = ""
        if resolved_key:
            resolved_source = "cli_option"
            resolved_hint = "--api-key-file"

    if not resolved_key and args.api_key_env_name:
        explicit_value = os.environ.get(args.api_key_env_name, "").strip()
        if explicit_value:
            resolved_key = explicit_value
            resolved_source = "process_env"
            resolved_hint = args.api_key_env_name

    if not resolved_key:
        for name in STANDARD_ENV_NAMES:
            candidate = os.environ.get(name, "").strip()
            if not candidate:
                continue
            resolved_key = candidate
            resolved_hint = name
            user_value = read_windows_env(name, "user")
            machine_value = read_windows_env(name, "machine")
            if user_value and candidate == user_value:
                resolved_source = "user_env"
            elif machine_value and candidate == machine_value:
                resolved_source = "machine_env"
            else:
                resolved_source = "process_env"
            break

    if not resolved_key and args.allow_local_config_api_key_env:
        for env_name in configured_env_names:
            if env_name in STANDARD_ENV_NAMES:
                continue
            candidate = os.environ.get(env_name, "").strip()
            if candidate:
                resolved_key = candidate
                resolved_source = "local_config"
                resolved_hint = env_name
                break

    if (
        args.allow_local_config_api_key_env
        and resolved_source == "local_config"
        and process_primary
        and resolved_key
        and process_primary != resolved_key
    ):
        warnings.append("An explicit local config env name overrides POKEWALLET_API_KEY.")

    report = {
        "schemaVersion": "1.0.0",
        "sourcePrecedence": [
            "cli_option",
            "process_env",
            "user_env",
            "machine_env",
            "local_config",
            "unknown",
        ],
        "resolved": {
            "apiKeyPresent": bool(resolved_key),
            "apiKeySource": resolved_source,
            "apiKeySourceHint": resolved_hint,
            "apiKeyFingerprint": secret_fingerprint(resolved_key) if resolved_key else None,
        },
        "multipleApiKeysDetected": multiple_detected,
        "configuredApiKeyEnvNames": configured_env_names,
        "localFilesChecked": local_files_checked,
        "warnings": warnings,
        "sources": sources,
    }
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Report safe PokeWallet API key source diagnostics.")
    parser.add_argument("--api-key-file", default="", help="Optional explicit API key file path to evaluate precedence.")
    parser.add_argument("--api-key-env-name", default="", help="Optional explicit API key env var name to evaluate precedence.")
    parser.add_argument(
        "--allow-local-config-api-key-env",
        action="store_true",
        help="Allow configured apiKeyEnv fallback in precedence evaluation.",
    )
    parser.add_argument("--json-out", default="", help="Optional output path for the diagnostics JSON.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_report(args)
    rendered = json.dumps(report, indent=2, sort_keys=True)

    # Safety check: ensure we never emit full secret values by mistake.
    for source in report.get("sources", []):
        if isinstance(source, dict) and "value" in source:
            print("Unsafe key field detected in report payload; aborting.")
            return 2

    print(rendered)

    if args.json_out:
        out_path = Path(args.json_out)
        if not out_path.is_absolute():
            out_path = (ROOT / out_path).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered + "\n", encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
