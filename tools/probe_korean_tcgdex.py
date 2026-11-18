#!/usr/bin/env python3
"""Bounded TCGdex Korean metadata coverage diagnostic, not an image crawler."""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable
from urllib.error import URLError, HTTPError

from tools.tcgdex_metadata_batch import API, fetch_json

MAX_SAMPLES = 12


def sample_set_ids(index: Any, count: int) -> list[str]:
    if not isinstance(index, list):
        raise ValueError("Korean set index must be a list")
    items = [(s.get("id"), s.get("cardCount")) for s in index if isinstance(s, dict)]
    unique: dict[str, int | None] = {}
    for sid, card_count in items:
        if (isinstance(sid, str) and 0 < len(sid) <= 48 and
                all(c.isalnum() or c in "_-." for c in sid) and sid not in unique):
            n = (card_count.get("total") if isinstance(card_count, dict) else card_count)
            unique[sid] = n if isinstance(n, int) else None
    # Favor explicitly populated sets, but spread across remaining provider index
    # rather than selecting alphanumerically "latest" IDs that aren't actual dates.
    positive = [s for s, n in unique.items() if n is not None and n > 0]
    other = [s for s in unique if s not in positive]
    if count < 1 or count > MAX_SAMPLES:
        raise ValueError("sample size must be 1..12")
    return (positive + other)[:count]


def probe(*, samples: int, get: Callable[[str], tuple[Any, bytes]] = fetch_json,
          sleep_seconds: float = 0.4) -> dict:
    if not 1 <= samples <= MAX_SAMPLES:
        raise ValueError("sample size out of bounds")
    report = {"schema_version": 1, "provider": "tcgdex", "language": "ko",
              "metadata_only": True, "artwork_downloaded": 0,
              "production_published": False, "set_details": [], "errors": []}
    try:
        index, raw = get(f"{API}/ko/sets")
        report["set_index_total"] = len(index) if isinstance(index, list) else 0
        report["set_index_sha256"] = hashlib.sha256(raw).hexdigest()
        selected = sample_set_ids(index, samples)
    except (ValueError, OSError, TimeoutError, URLError, HTTPError, json.JSONDecodeError) as err:
        report["errors"].append({"source": "set_index", "error_type": type(err).__name__})
        selected = []
    try:
        cards, raw = get(f"{API}/ko/cards")
        report["card_index_total"] = len(cards) if isinstance(cards, list) else None
        report["card_index_sha256"] = hashlib.sha256(raw).hexdigest()
    except (ValueError, OSError, TimeoutError, URLError, HTTPError, json.JSONDecodeError) as err:
        report["errors"].append({"source": "card_index", "error_type": type(err).__name__})
        report["card_index_total"] = None
    for sid in selected:
        if sleep_seconds:
            time.sleep(sleep_seconds)
        try:
            data, raw = get(f"{API}/ko/sets/{sid}")
            if not isinstance(data, dict) or data.get("id") != sid:
                raise ValueError("set identity mismatch")
            c = data.get("cards")
            report["set_details"].append({
                "set_id": sid, "card_rows": len(c) if isinstance(c, list) else None,
                "sha256": hashlib.sha256(raw).hexdigest(),
            })
        except (ValueError, OSError, TimeoutError, URLError, HTTPError, json.JSONDecodeError) as err:
            report["errors"].append({"source": "set_detail", "set_id": sid,
                                     "error_type": type(err).__name__})
    populated = sum(1 for r in report["set_details"] if (r["card_rows"] or 0) > 0)
    report["sampled_sets_with_cards"] = populated
    report["sampled_sets"] = len(report["set_details"])
    report["interpretation"] = ("provider_has_card_data" if populated or (report.get("card_index_total") or 0)
                                else "provider_coverage_incomplete_or_empty")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-sets", type=int, default=12)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    out = args.output
    if out.is_symlink() or (out.exists() and (not out.is_dir() or any(out.iterdir()))):
        parser.error("output must be a new empty directory")
    result = probe(samples=args.sample_sets)
    out.mkdir(parents=True, exist_ok=True)
    (out / "korean_provider_coverage.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({k: result.get(k) for k in ("set_index_total", "card_index_total",
          "sampled_sets", "sampled_sets_with_cards", "interpretation")}, sort_keys=True))
    return 0 if not result["errors"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
