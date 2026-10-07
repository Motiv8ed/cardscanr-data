#!/usr/bin/env python3
"""Classify every Pass1 over-broad dedup deletion; restore FALSE_MERGE/UNKNOWN.

Compares pre-repair git snapshot (default 412fff4e) to post-pass1 commit
(9a77f6d5), classifies each deleted canonical row, and restores missing
FALSE_MERGE/UNKNOWN rows into the working-tree catalogue.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(r"D:\cardscanr-data")
sys.path.insert(0, str(ROOT))

from cardscanr_catalogue_identity import (  # noqa: E402
    classify_pair,
    collector_position_key,
    names_compatible,
)

DEFAULT_PRE = "412fff4e"
DEFAULT_POST = "9a77f6d5"
DEFAULT_EN = ROOT / "public" / "v1" / "catalog" / "pokemon" / "en"
DEFAULT_OUT = Path(
    r"D:\CardScanR\reports\catalogue_integrity_20260830\false_merge_classification.json"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git_show_json(rev: str, rel: str) -> dict[str, Any] | None:
    try:
        raw = subprocess.check_output(
            ["git", "-C", str(ROOT), "show", f"{rev}:{rel}"],
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        return None
    return json.loads(raw.decode("utf-8"))


def list_card_files(rev: str) -> list[str]:
    out = subprocess.check_output(
        [
            "git",
            "-C",
            str(ROOT),
            "ls-tree",
            "-r",
            "--name-only",
            rev,
            "--",
            "public/v1/catalog/pokemon/en/cards",
        ],
        text=True,
    )
    return [line.strip() for line in out.splitlines() if line.strip().endswith(".json")]


def card_id(card: dict[str, Any]) -> str:
    return str(card.get("canonicalBaseId") or "").strip()


def load_rev_cards(rev: str) -> dict[str, dict[str, Any]]:
    """Map canonicalBaseId -> card(+setId)."""
    by_id: dict[str, dict[str, Any]] = {}
    for rel in list_card_files(rev):
        payload = git_show_json(rev, rel)
        if not payload:
            continue
        set_id = str(payload.get("setId") or Path(rel).stem)
        for card in payload.get("cards") or []:
            if not isinstance(card, dict):
                continue
            cid = card_id(card)
            if not cid:
                continue
            enriched = dict(card)
            enriched["_setId"] = set_id
            by_id[cid] = enriched
    return by_id


def load_worktree_cards(en_root: Path) -> dict[str, dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for path in (en_root / "cards").glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        set_id = str(payload.get("setId") or path.stem)
        for card in payload.get("cards") or []:
            if not isinstance(card, dict):
                continue
            cid = card_id(card)
            if not cid:
                continue
            enriched = dict(card)
            enriched["_setId"] = set_id
            by_id[cid] = enriched
    return by_id


def load_worktree_ids(en_root: Path) -> set[str]:
    return set(load_worktree_cards(en_root))


CLONE_SET_MAP = {
    "me03": "me3",
}


def find_retained(
    deleted: dict[str, Any],
    post_by_set_pos: dict[tuple[str, str], list[dict[str, Any]]],
) -> tuple[dict[str, Any] | None, str | None]:
    set_id = str(deleted.get("_setId") or "")
    pos = collector_position_key(deleted.get("collectorNumber"))
    deleted_cid = card_id(deleted)
    candidates = [
        c
        for c in (post_by_set_pos.get((set_id, pos)) or [])
        if card_id(c) != deleted_cid
    ]
    mapped = CLONE_SET_MAP.get(set_id)
    if mapped:
        candidates.extend(
            c
            for c in (post_by_set_pos.get((mapped, pos)) or [])
            if card_id(c) != deleted_cid
        )
    if not candidates:
        return None, None
    for cand in candidates:
        if names_compatible(deleted.get("name"), cand.get("name")):
            return cand, mapped if mapped and str(cand.get("_setId")) == mapped else set_id
    return candidates[0], mapped if mapped and str(candidates[0].get("_setId")) == mapped else set_id


def is_provider_contamination(deleted: dict[str, Any], retained: dict[str, Any] | None) -> bool:
    """Wrong-set PokéWallet rows (e.g. Prinplup Diamond/Pearl dumped into xy9)."""
    name = str(deleted.get("name") or "")
    set_id = str(deleted.get("_setId") or "")
    if set_id == "xy9" and "prinplup" in name.casefold() and "diamond" in name.casefold():
        return True
    if retained and not names_compatible(deleted.get("name"), retained.get("name")):
        # Extra heuristic: deleted name embeds an unrelated set product phrase.
        noise = ("diamond and pearl", "heartgold", "soulsilver", "black and white")
        lowered = name.casefold()
        if any(token in lowered for token in noise):
            return True
    return False


def restore_card(en_root: Path, set_id: str, card: dict[str, Any]) -> bool:
    path = en_root / "cards" / f"{set_id}.json"
    if not path.exists():
        payload = {
            "schemaVersion": "1.0.0",
            "language": "en",
            "setId": set_id,
            "setName": card.get("setName") or set_id,
            "cardCount": 0,
            "cards": [],
        }
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
    cards = [c for c in (payload.get("cards") or []) if isinstance(c, dict)]
    cid = card_id(card)
    if any(card_id(c) == cid for c in cards):
        return False
    clean = {k: v for k, v in card.items() if not str(k).startswith("_")}
    cards.append(clean)
    cards.sort(
        key=lambda c: (
            collector_position_key(c.get("collectorNumber")),
            str(c.get("name") or "").casefold(),
            card_id(c),
        )
    )
    payload["cards"] = cards
    payload["cardCount"] = len(cards)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pre-rev", default=DEFAULT_PRE)
    parser.add_argument("--post-rev", default=DEFAULT_POST)
    parser.add_argument("--en-root", type=Path, default=DEFAULT_EN)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--apply-restore", action="store_true")
    args = parser.parse_args()

    print(f"[classify] loading pre={args.pre_rev}")
    pre = load_rev_cards(args.pre_rev)
    print(f"[classify] pre cards={len(pre)}")
    print(f"[classify] loading post={args.post_rev}")
    post = load_rev_cards(args.post_rev)
    print(f"[classify] post cards={len(post)}")
    worktree_cards = load_worktree_cards(args.en_root)
    worktree_ids = set(worktree_cards)
    print(f"[classify] worktree cards={len(worktree_ids)}")

    post_by_set_pos: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for card in list(post.values()) + list(worktree_cards.values()):
        set_id = str(card.get("_setId") or "")
        pos = collector_position_key(card.get("collectorNumber"))
        if set_id and pos:
            post_by_set_pos[(set_id, pos)].append(card)

    deleted_ids = sorted(set(pre) - set(post))
    print(f"[classify] deleted_by_pass1={len(deleted_ids)}")

    rows: list[dict[str, Any]] = []
    class_counts: Counter[str] = Counter()
    restore_count = 0

    for deleted_id in deleted_ids:
        deleted = pre[deleted_id]
        set_id = str(deleted.get("_setId") or "")
        retained, retained_set = find_retained(deleted, post_by_set_pos)
        if retained is None:
            classification = "UNKNOWN"
            retained_id = None
        elif is_provider_contamination(deleted, retained):
            classification = "PROVIDER_CONTAMINATION"
            retained_id = card_id(retained)
        else:
            classification = classify_pair(
                retained, deleted, language="en", set_id=set_id
            )
            # Clone-set removals that match the canonical set are true duplicates.
            if (
                classification == "FALSE_MERGE"
                and retained_set
                and retained_set != set_id
                and names_compatible(deleted.get("name"), retained.get("name"))
            ):
                classification = "TRUE_DUPLICATE"
            if (
                set_id in CLONE_SET_MAP
                and retained is not None
                and names_compatible(deleted.get("name"), retained.get("name"))
            ):
                classification = "TRUE_DUPLICATE"
            retained_id = card_id(retained)
        class_counts[classification] += 1

        present_now = deleted_id in worktree_ids
        should_restore = (
            classification in {"FALSE_MERGE", "UNKNOWN"} and not present_now
        )
        restored = False
        reason = ""
        if classification == "TRUE_DUPLICATE":
            reason = "same position + compatible name; keep retained canonical row"
            if set_id in CLONE_SET_MAP:
                reason = f"clone set {set_id} collapsed into {CLONE_SET_MAP[set_id]}"
        elif classification == "PROVIDER_CONTAMINATION":
            reason = "wrong-set provider contamination; do not restore"
        elif present_now:
            reason = "already present in working-tree catalogue"
        elif should_restore and args.apply_restore:
            restored = restore_card(args.en_root, set_id, deleted)
            reason = "restored from pre-repair snapshot" if restored else "restore skipped"
            if restored:
                restore_count += 1
                worktree_ids.add(deleted_id)
        elif should_restore:
            reason = "needs restore (dry-run; pass --apply-restore)"
        else:
            reason = "no restore required"

        rows.append(
            {
                "deleted_id": deleted_id,
                "retained_id": retained_id,
                "set_id": set_id,
                "collector_number": deleted.get("collectorNumber"),
                "card_name": deleted.get("name"),
                "language": "en",
                "provider_source": deleted.get("imageSource") or deleted.get("source"),
                "retained_name": (retained or {}).get("name"),
                "retained_collector_number": (retained or {}).get("collectorNumber"),
                "retained_provider_source": (retained or {}).get("imageSource"),
                "retained_set_id": (retained or {}).get("_setId"),
                "position_key": collector_position_key(deleted.get("collectorNumber")),
                "classification": classification,
                "presentInWorktree": present_now or restored,
                "restored": restored,
                "reason": reason,
            }
        )

    # Recompute presence after restores
    if args.apply_restore:
        worktree_ids = load_worktree_ids(args.en_root)
        for row in rows:
            if row["classification"] in {"FALSE_MERGE", "UNKNOWN"}:
                row["presentInWorktree"] = row["deleted_id"] in worktree_ids

    unresolved_false = [
        r
        for r in rows
        if r["classification"] == "FALSE_MERGE" and not r["presentInWorktree"]
    ]
    unresolved_unknown = [
        r for r in rows if r["classification"] == "UNKNOWN" and not r["presentInWorktree"]
    ]

    report = {
        "generatedAtUtc": utc_now(),
        "preRev": args.pre_rev,
        "postRev": args.post_rev,
        "applyRestore": args.apply_restore,
        "summary": {
            "preCardCount": len(pre),
            "postCardCount": len(post),
            "deletedCount": len(deleted_ids),
            "classificationCounts": dict(class_counts),
            "restoredThisRun": restore_count,
            "unresolvedFalseMerge": len(unresolved_false),
            "unresolvedUnknown": len(unresolved_unknown),
            "unknownCount": class_counts.get("UNKNOWN", 0),
            "falseMergeCount": class_counts.get("FALSE_MERGE", 0),
            "trueDuplicateCount": class_counts.get("TRUE_DUPLICATE", 0),
        },
        "target": {
            "unknown": 0,
            "unresolvedFalseMerge": 0,
            "note": "UNKNOWN must reach 0 before repair completion",
        },
        "unresolvedFalseMergeSamples": unresolved_false[:50],
        "unresolvedUnknownSamples": unresolved_unknown[:50],
        "falseMergeRows": [r for r in rows if r["classification"] == "FALSE_MERGE"],
        "unknownRows": [r for r in rows if r["classification"] == "UNKNOWN"],
        "rows": rows,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], indent=2))
    print(f"wrote {args.out}")
    if unresolved_unknown or unresolved_false:
        return 1
    if class_counts.get("UNKNOWN", 0) > 0 and not args.apply_restore:
        # Still have UNKNOWN classifications even if present — fail completion gate.
        return 1
    return 0 if class_counts.get("UNKNOWN", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
