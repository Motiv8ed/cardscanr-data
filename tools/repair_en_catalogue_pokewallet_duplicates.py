#!/usr/bin/env python3
"""Idempotent EN catalogue repair: drop PokéWallet collector duplicates and clone sets.

Default mode is dry-run. Pass --apply to write changes.

Safety rule (2026-08-30 hardened):
  Cards only collide when collector identity keys match AND names are compatible
  (accent/punctuation tolerant). This prevents collapsing Celebrations Classic /
  McDonald's lettered decks / other sets that reuse local numbers for distinct
  physical printings.
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

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cardscanr_catalogue_identity import (  # noqa: E402
    collector_identity_key,
    name_fingerprint,
    names_compatible,
    normalize_card_name,
    variant_signature,
)

DEFAULT_EN_ROOT = Path(r"D:\cardscanr-data\public\v1\catalog\pokemon\en")
DEFAULT_REPORT = Path(
    r"D:\CardScanR\reports\catalogue_integrity_20260830\dedup_repair_report.json"
)

_ZERO_PADDED_SET = re.compile(r"^([A-Za-z]+)0+(\d+)$")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _urls_of(card: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("imageSmall", "imageLarge", "imageUrl", "thumbnailUrl", "largeImageUrl"):
        value = card.get(key)
        if value:
            parts.append(str(value))
    return " ".join(parts).lower()


def keep_rank(card: dict[str, Any], index: int) -> tuple[int, int, int]:
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
    """Drop duplicates only with proven physical-printing equivalence.

    Fuzzy name compatibility may form review candidates but MUST NOT authorize deletion.
    """
    groups: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for index, card in enumerate(cards):
        key = collector_identity_key(card.get("collectorNumber"))
        groups[key].append((index, card))

    winners_by_index: dict[int, dict[str, Any]] = {}
    dropped: list[dict[str, Any]] = []
    review_candidates: list[dict[str, Any]] = []

    for key, members in groups.items():
        if not key:
            for index, card in members:
                winners_by_index[index] = card
            continue

        clusters: list[list[tuple[int, dict[str, Any]]]] = []
        for member in members:
            placed = False
            member_variant = variant_signature(member[1])
            for cluster in clusters:
                head = cluster[0][1]
                if variant_signature(head) != member_variant:
                    continue
                if name_fingerprint(head.get("name")) == name_fingerprint(member[1].get("name")):
                    cluster.append(member)
                    placed = True
                    break
            if not placed:
                clusters.append([member])

        for cluster in clusters:
            if len(cluster) == 1:
                winners_by_index[cluster[0][0]] = cluster[0][1]
                continue
            ranked = sorted(cluster, key=lambda item: keep_rank(item[1], item[0]))
            winner_index, winner = ranked[0]
            winners_by_index[winner_index] = winner
            for loser_index, loser in ranked[1:]:
                exact = name_fingerprint(winner.get("name")) == name_fingerprint(loser.get("name"))
                if exact and variant_signature(winner) == variant_signature(loser):
                    dropped.append(
                        {
                            "identityKey": key,
                            "keptCollectorNumber": winner.get("collectorNumber"),
                            "keptName": winner.get("name"),
                            "keptCanonicalBaseId": winner.get("canonicalBaseId"),
                            "droppedCollectorNumber": loser.get("collectorNumber"),
                            "droppedName": loser.get("name"),
                            "droppedCanonicalBaseId": loser.get("canonicalBaseId"),
                            "originalIndex": loser_index,
                            "dedupeAuthorized": True,
                            "reason": "exact_name_and_variant_same_local",
                        }
                    )
                else:
                    winners_by_index[loser_index] = loser
                    review_candidates.append(
                        {
                            "identityKey": key,
                            "candidateCollectorNumber": loser.get("collectorNumber"),
                            "candidateName": loser.get("name"),
                            "winnerCollectorNumber": winner.get("collectorNumber"),
                            "winnerName": winner.get("name"),
                            "nameCompatibleOnly": names_compatible(winner.get("name"), loser.get("name")),
                            "dedupeAuthorized": False,
                            "reason": "fuzzy_or_variant_mismatch_review_only",
                        }
                    )

    kept = [winners_by_index[index] for index in sorted(winners_by_index)]
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
    clones: list[dict[str, Any]] = []
    lower_map = {sid.casefold(): sid for sid in set_ids}
    for set_id in sorted(set_ids):
        canon = canonical_set_id(set_id)
        if not canon or canon.casefold() == set_id.casefold():
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
                "autoDeleteAllowed": False,
                "reason": "heuristic_overlap_insufficient_for_deletion",
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
        payload = load_json(path)
        if not isinstance(payload, dict):
            continue
        cards = [card for card in (payload.get("cards") or []) if isinstance(card, dict)]
        cards_by_set[path.stem] = cards
        card_docs[path.stem] = payload

    cards_before = sum(len(cards) for cards in cards_by_set.values())
    per_set: list[dict[str, Any]] = []
    total_dropped = 0
    rewritten: dict[str, list[dict[str, Any]]] = {}

    for set_id, cards in sorted(cards_by_set.items()):
        kept, dropped = dedupe_cards(cards)
        rewritten[set_id] = kept
        if dropped:
            total_dropped += len(dropped)
            per_set.append(
                {
                    "setId": set_id,
                    "beforeCount": len(cards),
                    "afterCount": len(kept),
                    "droppedCount": len(dropped),
                    "droppedSamples": dropped[:50],
                }
            )

    clones = find_clone_sets(set(cards_by_set), rewritten)
    clone_card_total = 0
    clone_auto_delete = [c for c in clones if c.get("autoDeleteAllowed")]
    # Destructive clone-set deletion disabled — report candidates only.
    for clone in clone_auto_delete:
        clone_id = clone["cloneSetId"]
        clone_card_total += len(rewritten.get(clone_id) or [])
        rewritten.pop(clone_id, None)

    cards_after = sum(len(cards) for cards in rewritten.values())
    report = {
        "generatedAtUtc": utc_now(),
        "dryRun": not apply,
        "enRoot": str(en_root),
        "duplicateRule": {
            "collectorIdentity": "numerator-only for pure numeric/N/M; prefixed forms keep letters",
            "nameGate": "NFKD accent-fold + punctuation tolerant; require compatible names",
            "keepPreference": "pokemon_tcg_api over pokewallet URLs over first",
            "languagesTouched": ["en"],
            "doesNotMergeAcrossSets": True,
            "doesNotMergePrefixedWithBareNumeric": True,
        },
        "summary": {
            "setFilesScanned": len(card_files),
            "setsInIndexBefore": len(set_ids_in_index),
            "setsInIndexAfter": len(set_ids_in_index) - sum(
                1 for clone in clones if clone["cloneSetId"] in set_ids_in_index
            ),
            "cardsBefore": cards_before,
            "cardsAfterDedupe": cards_after + clone_card_total,
            "duplicateCardsDropped": total_dropped,
            "cloneCardsRemoved": clone_card_total,
            "setsWithDuplicates": len(per_set),
            "cloneSetsRemoved": len(clone_auto_delete),
            "cloneSetAutoDeleteCandidates": len(clone_auto_delete),
            "cloneSetReviewCandidates": len(clones),
            "aggregateCardCountAfter": sum(
                len(rewritten[sid]) for sid in rewritten if sid in set_ids_in_index
            ),
        },
        "cloneSets": clones,
        "perSetDedupe": per_set,
    }

    if apply:
        for set_id, cards in rewritten.items():
            path = cards_dir / f"{set_id}.json"
            payload = card_docs.get(set_id) or {
                "schemaVersion": "1.0.0",
                "game": "pokemon",
                "language": "en",
                "setId": set_id,
            }
            payload = dict(payload)
            payload["cards"] = cards
            payload["cardCount"] = len(cards)
            dump_json(path, payload)
            print(f"[repair] wrote {set_id}.json cardCount={len(cards)}")
        for clone in clone_auto_delete:
            path = cards_dir / f"{clone['cloneSetId']}.json"
            if path.exists():
                path.unlink()
                print(f"[repair] removed clone file {clone['cloneSetId']}.json")
        remaining_ids = set(rewritten)
        sets_doc["sets"] = [item for item in sets_list if str(item.get("id")) in remaining_ids]
        sets_doc["setCount"] = len(sets_doc["sets"])
        sets_doc["cardCount"] = sum(
            len(rewritten[str(item.get("id"))]) for item in sets_doc["sets"] if item.get("id")
        )
        dump_json(sets_path, sets_doc)
        print(
            f"[repair] wrote sets.json setCount={sets_doc['setCount']} "
            f"cardCount={sets_doc['cardCount']}"
        )

    dump_json(report_path, report)
    print(
        f"[repair] dry_run={not apply} scanned_sets={len(card_files)} "
        f"dropped_dupes={total_dropped} clone_sets={len(clones)} "
        f"cards {cards_before} -> {cards_after}"
    )
    print(f"[repair] report -> {report_path}")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--en-root", type=Path, default=DEFAULT_EN_ROOT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--apply", action="store_true", help="Write changes (default dry-run)")
    args = parser.parse_args()
    repair_catalogue(en_root=args.en_root, apply=args.apply, report_path=args.report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
