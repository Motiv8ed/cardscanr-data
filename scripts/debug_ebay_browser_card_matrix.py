#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.config import MarketEngineConfig
from cardscanr_market_engine.filters import filter_comps
from cardscanr_market_engine.pricing_stats import calculate_pricing_stats
from cardscanr_market_engine.providers.errors import sanitize_provider_diagnostics
from cardscanr_market_engine.providers.query_builder import build_provider_search_queries
from scripts.debug_ebay_browser_market_matrix import plan_market_matrix
from scripts.debug_ebay_browser_provider import build_request, comp_to_dict

LATEST_REPORT = ROOT / "reports" / "ebay_browser_card_matrix_latest.json"
RUNS_REPORT = ROOT / "reports" / "ebay_browser_card_matrix_runs.jsonl"
ARTIFACT_ROOT = ROOT / "reports" / "ebay_browser_debug" / "card_matrix" / "latest"

DEFAULT_CARD_ROWS: list[dict[str, str]] = [
    {
        "card_name": "Pancham",
        "collector_number": "050/100",
        "set_name": "Battle Partners",
        "set_code": "",
        "language": "jp",
        "variant": "non_holo",
        "condition": "raw",
    },
    {
        "card_name": "Lombre",
        "collector_number": "022/100",
        "set_name": "Battle Partners",
        "set_code": "",
        "language": "jp",
        "variant": "non_holo",
        "condition": "raw",
    },
    {
        "card_name": "Pupitar",
        "collector_number": "048/100",
        "set_name": "Battle Partners",
        "set_code": "",
        "language": "jp",
        "variant": "non_holo",
        "condition": "raw",
    },
    {
        "card_name": "Druddigon",
        "collector_number": "073/100",
        "set_name": "Battle Partners",
        "set_code": "",
        "language": "jp",
        "variant": "non_holo",
        "condition": "raw",
    },
    {
        "card_name": "Transformation Tome",
        "collector_number": "083/086",
        "set_name": "Chaos Rising",
        "set_code": "",
        "language": "en",
        "variant": "non_holo",
        "condition": "raw",
    },
    {
        "card_name": "Litleo",
        "collector_number": "014/086",
        "set_name": "Chaos Rising",
        "set_code": "",
        "language": "en",
        "variant": "non_holo",
        "condition": "raw",
    },
    {
        "card_name": "Ferroseed",
        "collector_number": "062/086",
        "set_name": "Chaos Rising",
        "set_code": "",
        "language": "en",
        "variant": "non_holo",
        "condition": "raw",
    },
    {
        "card_name": "Xerneas",
        "collector_number": "042/086",
        "set_name": "Chaos Rising",
        "set_code": "",
        "language": "en",
        "variant": "non_holo",
        "condition": "raw",
    },
    {
        "card_name": "Roxie's Performance",
        "collector_number": "081/086",
        "set_name": "Chaos Rising",
        "set_code": "",
        "language": "en",
        "variant": "non_holo",
        "condition": "raw",
    },
]


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "card"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run provider-only eBay browser QA for a fixed Pokemon card matrix with per-card debug reports."
    )
    parser.add_argument("--markets", default="AU", help="Comma-separated markets. Supported: AU, US, GB, CA")
    parser.add_argument("--cards-file", default="", help="Optional path to JSON list of card rows")
    parser.add_argument("--max-results", type=int, default=30)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--pause-between-lookups-seconds", type=int, default=20)
    parser.add_argument("--lookup-timeout-seconds", type=int, default=180)
    parser.add_argument("--browser-launch-timeout-seconds", type=int, default=120)
    parser.add_argument("--per-query-timeout-seconds", type=int, default=120)
    parser.add_argument("--total-card-timeout-seconds", type=int, default=180)
    return parser.parse_args()


def _require_live_flags() -> None:
    if os.getenv("MARKET_LOOKUP_PROVIDER", "").strip().lower() != "ebay_browser":
        raise RuntimeError("MARKET_LOOKUP_PROVIDER=ebay_browser is required")
    if os.getenv("ENABLE_EBAY_REAL_LOOKUP", "").strip().lower() != "true":
        raise RuntimeError("ENABLE_EBAY_REAL_LOOKUP=true is required")


def _load_cards(cards_file: str) -> list[dict[str, str]]:
    if not cards_file.strip():
        return list(DEFAULT_CARD_ROWS)
    path = Path(cards_file)
    if not path.is_absolute():
        path = ROOT / path
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("cards file must be a JSON list")
    rows: list[dict[str, str]] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise ValueError(f"cards file row {index} is not an object")
        rows.append(
            {
                "card_name": str(item.get("card_name", "")).strip(),
                "collector_number": str(item.get("collector_number", "")).strip(),
                "set_name": str(item.get("set_name", "")).strip(),
                "set_code": str(item.get("set_code", "")).strip(),
                "language": str(item.get("language", "en")).strip() or "en",
                "variant": str(item.get("variant", "non_holo")).strip() or "non_holo",
                "condition": str(item.get("condition", "raw")).strip() or "raw",
            }
        )
    return rows


def plan_card_matrix(cards_file: str = "") -> list[dict[str, str]]:
    return _load_cards(cards_file)


def _is_ascii_text(value: str) -> bool:
    return all(ord(ch) <= 127 for ch in value)


def _query_is_english_safe(query_text: str) -> bool:
    return _is_ascii_text(query_text)


def _compact_evaluated(item: Any) -> dict[str, Any]:
    raw = item.comp.raw_metadata if isinstance(item.comp.raw_metadata, dict) else {}
    return sanitize_provider_diagnostics(
        {
            "title": item.comp.title,
            "sold_price": item.comp.sold_price,
            "shipping_price": item.comp.shipping_price,
            "total_price": item.comp.total_price,
            "currency": item.comp.currency,
            "listing_url": item.comp.listing_url,
            "score": item.match_score,
            "rejection_reason": item.rejection_reason,
            "query_source": raw.get("query_source"),
            "query_index": raw.get("query_index"),
            "query_sources": raw.get("query_sources") or [],
            "comp_quality": {
                "card_name_match": raw.get("card_name_match"),
                "collector_number_match": raw.get("collector_number_match"),
                "collector_number_match_quality": raw.get("collector_number_match_quality"),
                "set_name_match": raw.get("set_name_match"),
                "set_match_quality": raw.get("set_match_quality"),
                "requested_variant": raw.get("requested_variant"),
                "detected_variant": raw.get("detected_variant"),
                "variant_match": raw.get("variant_match"),
                "url_quality": raw.get("url_quality"),
            },
        }
    )


def _read_json_if_exists(path: Path) -> dict[str, Any]:
    try:
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}
    return {}


def _loads_json_object(value: str) -> dict[str, Any]:
    try:
        payload = json.loads(value)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _rejection_summary(evaluated: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in evaluated:
        reason = item.rejection_reason if not item.included_in_estimate else "included"
        key = str(reason or "included")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda pair: pair[0]))


def _run_single_lookup(
    *,
    row: dict[str, str],
    market: str,
    currency: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    label = f"{row['card_name']} {row['collector_number']} {row['set_name']}"
    card_slug = slugify(f"{row['card_name']}-{row['collector_number']}-{row['set_name']}")
    artifact_dir = ARTIFACT_ROOT / card_slug / market.lower()
    os.environ["EBAY_BROWSER_DEBUG_ARTIFACT_DIR"] = str(artifact_dir)
    os.environ["EBAY_BROWSER_MAX_RESULTS"] = str(max(1, min(args.max_results, 100)))
    os.environ["EBAY_BROWSER_HEADLESS"] = "false" if args.headed else "true"

    request_args = argparse.Namespace(
        market=market,
        currency=currency,
        card_name=row["card_name"],
        collector_number=row["collector_number"],
        set_name=row["set_name"],
        set_code=row["set_code"],
        language=row["language"],
        variant=row["variant"],
        condition=row["condition"],
    )
    request = build_request(request_args)
    query_ladder = build_provider_search_queries(request)
    query_audit = [
        {
            "query_index": query.query_index,
            "query_source": query.query_source,
            "query_text": query.query_text,
            "search_url": query.search_url,
            "english_safe_ascii": _query_is_english_safe(query.query_text),
        }
        for query in query_ladder
    ]
    env = os.environ.copy()
    env["EBAY_BROWSER_DEBUG_ARTIFACT_DIR"] = str(artifact_dir)
    env["EBAY_BROWSER_MAX_RESULTS"] = str(max(1, min(args.max_results, 100)))
    env["EBAY_BROWSER_HEADLESS"] = "false" if args.headed else "true"
    env["EBAY_BROWSER_LAUNCH_TIMEOUT_SECONDS"] = str(max(1, int(args.browser_launch_timeout_seconds or 120)))
    env["EBAY_BROWSER_TIMEOUT_SECONDS"] = str(max(1, int(args.per_query_timeout_seconds or 120)))
    command = [
        sys.executable,
        str(ROOT / "scripts" / "debug_ebay_browser_provider.py"),
        "--market",
        market,
        "--currency",
        currency,
        "--card-name",
        row["card_name"],
        "--collector-number",
        row["collector_number"],
        "--set-name",
        row["set_name"],
        "--language",
        row["language"],
        "--variant",
        row["variant"],
        "--condition",
        row["condition"],
        "--browser-launch-timeout-seconds",
        str(max(1, int(args.browser_launch_timeout_seconds or 120))),
        "--per-query-timeout-seconds",
        str(max(1, int(args.per_query_timeout_seconds or 120))),
        "--total-card-timeout-seconds",
        str(max(1, int(args.total_card_timeout_seconds or args.lookup_timeout_seconds or 180))),
    ]
    if args.headed:
        command.append("--headed")
    if row["set_code"]:
        command.extend(["--set-code", row["set_code"]])

    try:
        completed = subprocess.run(
            command,
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=max(30, int(args.total_card_timeout_seconds or args.lookup_timeout_seconds or 180)),
            check=False,
        )
        provider_payload = _loads_json_object(completed.stdout)
        if completed.returncode != 0:
            if provider_payload:
                raise RuntimeError(json.dumps(provider_payload, ensure_ascii=False))
            raise RuntimeError(
                "lookup_command_failed "
                f"returncode={completed.returncode} stderr={completed.stderr.strip()[:800]} stdout={completed.stdout.strip()[:800]}"
            )

        payload = sanitize_provider_diagnostics(
            {
                "status": "success",
                "card": row,
                "market": market,
                "currency": currency,
                "label": label,
                "query_audit": {
                    "all_queries_english_safe_ascii": all(item["english_safe_ascii"] for item in query_audit),
                    "attempts": query_audit,
                    "query_attempts_used": provider_payload.get("queryAttemptsUsed"),
                    "query_stop_reason": provider_payload.get("queryStopReason"),
                    "query_attempt_summaries": provider_payload.get("queryAttempts") or [],
                    "failed_query_attempts": provider_payload.get("failedQueryAttempts") or [],
                },
                "timed_out_stage": provider_payload.get("timedOutStage"),
                "stage_timings": provider_payload.get("stageTimings") or {},
                "result_count": int(provider_payload.get("resultCount") or 0),
                "included_count": int(provider_payload.get("includedCount") or 0),
                "rejected_count": int(provider_payload.get("rejectedCount") or 0),
                "rejection_reason_summary": provider_payload.get("rejectionReasonSummary") or {},
                "quality_summary": provider_payload.get("urlQualityCounts") or {},
                "confidence": provider_payload.get("confidence"),
                "confidence_warnings": provider_payload.get("confidenceWarnings") or [],
                "recommended_price": provider_payload.get("recommendedPrice"),
                "price_basis": provider_payload.get("priceBasis"),
                "no_reliable_price_reason": provider_payload.get("noReliablePriceReason"),
                "included_evidence": provider_payload.get("topIncludedComps") or [],
                "rejected_evidence": provider_payload.get("topRejectedComps") or [],
                "all_results": provider_payload.get("results") or [],
                "artifact_paths": {
                    "directory": str(artifact_dir),
                    "screenshot": str(artifact_dir / "screenshot.png"),
                    "page_html": str(artifact_dir / "page.html"),
                    "debug_summary": str(artifact_dir / "debug_summary.json"),
                },
            }
        )
    except subprocess.TimeoutExpired:
        debug_summary = _read_json_if_exists(artifact_dir / "debug_summary.json")
        failed_attempts = debug_summary.get("failed_query_attempts") or []
        stage_timings = debug_summary.get("stage_timings") or {}
        payload = sanitize_provider_diagnostics(
            {
                "status": "failed",
                "card": row,
                "market": market,
                "currency": currency,
                "label": label,
                "query_audit": {
                    "all_queries_english_safe_ascii": all(item["english_safe_ascii"] for item in query_audit),
                    "attempts": query_audit,
                },
                "error": "lookup_timeout",
                "error_type": "TimeoutExpired",
                "timed_out_stage": stage_timings.get("timedOutStage")
                or (failed_attempts[0].get("timed_out_stage") if failed_attempts and isinstance(failed_attempts[0], dict) else "total_card_timeout"),
                "stage_timings": stage_timings,
                "failed_query_attempts": failed_attempts,
                "debug_summary": debug_summary,
                "artifact_paths": {
                    "directory": str(artifact_dir),
                    "screenshot": str(artifact_dir / "screenshot.png"),
                    "page_html": str(artifact_dir / "page.html"),
                    "debug_summary": str(artifact_dir / "debug_summary.json"),
                },
            }
        )
    except Exception as exc:
        provider_payload = _loads_json_object(str(exc))
        debug_summary = _read_json_if_exists(artifact_dir / "debug_summary.json")
        payload = sanitize_provider_diagnostics(
            {
                "status": "failed",
                "card": row,
                "market": market,
                "currency": currency,
                "label": label,
                "query_audit": {
                    "all_queries_english_safe_ascii": all(item["english_safe_ascii"] for item in query_audit),
                    "attempts": query_audit,
                },
                "error": provider_payload.get("error") or str(exc),
                "error_type": provider_payload.get("errorType") or type(exc).__name__,
                "timed_out_stage": provider_payload.get("timedOutStage")
                or (provider_payload.get("providerDiagnostics") or {}).get("timedOutStage")
                or (debug_summary.get("stage_timings") or {}).get("timedOutStage"),
                "stage_timings": provider_payload.get("stageTimings") or debug_summary.get("stage_timings") or {},
                "failed_query_attempts": provider_payload.get("failedQueryAttempts")
                or debug_summary.get("failed_query_attempts")
                or [],
                "debug_summary": debug_summary,
                "artifact_paths": {
                    "directory": str(artifact_dir),
                    "screenshot": str(artifact_dir / "screenshot.png"),
                    "page_html": str(artifact_dir / "page.html"),
                    "debug_summary": str(artifact_dir / "debug_summary.json"),
                },
            }
        )

    write_json(artifact_dir / "card_qa_report.json", payload)
    return payload


def _summarize_status(rows: list[dict[str, Any]]) -> dict[str, Any]:
    success = [row for row in rows if row.get("status") == "success"]
    failed = [row for row in rows if row.get("status") != "success"]
    no_reliable = [row for row in success if row.get("no_reliable_price_reason")]
    english_query_failures = [
        row
        for row in rows
        if not bool(((row.get("query_audit") or {}).get("all_queries_english_safe_ascii")))
    ]
    return {
        "total": len(rows),
        "success": len(success),
        "failed": len(failed),
        "no_reliable_price": len(no_reliable),
        "english_query_failures": len(english_query_failures),
    }


def main() -> int:
    args = parse_args()
    _require_live_flags()
    markets = plan_market_matrix(args.markets)
    cards = plan_card_matrix(args.cards_file)

    started = utc_iso()
    rows: list[dict[str, Any]] = []
    for card in cards:
        for market_row in markets:
            if rows and args.pause_between_lookups_seconds > 0:
                time.sleep(args.pause_between_lookups_seconds)
            rows.append(
                _run_single_lookup(
                    row=card,
                    market=market_row["market"],
                    currency=market_row["currency"],
                    args=args,
                )
            )

    summary = _summarize_status(rows)
    report = sanitize_provider_diagnostics(
        {
            "status": "success" if summary["failed"] == 0 else "partial",
            "startedAtUtc": started,
            "finishedAtUtc": utc_iso(),
            "markets": markets,
            "cards": cards,
            "summary": summary,
            "results": rows,
        }
    )
    write_json(LATEST_REPORT, report)
    append_jsonl(RUNS_REPORT, report)
    print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
    return 0 if report["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
