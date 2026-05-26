#!/usr/bin/env python3
"""Build safe market pricing query samples (no live marketplace calls)."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from market_pricing_job_queue import (
    BANNED_TERMS,
    ROOT,
    build_jobs,
    build_market_query,
    iter_catalog_cards,
    market_config,
    normalize_market,
    utc_now_iso,
    write_json_atomic,
)


REPORT_JSON_PATH = ROOT / "reports" / "market_price_query_samples_latest.json"
REPORT_MD_PATH = ROOT / "reports" / "market_price_query_samples_latest.md"
SAMPLE_MARKETS = ["au", "us", "gb", "ca"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build market pricing query samples")
    parser.add_argument("--market", default="AU", help="Target market (AU/US/GB/CA/EU)")
    parser.add_argument("--language", default="en", choices=["en", "jp"], help="Card language")
    parser.add_argument("--game", default="pokemon", help="Game id")
    parser.add_argument("--limit", type=int, default=20, help="Maximum card rows to sample")
    parser.add_argument("--card-id", default=None, help="Optional canonical card id")
    parser.add_argument("--set-id", default=None, help="Optional set id")
    parser.add_argument("--variant", default="raw", help="Card variant for query text")
    parser.add_argument("--condition", default="near_mint", help="Card condition")
    parser.add_argument("--graded", action="store_true", help="Generate graded query style")
    return parser.parse_args()


def build_query_samples(args: argparse.Namespace) -> dict[str, Any]:
    selected_market = normalize_market(args.market)
    cards = iter_catalog_cards(
        root=ROOT,
        game=args.game,
        language=args.language,
        card_id=args.card_id,
        set_id=args.set_id,
        limit=max(args.limit, 1),
    )
    jobs = build_jobs(
        cards=cards,
        game=args.game,
        language=args.language,
        market=selected_market,
        condition=args.condition,
        variant=args.variant,
        graded_state="graded" if args.graded else "ungraded",
    )

    if not jobs:
        raise SystemExit("No catalog cards matched query sample filters.")

    sample_jobs = jobs[: min(10, len(jobs))]

    sample_by_market: dict[str, list[dict[str, Any]]] = {}
    for market in SAMPLE_MARKETS:
        cfg = market_config(market)
        rows: list[dict[str, Any]] = []
        for job in sample_jobs[: min(5, len(sample_jobs))]:
            market_job = job.__class__(
                game=job.game,
                language=job.language,
                market=cfg["market"],
                currency=cfg["currency"],
                canonical_card_id=job.canonical_card_id,
                set_id=job.set_id,
                set_name=job.set_name,
                collector_number=job.collector_number,
                card_name=job.card_name,
                variant=job.variant,
                condition=job.condition,
                graded_state=job.graded_state,
            )
            query_text = build_market_query(market_job)
            rows.append(
                {
                    "canonicalCardId": market_job.canonical_card_id,
                    "setId": market_job.set_id,
                    "cardName": market_job.card_name,
                    "collectorNumber": market_job.collector_number,
                    "market": cfg["market"],
                    "currency": cfg["currency"],
                    "query": query_text,
                    "excludeTerms": [f"-{term}" for term in BANNED_TERMS] + (["-damaged"] if args.condition != "damaged" else []),
                }
            )
        sample_by_market[cfg["market"]] = rows

    report = {
        "schemaVersion": "1.0.0",
        "generatedAtUtc": utc_now_iso(),
        "status": "ok",
        "mode": "query_generation_only",
        "liveEbayEnabled": False,
        "selectedInput": {
            "market": market_config(selected_market)["market"],
            "language": args.language,
            "game": args.game,
            "limit": args.limit,
            "cardId": args.card_id,
            "setId": args.set_id,
            "variant": args.variant,
            "condition": args.condition,
            "graded": bool(args.graded),
        },
        "querySafety": {
            "bannedTerms": BANNED_TERMS,
            "excludeDamagedWhenConditionIsNotDamaged": args.condition != "damaged",
        },
        "marketSamples": sample_by_market,
    }
    return report


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    a = lines.append
    a("# Market Price Query Samples")
    a("")
    a(f"Generated: {report.get('generatedAtUtc')}")
    a("")
    a("Live eBay scraping enabled: no")
    a("")
    a("## Safety")
    a("")
    a(f"- banned terms: {', '.join(report['querySafety']['bannedTerms'])}")
    a(
        "- exclude '-damaged' by default: "
        + ("yes" if report["querySafety"].get("excludeDamagedWhenConditionIsNotDamaged") else "no")
    )
    a("")

    for market_key in ["AU", "US", "GB", "CA"]:
        rows = report.get("marketSamples", {}).get(market_key, [])
        a(f"## {market_key} eBay sold listing query samples")
        a("")
        if not rows:
            a("No rows")
            a("")
            continue
        for idx, row in enumerate(rows, start=1):
            a(f"{idx}. {row.get('query', '')}")
        a("")

    return "\n".join(lines)


def main() -> None:
    args = parse_args()
    report = build_query_samples(args)

    write_json_atomic(REPORT_JSON_PATH, report)
    REPORT_MD_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_MD_PATH.write_text(render_markdown(report) + "\n", encoding="utf-8")

    print("Market query sample reports written:")
    print(f"  {REPORT_JSON_PATH.relative_to(ROOT).as_posix()}")
    print(f"  {REPORT_MD_PATH.relative_to(ROOT).as_posix()}")


if __name__ == "__main__":
    main()
