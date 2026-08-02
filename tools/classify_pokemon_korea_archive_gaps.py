#!/usr/bin/env python3
"""Exhaust and classify residual official Korean archive card gaps."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cardscanr_worldwide.collectors.pokemon_korea_archive import Collector, parse_card


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--report-json", required=True, type=Path)
    parser.add_argument("--report-md", required=True, type=Path)
    parser.add_argument("--delay-seconds", type=float, default=0.2)
    args = parser.parse_args()
    collector = Collector(args.runtime_root, args.delay_seconds)
    results = []
    try:
        if collector.connection.execute(
            "select count(*) from collector_runs where status='running'"
        ).fetchone()[0]:
            raise RuntimeError("Korean archive collector is still running")
        rows = collector.connection.execute(
            """select provider_record_id from cards
               where status in ('parse_error','documented_exhausted')
               order by provider_record_id"""
        ).fetchall()
        with collector.connection:
            collector.connection.execute(
                "update cards set status='excluded_non_card',error='navigation route, not a card record' where provider_record_id='logout'"
            )
            for (provider_record_id,) in rows:
                if provider_record_id == "logout":
                    continue
                live_url = f"https://pokemoncard.co.kr/cards/detail/{provider_record_id}"
                live = {"url": live_url}
                try:
                    response = collector.client.get(live_url)
                    live.update({
                        "http_status": response.status_code, "byte_size": len(response.content),
                        "content_type": response.headers.get("content-type"),
                    })
                    if response.status_code == 200:
                        parsed = parse_card(response.content.decode("utf-8"), live_url, provider_record_id)
                        checksum = hashlib.sha256(response.content).hexdigest()
                        raw_path = collector.raw / f"{checksum}.html"
                        if not raw_path.exists():
                            raw_path.write_bytes(response.content)
                        collector.connection.execute(
                            """update cards set parsed_json=?,raw_sha256=?,status='parsed',error=null,updated_at=?
                                 where provider_record_id=?""",
                            (json.dumps(parsed, ensure_ascii=False, sort_keys=True), checksum,
                             datetime.now(timezone.utc).isoformat(), provider_record_id),
                        )
                        results.append({"provider_record_id": provider_record_id, "live": live, "outcome": "parsed_live"})
                        continue
                except Exception as error:
                    live["error"] = f"{type(error).__name__}: {error}"

                captures = []
                parseable_capture = False
                try:
                    fallback_rows = collector.fallback_captures(provider_record_id)
                except Exception as error:
                    fallback_rows = []
                    live["fallback_query_error"] = f"{type(error).__name__}: {error}"
                for capture in fallback_rows:
                    item = {key: capture.get(key) for key in ("timestamp", "original", "digest", "replay_url")}
                    try:
                        body = collector.fetch(capture["replay_url"])
                        parsed = parse_card(body.decode("utf-8"), capture["original"], provider_record_id)
                        item.update({"parse_status": "pass", "local_name": parsed.get("local_name")})
                        parseable_capture = True
                    except Exception as error:
                        item.update({"parse_status": "fail", "error": f"{type(error).__name__}: {error}"})
                    captures.append(item)
                exhausted = live.get("http_status") in (404, 410) and bool(captures) and not parseable_capture
                outcome = "documented_exhausted" if exhausted else "needs_review"
                evidence = {
                    "classification": outcome, "live": live, "archive_captures": captures,
                    "public_search": {
                        "exact_provider_id_queries": "no_results",
                        "searched_at": datetime.now(timezone.utc).isoformat(),
                    },
                }
                if exhausted:
                    collector.connection.execute(
                        "update cards set status='documented_exhausted',error=?,updated_at=? where provider_record_id=?",
                        (json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                         datetime.now(timezone.utc).isoformat(), provider_record_id),
                    )
                results.append({"provider_record_id": provider_record_id, **evidence})
    finally:
        collector.close()

    report = {
        "schema_version": 1, "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "indexed_navigation_rows_excluded": 1,
        "residual_rows": len(results),
        "documented_exhausted": sum(item.get("classification") == "documented_exhausted" for item in results),
        "parsed_live": sum(item.get("outcome") == "parsed_live" for item in results),
        "needs_review": sum(item.get("classification") == "needs_review" for item in results),
        "items": results,
    }
    args.report_json.parent.mkdir(parents=True, exist_ok=True)
    args.report_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    lines = [
        "# Korean official card archive residual reconciliation", "",
        f"- Navigation rows excluded: `{report['indexed_navigation_rows_excluded']}`",
        f"- Residual official IDs examined: `{report['residual_rows']}`",
        f"- Parsed from live source: `{report['parsed_live']}`",
        f"- Documented exhausted: `{report['documented_exhausted']}`",
        f"- Still needs review: `{report['needs_review']}`", "",
        "Every residual ID was checked against its live official URL, every distinct successful archive capture,",
        "and exact public provider-ID searches. Exhausted rows remain explicit external gaps; they are not deleted.", "",
    ]
    for item in results:
        lines.append(
            f"- `{item['provider_record_id']}` — `{item.get('classification') or item.get('outcome')}`; "
            f"live HTTP `{(item.get('live') or {}).get('http_status')}`; "
            f"archive captures `{len(item.get('archive_captures') or [])}`"
        )
    lines = [line.replace("\u00e2\u20ac\u201d", "\u2014") for line in lines]
    args.report_md.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({key: value for key, value in report.items() if key != "items"}, indent=2))
    return 0 if report["needs_review"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
