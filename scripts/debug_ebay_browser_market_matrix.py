#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.providers import create_market_comps_provider
from cardscanr_market_engine.providers.errors import sanitize_provider_diagnostics
from cardscanr_market_engine.providers.query_builder import build_provider_search_query
from scripts.debug_ebay_browser_provider import build_request, comp_to_dict

MARKET_CURRENCIES = {"AU": "AUD", "US": "USD", "GB": "GBP", "CA": "CAD"}
LATEST_REPORT = ROOT / "reports" / "ebay_browser_market_matrix_latest.json"
RUNS_REPORT = ROOT / "reports" / "ebay_browser_market_matrix_runs.jsonl"


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run provider-only eBay browser validation across local markets.")
    parser.add_argument("--markets", default="AU,US,GB,CA")
    parser.add_argument("--card-name", default="Charizard ex")
    parser.add_argument("--collector-number", default="125/197")
    parser.add_argument("--set-name", default="Obsidian Flames")
    parser.add_argument("--max-results", type=int, default=30)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--pause-between-markets-seconds", type=int, default=20)
    return parser.parse_args()


def plan_market_matrix(markets_csv: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for raw in markets_csv.split(","):
        market = raw.strip().upper()
        if not market:
            continue
        if market not in MARKET_CURRENCIES:
            raise ValueError(f"Unsupported matrix market '{market}'. Supported: AU, US, GB, CA")
        rows.append({"market": market, "currency": MARKET_CURRENCIES[market]})
    return rows


def _require_live_flags() -> None:
    if os.getenv("MARKET_LOOKUP_PROVIDER", "").strip().lower() != "ebay_browser":
        raise RuntimeError("MARKET_LOOKUP_PROVIDER=ebay_browser is required")
    if os.getenv("ENABLE_EBAY_REAL_LOOKUP", "").strip().lower() != "true":
        raise RuntimeError("ENABLE_EBAY_REAL_LOOKUP=true is required")


def run_market(args: argparse.Namespace, *, market: str, currency: str) -> dict[str, Any]:
    artifact_dir = ROOT / "reports" / "ebay_browser_debug" / "market_matrix" / "latest" / market.lower()
    os.environ["EBAY_BROWSER_DEBUG_ARTIFACT_DIR"] = str(artifact_dir)
    os.environ["EBAY_BROWSER_MAX_RESULTS"] = str(max(1, min(args.max_results, 100)))
    os.environ["EBAY_BROWSER_HEADLESS"] = "false" if args.headed else "true"
    request_args = argparse.Namespace(
        market=market,
        currency=currency,
        card_name=args.card_name,
        collector_number=args.collector_number,
        set_name=args.set_name,
        set_code="",
        language="en",
        variant="raw",
        condition="raw",
    )
    request = build_request(request_args)
    query = build_provider_search_query(request)
    try:
        provider = create_market_comps_provider("ebay_browser")
        result = provider.fetch_comps(request)
        quality = result.raw_metadata.get("qualitySummary", {}) if isinstance(result.raw_metadata, dict) else {}
        selector_counts = result.raw_metadata.get("candidateSelectorCounts", {}) if isinstance(result.raw_metadata, dict) else {}
        return sanitize_provider_diagnostics(
            {
                "status": "success",
                "market": market,
                "provider_domain": request.provider_domain,
                "provider_marketplace_id": request.provider_marketplace_id,
                "currency": currency,
                "search_url": query.search_url,
                "result_count": len(result.comps),
                "useful_candidate_count": quality.get("useful_candidate_count"),
                "rejected_candidate_count": max(0, len(result.comps) - int(quality.get("useful_candidate_count", 0) or 0)),
                "quality_summary": quality,
                "selector_counts": selector_counts,
                "detected_captcha_or_block": False,
                "first_3_listings": [comp_to_dict(comp) for comp in result.comps[:3]],
                "international_origin_count": quality.get("international_origin_count"),
                "artifact_paths": {
                    "directory": str(artifact_dir),
                    "screenshot": str(artifact_dir / "screenshot.png"),
                    "page_html": str(artifact_dir / "page.html"),
                    "debug_summary": str(artifact_dir / "debug_summary.json"),
                },
            }
        )
    except Exception as exc:
        return sanitize_provider_diagnostics(
            {
                "status": "failed",
                "market": market,
                "provider_domain": request.provider_domain,
                "provider_marketplace_id": request.provider_marketplace_id,
                "currency": currency,
                "search_url": query.search_url,
                "error": str(exc),
                "error_type": type(exc).__name__,
                "artifact_paths": {
                    "directory": str(artifact_dir),
                    "screenshot": str(artifact_dir / "screenshot.png"),
                    "page_html": str(artifact_dir / "page.html"),
                    "debug_summary": str(artifact_dir / "debug_summary.json"),
                },
            }
        )


def main() -> int:
    args = parse_args()
    _require_live_flags()
    started = utc_iso()
    markets = plan_market_matrix(args.markets)
    results: list[dict[str, Any]] = []
    for index, row in enumerate(markets):
        if index > 0 and args.pause_between_markets_seconds > 0:
            time.sleep(args.pause_between_markets_seconds)
        results.append(run_market(args, market=row["market"], currency=row["currency"]))
    report = sanitize_provider_diagnostics(
        {
            "status": "success" if all(item.get("status") == "success" for item in results) else "partial",
            "startedAtUtc": started,
            "finishedAtUtc": utc_iso(),
            "card": {
                "card_name": args.card_name,
                "collector_number": args.collector_number,
                "set_name": args.set_name,
            },
            "markets": results,
        }
    )
    write_json(LATEST_REPORT, report)
    append_jsonl(RUNS_REPORT, report)
    print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
    return 0 if report["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
