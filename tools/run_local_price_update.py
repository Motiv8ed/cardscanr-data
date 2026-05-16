#!/usr/bin/env python3
"""Run a local-first small-batch CardScanR price cache update."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CATALOG_CONFIG_PATH = ROOT / "data" / "catalog_config.json"
EN_SETS_PATH = ROOT / "public" / "v1" / "catalog" / "pokemon" / "en" / "sets.json"
DEFAULT_STATE_PATH = ROOT / "data" / "scheduled_price_refresh_state.json"


def load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def load_config() -> dict:
    if not CATALOG_CONFIG_PATH.exists():
        return {}
    return load_json(CATALOG_CONFIG_PATH)


def resolve_state_path(config: dict) -> Path:
    raw = str(config.get("scheduledCurrentPriceStatePath") or "data/scheduled_price_refresh_state.json").strip()
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    return path


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"enCurrentPriceCursor": 0}
    payload = load_json(path)
    try:
        payload["enCurrentPriceCursor"] = max(0, int(payload.get("enCurrentPriceCursor", 0)))
    except (TypeError, ValueError):
        payload["enCurrentPriceCursor"] = 0
    return payload


def planned_batch(batch_size: int, config: dict) -> tuple[int, list[str], Path]:
    sets_payload = load_json(EN_SETS_PATH)
    sets = [item for item in sets_payload.get("sets", []) if isinstance(item, dict) and item.get("id")]
    sets.sort(key=lambda item: str(item.get("id") or ""))
    if not sets:
        return 0, [], resolve_state_path(config)

    state_path = resolve_state_path(config)
    state = load_state(state_path)
    cursor = int(state.get("enCurrentPriceCursor", 0)) % len(sets)
    size = max(1, batch_size)
    selected = [str(sets[(cursor + idx) % len(sets)].get("id")) for idx in range(min(size, len(sets)))]
    return cursor, selected, state_path


def run_cmd(command: list[str], env: dict | None = None) -> None:
    completed = subprocess.run(command, cwd=ROOT, env=env, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"Command failed ({completed.returncode}): {' '.join(command)}")


def git_changed_files() -> list[str]:
    completed = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("Failed to read git status")
    lines = [line.rstrip() for line in completed.stdout.splitlines() if line.strip()]
    files: list[str] = []
    for line in lines:
        files.append(line[3:])
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description="Run local CardScanR batch price refresh")
    parser.add_argument("--batch-size", type=int, default=10, help="Number of EN sets to refresh in this run")
    parser.add_argument("--dry-run", action="store_true", help="Show planned batch without writing files")
    parser.add_argument("--commit", action="store_true", help="Commit any generated changes")
    parser.add_argument("--push", action="store_true", help="Push committed changes to origin/main")
    args = parser.parse_args()

    if args.push and not args.commit:
        print("--push requires --commit", file=sys.stderr)
        return 2

    config = load_config()
    cursor, set_ids, state_path = planned_batch(args.batch_size, config)
    if args.dry_run:
        print("Local updater dry run")
        print(f"- Batch size: {max(1, args.batch_size)}")
        print(f"- Cursor: {cursor}")
        print(f"- Planned set IDs: {', '.join(set_ids) if set_ids else '(none)'}")
        print(f"- State path: {state_path.relative_to(ROOT)}")
        return 0

    env = os.environ.copy()
    env["CARDSCANR_CURRENT_PRICE_BATCH_SIZE"] = str(max(1, args.batch_size))

    print("Running batch price refresh...")
    run_cmd([sys.executable, "tools/build_price_cache.py", "current_prices"], env=env)

    print("Running validation...")
    validate_env = os.environ.copy()
    validate_env["CARDSCANR_VALIDATE_QUIET"] = "1"
    run_cmd([sys.executable, "tools/validate_cache.py"], env=validate_env)

    changed_files = git_changed_files()
    if not changed_files:
        print("No repository changes detected.")
        return 0

    print(f"Changed files: {len(changed_files)}")
    for path in changed_files:
        print(f"- {path}")

    if not args.commit:
        return 0

    run_cmd(["git", "add", "public/v1", "data/scheduled_price_refresh_state.json"])
    post_add_changes = git_changed_files()
    if not post_add_changes:
        print("No staged changes to commit.")
        return 0

    run_cmd(["git", "commit", "-m", f"chore: local batch cache refresh ({max(1, args.batch_size)} sets)"])
    if args.push:
        run_cmd(["git", "push", "origin", "main"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
