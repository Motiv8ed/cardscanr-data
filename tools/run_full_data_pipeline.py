#!/usr/bin/env python3
"""Run the full non-eBay CardScanR data pipeline."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
PUBLIC_DIR = ROOT / "public"
V1_DIR = PUBLIC_DIR / "v1"
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"
REPORT_JSON_PATH = REPORTS_DIR / "latest_full_data_pipeline.json"
REPORT_MD_PATH = REPORTS_DIR / "latest_full_data_pipeline.md"
PROVIDER_WORKER_STATUS_PATH = DATA_DIR / "pokewallet_catalog_worker_status.json"
SCHEMA_VERSION = "1.0.0"
DEFAULT_HEARTBEAT_SECONDS = 60

GENERATED_PREFIXES = (
    "public/v1/",
    "data/pokewallet_catalog_full_state.json",
    "data/scheduled_price_refresh_state.json",
)
RUNTIME_PREFIXES = ("reports/", "logs/")
VOLATILE_JSON_KEYS = {
    "generatedAtUtc",
    "builtAtUtc",
    "updatedAtUtc",
    "lastCheckedAtUtc",
    "cacheVersion",
}


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(message: str) -> None:
    print(f"{now_utc()} {message}", flush=True)


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def load_json(path: Path) -> Any:
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def try_load_json(path: Path) -> Any:
    try:
        return load_json(path)
    except (OSError, json.JSONDecodeError):
        return None


def json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write_json_if_changed(path: Path, payload: Any) -> bool:
    encoded = json_bytes(payload)
    if path.exists() and path.read_bytes() == encoded:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_bytes(encoded)
    os.replace(tmp_path, path)
    return True


def write_text_if_changed(path: Path, text: str) -> bool:
    encoded = text.encode("utf-8")
    if path.exists() and path.read_bytes() == encoded:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_bytes(encoded)
    os.replace(tmp_path, path)
    return True


def run_git(args: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def normalize_path(path: str) -> str:
    return path.replace("\\", "/").strip()


def git_status_paths() -> list[str]:
    result = run_git(["status", "--porcelain", "-z"])
    if result.returncode != 0:
        return []
    items = result.stdout.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(items):
        item = items[index]
        index += 1
        if not item:
            continue
        status = item[:2]
        path = normalize_path(item[3:])
        if status.startswith("R") and index < len(items):
            path = normalize_path(items[index])
            index += 1
        if path:
            paths.append(path)
    return paths


def ensure_no_unrelated_dirty_files() -> None:
    unrelated: list[str] = []
    for path in git_status_paths():
        allowed = path.startswith(GENERATED_PREFIXES) or path.startswith(RUNTIME_PREFIXES)
        allowed = allowed or path in {".tmp_validation.log"}
        if not allowed:
            unrelated.append(path)
    if unrelated:
        joined = "\n  ".join(unrelated)
        raise RuntimeError(f"Unrelated dirty files are present; stop before pipeline writes:\n  {joined}")


def strip_volatile(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: strip_volatile(item) for key, item in value.items() if key not in VOLATILE_JSON_KEYS}
    if isinstance(value, list):
        return [strip_volatile(item) for item in value]
    return value


def material_hash(path: Path) -> str:
    if path.suffix.lower() == ".json":
        payload = try_load_json(path)
        if payload is not None:
            encoded = json.dumps(strip_volatile(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tracked_generated_files() -> list[Path]:
    result = run_git(["ls-files", "public/v1", "data"])
    if result.returncode != 0:
        return []
    paths: list[Path] = []
    for line in result.stdout.splitlines():
        path = ROOT / line.strip()
        if path.exists() and path.is_file():
            paths.append(path)
    return paths


def take_snapshot() -> dict[str, dict[str, Any]]:
    snapshot: dict[str, dict[str, Any]] = {}
    for path in tracked_generated_files():
        rel = path.relative_to(ROOT).as_posix()
        snapshot[rel] = {"bytes": path.read_bytes(), "materialHash": material_hash(path)}
    return snapshot


def restore_timestamp_only_changes(snapshot: dict[str, dict[str, Any]]) -> list[str]:
    restored: list[str] = []
    for rel in git_status_paths():
        if not rel.startswith(GENERATED_PREFIXES):
            continue
        old = snapshot.get(rel)
        path = ROOT / rel
        if old is None or not path.exists() or not path.is_file():
            continue
        if path.read_bytes() == old["bytes"]:
            continue
        try:
            current_material = material_hash(path)
        except OSError:
            continue
        if current_material == old["materialHash"]:
            path.write_bytes(old["bytes"])
            restored.append(rel)
    return restored


def normalize_languages(raw: str | None, *, include_zh: bool) -> list[str]:
    values = [item.strip().lower() for item in str(raw or "en,jp").split(",") if item.strip()]
    if include_zh and "zh" not in values:
        values.append("zh")
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def python_executable() -> str:
    venv_python = ROOT / ".venv" / "Scripts" / "python.exe"
    return str(venv_python) if venv_python.exists() else sys.executable


def powershell_executable() -> str:
    return "powershell"


def command_for_display(command: list[str]) -> str:
    return " ".join(command)


def get_heartbeat_seconds() -> int:
    raw = os.getenv("CARDSCANR_PIPELINE_HEARTBEAT_SECONDS", "").strip()
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return DEFAULT_HEARTBEAT_SECONDS


def stream_reader(pipe: Any, output_queue: "queue.Queue[str]") -> None:
    try:
        for line in iter(pipe.readline, ""):
            output_queue.put(line)
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def provider_worker_status_summary(env: dict[str, str] | None) -> str:
    status = try_load_json(PROVIDER_WORKER_STATUS_PATH)
    if not isinstance(status, dict):
        return f"worker status file not available: {PROVIDER_WORKER_STATUS_PATH.relative_to(ROOT).as_posix()}"

    def value(key: str) -> str:
        raw = status.get(key)
        if raw is None or str(raw).strip() == "":
            return "None"
        return str(raw)

    budget_parts = [
        f"cycleMaxRequests={(env or {}).get('CARDSCANR_CURRENT_PRICE_REQUEST_CAP', 'None')}",
        f"hourly={value('hourlyUsedEstimate')}/{value('hourlyTarget')} remaining={value('hourlyRemaining')}",
        f"daily={value('dailyUsedEstimate')}/{value('dailyTarget')} remaining={value('dailyRemaining')}",
        f"source={value('budgetSource')}",
    ]
    return (
        f"currentPriorityLanguage={value('currentPriorityLanguage')} "
        f"nextLanguageToProcess={value('nextLanguageToProcess')} "
        f"lastCycleStartedAtUtc={value('lastCycleStartedAtUtc')} "
        f"lastCycleFinishedAtUtc={value('lastCycleFinishedAtUtc')} "
        f"lastStatus={value('lastStatus')} "
        f"lastCommit={value('lastCommit')} "
        f"requestBudget=({' ; '.join(budget_parts)})"
    )


def print_provider_heartbeat(stage: str, started_monotonic: float, env: dict[str, str] | None) -> None:
    elapsed = format_duration(time.monotonic() - started_monotonic)
    log(f"[{stage}] heartbeat elapsed={elapsed} {provider_worker_status_summary(env)}")


def run_command(
    *,
    stage: str,
    command: list[str],
    env: dict[str, str] | None = None,
    dry_run: bool = False,
    allow_failure: bool = False,
    snapshot: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    started = now_utc()
    started_monotonic = time.monotonic()
    if dry_run:
        log(f"[{stage}] DRY RUN {command_for_display(command)}")
        return {
            "name": stage,
            "status": "dry_run",
            "startedAtUtc": started,
            "finishedAtUtc": now_utc(),
            "command": command,
            "exitCode": None,
            "restoredTimestampOnlyFiles": [],
        }

    log(f"[{stage}] START {command_for_display(command)}")
    if stage == "provider_catalogue":
        if "-UntilComplete" in command:
            log(
                f"[{stage}] -UntilComplete runs provider cycles until complete or budget-limited; "
                "later stages will not start until this provider stage exits."
            )
        else:
            log(f"[{stage}] running one provider catalogue cycle; use -NoFetch for derived-only rebuilds.")
        print_provider_heartbeat(stage, started_monotonic, env)

    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    run_env.setdefault("PYTHONUNBUFFERED", "1")

    process = subprocess.Popen(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=run_env,
        bufsize=1,
    )
    output_queue: "queue.Queue[str]" = queue.Queue()
    if process.stdout is not None:
        reader = threading.Thread(target=stream_reader, args=(process.stdout, output_queue), daemon=True)
        reader.start()

    heartbeat_seconds = get_heartbeat_seconds()
    next_heartbeat = time.monotonic() + heartbeat_seconds
    while True:
        try:
            line = output_queue.get(timeout=1)
            print(line, end="" if line.endswith("\n") else "\n", flush=True)
        except queue.Empty:
            pass

        process_done = process.poll() is not None
        if stage == "provider_catalogue" and not process_done and time.monotonic() >= next_heartbeat:
            print_provider_heartbeat(stage, started_monotonic, env)
            next_heartbeat = time.monotonic() + heartbeat_seconds

        if process_done and output_queue.empty():
            break

    exit_code = process.wait()
    while not output_queue.empty():
        line = output_queue.get_nowait()
        print(line, end="" if line.endswith("\n") else "\n", flush=True)

    restored = restore_timestamp_only_changes(snapshot or {}) if snapshot else []
    status = "passed" if exit_code == 0 else "failed"
    finished = now_utc()
    log(f"[{stage}] FINISH status={status} exitCode={exit_code} elapsed={format_duration(time.monotonic() - started_monotonic)}")
    if exit_code != 0 and not allow_failure:
        raise RuntimeError(f"Stage {stage} failed with exit code {exit_code}")
    return {
        "name": stage,
        "status": status,
        "startedAtUtc": started,
        "finishedAtUtc": finished,
        "command": command,
        "exitCode": exit_code,
        "restoredTimestampOnlyFiles": restored,
    }


def load_provider_worker_config() -> dict[str, Any]:
    config = try_load_json(DATA_DIR / "pokewallet_catalog_config.json")
    worker = config.get("fullCatalogueWorker") if isinstance(config, dict) else {}
    return worker if isinstance(worker, dict) else {}


def resolve_budget(args: argparse.Namespace) -> tuple[int, int, int]:
    worker = load_provider_worker_config()
    hourly = int(args.max_requests_per_hour or os.getenv("CARDSCANR_MAX_REQUESTS_PER_HOUR") or worker.get("maxRequestsPerHour") or 90)
    daily = int(args.max_requests_per_day or os.getenv("CARDSCANR_MAX_REQUESTS_PER_DAY") or worker.get("maxRequestsPerDay") or 900)
    per_cycle = int(worker.get("maxRequestsPerCycle") or 80)
    return hourly, daily, max(1, min(hourly, daily, per_cycle))


def build_provider_command(args: argparse.Namespace, max_requests: int) -> list[str]:
    if args.until_complete:
        command = [
            powershell_executable(),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "scripts" / "run_pokewallet_catalog_worker_loop.ps1"),
            "-MaxRequests",
            str(max_requests),
            "-UntilComplete",
        ]
    else:
        command = [
            powershell_executable(),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "scripts" / "run_pokewallet_catalog_cycle.ps1"),
            "-MaxRequests",
            str(max_requests),
            "-AllLanguages",
        ]
    return command


def build_counts() -> dict[str, Any]:
    provider_status = try_load_json(V1_DIR / "provider-catalog" / "pokewallet" / "status.json")
    provider_counts: dict[str, int] = {}
    if isinstance(provider_status, dict) and isinstance(provider_status.get("languages"), dict):
        for language, payload in provider_status["languages"].items():
            if isinstance(payload, dict):
                provider_counts[str(language)] = int(payload.get("cardCount") or 0)

    app_counts: dict[str, int] = {}
    for path in sorted((V1_DIR / "catalog" / "pokemon").glob("*/sets.json"), key=lambda item: item.as_posix().lower()):
        payload = try_load_json(path)
        if isinstance(payload, dict):
            app_counts[path.parent.name] = int(payload.get("cardCount") or 0)

    image_manifest = try_load_json(V1_DIR / "images" / "cards-manifest.json")
    image_records = image_manifest.get("records") if isinstance(image_manifest, dict) else []
    image_counts: Counter[str] = Counter()
    cached_image_files = 0
    if isinstance(image_records, list):
        for record in image_records:
            if not isinstance(record, dict):
                continue
            image_counts[str(record.get("language") or "unknown")] += 1
            for field in ("localImageSmallPath", "localImageLargePath"):
                value = record.get(field)
                if isinstance(value, str) and value:
                    path = Path(value)
                    if not path.is_absolute():
                        path = ROOT / path
                    if path.is_file():
                        cached_image_files += 1

    price_counts: dict[str, dict[str, Any]] = {}
    current_root = V1_DIR / "prices" / "current" / "pokemon"
    if current_root.exists():
        for language_dir in sorted([item for item in current_root.iterdir() if item.is_dir()], key=lambda item: item.name.lower()):
            source_counts: Counter[str] = Counter()
            status_counts: Counter[str] = Counter()
            total = 0
            for path in sorted(language_dir.glob("*.json"), key=lambda item: item.name.lower()):
                if path.name == "status.json":
                    continue
                payload = try_load_json(path)
                prices = payload.get("prices") if isinstance(payload, dict) else []
                if not isinstance(prices, list):
                    continue
                for record in prices:
                    if not isinstance(record, dict):
                        continue
                    total += 1
                    source_counts[str(record.get("source") or payload.get("source") or "unknown")] += 1
                    status_counts[str(record.get("status") or payload.get("status") or "unknown")] += 1
            price_counts[language_dir.name] = {
                "recordCount": total,
                "sourceCounts": dict(sorted(source_counts.items())),
                "statusCounts": dict(sorted(status_counts.items())),
            }

    history_dates: set[str] = set()
    daily_root = V1_DIR / "history" / "daily"
    if daily_root.exists():
        history_dates = {path.name for path in daily_root.iterdir() if path.is_dir()}

    return {
        "providerCardCountsByLanguage": dict(sorted(provider_counts.items())),
        "appCatalogueCardCountsByLanguage": dict(sorted(app_counts.items())),
        "imageManifestCountByLanguage": dict(sorted(image_counts.items())),
        "localCachedImageFileCount": cached_image_files,
        "currentPriceRecordCountsByLanguageSourceStatus": price_counts,
        "historyDateRange": {
            "firstDate": min(history_dates) if history_dates else None,
            "lastDate": max(history_dates) if history_dates else None,
            "dateCount": len(history_dates),
        },
    }


def missing_areas(languages: list[str], counts: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    app_counts = counts["appCatalogueCardCountsByLanguage"]
    image_counts = counts["imageManifestCountByLanguage"]
    price_counts = counts["currentPriceRecordCountsByLanguageSourceStatus"]
    if "zh" in counts["providerCardCountsByLanguage"] and "zh" not in app_counts:
        missing.append("ZH provider data exists, but ZH is not app-supported or promoted to app catalogue.")
    for language in languages:
        if app_counts.get(language, 0) <= 0:
            missing.append(f"{language.upper()} app catalogue is missing or empty.")
        if image_counts.get(language, 0) <= 0:
            missing.append(f"{language.upper()} image manifest records are missing.")
        if language != "en" and price_counts.get(language, {}).get("recordCount", 0) <= 0:
            missing.append(f"{language.upper()} current prices are unavailable from non-eBay sources.")
    if counts["localCachedImageFileCount"] <= 0:
        missing.append("No local image binaries are cached; URL manifest is the active image path.")
    return missing


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# CardScanR Full Data Pipeline",
        "",
        f"- startedAtUtc: {report['startedAtUtc']}",
        f"- finishedAtUtc: {report['finishedAtUtc']}",
        f"- status: {report['status']}",
        f"- languages: {', '.join(report['languagesProcessed'])}",
        f"- validation: {report['validationResult']}",
        f"- commit: {report.get('commitHash') or 'not committed'}",
        "",
        "## Stages",
    ]
    for stage in report["stagesRun"]:
        lines.append(f"- {stage['name']}: {stage['status']}")
    if report["stagesSkipped"]:
        lines.append("")
        lines.append("## Skipped")
        for item in report["stagesSkipped"]:
            lines.append(f"- {item['name']}: {item['reason']}")
    lines.append("")
    lines.append("## Missing Or Incomplete")
    for item in report["missingIncompleteAreas"] or ["none"]:
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


def commit_and_push() -> str | None:
    run_git(["add", "public/v1", "data/scheduled_price_refresh_state.json"], check=False)
    cached = run_git(["diff", "--cached", "--quiet"])
    if cached.returncode == 0:
        return None
    run_git(["commit", "-m", "Update CardScanR data pipeline outputs"], check=True)
    commit_hash = run_git(["rev-parse", "--short", "HEAD"], check=True).stdout.strip()
    push = run_git(["push", "origin", "main"])
    if push.returncode != 0:
        fetch = run_git(["fetch", "origin"])
        if fetch.returncode == 0:
            rebase = run_git(["rebase", "origin/main"])
            if rebase.returncode == 0:
                retry = run_git(["push", "origin", "main"])
                if retry.returncode == 0:
                    return run_git(["rev-parse", "--short", "HEAD"], check=True).stdout.strip()
        raise RuntimeError("Push failed after one safe fetch/rebase retry. Do not force-push.")
    return commit_hash


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the full non-eBay CardScanR data pipeline.")
    parser.add_argument("--no-fetch", action="store_true", help="Skip provider/API fetching and rebuild derived layers only.")
    parser.add_argument("--until-complete", action="store_true", help="Run provider cycles until complete or budget-limited.")
    parser.add_argument("--max-requests-per-hour", type=int, default=0, help="Provider hourly request budget.")
    parser.add_argument("--max-requests-per-day", type=int, default=0, help="Provider daily request budget.")
    parser.add_argument("--languages", default="en,jp", help="Comma-separated app languages to process.")
    parser.add_argument("--include-zh", action="store_true", help="Include ZH where downstream stages can safely report/build it.")
    parser.add_argument("--build-app-catalogue", dest="build_app_catalogue", action="store_true", default=True)
    parser.add_argument("--skip-app-catalogue", dest="build_app_catalogue", action="store_false")
    parser.add_argument("--build-images", dest="build_images", action="store_true", default=True)
    parser.add_argument("--skip-images", dest="build_images", action="store_false")
    parser.add_argument("--download-images", action="store_true", help="Download a bounded local image cache batch.")
    parser.add_argument("--image-batch-size", type=int, default=20, help="Image download record batch size.")
    parser.add_argument("--build-prices", dest="build_prices", action="store_true", default=True)
    parser.add_argument("--skip-prices", dest="build_prices", action="store_false")
    parser.add_argument("--build-history", dest="build_history", action="store_true", default=True)
    parser.add_argument("--skip-history", dest="build_history", action="store_false")
    parser.add_argument("--validate", dest="validate", action="store_true", default=True)
    parser.add_argument("--skip-validate", dest="validate", action="store_false")
    parser.add_argument("--commit", action="store_true", help="Commit and push meaningful generated output changes.")
    parser.add_argument("--dry-run", action="store_true", help="Show planned actions without writing or fetching.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    started_at = now_utc()
    languages = normalize_languages(args.languages, include_zh=args.include_zh)
    hourly, daily, max_requests = resolve_budget(args)
    stages_run: list[dict[str, Any]] = []
    stages_skipped: list[dict[str, Any]] = []
    validation_result = "skipped"
    commit_hash: str | None = None

    if not args.dry_run:
        ensure_no_unrelated_dirty_files()

    base_env = os.environ.copy()
    base_env["CARDSCANR_MAX_REQUESTS_PER_HOUR"] = str(hourly)
    base_env["CARDSCANR_MAX_REQUESTS_PER_DAY"] = str(daily)
    base_env["CARDSCANR_CURRENT_PRICE_REQUEST_CAP"] = str(max_requests)
    base_env.setdefault("CARDSCANR_PRICE_PROVIDER_PRIORITY", "pokemon_tcg_api,pokewallet")

    try:
        if args.no_fetch:
            stages_skipped.append({"name": "provider_catalogue", "reason": "--no-fetch"})
        else:
            snapshot = take_snapshot()
            stages_run.append(
                run_command(
                    stage="provider_catalogue",
                    command=build_provider_command(args, max_requests),
                    env=base_env,
                    dry_run=args.dry_run,
                    snapshot=snapshot,
                )
            )

        if not args.build_app_catalogue:
            stages_skipped.append({"name": "app_catalogue", "reason": "--skip-app-catalogue"})
            stages_skipped.append({"name": "app_catalogue_promotion", "reason": "--skip-app-catalogue"})
        elif args.no_fetch:
            stages_skipped.append({"name": "app_catalogue", "reason": "--no-fetch skips provider/API app catalogue fetching"})
        else:
            snapshot = take_snapshot()
            stages_run.append(
                run_command(
                    stage="app_catalogue",
                    command=[python_executable(), "tools/build_price_cache.py", "app_catalogue"],
                    env=base_env,
                    dry_run=args.dry_run,
                    snapshot=snapshot,
                )
            )

        if args.build_app_catalogue:
            promotion_command = [
                python_executable(),
                "tools/promote_provider_catalog_to_app_catalog.py",
                "--languages",
                ",".join(languages),
            ]
            if args.include_zh:
                promotion_command.append("--include-zh")
            snapshot = take_snapshot()
            stages_run.append(
                run_command(
                    stage="app_catalogue_promotion",
                    command=promotion_command,
                    dry_run=args.dry_run,
                    snapshot=snapshot,
                )
            )

        if args.build_images:
            image_command = [
                python_executable(),
                "tools/build_image_cache.py",
                "--languages",
                ",".join(languages),
            ]
            if args.include_zh:
                image_command.append("--include-zh")
            if args.download_images:
                image_command.extend(["--download", "--batch-size", str(args.image_batch_size)])
            snapshot = take_snapshot()
            stages_run.append(
                run_command(
                    stage="image_manifest",
                    command=image_command,
                    dry_run=args.dry_run,
                    snapshot=snapshot,
                )
            )
        else:
            stages_skipped.append({"name": "image_manifest", "reason": "--skip-images"})

        if args.build_prices:
            if args.no_fetch:
                stages_skipped.append({"name": "current_prices", "reason": "--no-fetch"})
            else:
                snapshot = take_snapshot()
                stages_run.append(
                    run_command(
                        stage="current_prices_en",
                        command=[python_executable(), "tools/build_price_cache.py", "current_prices"],
                        env=base_env,
                        dry_run=args.dry_run,
                        snapshot=snapshot,
                    )
                )
                if "jp" in languages:
                    snapshot = take_snapshot()
                    stages_run.append(
                        run_command(
                            stage="current_prices_jp_non_ebay",
                            command=[python_executable(), "tools/build_pokewallet_jp_prices.py"],
                            env=base_env,
                            dry_run=args.dry_run,
                            snapshot=snapshot,
                        )
                    )
                if "zh" in languages:
                    stages_skipped.append(
                        {"name": "current_prices_zh_non_ebay", "reason": "No safe non-eBay ZH price builder exists yet"}
                    )
        else:
            stages_skipped.append({"name": "current_prices", "reason": "--skip-prices"})

        if args.build_history:
            snapshot = take_snapshot()
            stages_run.append(
                run_command(
                    stage="history_snapshots",
                    command=[
                        python_executable(),
                        "tools/build_price_history_snapshots.py",
                        "--languages",
                        ",".join(languages),
                    ],
                    dry_run=args.dry_run,
                    snapshot=snapshot,
                )
            )
        else:
            stages_skipped.append({"name": "history_snapshots", "reason": "--skip-history"})

        snapshot = take_snapshot()
        stages_run.append(
            run_command(
                stage="index_manifest_hash_refresh",
                command=[python_executable(), "tools/refresh_public_index.py"],
                dry_run=args.dry_run,
                snapshot=snapshot,
            )
        )

        if args.validate:
            validation_commands = [
                ("validate_cache", [python_executable(), "tools/validate_cache.py"]),
                ("report_en_current_price_migration", [python_executable(), "tools/report_en_current_price_migration.py"]),
                ("report_dataset_coverage", [python_executable(), "tools/report_dataset_coverage.py"]),
                ("report_provider_to_app_gap", [python_executable(), "tools/report_provider_to_app_gap.py", "--summary-only"]),
                ("report_last_worker_change", [python_executable(), "tools/report_last_worker_change.py"]),
            ]
            for name, command in validation_commands:
                stages_run.append(run_command(stage=name, command=command, dry_run=args.dry_run))
            validation_result = "passed" if not args.dry_run else "dry_run"
        else:
            stages_skipped.append({"name": "validation", "reason": "--skip-validate"})

        if args.commit and not args.dry_run:
            commit_hash = commit_and_push()

        counts = build_counts()
        report = {
            "schemaVersion": SCHEMA_VERSION,
            "startedAtUtc": started_at,
            "finishedAtUtc": now_utc(),
            "status": "dry_run" if args.dry_run else "passed",
            "dryRun": bool(args.dry_run),
            "nonEbayOnly": True,
            "requestBudgets": {
                "maxRequestsPerHour": hourly,
                "maxRequestsPerDay": daily,
                "maxRequestsThisProviderCycle": max_requests,
            },
            "languagesProcessed": languages,
            "stagesRun": stages_run,
            "stagesSkipped": stages_skipped,
            **counts,
            "validationResult": validation_result,
            "filesChanged": git_status_paths(),
            "commitHash": commit_hash,
            "missingIncompleteAreas": missing_areas(languages, counts),
        }
        if not args.dry_run:
            write_json_if_changed(REPORT_JSON_PATH, report)
            write_text_if_changed(REPORT_MD_PATH, markdown_report(report))
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001
        counts = build_counts()
        report = {
            "schemaVersion": SCHEMA_VERSION,
            "startedAtUtc": started_at,
            "finishedAtUtc": now_utc(),
            "status": "failed",
            "dryRun": bool(args.dry_run),
            "nonEbayOnly": True,
            "languagesProcessed": languages,
            "stagesRun": stages_run,
            "stagesSkipped": stages_skipped,
            **counts,
            "validationResult": validation_result,
            "filesChanged": git_status_paths(),
            "commitHash": commit_hash,
            "error": str(exc),
            "missingIncompleteAreas": missing_areas(languages, counts),
        }
        if not args.dry_run:
            write_json_if_changed(REPORT_JSON_PATH, report)
            write_text_if_changed(REPORT_MD_PATH, markdown_report(report))
        print(json.dumps(report, indent=2, sort_keys=True))
        print(f"Pipeline failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
