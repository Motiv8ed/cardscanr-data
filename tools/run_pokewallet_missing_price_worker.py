#!/usr/bin/env python3
"""Automate bounded JP PokeWallet missing-set price imports."""

from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime, timezone
import json
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = ROOT / "reports"
IMPORTER_REPORT_JSON = REPORTS_DIR / "pokewallet_price_import_latest.json"
WORKER_REPORT_JSON = REPORTS_DIR / "pokewallet_missing_price_worker_latest.json"
WORKER_REPORT_MD = REPORTS_DIR / "pokewallet_missing_price_worker_latest.md"
WORKER_RUNS_JSONL = REPORTS_DIR / "pokewallet_missing_price_worker_runs.jsonl"
CURRENT_JP_DIR = ROOT / "public" / "v1" / "prices" / "current" / "pokemon" / "jp"

EXPECTED_GENERATED_DIR_PREFIXES = (
    "public/v1/prices/current/pokemon/jp/",
    "public/v1/prices/history/pokemon/jp/",
    "reports/",
)
EXPECTED_GENERATED_FILES = {
    "public/v1/index.json",
    "public/v1/prices/status.json",
}
FORBIDDEN_COMMIT_PATHS = {
    "data/pokewallet_price_request_ledger.json",
    "reports/pokewallet_price_import_latest.json",
    "reports/pokewallet_price_import_latest.md",
}
WORKER_RUNTIME_REPORT_PATHS = {
    "reports/pokewallet_missing_price_worker_latest.json",
    "reports/pokewallet_missing_price_worker_latest.md",
    "reports/pokewallet_missing_price_worker_runs.jsonl",
}
HEARTBEAT_SECONDS = 60


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(value: datetime | None = None) -> str:
    ts = value or utc_now()
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def redact_text(text: str) -> str:
    value = text
    value = re.sub(r"(?i)(authorization\s*:\s*bearer\s+)([^\s]+)", r"\1[REDACTED]", value)
    value = re.sub(r"(?i)(x-api-key\s*[:=]\s*)([^\s]+)", r"\1[REDACTED]", value)
    value = re.sub(r"(?i)(api[_-]?key\s*[:=]\s*)([^\s]+)", r"\1[REDACTED]", value)
    value = re.sub(r"(?i)(POKEWALLET_API_KEY\s*[:=]\s*)([^\s]+)", r"\1[REDACTED]", value)
    value = re.sub(r"(?i)(CARDSCANR_POKEWALLET_API_KEY\s*[:=]\s*)([^\s]+)", r"\1[REDACTED]", value)
    return value


def worker_log(message: str) -> None:
    print(f"[{utc_iso()}] {redact_text(message)}", flush=True)


def child_log(stage: str, line: str) -> None:
    cleaned = line.rstrip("\r\n")
    if not cleaned:
        return
    print(f"[{stage}] {redact_text(cleaned)}", flush=True)


def stage_name_for_command(command: list[str], fallback: str) -> str:
    for token in command:
        lower = token.lower()
        if lower.endswith("validate_cache.py"):
            return "validate_cache"
        if lower.endswith("report_dataset_coverage.py"):
            return "report_dataset_coverage"
        if lower.endswith("report_data_health.py"):
            return "report_data_health"
        if lower.endswith("export_chatgpt_report.py"):
            return "export"
        if lower.endswith("import_pokewallet_set_prices.py"):
            return "importer"
        if lower.endswith("release_cardscanr_data.ps1"):
            return "release"
        if lower.endswith("git"):
            continue
    return fallback


def heartbeat_message(stage: str, elapsed_seconds: int) -> str:
    import_report = read_json(IMPORTER_REPORT_JSON) or {}
    parts = [f"heartbeat stage={stage} elapsed={elapsed_seconds}s"]
    if "importedRecords" in import_report:
        parts.append(f"latestImported={int(import_report.get('importedRecords') or 0)}")
    if "hourlyUsed" in import_report or "hourlyRemaining" in import_report:
        parts.append(
            f"hourlyUsed={int(import_report.get('hourlyUsed') or 0)} hourlyRemaining={int(import_report.get('hourlyRemaining') or 0)}"
        )
    if "dailyUsed" in import_report or "dailyRemaining" in import_report:
        parts.append(
            f"dailyUsed={int(import_report.get('dailyUsed') or 0)} dailyRemaining={int(import_report.get('dailyRemaining') or 0)}"
        )
    selected_set_ids = import_report.get("selectedSetIds") if isinstance(import_report.get("selectedSetIds"), list) else []
    if selected_set_ids:
        preview = ",".join(str(item) for item in selected_set_ids[:5])
        if len(selected_set_ids) > 5:
            preview = f"{preview},..."
        parts.append(f"selectedSetIds={preview}")
    return " ".join(parts)


def sleep_with_progress(wait_seconds: int, poll_seconds: int, reason: str) -> None:
    remaining = max(0, int(wait_seconds))
    step_seconds = max(1, int(poll_seconds or 1))
    while remaining > 0:
        this_sleep = min(step_seconds, remaining)
        worker_log(f"{reason}; sleeping {this_sleep} seconds before retry")
        time.sleep(this_sleep)
        remaining -= this_sleep


def run_command(
    command: list[str],
    *,
    allow_failure: bool = False,
    stage: str = "command",
    heartbeat_stage: str | None = None,
    heartbeat_seconds: int = HEARTBEAT_SECONDS,
    stream_output: bool = True,
) -> dict[str, Any]:
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=1,
    )

    stream_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()
    stdout_tail: deque[str] = deque(maxlen=40)
    stderr_tail: deque[str] = deque(maxlen=40)

    def pump_output(label: str, stream: Any) -> None:
        try:
            for raw in iter(stream.readline, ""):
                stream_queue.put((label, raw))
        finally:
            stream.close()
            stream_queue.put((label, None))

    stdout_thread = threading.Thread(target=pump_output, args=("stdout", process.stdout), daemon=True)
    stderr_thread = threading.Thread(target=pump_output, args=("stderr", process.stderr), daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    closed_streams = 0
    started = time.monotonic()
    last_heartbeat = started
    effective_heartbeat_stage = heartbeat_stage or stage

    while closed_streams < 2:
        try:
            label, raw_line = stream_queue.get(timeout=0.25)
            if raw_line is None:
                closed_streams += 1
                continue
            line = raw_line.rstrip("\r\n")
            if label == "stdout":
                stdout_tail.append(line)
            else:
                stderr_tail.append(line)
            if stream_output:
                child_log(stage, line)
        except queue.Empty:
            pass

        now = time.monotonic()
        if process.poll() is None and (now - last_heartbeat) >= max(1, int(heartbeat_seconds)):
            elapsed = int(now - started)
            worker_log(heartbeat_message(effective_heartbeat_stage, elapsed))
            last_heartbeat = now

    stdout_thread.join(timeout=1)
    stderr_thread.join(timeout=1)
    return_code = process.wait()

    result = {
        "command": " ".join(command),
        "returnCode": return_code,
        "stdoutTail": list(stdout_tail),
        "stderrTail": list(stderr_tail),
    }
    if return_code != 0 and not allow_failure:
        raise RuntimeError(f"Command failed ({return_code}): {' '.join(command)}")
    return result


def command_return_code(result: dict[str, Any]) -> int:
    raw_code = result.get("returnCode")
    try:
        return int(raw_code)
    except (TypeError, ValueError):
        return 1


def first_failed_command(results: list[dict[str, Any]]) -> dict[str, Any] | None:
    for item in results:
        if command_return_code(item) != 0:
            return {
                "command": item.get("command"),
                "returnCode": command_return_code(item),
                "stdoutTail": item.get("stdoutTail") if isinstance(item.get("stdoutTail"), list) else [],
                "stderrTail": item.get("stderrTail") if isinstance(item.get("stderrTail"), list) else [],
            }
    return None


def run_git_sync(*, skip: bool) -> dict[str, Any]:
    if skip:
        return {
            "skipped": True,
            "status": "skipped",
            "steps": [],
            "commandStyle": "manual_skip",
        }

    steps: list[dict[str, Any]] = []
    for command, stage in (
        (["git", "fetch", "origin"], "git_fetch"),
        (["git", "rebase", "origin/main"], "git_rebase"),
    ):
        step = run_command(
            command,
            allow_failure=True,
            stage=stage,
            heartbeat_stage=stage,
        )
        step["stdout"] = "\n".join(step.get("stdoutTail") or [])
        step["stderr"] = "\n".join(step.get("stderrTail") or [])
        steps.append(step)
        if command_return_code(step) != 0:
            return {
                "skipped": False,
                "status": "failed",
                "steps": steps,
                "commandStyle": "fetch_then_rebase",
            }

    return {
        "skipped": False,
        "status": "ok",
        "steps": steps,
        "commandStyle": "fetch_then_rebase",
    }


def normalize_git_path(path: str) -> str:
    value = path.replace("\\", "/").strip()
    if " -> " in value:
        value = value.split(" -> ", 1)[1].strip()
    return value


def git_changed_paths() -> list[str]:
    paths: list[str] = []
    full = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    lines = full.stdout.splitlines()
    for line in lines:
        if len(line) < 4:
            continue
        raw = normalize_git_path(line[3:])
        if raw:
            paths.append(raw)
    return sorted(set(paths))


def is_expected_generated_path(path: str) -> bool:
    normalized = normalize_git_path(path).lower()
    if normalized in WORKER_RUNTIME_REPORT_PATHS:
        return True
    if normalized in FORBIDDEN_COMMIT_PATHS:
        return False
    if normalized in EXPECTED_GENERATED_FILES:
        return True
    return any(normalized.startswith(prefix) for prefix in EXPECTED_GENERATED_DIR_PREFIXES)


def count_jp_price_state() -> dict[str, int]:
    record_count = 0
    file_count = 0
    if not CURRENT_JP_DIR.exists():
        return {"recordCount": 0, "fileCount": 0}
    for path in sorted(CURRENT_JP_DIR.glob("*.json"), key=lambda item: item.name):
        if path.name == "status.json":
            continue
        payload = read_json(path)
        if not isinstance(payload, dict):
            continue
        prices = payload.get("prices")
        if not isinstance(prices, list):
            continue
        file_count += 1
        record_count += sum(1 for item in prices if isinstance(item, dict))
    return {"recordCount": record_count, "fileCount": file_count}


def build_importer_command(args: argparse.Namespace) -> list[str]:
    command = [
        sys.executable,
        "tools/import_pokewallet_set_prices.py",
        "--languages",
        args.language,
        "--source",
        "both",
        "--only-missing-set-prices",
        "--max-new-sets",
        str(args.max_new_sets_per_cycle),
        "--respect-budget",
        "--fit-budget",
        "--commit-safe-report",
    ]
    if args.dry_run_only:
        command.append("--dry-run")
    else:
        command.append("--write")
    if args.reset_budget_ledger:
        command.append("--reset-budget-ledger")
    return command


def build_worker_command(args: argparse.Namespace, *, sleep_mode: bool = False) -> str:
    pieces = [
        ".\\scripts\\run_pokewallet_missing_price_worker.ps1",
        f"-Language {args.language}",
        f"-MaxNewSetsPerCycle {args.max_new_sets_per_cycle}",
        "-UntilComplete",
    ]
    if args.commit:
        pieces.append("-Commit")
    if args.push and not args.no_push:
        pieces.append("-Push")
    if args.validate:
        pieces.append("-Validate")
    if args.export_chatgpt_report:
        pieces.append("-ExportChatGPTReport")
    if sleep_mode:
        pieces.append("-SleepWhenBudgetBlocked")
    pieces.append(f"-PollSeconds {args.poll_seconds}")
    if args.stop_after_daily_budget:
        pieces.append("-StopAfterDailyBudget")
    if args.dry_run_only:
        pieces.append("-DryRunOnly")
    if args.no_push:
        pieces.append("-NoPush")
    if args.skip_git_sync:
        pieces.append("-SkipGitSync")
    if args.reset_budget_ledger:
        pieces.append("-ResetBudgetLedger")
    return " ".join(pieces)


def should_stop_for_dirty_tree(paths: list[str], commit_enabled: bool) -> tuple[bool, str]:
    if not paths:
        return False, ""
    if not commit_enabled:
        return True, "git_dirty"
    if all(is_expected_generated_path(path) for path in paths):
        return False, ""
    return True, "git_dirty"


def should_mark_complete(import_report: dict[str, Any]) -> bool:
    planned = int(import_report.get("plannedRequests") or 0)
    selected = import_report.get("selectedSetIds") if isinstance(import_report.get("selectedSetIds"), list) else []
    return planned == 0 and len(selected) == 0


def is_budget_blocked(import_report: dict[str, Any]) -> bool:
    status = str(import_report.get("status") or "")
    decision = str(import_report.get("budgetDecision") or "")
    return (
        int(import_report.get("apiRequestsUsed") or 0) == 0
        and status == "blocked"
        and (
            decision.startswith("blocked_")
            or decision == "blocked_planned_exceeds_budget"
            or int(import_report.get("requestsAllowedByBudget") or 0) <= 0
        )
    )


def estimate_budget_wait_seconds(import_report: dict[str, Any], fallback_seconds: int) -> int:
    now = utc_now()
    candidates: list[int] = []
    for key in ("hourlyResetAtUtc", "dailyResetAtUtc"):
        parsed = parse_utc(import_report.get(key))
        if parsed is None:
            continue
        seconds = int((parsed - now).total_seconds())
        if seconds > 0:
            candidates.append(seconds)
    if not candidates:
        return max(1, fallback_seconds)
    return max(1, min(candidates))


def run_validations() -> list[dict[str, Any]]:
    checks = [
        [sys.executable, "tools/validate_cache.py"],
        [sys.executable, "tools/report_dataset_coverage.py"],
        [sys.executable, "tools/report_data_health.py"],
    ]
    results: list[dict[str, Any]] = []
    for command in checks:
        result = run_command(
            command,
            allow_failure=True,
            stage=stage_name_for_command(command, "validation"),
            heartbeat_stage="validation",
        )
        results.append(result)
        if command_return_code(result) != 0:
            break
    return results


def run_release(push_enabled: bool) -> dict[str, Any]:
    before = run_command(["git", "rev-parse", "HEAD"], allow_failure=False, stage="git", stream_output=False)
    before_hash = before["stdoutTail"][-1].strip() if before["stdoutTail"] else ""

    command = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "scripts/release_cardscanr_data.ps1"]
    if push_enabled:
        command.append("-Push")
    release_result = run_command(command, allow_failure=True, stage="release", heartbeat_stage="release")

    after = run_command(["git", "rev-parse", "HEAD"], allow_failure=False, stage="git", stream_output=False)
    after_hash = after["stdoutTail"][-1].strip() if after["stdoutTail"] else ""

    committed = bool(after_hash and before_hash and after_hash != before_hash)
    pushed_hashes: list[str] = []
    if committed and push_enabled and command_return_code(release_result) == 0:
        pushed_hashes.append(after_hash)

    return {
        "beforeHead": before_hash,
        "afterHead": after_hash,
        "committed": committed,
        "pushedCommitHashes": pushed_hashes,
        "result": release_result,
    }


def run_export_chatgpt() -> dict[str, Any]:
    return run_command(
        [sys.executable, "tools/export_chatgpt_report.py"],
        allow_failure=True,
        stage="export",
        heartbeat_stage="export",
    )


def render_markdown(summary: dict[str, Any]) -> str:
    lines: list[str] = []
    add = lines.append
    add("# PokeWallet Missing Price Worker")
    add("")
    add(f"- startedAtUtc: {summary.get('startedAtUtc')}")
    add(f"- finishedAtUtc: {summary.get('finishedAtUtc')}")
    add(f"- status: {summary.get('status')}")
    add(f"- stopReason: {summary.get('stopReason')}")
    add(f"- cyclesAttempted: {summary.get('cyclesAttempted', 0)}")
    add(f"- cyclesCompleted: {summary.get('cyclesCompleted', 0)}")
    add(f"- cyclesBlockedByBudget: {summary.get('cyclesBlockedByBudget', 0)}")
    add(f"- totalApiRequests: {summary.get('totalApiRequests', 0)}")
    add(f"- totalImportedRecords: {summary.get('totalImportedRecords', 0)}")
    add(f"- apiKeyPresent: {summary.get('apiKeyPresent')}")
    add(f"- apiKeySource: {summary.get('apiKeySource')}")
    add(f"- apiKeyFingerprint: {summary.get('apiKeyFingerprint')}")
    add(f"- multipleApiKeysDetected: {summary.get('multipleApiKeysDetected')}")
    add(f"- keySourceWarning: {summary.get('keySourceWarning')}")
    add(f"- beforeJpPriceCount: {summary.get('beforeJpPriceCount', 0)}")
    add(f"- afterJpPriceCount: {summary.get('afterJpPriceCount', 0)}")
    add(f"- beforeJpPriceFileCount: {summary.get('beforeJpPriceFileCount', 0)}")
    add(f"- afterJpPriceFileCount: {summary.get('afterJpPriceFileCount', 0)}")
    add(f"- lastSelectedSetIds: {summary.get('lastSelectedSetIds', [])}")
    add(f"- lastImporterStatus: {summary.get('lastImporterStatus')}")
    add(f"- commitHashesPushed: {summary.get('commitHashesPushed', [])}")
    add(f"- nextRecommendedCommand: {summary.get('nextRecommendedCommand')}")
    add("")
    add("## Validation Results")
    for item in summary.get("validationResults", []):
        add(f"- {item.get('command')}: rc={item.get('returnCode')}")
    validation_failure = summary.get("validationFailure")
    if isinstance(validation_failure, dict):
        add("")
        add("## Validation Failure")
        add(f"- command: {validation_failure.get('command')}")
        add(f"- returnCode: {validation_failure.get('returnCode')}")
        add(f"- stdoutTail: {validation_failure.get('stdoutTail', [])}")
        add(f"- stderrTail: {validation_failure.get('stderrTail', [])}")
    add("")
    add("## Cycle Notes")
    for note in summary.get("cycleNotes", []):
        add(f"- {note}")
    add("")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the PokeWallet missing-price worker loop.")
    parser.add_argument("--language", default="jp", choices=["jp"], help="Target language.")
    parser.add_argument("--max-new-sets-per-cycle", type=int, default=20)
    parser.add_argument("--until-complete", action="store_true")
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--push", action="store_true")
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--export-chatgpt-report", action="store_true")
    parser.add_argument("--sleep-when-budget-blocked", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=300)
    parser.add_argument("--max-cycles", type=int, default=0)
    parser.add_argument("--stop-after-daily-budget", action="store_true")
    parser.add_argument("--dry-run-only", action="store_true")
    parser.add_argument("--no-push", action="store_true")
    parser.add_argument("--skip-git-sync", action="store_true")
    parser.add_argument("--reset-budget-ledger", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    started_at = utc_now()
    before_counts = count_jp_price_state()
    worker_log("worker started")
    worker_log(
        "settings "
        f"language={args.language} "
        f"maxNewSetsPerCycle={args.max_new_sets_per_cycle} "
        f"untilComplete={'yes' if args.until_complete else 'no'} "
        f"commit={'yes' if args.commit else 'no'} "
        f"push={'yes' if (args.push and not args.no_push) else 'no'} "
        f"validate={'yes' if args.validate else 'no'} "
        f"sleepWhenBudgetBlocked={'yes' if args.sleep_when_budget_blocked else 'no'} "
        f"pollSeconds={args.poll_seconds} "
        f"skipGitSync={'yes' if args.skip_git_sync else 'no'} "
        f"resetBudgetLedger={'yes' if args.reset_budget_ledger else 'no'}"
    )

    summary: dict[str, Any] = {
        "schemaVersion": "1.0.0",
        "startedAtUtc": utc_iso(started_at),
        "finishedAtUtc": None,
        "status": "running",
        "stopReason": "",
        "cyclesAttempted": 0,
        "cyclesCompleted": 0,
        "cyclesBlockedByBudget": 0,
        "totalApiRequests": 0,
        "totalImportedRecords": 0,
        "apiKeyPresent": False,
        "apiKeySource": "unknown",
        "apiKeyFingerprint": None,
        "multipleApiKeysDetected": False,
        "keySourceWarning": None,
        "beforeJpPriceCount": int(before_counts.get("recordCount") or 0),
        "afterJpPriceCount": int(before_counts.get("recordCount") or 0),
        "beforeJpPriceFileCount": int(before_counts.get("fileCount") or 0),
        "afterJpPriceFileCount": int(before_counts.get("fileCount") or 0),
        "lastSelectedSetIds": [],
        "lastImporterStatus": "not_run",
        "validationResults": [],
        "validationFailure": None,
        "commitHashesPushed": [],
        "nextRecommendedCommand": build_worker_command(args, sleep_mode=True),
        "cycleNotes": [],
        "gitSync": {
            "skipped": False,
            "status": "not_run",
            "steps": [],
            "commandStyle": "fetch_then_rebase",
        },
    }

    keep_running = True
    cycle_index = 0
    max_cycles = max(0, int(args.max_cycles or 0))
    push_enabled = bool(args.push) and not bool(args.no_push)

    while keep_running:
        if max_cycles > 0 and cycle_index >= max_cycles:
            summary["status"] = "max_cycles_reached"
            summary["stopReason"] = "max_cycles"
            worker_log("max cycles reached; stopping")
            break
        if not args.until_complete and cycle_index >= 1:
            summary["status"] = "single_cycle_complete"
            summary["stopReason"] = "single_cycle"
            worker_log("single-cycle mode complete; stopping")
            break

        worker_log(f"cycle {cycle_index + 1} starting")

        changed_paths = git_changed_paths()
        stop_for_dirty, dirty_reason = should_stop_for_dirty_tree(changed_paths, bool(args.commit))
        if stop_for_dirty:
            summary["status"] = "git_dirty"
            summary["stopReason"] = dirty_reason
            summary["cycleNotes"].append("Worker stopped because repository has unexpected local changes.")
            worker_log("git status dirty; stopping before importer")
            break

        if not changed_paths:
            worker_log("git status clean")
            git_sync = run_git_sync(skip=bool(args.skip_git_sync))
            summary["gitSync"] = git_sync
            if git_sync.get("status") == "failed":
                summary["status"] = "git_rebase_failed"
                summary["stopReason"] = "git_rebase_failed"
                summary["cycleNotes"].append("Git sync failed before importer start.")
                worker_log("git sync failed before importer")
                break
        else:
            summary["cycleNotes"].append("Continuing with expected generated changes already in working tree.")
            worker_log("git status has expected generated paths; continuing")

        cycle_index += 1
        summary["cyclesAttempted"] = cycle_index

        worker_log(f"selecting missing {args.language} price sets")
        worker_log(f"running importer: maxNewSets={args.max_new_sets_per_cycle}")
        importer_result = run_command(
            build_importer_command(args),
            allow_failure=True,
            stage="importer",
            heartbeat_stage="importer",
        )
        import_report = read_json(IMPORTER_REPORT_JSON) or {}
        summary["lastImporterStatus"] = str(import_report.get("status") or "unknown")
        summary["apiKeyPresent"] = bool(import_report.get("apiKeyPresent"))
        summary["apiKeySource"] = str(import_report.get("apiKeySource") or "unknown")
        summary["apiKeyFingerprint"] = import_report.get("apiKeyFingerprint")
        summary["multipleApiKeysDetected"] = bool(import_report.get("multipleApiKeysDetected"))
        summary["keySourceWarning"] = import_report.get("keySourceWarning")
        summary["lastSelectedSetIds"] = (
            import_report.get("selectedSetIds") if isinstance(import_report.get("selectedSetIds"), list) else []
        )
        summary["totalApiRequests"] += int(import_report.get("apiRequestsUsed") or 0)
        summary["totalImportedRecords"] += int(import_report.get("importedRecords") or 0)
        worker_log(
            "importer finished: "
            f"status={summary['lastImporterStatus']} "
            f"apiRequests={int(import_report.get('apiRequestsUsed') or 0)} "
            f"importedRecords={int(import_report.get('importedRecords') or 0)} "
            f"endpointFailures={int(import_report.get('endpointFailures') or 0)}"
        )
        if summary.get("apiKeyFingerprint"):
            worker_log(f"api key fingerprint: {summary.get('apiKeyFingerprint')}")

        if should_mark_complete(import_report):
            summary["status"] = "complete"
            summary["stopReason"] = "complete"
            worker_log("no missing JP price sets remain")
            break

        if bool(import_report.get("rateLimitDetected")):
            summary["status"] = "rate_limited"
            summary["stopReason"] = "rate_limited"
            summary["cyclesBlockedByBudget"] += 1
            worker_log(
                "rate limited: "
                f"hourlyUsed={int(import_report.get('hourlyUsed') or 0)} "
                f"hourlyRemaining={int(import_report.get('hourlyRemaining') or 0)} "
                f"dailyUsed={int(import_report.get('dailyUsed') or 0)} "
                f"dailyRemaining={int(import_report.get('dailyRemaining') or 0)}"
            )
            worker_log(f"ledger path: {import_report.get('budgetLedgerPath') or 'n/a'}")
            worker_log(f"api key fingerprint: {import_report.get('apiKeyFingerprint') or 'n/a'}")
            worker_log("If dashboard shows available quota, run with --reset-budget-ledger / -ResetBudgetLedger.")
            if args.sleep_when_budget_blocked:
                wait_seconds = estimate_budget_wait_seconds(import_report, max(1, int(args.poll_seconds or 300)))
                summary["cycleNotes"].append(f"Rate-limited. Sleeping for {wait_seconds}s before retry.")
                worker_log(f"rate-limited wait estimate: {wait_seconds}s pollSeconds={int(args.poll_seconds or 300)}")
                if args.stop_after_daily_budget and int(import_report.get("dailyRemaining") or 0) <= 0:
                    summary["status"] = "budget_exhausted"
                    summary["stopReason"] = "daily_budget_exhausted"
                    worker_log("daily budget exhausted; stopping")
                    break
                sleep_with_progress(wait_seconds, int(args.poll_seconds or 300), "[worker] rate-limited")
                continue
            break

        if bool(import_report.get("allEndpointsFailed")):
            summary["status"] = "all_endpoints_failed"
            summary["stopReason"] = "all_endpoints_failed"
            worker_log("all importer endpoints failed; stopping")
            break

        if str(import_report.get("status") or "") == "auth_or_plan_failure":
            summary["status"] = "auth_or_plan_failure"
            summary["stopReason"] = "auth_or_plan_failure"
            summary["cycleNotes"].append("Importer stopped on first 401/403 auth-or-plan failure to protect request budget.")
            worker_log("auth-or-plan failure detected; stopping to protect budget")
            break

        if is_budget_blocked(import_report):
            summary["cyclesBlockedByBudget"] += 1
            summary["status"] = "budget_blocked"
            summary["stopReason"] = "budget_blocked"
            wait_seconds = estimate_budget_wait_seconds(import_report, max(1, int(args.poll_seconds or 300)))
            summary["cycleNotes"].append(f"Budget blocked with zero API calls. Next wait estimate: {wait_seconds}s.")
            worker_log(
                "budget blocked: "
                f"hourlyUsed={int(import_report.get('hourlyUsed') or 0)} "
                f"hourlyRemaining={int(import_report.get('hourlyRemaining') or 0)} "
                f"dailyUsed={int(import_report.get('dailyUsed') or 0)} "
                f"dailyRemaining={int(import_report.get('dailyRemaining') or 0)} "
                f"waitEstimate={wait_seconds}s pollSeconds={int(args.poll_seconds or 300)}"
            )
            worker_log(f"ledger path: {import_report.get('budgetLedgerPath') or 'n/a'}")
            worker_log(f"api key fingerprint: {import_report.get('apiKeyFingerprint') or 'n/a'}")
            worker_log("If dashboard shows available quota, run with --reset-budget-ledger / -ResetBudgetLedger.")
            if args.stop_after_daily_budget and int(import_report.get("dailyRemaining") or 0) <= 0:
                summary["status"] = "budget_exhausted"
                summary["stopReason"] = "daily_budget_exhausted"
                worker_log("daily budget exhausted; stopping")
                break
            if args.sleep_when_budget_blocked:
                sleep_with_progress(wait_seconds, int(args.poll_seconds or 300), "[worker] budget blocked")
                continue
            break

        imported_this_cycle = int(import_report.get("importedRecords") or 0)
        api_used_this_cycle = int(import_report.get("apiRequestsUsed") or 0)
        if int(importer_result.get("returnCode") or 0) != 0 and imported_this_cycle == 0 and api_used_this_cycle == 0:
            summary["status"] = "importer_failed"
            summary["stopReason"] = "importer_failed"
            worker_log("importer failed with zero imported records and zero API usage")
            break

        validation_results: list[dict[str, Any]] = []
        if imported_this_cycle > 0 or bool(args.validate):
            worker_log("validation starting")
            validation_results = run_validations()
            summary["validationResults"] = validation_results
            validation_failure = first_failed_command(validation_results)
            summary["validationFailure"] = validation_failure
            failed_validation = validation_failure is not None
            if failed_validation:
                summary["status"] = "validation_failed"
                summary["stopReason"] = "validation_failed"
                summary["cycleNotes"].append(
                    "Validation failed for command "
                    f"{validation_failure.get('command')} "
                    f"(rc={validation_failure.get('returnCode')})."
                )
                worker_log(
                    "validation failed: "
                    f"command={validation_failure.get('command')} "
                    f"rc={validation_failure.get('returnCode')} "
                    f"stdoutTail={validation_failure.get('stdoutTail')} "
                    f"stderrTail={validation_failure.get('stderrTail')}"
                )
                break
            worker_log("validation passed")

        if imported_this_cycle > 0 and bool(args.commit) and not bool(args.dry_run_only):
            worker_log("release/commit/push starting")
            release = run_release(push_enabled)
            summary["validationResults"].append({
                "command": release["result"]["command"],
                "returnCode": release["result"]["returnCode"],
            })
            if command_return_code(release["result"]) != 0:
                summary["status"] = "release_failed"
                summary["stopReason"] = "release_failed"
                worker_log("release/commit/push failed")
                break
            for commit_hash in release.get("pushedCommitHashes", []):
                if commit_hash not in summary["commitHashesPushed"]:
                    summary["commitHashesPushed"].append(commit_hash)
            worker_log(
                "release/commit/push passed "
                f"commit={release.get('afterHead') or 'none'}"
            )

        if bool(args.export_chatgpt_report):
            worker_log("exporting ChatGPT report")
            export_result = run_export_chatgpt()
            summary["validationResults"].append(
                {
                    "command": export_result["command"],
                    "returnCode": export_result["returnCode"],
                }
            )
            if command_return_code(export_result) != 0:
                summary["status"] = "export_failed"
                summary["stopReason"] = "export_failed"
                worker_log("export ChatGPT report failed")
                break
            worker_log("export ChatGPT report passed")

        summary["cyclesCompleted"] = int(summary.get("cyclesCompleted") or 0) + 1
        worker_log(f"cycle {cycle_index} complete")

        if not args.until_complete:
            summary["status"] = "single_cycle_complete"
            summary["stopReason"] = "single_cycle"
            worker_log("single-cycle mode complete; stopping")
            break

        if args.stop_after_daily_budget and int(import_report.get("dailyRemaining") or 0) <= 0:
            summary["status"] = "budget_exhausted"
            summary["stopReason"] = "daily_budget_exhausted"
            worker_log("daily budget exhausted; stopping")
            break

        if imported_this_cycle == 0 and api_used_this_cycle > 0:
            summary["status"] = "no_imported_records"
            summary["stopReason"] = "no_imported_records"
            worker_log("no records imported this cycle despite API usage; stopping")
            break

    if summary.get("status") == "running":
        summary["status"] = "stopped"
        summary["stopReason"] = "stopped"

    after_counts = count_jp_price_state()
    summary["afterJpPriceCount"] = int(after_counts.get("recordCount") or 0)
    summary["afterJpPriceFileCount"] = int(after_counts.get("fileCount") or 0)
    summary["finishedAtUtc"] = utc_iso()

    if summary.get("status") == "complete":
        worker_log(
            "no missing JP price sets remain "
            f"finalJpPriceCount={summary['afterJpPriceCount']} finalJpPriceFileCount={summary['afterJpPriceFileCount']}"
        )

    if summary.get("status") in {"budget_blocked", "budget_exhausted", "rate_limited"}:
        summary["nextRecommendedCommand"] = build_worker_command(args, sleep_mode=True)
    else:
        summary["nextRecommendedCommand"] = build_worker_command(args, sleep_mode=bool(args.sleep_when_budget_blocked))

    write_json(WORKER_REPORT_JSON, summary)
    WORKER_REPORT_MD.write_text(render_markdown(summary), encoding="utf-8", newline="\n")
    append_jsonl(WORKER_RUNS_JSONL, summary)

    print("PokeWallet missing-price worker")
    print(f"  status: {summary.get('status')}")
    print(f"  stop reason: {summary.get('stopReason')}")
    print(f"  cycles attempted/completed: {summary.get('cyclesAttempted')} / {summary.get('cyclesCompleted')}")
    print(f"  cycles blocked by budget: {summary.get('cyclesBlockedByBudget')}")
    print(f"  total API requests: {summary.get('totalApiRequests')}")
    print(f"  total imported records: {summary.get('totalImportedRecords')}")
    print(f"  wrote: {WORKER_REPORT_JSON.relative_to(ROOT)}")
    print(f"  wrote: {WORKER_REPORT_MD.relative_to(ROOT)}")
    print(f"  appended: {WORKER_RUNS_JSONL.relative_to(ROOT)}")

    if summary.get("status") in {"complete", "single_cycle_complete", "max_cycles_reached"}:
        return 0
    if summary.get("status") in {"budget_blocked", "budget_exhausted"}:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
