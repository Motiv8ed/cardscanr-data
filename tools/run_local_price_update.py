#!/usr/bin/env python3
"""Run a local-first small-batch CardScanR price cache update."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CATALOG_CONFIG_PATH = ROOT / "data" / "catalog_config.json"
EN_SETS_PATH = ROOT / "public" / "v1" / "catalog" / "pokemon" / "en" / "sets.json"
DEFAULT_STATE_PATH = ROOT / "data" / "scheduled_price_refresh_state.json"
DEFAULT_RESULT_PATH = ROOT / "logs" / "local_price_update_last_result.json"
PHASE_PREFIX = "CARDSCANR_PHASE "


def load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def write_json_atomic(path: Path, payload: dict) -> None:
    ensure_parent_dir(path)
    tmp_path = path.with_name(f"{path.name}.tmp")
    with open(tmp_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")
    os.replace(tmp_path, path)


def emit_phase(phase: str) -> None:
    print(f"{PHASE_PREFIX}{phase}")


def read_text(path: Path) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


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


def run_cmd(command: list[str], env: dict | None = None) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.stdout:
        print(completed.stdout, end="" if completed.stdout.endswith("\n") else "\n")
    if completed.stderr:
        print(completed.stderr, end="" if completed.stderr.endswith("\n") else "\n", file=sys.stderr)
    if completed.returncode != 0:
        raise RuntimeError(f"Command failed ({completed.returncode}): {' '.join(command)}")
    return completed


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


def price_set_ids_from_files(paths: list[str]) -> list[str]:
    prefix = "public/v1/prices/current/pokemon/en/"
    set_ids: list[str] = []
    for path in paths:
        normalized = path.replace("\\", "/")
        if normalized.startswith(prefix) and normalized.endswith(".json"):
            set_ids.append(Path(normalized).stem)
    return sorted(dict.fromkeys(set_ids))


def git_head_short_hash() -> str | None:
    completed = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return value or None


def main() -> int:
    parser = argparse.ArgumentParser(description="Run local CardScanR batch price refresh")
    parser.add_argument("--batch-size", type=int, default=10, help="Number of EN sets to refresh in this run")
    parser.add_argument("--dry-run", action="store_true", help="Show planned batch without writing files")
    parser.add_argument("--commit", action="store_true", help="Commit any generated changes")
    parser.add_argument("--push", action="store_true", help="Push committed changes to origin/main")
    args = parser.parse_args()

    started_at = time.monotonic()
    started_at_utc = utc_now_iso()
    result_path = Path(os.environ.get("CARDSCANR_LOCAL_UPDATE_RESULT_PATH") or DEFAULT_RESULT_PATH)
    result: dict = {
        "schemaVersion": "1.0.0",
        "startedAtUtc": started_at_utc,
        "finishedAtUtc": None,
        "durationSeconds": None,
        "batchSize": max(1, args.batch_size),
        "plannedSetIds": [],
        "updatedSetIds": [],
        "changedFiles": [],
        "validationPassed": False,
        "commitCreated": False,
        "commitHash": None,
        "pushSucceeded": False,
        "exitCode": 0,
        "error": None,
    }

    def finalize(exit_code: int, error: str | None = None) -> int:
        result["finishedAtUtc"] = utc_now_iso()
        result["durationSeconds"] = max(0, int(round(time.monotonic() - started_at)))
        result["exitCode"] = exit_code
        result["error"] = error
        write_json_atomic(result_path, result)
        return exit_code

    if args.push and not args.commit:
        print("--push requires --commit", file=sys.stderr)
        return finalize(2, "--push requires --commit")

    config = load_config()
    cursor, set_ids, state_path = planned_batch(args.batch_size, config)
    result["plannedSetIds"] = set_ids
    if args.dry_run:
        print("Local updater dry run")
        print(f"- Batch size: {max(1, args.batch_size)}")
        print(f"- Cursor: {cursor}")
        print(f"- Planned set IDs: {', '.join(set_ids) if set_ids else '(none)'}")
        print(f"- State path: {state_path.relative_to(ROOT)}")
        return finalize(0)

    try:
        env = os.environ.copy()
        env["CARDSCANR_CURRENT_PRICE_BATCH_SIZE"] = str(max(1, args.batch_size))

        emit_phase("updating")
        print("Running batch price refresh...")
        run_cmd([sys.executable, "tools/build_price_cache.py", "current_prices"], env=env)

        emit_phase("validating")
        print("Running validation...")
        validate_env = os.environ.copy()
        validate_env["CARDSCANR_VALIDATE_QUIET"] = "1"
        run_cmd([sys.executable, "tools/validate_cache.py"], env=validate_env)
        result["validationPassed"] = True

        changed_files = git_changed_files()
        result["changedFiles"] = changed_files
        result["updatedSetIds"] = price_set_ids_from_files(changed_files)
        if not changed_files:
            print("No repository changes detected.")
            return finalize(0)

        print(f"Changed files: {len(changed_files)}")
        for path in changed_files:
            print(f"- {path}")

        if not args.commit:
            return finalize(0)

        emit_phase("committing")
        run_cmd(["git", "add", "public/v1", "data/scheduled_price_refresh_state.json"])
        post_add_changes = git_changed_files()
        if not post_add_changes:
            print("No staged changes to commit.")
            return finalize(0)

        run_cmd(["git", "commit", "-m", f"chore: local batch cache refresh ({max(1, args.batch_size)} sets)"])
        result["commitCreated"] = True
        result["commitHash"] = git_head_short_hash()

        if args.push:
            emit_phase("pushing")
            run_cmd(["git", "push", "origin", "main"])
            result["pushSucceeded"] = True
        return finalize(0)
    except Exception as exc:
        return finalize(1, str(exc))


if __name__ == "__main__":
    sys.exit(main())
