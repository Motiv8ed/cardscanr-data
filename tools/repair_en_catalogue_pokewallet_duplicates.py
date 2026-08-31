#!/usr/bin/env python3
"""Plan and apply explicitly approved EN catalogue duplicate removals.

The default mode is non-destructive candidate generation. ``--apply`` is
fail-closed and requires a separate ``--approved-plan`` document containing
exact kept/removed card pairs and independently approved provider equivalence.

Clone-set discovery is review-only. This tool never removes clone files or
unlinks a set from ``sets.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cardscanr_catalogue_identity import (  # noqa: E402
    identity_collector_key,
    name_fingerprint,
    names_compatible,
    variant_signature,
)

DEFAULT_EN_ROOT = Path(r"D:\cardscanr-data\public\v1\catalog\pokemon\en")
DEFAULT_REPORT = Path(
    r"D:\CardScanR\reports\catalogue_integrity_20260831\dedup_dry_run_v5.json"
)
DEFAULT_NUMBERING_POLICY_REGISTRY = Path(
    r"D:\CardScanR\reports\catalogue_integrity_20260831\numbering_policy_registry.json"
)

_ZERO_PADDED_SET = re.compile(r"^([A-Za-z]+)0+(\d+)$")
_DEFAULT_NUMBERING_POLICY = "SEQUENTIAL_FRACTION"
_CANDIDATE_PLAN_SCHEMA = "dedup-candidate-plan-v5"
_APPROVED_PLAN_SCHEMA = "dedup-approved-plan-v5"


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


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _card_sha256(card: dict[str, Any]) -> str:
    canonical = json.dumps(
        card,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _pair_id(
    *,
    set_id: str,
    kept_sha256: str,
    removed_sha256: str,
) -> str:
    payload = f"{set_id}\0{kept_sha256}\0{removed_sha256}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _card_locator(
    card: dict[str, Any],
    *,
    original_index: int,
) -> dict[str, Any]:
    return {
        "canonicalBaseId": str(card.get("canonicalBaseId") or "").strip() or None,
        "collectorNumber": str(card.get("collectorNumber") or "").strip(),
        "name": str(card.get("name") or "").strip(),
        "variantSignature": variant_signature(card),
        "cardSha256": _card_sha256(card),
        "originalIndex": original_index,
        "imageSource": str(card.get("imageSource") or "").strip() or None,
        "providerIds": card.get("providerIds")
        if isinstance(card.get("providerIds"), dict)
        else {},
    }


def load_numbering_policy_registry(path: Path | None) -> tuple[str, dict[str, str]]:
    default_policy = _DEFAULT_NUMBERING_POLICY
    if path is None or not path.is_file():
        return default_policy, {}
    payload = load_json(path)
    if not isinstance(payload, dict):
        raise ValueError(f"Numbering policy registry must be an object: {path}")
    set_policies = payload.get("setPolicies")
    if not isinstance(set_policies, dict):
        return default_policy, {}
    default_entry = set_policies.get("defaultMainExpansion")
    if isinstance(default_entry, dict):
        value = str(default_entry.get("numberingPolicy") or "").strip()
        if value:
            default_policy = value
    policies: dict[str, str] = {}
    for set_id, entry in set_policies.items():
        if set_id == "defaultMainExpansion" or not isinstance(entry, dict):
            continue
        policy = str(entry.get("numberingPolicy") or "").strip()
        if policy:
            policies[str(set_id)] = policy
    return default_policy, policies


def build_candidate_pairs(
    cards: list[dict[str, Any]],
    *,
    set_id: str,
    numbering_policy: str,
    set_printed_total: int | None,
) -> list[dict[str, Any]]:
    """Build review candidates without authorizing any deletion.

    Fuzzy ``names_compatible`` matching is used only to collect pairs for human
    review. It is never part of the apply authorization decision.
    """
    groups: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for index, card in enumerate(cards):
        key = identity_collector_key(
            card.get("collectorNumber"),
            numbering_policy=numbering_policy,
            set_printed_total=set_printed_total,
        )
        if key:
            groups[key].append((index, card))

    candidates: list[dict[str, Any]] = []
    for collector_key, members in sorted(groups.items()):
        if len(members) < 2:
            continue

        clusters: list[list[tuple[int, dict[str, Any]]]] = []
        for member in members:
            member_name = member[1].get("name")
            for cluster in clusters:
                if names_compatible(cluster[0][1].get("name"), member_name):
                    cluster.append(member)
                    break
            else:
                clusters.append([member])

        for cluster in clusters:
            if len(cluster) < 2:
                continue
            ranked = sorted(cluster, key=lambda item: keep_rank(item[1], item[0]))
            kept_index, kept = ranked[0]
            kept_name_fp = name_fingerprint(kept.get("name"))
            kept_variant = variant_signature(kept)
            kept_locator = _card_locator(kept, original_index=kept_index)
            for removed_index, removed in ranked[1:]:
                removed_name_fp = name_fingerprint(removed.get("name"))
                removed_variant = variant_signature(removed)
                removed_locator = _card_locator(removed, original_index=removed_index)
                exact_name = bool(kept_name_fp) and kept_name_fp == removed_name_fp
                same_variant = kept_variant == removed_variant
                evidence = {
                    "sameSet": True,
                    "numberingPolicy": numbering_policy,
                    "setPrintedTotal": set_printed_total,
                    "keptIdentityCollectorKey": collector_key,
                    "removedIdentityCollectorKey": identity_collector_key(
                        removed.get("collectorNumber"),
                        numbering_policy=numbering_policy,
                        set_printed_total=set_printed_total,
                    ),
                    "sameCollectorIdentity": True,
                    "keptVariantSignature": kept_variant,
                    "removedVariantSignature": removed_variant,
                    "sameVariantSignature": same_variant,
                    "keptNameFingerprint": kept_name_fp,
                    "removedNameFingerprint": removed_name_fp,
                    "exactNameFingerprint": exact_name,
                    "fuzzyNamesCompatible": names_compatible(
                        kept.get("name"),
                        removed.get("name"),
                    ),
                    # Deliberately false in generated plans. An independent
                    # reviewer must explicitly set this to true in an approved
                    # plan after verifying both provider records are equivalent.
                    "provider_equivalence": False,
                }
                candidates.append(
                    {
                        "pairId": _pair_id(
                            set_id=set_id,
                            kept_sha256=kept_locator["cardSha256"],
                            removed_sha256=removed_locator["cardSha256"],
                        ),
                        "setId": set_id,
                        "kept": kept_locator,
                        "removed": removed_locator,
                        "identityEvidence": evidence,
                        "candidateOnly": True,
                        "dedupeAuthorized": False,
                        "deletionRequirementsMetExceptProviderEquivalence": (
                            exact_name and same_variant
                        ),
                        "reason": (
                            "exact_identity_review_requires_provider_equivalence"
                            if exact_name and same_variant
                            else "fuzzy_name_or_variant_mismatch_review_only"
                        ),
                    }
                )
    return candidates


def local_number_set(
    cards: list[dict[str, Any]],
    *,
    numbering_policy: str = _DEFAULT_NUMBERING_POLICY,
    set_printed_total: int | None = None,
) -> set[str]:
    keys: set[str] = set()
    for card in cards:
        key = identity_collector_key(
            card.get("collectorNumber"),
            numbering_policy=numbering_policy,
            set_printed_total=set_printed_total,
        )
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


def _resolve_approved_locator(
    *,
    pair_index: int,
    role: str,
    locator: Any,
    cards: list[dict[str, Any]],
) -> tuple[int, dict[str, Any]]:
    if not isinstance(locator, dict):
        raise ValueError(f"approvedPairs[{pair_index}].{role} must be an object")
    original_index = locator.get("originalIndex")
    if not isinstance(original_index, int) or not (0 <= original_index < len(cards)):
        raise ValueError(
            f"approvedPairs[{pair_index}].{role}.originalIndex is invalid"
        )
    card = cards[original_index]
    actual = _card_locator(card, original_index=original_index)
    required_fields = (
        "canonicalBaseId",
        "collectorNumber",
        "name",
        "variantSignature",
        "cardSha256",
        "originalIndex",
        "imageSource",
        "providerIds",
    )
    for field in required_fields:
        if field not in locator:
            raise ValueError(
                f"approvedPairs[{pair_index}].{role}.{field} is required"
            )
        if locator.get(field) != actual.get(field):
            raise ValueError(
                f"approvedPairs[{pair_index}].{role}.{field} does not match "
                "the current catalogue"
            )
    return original_index, card


def _validate_approved_plan(
    *,
    approved_plan: Any,
    cards_by_set: dict[str, list[dict[str, Any]]],
    set_contexts: dict[str, tuple[str, int | None]],
) -> list[dict[str, Any]]:
    if not isinstance(approved_plan, dict):
        raise ValueError("Approved plan must be a JSON object")
    if approved_plan.get("schemaVersion") != _APPROVED_PLAN_SCHEMA:
        raise ValueError(
            f"Approved plan schemaVersion must be {_APPROVED_PLAN_SCHEMA!r}"
        )
    pairs = approved_plan.get("approvedPairs")
    if not isinstance(pairs, list):
        raise ValueError("Approved plan must contain an approvedPairs array")

    validated: list[dict[str, Any]] = []
    removed_locations: set[tuple[str, int]] = set()
    kept_locations: set[tuple[str, int]] = set()
    pair_ids: set[str] = set()
    for pair_index, pair in enumerate(pairs):
        if not isinstance(pair, dict):
            raise ValueError(f"approvedPairs[{pair_index}] must be an object")
        set_id = str(pair.get("setId") or "").strip()
        if not set_id or set_id not in cards_by_set:
            raise ValueError(
                f"approvedPairs[{pair_index}].setId is not a current card file"
            )
        evidence = pair.get("identityEvidence")
        if not isinstance(evidence, dict):
            raise ValueError(
                f"approvedPairs[{pair_index}].identityEvidence must be an object"
            )
        if evidence.get("provider_equivalence") is not True:
            raise ValueError(
                f"approvedPairs[{pair_index}] requires explicit "
                "identityEvidence.provider_equivalence=true"
            )

        cards = cards_by_set[set_id]
        kept_index, kept = _resolve_approved_locator(
            pair_index=pair_index,
            role="kept",
            locator=pair.get("kept"),
            cards=cards,
        )
        removed_index, removed = _resolve_approved_locator(
            pair_index=pair_index,
            role="removed",
            locator=pair.get("removed"),
            cards=cards,
        )
        if kept_index == removed_index:
            raise ValueError(
                f"approvedPairs[{pair_index}] kept and removed are the same card"
            )

        numbering_policy, set_printed_total = set_contexts.get(
            set_id,
            (_DEFAULT_NUMBERING_POLICY, None),
        )
        kept_set_id = str(kept.get("setId") or set_id).strip()
        removed_set_id = str(removed.get("setId") or set_id).strip()
        same_set = kept_set_id == removed_set_id == set_id
        kept_key = identity_collector_key(
            kept.get("collectorNumber"),
            numbering_policy=numbering_policy,
            set_printed_total=set_printed_total,
        )
        removed_key = identity_collector_key(
            removed.get("collectorNumber"),
            numbering_policy=numbering_policy,
            set_printed_total=set_printed_total,
        )
        kept_variant = variant_signature(kept)
        removed_variant = variant_signature(removed)
        kept_name_fp = name_fingerprint(kept.get("name"))
        removed_name_fp = name_fingerprint(removed.get("name"))
        same_collector = bool(kept_key) and kept_key == removed_key
        same_variant = kept_variant == removed_variant
        exact_name = bool(kept_name_fp) and kept_name_fp == removed_name_fp

        if not same_set:
            raise ValueError(
                f"approvedPairs[{pair_index}] cards do not belong to the same set"
            )
        if not same_collector:
            raise ValueError(
                f"approvedPairs[{pair_index}] collector identities differ under "
                f"{numbering_policy}"
            )
        if not same_variant:
            raise ValueError(
                f"approvedPairs[{pair_index}] variant signatures differ"
            )
        if not exact_name:
            raise ValueError(
                f"approvedPairs[{pair_index}] requires an exact name fingerprint; "
                "fuzzy names_compatible evidence cannot authorize deletion"
            )

        expected_evidence = {
            "sameSet": True,
            "numberingPolicy": numbering_policy,
            "setPrintedTotal": set_printed_total,
            "keptIdentityCollectorKey": kept_key,
            "removedIdentityCollectorKey": removed_key,
            "sameCollectorIdentity": True,
            "keptVariantSignature": kept_variant,
            "removedVariantSignature": removed_variant,
            "sameVariantSignature": True,
            "keptNameFingerprint": kept_name_fp,
            "removedNameFingerprint": removed_name_fp,
            "exactNameFingerprint": True,
        }
        for field, expected in expected_evidence.items():
            if field not in evidence or evidence.get(field) != expected:
                raise ValueError(
                    f"approvedPairs[{pair_index}].identityEvidence.{field} "
                    "does not match recomputed evidence"
                )

        kept_sha = _card_sha256(kept)
        removed_sha = _card_sha256(removed)
        expected_pair_id = _pair_id(
            set_id=set_id,
            kept_sha256=kept_sha,
            removed_sha256=removed_sha,
        )
        if pair.get("pairId") != expected_pair_id:
            raise ValueError(
                f"approvedPairs[{pair_index}].pairId does not match the exact pair"
            )
        if expected_pair_id in pair_ids:
            raise ValueError(f"approvedPairs[{pair_index}] duplicates an earlier pair")
        pair_ids.add(expected_pair_id)

        kept_location = (set_id, kept_index)
        removed_location = (set_id, removed_index)
        if removed_location in removed_locations:
            raise ValueError(
                f"approvedPairs[{pair_index}] removes a card more than once"
            )
        removed_locations.add(removed_location)
        kept_locations.add(kept_location)
        validated.append(
            {
                "pairId": expected_pair_id,
                "setId": set_id,
                "keptIndex": kept_index,
                "removedIndex": removed_index,
                "kept": _card_locator(kept, original_index=kept_index),
                "removed": _card_locator(removed, original_index=removed_index),
                "identityEvidence": {
                    **expected_evidence,
                    "provider_equivalence": True,
                },
            }
        )

    conflict = kept_locations & removed_locations
    if conflict:
        set_id, index = sorted(conflict)[0]
        raise ValueError(
            f"Approved plan both keeps and removes {set_id} card index {index}"
        )
    return validated


def repair_catalogue(
    *,
    en_root: Path,
    apply: bool,
    report_path: Path,
    approved_plan_path: Path | None = None,
    numbering_policy_registry_path: Path | None = DEFAULT_NUMBERING_POLICY_REGISTRY,
) -> dict[str, Any]:
    if apply and approved_plan_path is None:
        raise SystemExit("--apply requires --approved-plan PATH")
    if not apply and approved_plan_path is not None:
        raise SystemExit("--approved-plan is only valid together with --apply")

    sets_path = en_root / "sets.json"
    cards_dir = en_root / "cards"
    if not sets_path.is_file():
        raise FileNotFoundError(f"Missing sets.json: {sets_path}")
    if not cards_dir.is_dir():
        raise FileNotFoundError(f"Missing cards dir: {cards_dir}")

    sets_doc = load_json(sets_path)
    sets_list: list[dict[str, Any]] = list(sets_doc.get("sets") or [])
    set_ids_in_index = {str(item.get("id")) for item in sets_list if item.get("id")}
    sets_by_id = {
        str(item.get("id")): item
        for item in sets_list
        if isinstance(item, dict) and item.get("id")
    }
    default_policy, numbering_policies = load_numbering_policy_registry(
        numbering_policy_registry_path
    )

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
    set_contexts: dict[str, tuple[str, int | None]] = {}
    candidate_pairs: list[dict[str, Any]] = []
    per_set_candidates: list[dict[str, Any]] = []
    for set_id, cards in sorted(cards_by_set.items()):
        set_meta = sets_by_id.get(set_id) or {}
        numbering_policy = numbering_policies.get(set_id, default_policy)
        set_printed_total = _optional_int(set_meta.get("printedTotal"))
        set_contexts[set_id] = (numbering_policy, set_printed_total)
        pairs = build_candidate_pairs(
            cards,
            set_id=set_id,
            numbering_policy=numbering_policy,
            set_printed_total=set_printed_total,
        )
        if pairs:
            candidate_pairs.extend(pairs)
            per_set_candidates.append(
                {
                    "setId": set_id,
                    "cardCount": len(cards),
                    "numberingPolicy": numbering_policy,
                    "setPrintedTotal": set_printed_total,
                    "candidatePairCount": len(pairs),
                    "candidatePairIds": [pair["pairId"] for pair in pairs],
                }
            )

    clones = find_clone_sets(set(cards_by_set), cards_by_set)
    for clone in clones:
        # Defense in depth: clone deletion is never an apply operation.
        clone["autoDeleteAllowed"] = False

    generated_at = utc_now()
    candidate_plan = {
        "schemaVersion": _CANDIDATE_PLAN_SCHEMA,
        "generatedAtUtc": generated_at,
        "source": {
            "enRoot": str(en_root),
            "setsPath": str(sets_path),
            "setsSha256": hashlib.sha256(sets_path.read_bytes()).hexdigest(),
            "numberingPolicyRegistry": (
                str(numbering_policy_registry_path)
                if numbering_policy_registry_path is not None
                else None
            ),
            "numberingPolicyRegistrySha256": (
                hashlib.sha256(numbering_policy_registry_path.read_bytes()).hexdigest()
                if numbering_policy_registry_path is not None
                and numbering_policy_registry_path.is_file()
                else None
            ),
        },
        "approvalInstructions": {
            "generatedPlanIsNotApproved": True,
            "approvedPlanSchemaVersion": _APPROVED_PLAN_SCHEMA,
            "approvedPlanArray": "approvedPairs",
            "requiredManualEvidenceFlag": "identityEvidence.provider_equivalence=true",
            "copyOnlyIndependentlyVerifiedPairs": True,
        },
        "authorizationRule": {
            "sameSet": True,
            "sameNumberingPolicyAwareCollectorIdentity": True,
            "sameVariantSignature": True,
            "exactNameFingerprint": True,
            "provider_equivalence": True,
            "fuzzyNamesCompatibleAuthorizesDeletion": False,
        },
        "candidatePairs": candidate_pairs,
        "cloneSets": clones,
    }

    validated_pairs: list[dict[str, Any]] = []
    approved_plan_sha256: str | None = None
    if apply:
        assert approved_plan_path is not None
        if not approved_plan_path.is_file():
            raise SystemExit(f"Approved plan not found: {approved_plan_path}")
        approved_plan_sha256 = hashlib.sha256(
            approved_plan_path.read_bytes()
        ).hexdigest()
        try:
            validated_pairs = _validate_approved_plan(
                approved_plan=load_json(approved_plan_path),
                cards_by_set=cards_by_set,
                set_contexts=set_contexts,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SystemExit(f"Approved plan rejected: {exc}") from exc

    removals_by_set: dict[str, set[int]] = defaultdict(set)
    for pair in validated_pairs:
        removals_by_set[pair["setId"]].add(pair["removedIndex"])

    cards_after = cards_before - sum(len(indices) for indices in removals_by_set.values())
    report = {
        "schemaVersion": "dedup-dry-run-report-v5" if not apply else "dedup-apply-report-v5",
        "generatedAtUtc": generated_at,
        "dryRun": not apply,
        "mode": "CANDIDATE_GENERATION_ONLY" if not apply else "APPROVED_PAIR_APPLY",
        "enRoot": str(en_root),
        "approvedPlanPath": str(approved_plan_path) if approved_plan_path else None,
        "approvedPlanSha256": approved_plan_sha256,
        "candidatePlan": candidate_plan,
        "summary": {
            "setFilesScanned": len(card_files),
            "setsInIndexBefore": len(set_ids_in_index),
            "setsInIndexAfter": len(set_ids_in_index),
            "cardsBefore": cards_before,
            "cardsAfter": cards_after,
            "candidatePairs": len(candidate_pairs),
            "candidatePairsMeetingExactIdentityExceptProviderEquivalence": sum(
                1
                for pair in candidate_pairs
                if pair["deletionRequirementsMetExceptProviderEquivalence"]
            ),
            "approvedPairsValidated": len(validated_pairs),
            "duplicateCardsRemoved": len(validated_pairs) if apply else 0,
            "cloneCardsRemoved": 0,
            "setsWithCandidates": len(per_set_candidates),
            "cloneSetsRemoved": 0,
            "cloneSetAutoDeleteCandidates": 0,
            "cloneSetReviewCandidates": len(clones),
            "aggregateCardCountAfter": sum(
                len(cards_by_set[sid]) - len(removals_by_set.get(sid, set()))
                for sid in cards_by_set
                if sid in set_ids_in_index
            ),
        },
        "cloneSets": clones,
        "perSetCandidates": per_set_candidates,
        "appliedPairs": validated_pairs if apply else [],
        "safety": {
            "defaultIsDryRun": True,
            "dryRunWritesCatalogueFiles": False,
            "cloneSetAutoDeleteAllowed": False,
            "cloneFilesUnlinked": False,
            "setsJsonRewritten": False,
            "fuzzyNamesCompatibleAuthorizesDeletion": False,
        },
    }

    if apply:
        for set_id, removed_indices in sorted(removals_by_set.items()):
            path = cards_dir / f"{set_id}.json"
            payload = dict(card_docs[set_id])
            cards = cards_by_set[set_id]
            payload["cards"] = [
                card for index, card in enumerate(cards) if index not in removed_indices
            ]
            payload["cardCount"] = len(payload["cards"])
            dump_json(path, payload)
            print(
                f"[repair] wrote {set_id}.json cardCount={len(payload['cards'])} "
                f"approvedRemovals={len(removed_indices)}"
            )

    dump_json(report_path, report)
    print(
        f"[repair] dry_run={not apply} scanned_sets={len(card_files)} "
        f"candidate_pairs={len(candidate_pairs)} "
        f"approved_removals={len(validated_pairs) if apply else 0} "
        f"clone_sets={len(clones)} "
        f"cards {cards_before} -> {cards_after}"
    )
    print(f"[repair] report -> {report_path}")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--en-root", type=Path, default=DEFAULT_EN_ROOT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--numbering-policy-registry",
        type=Path,
        default=DEFAULT_NUMBERING_POLICY_REGISTRY,
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Remove only exact pairs from --approved-plan (default: candidate dry-run)",
    )
    parser.add_argument(
        "--approved-plan",
        type=Path,
        help="Required JSON approval document when --apply is set",
    )
    args = parser.parse_args()
    if args.apply and args.approved_plan is None:
        parser.error("--apply requires --approved-plan PATH")
    if not args.apply and args.approved_plan is not None:
        parser.error("--approved-plan requires --apply")
    repair_catalogue(
        en_root=args.en_root,
        apply=args.apply,
        report_path=args.report,
        approved_plan_path=args.approved_plan,
        numbering_policy_registry_path=args.numbering_policy_registry,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
