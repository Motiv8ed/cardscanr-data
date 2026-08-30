#!/usr/bin/env python3
"""Idempotent EN catalogue repair: drop PokéWallet collector duplicates and clone sets.

Default mode is dry-run. Pass --apply to write changes.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_EN_ROOT = Path(r"D:\cardscanr-data\public\v1\catalog\pokemon\en")
DEFAULT_REPORT = Path(
    r"D:\CardScanR\reports\catalogue_integrity_20260830\dedup_repair_report.json"
)

_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
_DIGIT_GROUP = re.compile(r"\d+")
_SPACE = re.compile(r"\s+")
_SEPARATORS = re.compile(r"[/.\-\s]+")
_PURE_NUMERIC = re.compile(r"^(\d+)(?:/(\d+))?$")
# letter prefix + one or more zeros + digits (me03, sv001 style set ids)
_ZERO_PADDED_SET = re.compile(r"^([A-Za-z]+)0+(\d+)$")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def collector_identity_key(value: object) -> str:
    """Collector identity used for duplicate collision.

    Strips spaces; strips leading zeros from each digit group; for pure numeric
    or numeric/numeric forms uses the numerator only (so ``24`` and ``024/086``
    collide). Other forms keep letter prefixes after separator stripping.
    """
    raw = unicodedata.normalize("NFKC", str(value or ""))
    raw = raw.translate(_FULLWIDTH_DIGITS).strip()
    if not raw:
        return ""
    raw = _SPACE.sub("", raw)
    zero_stripped = _DIGIT_GROUP.sub(lambda match: str(int(match.group(0))), raw)
    match = _PURE_NUMERIC.match(zero_stripped)
    if match:
        return match.group(1)
    compact = _SEPARATORS.sub("", zero_stripped).casefold()
    return compact


def _urls_of(card: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("imageSmall", "imageLarge", "imageUrl", "thumbnailUrl", "largeImageUrl"):
        value = card.get(key)
        if value:
            parts.append(str(value))
    return " ".join(parts).lower()


def keep_rank(card: dict[str, Any], index: int) -> tuple[int, int, int]:
    """Lower is better: pokemon_tcg source, then non-pokewallet URLs, then first."""
    source = str(card.get("imageSource") or "").casefold()
    is_tcg = source == "pokemon_tcg_api" or "pokemon_tcg" in source
    urls = _urls_of(card)
    is_pokewallet_url = "pokewallet.io" in urls
    return (
        0 if is_tcg else 1,
        0 if not is_pokewallet_url else 1,
        index,
    )


def dedupe_cards(cards: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    groups: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for index, card in enumerate(cards):
        key = collector_identity_key(card.get("collectorNumber"))
        groups[key].append((index, card))

    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    # Preserve original relative order of winners.
    winners_by_index: dict[int, dict[str, Any]] = {}
    for key, members in groups.items():
        if not key:
            for index, card in members:
                winners_by_index[index] = card
            continue
        ranked = sorted(members, key=lambda item: keep_rank(item[1], item[0]))
        winner_index, winner = ranked[0]
        winners_by_index[winner_index] = winner
        for loser_index, loser in ranked[1:]:
            dropped.append(
                {
                    "identityKey": key,
                    "keptCollectorNumber": winner.get("collectorNumber"),
                    "keptName": winner.get("name"),
                    "keptImageSource": winner.get("imageSource"),
                    "droppedCollectorNumber": loser.get("collectorNumber"),
                    "droppedName": loser.get("name"),
                    "droppedImageSource": loser.get("imageSource"),
                    "droppedCanonicalBaseId": loser.get("canonicalBaseId"),
                    "originalIndex": loser_index,
                }
            )
    for index in sorted(winners_by_index):
        kept.append(winners_by_index[index])
    return kept, dropped


def local_number_set(cards: list[dict[str, Any]]) -> set[str]:
    keys: set[str] = set()
    for card in cards:
        key = collector_identity_key(card.get("collectorNumber"))
        if key:
            keys.add(key)
    return keys


def canonical_set_id(set_id: str) -> str | None:
    match = _ZERO_PADDED_SET.match(str(set_id or "").strip())
    if not match:
        return None
    return f"{match.group(1)}{match.group(2)}"


def find_clone_sets(
    set_ids: set[str],
    cards_by_set: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Detect whole-set PokéWallet clones of official sets (me03 → me3, etc.)."""
    clones: list[dict[str, Any]] = []
    # Case-insensitive lookup for canon ids present in catalogue.
    lower_map = {sid.casefold(): sid for sid in set_ids}
    for set_id in sorted(set_ids):
        canon = canonical_set_id(set_id)
        if not canon:
            continue
        if canon.casefold() == set_id.casefold():
            continue
        official_id = lower_map.get(canon.casefold())
        if not official_id:
            continue
        clone_cards = cards_by_set.get(set_id) or []
        official_cards = cards_by_set.get(official_id) or []
        clone_n = len(clone_cards)
        official_n = len(official_cards)
        if abs(clone_n - official_n) > 5:
            continue
        clone_locals = local_number_set(clone_cards)
        official_locals = local_number_set(official_cards)
        if not clone_locals:
            continue
        overlap = len(clone_locals & official_locals)
        overlap_pct = overlap / len(clone_locals)
        if overlap_pct < 0.50:
            continue
        clones.append(
            {
                "cloneSetId": set_id,
                "canonicalSetId": official_id,
                "cloneCardCount": clone_n,
                "canonicalCardCount": official_n,
                "overlappingLocalNumbers": overlap,
                "overlapPct": round(overlap_pct, 4),
            }
        )
    return clones


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def dump_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def repair_catalogue(*, en_root: Path, apply: bool, report_path: Path) -> dict[str, Any]:
    sets_path = en_root / "sets.json"
    cards_dir = en_root / "cards"
    if not sets_path.is_file():
        raise FileNotFoundError(f"Missing sets.json: {sets_path}")
    if not cards_dir.is_dir():
        raise FileNotFoundError(f"Missing cards dir: {cards_dir}")

    sets_doc = load_json(sets_path)
    sets_list: list[dict[str, Any]] = list(sets_doc.get("sets") or [])
    set_ids_in_index = {str(item.get("id")) for item in sets_list if item.get("id")}

    card_files = sorted(cards_dir.glob("*.json"))
    cards_by_set: dict[str, list[dict[str, Any]]] = {}
    card_docs: dict[str, dict[str, Any]] = {}
    for path in card_files:
        doc = load_json(path)
        set_id = str(doc.get("setId") or path.stem)
        cards_by_set[set_id] = list(doc.get("cards") or [])
        card_docs[set_id] = doc

    all_set_ids = set(cards_by_set) | set_ids_in_index

    per_set_dedupe: list[dict[str, Any]] = []
    total_dropped = 0
    total_before = 0
    total_after = 0
    rewritten_sets: list[str] = []

    for set_id in sorted(cards_by_set):
        before_cards = cards_by_set[set_id]
        total_before += len(before_cards)
        kept, dropped = dedupe_cards(before_cards)
        total_after += len(kept)
        total_dropped += len(dropped)
        cards_by_set[set_id] = kept
        if dropped:
            per_set_dedupe.append(
                {
                    "setId": set_id,
                    "beforeCount": len(before_cards),
                    "afterCount": len(kept),
                    "droppedCount": len(dropped),
                    "droppedSamples": dropped[:20],
                }
            )
            rewritten_sets.append(set_id)

    clones = find_clone_sets(all_set_ids, cards_by_set)
    removed_clone_ids = [item["cloneSetId"] for item in clones]
    clone_cards_removed = sum(len(cards_by_set.get(clone_id) or []) for clone_id in removed_clone_ids)

    # Apply in-memory: drop clone sets from card map and sets index.
    for clone_id in removed_clone_ids:
        cards_by_set.pop(clone_id, None)
        card_docs.pop(clone_id, None)

    remaining_sets = [item for item in sets_list if str(item.get("id")) not in removed_clone_ids]
    # Recompute aggregates from remaining indexed set card files.
    indexed_ids = {str(item.get("id")) for item in remaining_sets if item.get("id")}
    aggregate_card_count = 0
    for set_id in indexed_ids:
        aggregate_card_count += len(cards_by_set.get(set_id) or [])
    cards_after = total_after - clone_cards_removed

    report: dict[str, Any] = {
        "generatedAtUtc": utc_now(),
        "dryRun": not apply,
        "enRoot": str(en_root),
        "summary": {
            "setFilesScanned": len(card_files),
            "setsInIndexBefore": len(sets_list),
            "setsInIndexAfter": len(remaining_sets),
            "cardsBefore": total_before,
            "cardsAfterDedupe": cards_after,
            "duplicateCardsDropped": total_dropped,
            "cloneCardsRemoved": clone_cards_removed,
            "setsWithDuplicates": len(per_set_dedupe),
            "cloneSetsRemoved": len(removed_clone_ids),
            "aggregateCardCountAfter": aggregate_card_count,
        },
        "cloneSets": clones,
        "perSetDedupe": per_set_dedupe,
        "rewrittenSetIds": rewritten_sets,
        "removedCloneSetIds": removed_clone_ids,
    }

    print(
        f"[repair] dry_run={not apply} scanned_sets={len(card_files)} "
        f"dropped_dupes={total_dropped} clone_sets={len(removed_clone_ids)} "
        f"cards {total_before} -> {cards_after}"
    )
    for clone in clones:
        print(
            f"[repair] clone {clone['cloneSetId']} -> keep {clone['canonicalSetId']} "
            f"(overlap={clone['overlapPct']:.0%}, "
            f"counts={clone['cloneCardCount']}/{clone['canonicalCardCount']})"
        )

    if apply:
        for set_id in rewritten_sets:
            if set_id in removed_clone_ids:
                continue
            doc = card_docs[set_id]
            doc["cards"] = cards_by_set[set_id]
            doc["cardCount"] = len(doc["cards"])
            dump_json(cards_dir / f"{set_id}.json", doc)
            print(f"[repair] wrote {set_id}.json cardCount={doc['cardCount']}")

        for clone_id in removed_clone_ids:
            clone_path = cards_dir / f"{clone_id}.json"
            if clone_path.exists():
                clone_path.unlink()
                print(f"[repair] removed clone file {clone_path.name}")

        sets_doc["sets"] = remaining_sets
        sets_doc["setCount"] = len(remaining_sets)
        sets_doc["cardCount"] = aggregate_card_count
        dump_json(sets_path, sets_doc)
        print(
            f"[repair] wrote sets.json setCount={sets_doc['setCount']} "
            f"cardCount={sets_doc['cardCount']}"
        )
    else:
        print("[repair] dry-run only; pass --apply to write changes")

    dump_json(report_path, report)
    print(f"[repair] report -> {report_path}")
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Repair EN catalogue PokéWallet collector duplicates and clone sets."
    )
    parser.add_argument(
        "--en-root",
        type=Path,
        default=DEFAULT_EN_ROOT,
        help="EN catalogue root containing sets.json and cards/",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT,
        help="Path for JSON repair report",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="Do not write catalogue files (default)",
    )
    mode.add_argument(
        "--apply",
        dest="dry_run",
        action="store_false",
        help="Write deduped card files and updated sets.json",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repair_catalogue(en_root=args.en_root, apply=not args.dry_run, report_path=args.report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
