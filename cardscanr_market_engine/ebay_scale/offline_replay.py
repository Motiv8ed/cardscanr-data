from __future__ import annotations

from pathlib import Path
from typing import Any

from .queue_priority import estimate_cards_per_sold_search
from .set_sweep import run_set_sweep
from .set_sweep_matcher import SetSweepCatalogueCard

# UNVERIFIED: offline catalogue sizes for five pilot sets (fixture replay only).
REPLAY_SETS: tuple[dict[str, Any], ...] = (
    {"setCode": "sv3", "setName": "Obsidian Flames", "cardCount": 197},
    {"setCode": "sv8", "setName": "Surging Sparks", "cardCount": 191},
    {"setCode": "sv6", "setName": "Twilight Masquerade", "cardCount": 167},
    {"setCode": "sv4", "setName": "Paradox Rift", "cardCount": 182},
    {"setCode": "sv2", "setName": "Paldea Evolved", "cardCount": 193},
)


def offline_five_set_throughput_estimate() -> dict[str, Any]:
    """Throughput table for PR body — label UNVERIFIED until live PC run."""
    rows = [estimate_cards_per_sold_search(set_card_count=int(s["cardCount"])) for s in REPLAY_SETS]
    for row, meta in zip(rows, REPLAY_SETS):
        row["setCode"] = meta["setCode"]
        row["setName"] = meta["setName"]
    return {
        "label": "UNVERIFIED",
        "sets": rows,
        "note": "Estimated from catalogue card counts and _ipg=240; not validated on live eBay.",
    }


def replay_fixture_set_sweep(
    *,
    fixture_path: Path,
    catalogue: list[SetSweepCatalogueCard],
) -> dict[str, Any]:
    html = fixture_path.read_text(encoding="utf-8")
    stats, _rows = run_set_sweep(html=html, catalogue=catalogue)
    return stats.to_public_dict()