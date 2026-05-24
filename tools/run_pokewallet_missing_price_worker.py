#!/usr/bin/env python3
"""Automate bounded JP PokeWallet missing-set price imports."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
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


def run_command(command: list[str], *, allow_failure: bool = False) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    result = {
        "command": " ".join(command),
        "returnCode": completed.returncode,
        "stdoutTail": completed.stdout.splitlines()[-40:],
        "stderrTail": completed.stderr.splitlines()[-40:],
    }
    if completed.returncode != 0 and not allow_failure:
        raise RuntimeError(f"Command failed ({completed.returncode}): {' '.join(command)}")
    return result


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
        result = run_command(command, allow_failure=True)
        results.append(result)
        if int(result.get("returnCode") or 1) != 0:
            break
    return results


def run_release(push_enabled: bool) -> dict[str, Any]:
    before = run_command(["git", "rev-parse", "HEAD"], allow_failure=False)
    before_hash = before["stdoutTail"][-1].strip() if before["stdoutTail"] else ""

    command = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "scripts/release_cardscanr_data.ps1"]
    if push_enabled:
        command.append("-Push")
    release_result = run_command(command, allow_failure=True)

    after = run_command(["git", "rev-parse", "HEAD"], allow_failure=False)
    after_hash = after["stdoutTail"][-1].strip() if after["stdoutTail"] else ""

    committed = bool(after_hash and before_hash and after_hash != before_hash)
    pushed_hashes: list[str] = []
    if committed and push_enabled and int(release_result.get("returnCode") or 1) == 0:
        pushed_hashes.append(after_hash)

    return {
        "beforeHead": before_hash,
        "afterHead": after_hash,
        "committed": committed,
        "pushedCommitHashes": pushed_hashes,
        "result": release_result,
    }


def run_export_chatgpt() -> dict[str, Any]:
    return run_command([sys.executable, "tools/export_chatgpt_report.py"], allow_failure=True)


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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    started_at = utc_now()
    before_counts = count_jp_price_state()

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
        "beforeJpPriceCount": int(before_counts.get("recordCount") or 0),
        "afterJpPriceCount": int(before_counts.get("recordCount") or 0),
        "beforeJpPriceFileCount": int(before_counts.get("fileCount") or 0),
        "afterJpPriceFileCount": int(before_counts.get("fileCount") or 0),
        "lastSelectedSetIds": [],
        "lastImporterStatus": "not_run",
        "validationResults": [],
        "commitHashesPushed": [],
        "nextRecommendedCommand": build_worker_command(args, sleep_mode=True),
        "cycleNotes": [],
    }

    keep_running = True
    cycle_index = 0
    max_cycles = max(0, int(args.max_cycles or 0))
    push_enabled = bool(args.push) and not bool(args.no_push)

    while keep_running:
        if max_cycles > 0 and cycle_index >= max_cycles:
            summary["status"] = "max_cycles_reached"
            summary["stopReason"] = "max_cycles"
            break
        if not args.until_complete and cycle_index >= 1:
            summary["status"] = "single_cycle_complete"
            summary["stopReason"] = "single_cycle"
            break

        changed_paths = git_changed_paths()
        stop_for_dirty, dirty_reason = should_stop_for_dirty_tree(changed_paths, bool(args.commit))
        if stop_for_dirty:
            summary["status"] = "git_dirty"
            summary["stopReason"] = dirty_reason
            summary["cycleNotes"].append("Worker stopped because repository has unexpected local changes.")
            break

        if not changed_paths:
            pull_result = run_command(["git", "pull", "--rebase", "origin", "main"], allow_failure=True)
            if int(pull_result.get("returnCode") or 1) != 0:
                summary["status"] = "git_rebase_failed"
                summary["stopReason"] = "git_rebase_failed"
                summary["cycleNotes"].append("git pull --rebase origin main failed.")
                break
        else:
            summary["cycleNotes"].append("Continuing with expected generated changes already in working tree.")

        cycle_index += 1
        summary["cyclesAttempted"] = cycle_index

        importer_result = run_command(build_importer_command(args), allow_failure=True)
        import_report = read_json(IMPORTER_REPORT_JSON) or {}
        summary["lastImporterStatus"] = str(import_report.get("status") or "unknown")
        summary["lastSelectedSetIds"] = (
            import_report.get("selectedSetIds") if isinstance(import_report.get("selectedSetIds"), list) else []
        )
        summary["totalApiRequests"] += int(import_report.get("apiRequestsUsed") or 0)
        summary["totalImportedRecords"] += int(import_report.get("importedRecords") or 0)

        if should_mark_complete(import_report):
            summary["status"] = "complete"
            summary["stopReason"] = "complete"
            break

        if bool(import_report.get("rateLimitDetected")):
            summary["status"] = "rate_limited"
            summary["stopReason"] = "rate_limited"
            summary["cyclesBlockedByBudget"] += 1
            if args.sleep_when_budget_blocked:
                wait_seconds = estimate_budget_wait_seconds(import_report, max(1, int(args.poll_seconds or 300)))
                summary["cycleNotes"].append(f"Rate-limited. Sleeping for {wait_seconds}s before retry.")
                if args.stop_after_daily_budget and int(import_report.get("dailyRemaining") or 0) <= 0:
                    summary["status"] = "budget_exhausted"
                    summary["stopReason"] = "daily_budget_exhausted"
                    break
                time.sleep(wait_seconds)
                continue
            break

        if bool(import_report.get("allEndpointsFailed")):
            summary["status"] = "all_endpoints_failed"
            summary["stopReason"] = "all_endpoints_failed"
            break

        if is_budget_blocked(import_report):
            summary["cyclesBlockedByBudget"] += 1
            summary["status"] = "budget_blocked"
            summary["stopReason"] = "budget_blocked"
            wait_seconds = estimate_budget_wait_seconds(import_report, max(1, int(args.poll_seconds or 300)))
            summary["cycleNotes"].append(f"Budget blocked with zero API calls. Next wait estimate: {wait_seconds}s.")
            if args.stop_after_daily_budget and int(import_report.get("dailyRemaining") or 0) <= 0:
                summary["status"] = "budget_exhausted"
                summary["stopReason"] = "daily_budget_exhausted"
                break
            if args.sleep_when_budget_blocked:
                time.sleep(wait_seconds)
                continue
            break

        imported_this_cycle = int(import_report.get("importedRecords") or 0)
        api_used_this_cycle = int(import_report.get("apiRequestsUsed") or 0)
        if int(importer_result.get("returnCode") or 0) != 0 and imported_this_cycle == 0 and api_used_this_cycle == 0:
            summary["status"] = "importer_failed"
            summary["stopReason"] = "importer_failed"
            break

        validation_results: list[dict[str, Any]] = []
        if imported_this_cycle > 0 or bool(args.validate):
            validation_results = run_validations()
            summary["validationResults"] = validation_results
            failed_validation = any(int(item.get("returnCode") or 1) != 0 for item in validation_results)
            if failed_validation:
                summary["status"] = "validation_failed"
                summary["stopReason"] = "validation_failed"
                break

        if imported_this_cycle > 0 and bool(args.commit) and not bool(args.dry_run_only):
            release = run_release(push_enabled)
            summary["validationResults"].append({
                "command": release["result"]["command"],
                "returnCode": release["result"]["returnCode"],
            })
            if int(release["result"].get("returnCode") or 1) != 0:
                summary["status"] = "release_failed"
                summary["stopReason"] = "release_failed"
                break
            for commit_hash in release.get("pushedCommitHashes", []):
                if commit_hash not in summary["commitHashesPushed"]:
                    summary["commitHashesPushed"].append(commit_hash)

        if bool(args.export_chatgpt_report):
            export_result = run_export_chatgpt()
            summary["validationResults"].append(
                {
                    "command": export_result["command"],
                    "returnCode": export_result["returnCode"],
                }
            )
            if int(export_result.get("returnCode") or 1) != 0:
                summary["status"] = "export_failed"
                summary["stopReason"] = "export_failed"
                break

        summary["cyclesCompleted"] = int(summary.get("cyclesCompleted") or 0) + 1

        if not args.until_complete:
            summary["status"] = "single_cycle_complete"
            summary["stopReason"] = "single_cycle"
            break

        if args.stop_after_daily_budget and int(import_report.get("dailyRemaining") or 0) <= 0:
            summary["status"] = "budget_exhausted"
            summary["stopReason"] = "daily_budget_exhausted"
            break

        if imported_this_cycle == 0 and api_used_this_cycle > 0:
            summary["status"] = "no_imported_records"
            summary["stopReason"] = "no_imported_records"
            break

    if summary.get("status") == "running":
        summary["status"] = "stopped"
        summary["stopReason"] = "stopped"

    after_counts = count_jp_price_state()
    summary["afterJpPriceCount"] = int(after_counts.get("recordCount") or 0)
    summary["afterJpPriceFileCount"] = int(after_counts.get("fileCount") or 0)
    summary["finishedAtUtc"] = utc_iso()

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
