from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .constants import DEFAULT_CATALOGUE_ROOT, SUPPORTED_LANGUAGES
from .normalization import (
    build_search_aliases,
    normalize_collector_number,
    normalize_search_text,
    normalize_set_name,
)

try:
    from cardscanr_catalogue_identity import (
        IDENTITY_MODEL_VERSION,
        physical_printing_id,
        variant_signature,
    )
except ImportError:  # pragma: no cover - package co-located in monorepo checkout
    IDENTITY_MODEL_VERSION = "physical-printing-v1"
    physical_printing_id = None  # type: ignore[assignment]
    variant_signature = None  # type: ignore[assignment]

DEFAULT_NUMBERING_POLICY = "SEQUENTIAL_FRACTION"


@dataclass(frozen=True)
class SetRecord:
    set_id: str
    language: str
    name: str
    normalized_set_name: str
    total: int | None
    printed_total: int | None
    release_date: str | None
    ptcgo_code: str | None
    series: str | None
    numbering_policy: str = DEFAULT_NUMBERING_POLICY


@dataclass(frozen=True)
class CardRecord:
    schema_version: str
    generated_at: str
    canonical_base_id: str
    canonical_english_name: str | None
    localized_name: str | None
    normalized_canonical_name: str
    normalized_localized_name: str
    search_aliases: list[str]
    language: str
    set_id: str
    set_name: str
    normalized_set_name: str
    provider_set_codes: list[str]
    collector_number: str
    normalized_collector_number: str
    local_number: str | None
    set_total: int | None
    rarity: str | None
    thumbnail_url: str | None
    large_image_url: str | None
    image_source: str | None
    image_cached: bool
    provider_ids_json: str
    promotion_provider_set_id: str | None
    release_date: str | None
    set_release_date: str | None
    set_ptcgo_code: str | None
    physical_printing_id: str | None = None
    identity_model_version: str | None = None
    base_card_reference: str | None = None
    printing_class: str | None = None
    variant_signature: str | None = None
    product_family: str | None = None
    stamp_type: str | None = None
    card_size: str | None = None
    edition: str | None = None
    deck_variant: str | None = None
    event_context: str | None = None


@dataclass
class CatalogueSnapshot:
    source_hashes: dict[str, str] = field(default_factory=dict)
    per_language_counts: dict[str, int] = field(default_factory=dict)
    total_cards: int = 0


def load_json(path: Path) -> Any:
    with open(path, encoding="utf-8-sig") as handle:
        return json.load(handle)


def load_numbering_policies(path: Path | None) -> dict[str, str]:
    """Load set-id to numbering-policy mappings from the optional registry."""
    if path is None or not path.is_file():
        return {}
    payload = load_json(path)
    if not isinstance(payload, dict):
        raise ValueError(f"Numbering policy registry must be an object: {path}")
    set_policies = payload.get("setPolicies")
    if not isinstance(set_policies, dict):
        return {}
    policies: dict[str, str] = {}
    for set_id, entry in set_policies.items():
        if set_id == "defaultMainExpansion" or not isinstance(entry, dict):
            continue
        policy = str(entry.get("numberingPolicy") or "").strip()
        if policy:
            policies[str(set_id)] = policy
    return policies


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_local_number(collector_number: str) -> str | None:
    normalized = normalize_collector_number(collector_number)
    if "/" in normalized:
        return normalized.split("/", 1)[0]
    return normalized or None


def _provider_set_codes(card: dict[str, Any], set_meta: SetRecord) -> list[str]:
    codes: set[str] = set()
    promo = card.get("promotionMetadata")
    if isinstance(promo, dict):
        for key in ("providerSetId", "providerSetCode"):
            value = promo.get(key)
            if value:
                codes.add(str(value).strip())
    if set_meta.ptcgo_code:
        codes.add(set_meta.ptcgo_code.strip())
    codes.add(set_meta.set_id)
    return sorted(code for code in codes if code)


def _card_to_record(
    card: dict[str, Any],
    *,
    set_meta: SetRecord,
    file_generated_at: str,
    file_schema_version: str,
) -> CardRecord | None:
    canonical_base_id = str(card.get("canonicalBaseId") or "").strip()
    if not canonical_base_id:
        return None
    language = str(card.get("language") or set_meta.language).strip().lower()
    set_id = str(card.get("setId") or set_meta.set_id).strip()
    collector_number = str(card.get("collectorNumber") or "").strip()
    name = str(card.get("name") or "").strip()
    normalized_name = str(card.get("normalizedName") or normalize_search_text(name)).strip()
    display_name = str(card.get("displayName") or "").strip() or None
    original_name = str(card.get("originalName") or "").strip() or None
    localized_name = original_name or (display_name if language != "en" else None) or name
    canonical_english_name = name if language == "en" else (display_name if display_name else None)
    promo = card.get("promotionMetadata") if isinstance(card.get("promotionMetadata"), dict) else {}
    provider_set_id = promo.get("providerSetId")
    provider_set_id = str(provider_set_id).strip() if provider_set_id else None
    provider_ids = card.get("providerIds") if isinstance(card.get("providerIds"), dict) else {}

    # productFamily is identity-bearing when it represents the physical product
    # and no more-specific productVariant is present. Keep variantSignature
    # consistent with physical_printing_id() so downstream comparison is exact.
    card_for_sig = dict(card)
    if card.get("productFamily") and not card.get("productVariant"):
        card_for_sig.setdefault("productVariant", card.get("productFamily"))
    v_sig = (
        str(card.get("variantSignature") or "").strip()
        or (variant_signature(card_for_sig) if variant_signature else None)
    )
    persisted_p_pid = str(card.get("physicalPrintingId") or "").strip() or None
    persisted_version = str(card.get("identityModelVersion") or "").strip()
    if physical_printing_id:
        p_pid = physical_printing_id(
            language=language,
            set_id=set_id,
            collector_number=collector_number,
            card=card,
            numbering_policy=set_meta.numbering_policy,
            set_printed_total=set_meta.printed_total,
        )
    elif persisted_p_pid and persisted_version == IDENTITY_MODEL_VERSION:
        p_pid = persisted_p_pid
    else:
        p_pid = None
    identity_version = (
        persisted_version
        if p_pid and persisted_p_pid == p_pid and persisted_version == IDENTITY_MODEL_VERSION
        else (IDENTITY_MODEL_VERSION if p_pid else None)
    )
    aliases = build_search_aliases(
        name=name,
        normalized_name=normalized_name,
        localized_name=localized_name,
        display_name=display_name,
        original_name=original_name,
        set_name=set_meta.name,
        set_code=set_meta.ptcgo_code,
        provider_set_id=provider_set_id,
        collector_number=collector_number,
    )
    return CardRecord(
        schema_version=file_schema_version,
        generated_at=file_generated_at,
        canonical_base_id=canonical_base_id,
        canonical_english_name=canonical_english_name,
        localized_name=localized_name,
        normalized_canonical_name=normalize_search_text(normalized_name or name),
        normalized_localized_name=normalize_search_text(localized_name or name),
        search_aliases=aliases,
        language=language,
        set_id=set_id,
        set_name=set_meta.name,
        normalized_set_name=set_meta.normalized_set_name,
        provider_set_codes=_provider_set_codes(card, set_meta),
        collector_number=collector_number,
        normalized_collector_number=normalize_collector_number(collector_number),
        local_number=_parse_local_number(collector_number),
        set_total=_optional_int(card.get("setTotal")) or set_meta.total,
        rarity=str(card.get("rarity") or "").strip() or None,
        thumbnail_url=str(card.get("imageSmall") or card.get("imageUrlSmall") or "").strip() or None,
        large_image_url=str(card.get("imageLarge") or card.get("imageUrlLarge") or "").strip() or None,
        image_source=str(card.get("imageSource") or card.get("providerImageSource") or "").strip() or None,
        image_cached=bool(card.get("imageCached")),
        provider_ids_json=json.dumps(provider_ids, ensure_ascii=False, sort_keys=True),
        promotion_provider_set_id=provider_set_id,
        release_date=set_meta.release_date,
        set_release_date=set_meta.release_date,
        set_ptcgo_code=set_meta.ptcgo_code,
        physical_printing_id=p_pid,
        identity_model_version=identity_version,
        base_card_reference=str(card.get("baseCardReference") or "").strip() or None,
        printing_class=str(card.get("printingClass") or "").strip() or None,
        variant_signature=v_sig,
        product_family=str(card.get("productFamily") or "").strip() or None,
        stamp_type=str(card.get("stampType") or "").strip() or None,
        card_size=str(card.get("cardSize") or "").strip() or None,
        edition=str(card.get("edition") or "").strip() or None,
        deck_variant=str(card.get("deckVariant") or "").strip() or None,
        event_context=str(card.get("eventContext") or "").strip() or None,
    )


def iter_set_records(
    catalogue_root: Path,
    *,
    language: str,
    numbering_policies: Mapping[str, str] | None = None,
) -> list[SetRecord]:
    sets_path = catalogue_root / "catalog" / "pokemon" / language / "sets.json"
    payload = load_json(sets_path)
    policies = numbering_policies or {}
    records: list[SetRecord] = []
    for item in payload.get("sets") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        set_id = str(item["id"]).strip()
        name = str(item.get("name") or item.get("id")).strip()
        records.append(
            SetRecord(
                set_id=set_id,
                language=language,
                name=name,
                normalized_set_name=normalize_set_name(name),
                total=_optional_int(item.get("total")),
                printed_total=_optional_int(item.get("printedTotal")),
                release_date=str(item.get("releaseDate") or "").strip() or None,
                ptcgo_code=str(item.get("ptcgoCode") or "").strip() or None,
                series=str(item.get("series") or "").strip() or None,
                numbering_policy=policies.get(set_id, DEFAULT_NUMBERING_POLICY),
            )
        )
    return records


def _supplemental_search_gates_ok(item: dict[str, Any]) -> bool:
    required_bools = ("languageVerified", "physicalProductVerified", "rosterVerified")
    for key in required_bools:
        if item.get(key) is not True:
            return False
    for key in ("identityModelVersion", "authorityEvidenceId"):
        if not str(item.get(key) or "").strip():
            return False
    return str(item.get("searchInclusion") or "").strip() == "approved_supplemental"


def _card_search_included(card: dict[str, Any]) -> bool:
    """Exclude authority-pending cards from user-facing staging search."""
    status = str(card.get("authorityStatus") or "").strip()
    if status == "ROSTER_AUTHORITY_PENDING":
        return False
    return True


def _approved_supplemental_set_ids(
    catalogue_root: Path,
    *,
    language: str,
    numbering_policies: Mapping[str, str] | None = None,
) -> dict[str, SetRecord]:
    """Optional explicit supplemental registry — never directory-scan orphans.

    File: catalog/pokemon/<lang>/approved_supplemental_sets.json
    Shape: { \"sets\": [ {\"id\", \"name\", \"searchInclusion\": \"approved_supplemental\", ...} ] }
    Only entries with searchInclusion == approved_supplemental are indexed.
    """
    path = catalogue_root / "catalog" / "pokemon" / language / "approved_supplemental_sets.json"
    if not path.exists():
        return {}
    payload = load_json(path)
    policies = numbering_policies or {}
    out: dict[str, SetRecord] = {}
    for item in payload.get("sets") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        if str(item.get("searchInclusion") or "").strip() != "approved_supplemental":
            continue
        if not _supplemental_search_gates_ok(item):
            continue
        set_id = str(item["id"]).strip()
        name = str(item.get("name") or set_id).strip()
        out[set_id] = SetRecord(
            set_id=set_id,
            language=language,
            name=name,
            normalized_set_name=normalize_set_name(name),
            total=_optional_int(item.get("total")),
            printed_total=_optional_int(item.get("printedTotal")),
            release_date=str(item.get("releaseDate") or "").strip() or None,
            ptcgo_code=str(item.get("ptcgoCode") or "").strip() or None,
            series=str(item.get("series") or item.get("productType") or "").strip() or None,
            numbering_policy=policies.get(set_id, DEFAULT_NUMBERING_POLICY),
        )
    return out


def iter_catalogue_cards(
    catalogue_root: Path = DEFAULT_CATALOGUE_ROOT,
    *,
    languages: tuple[str, ...] = SUPPORTED_LANGUAGES,
    numbering_policy_path: Path | None = None,
) -> Iterator[CardRecord]:
    """Index only sets.json + explicitly approved supplemental sets.

    Quarantined / unresolved provider card files must NOT enter user search
    merely because they exist under cards/*.json. When supplied,
    ``numbering_policy_path`` controls policy-aware physical printing IDs.
    """
    numbering_policies = load_numbering_policies(numbering_policy_path)
    for language in languages:
        set_index = {
            item.set_id: item
            for item in iter_set_records(
                catalogue_root,
                language=language,
                numbering_policies=numbering_policies,
            )
        }
        supplemental = _approved_supplemental_set_ids(
            catalogue_root,
            language=language,
            numbering_policies=numbering_policies,
        )
        allowed = {**set_index, **{k: v for k, v in supplemental.items() if k not in set_index}}
        cards_dir = catalogue_root / "catalog" / "pokemon" / language / "cards"
        for set_id, set_meta in sorted(allowed.items(), key=lambda kv: kv[0].lower()):
            path = cards_dir / f"{set_id}.json"
            if not path.exists():
                continue
            payload = load_json(path)
            if not isinstance(payload, dict):
                continue
            file_generated_at = str(payload.get("generatedAtUtc") or "")
            file_schema_version = str(payload.get("schemaVersion") or "1.0.0")
            if set_id in supplemental:
                file_set_id = str(payload.get("setId") or set_id).strip()
                file_language = str(payload.get("language") or language).strip().lower()
                if file_set_id != set_id or file_language != language:
                    continue
            # Prefer file display name when sets.json name is sparse.
            file_name = str(payload.get("setName") or "").strip()
            if file_name and set_id in supplemental:
                set_meta = SetRecord(
                    set_id=set_meta.set_id,
                    language=set_meta.language,
                    name=file_name,
                    normalized_set_name=normalize_set_name(file_name),
                    total=set_meta.total,
                    printed_total=set_meta.printed_total,
                    release_date=set_meta.release_date,
                    ptcgo_code=set_meta.ptcgo_code,
                    series=set_meta.series,
                    numbering_policy=set_meta.numbering_policy,
                )
            for card in payload.get("cards") or []:
                if not isinstance(card, dict):
                    continue
                if not _card_search_included(card):
                    continue
                card_set_id = str(card.get("setId") or set_id).strip()
                card_lang = str(card.get("language") or set_meta.language).strip().lower()
                if set_id in supplemental:
                    if card_set_id != set_id:
                        continue
                    if card_lang != language:
                        continue
                record = _card_to_record(
                    card,
                    set_meta=set_meta,
                    file_generated_at=file_generated_at,
                    file_schema_version=file_schema_version,
                )
                if record is not None:
                    yield record


def collect_catalogue_snapshot(catalogue_root: Path = DEFAULT_CATALOGUE_ROOT) -> CatalogueSnapshot:
    snapshot = CatalogueSnapshot()
    for language in SUPPORTED_LANGUAGES:
        sets_path = catalogue_root / "catalog" / "pokemon" / language / "sets.json"
        snapshot.source_hashes[f"catalog/pokemon/{language}/sets.json"] = sha256_file(sets_path)
        supp_path = catalogue_root / "catalog" / "pokemon" / language / "approved_supplemental_sets.json"
        if supp_path.exists():
            snapshot.source_hashes[
                f"catalog/pokemon/{language}/approved_supplemental_sets.json"
            ] = sha256_file(supp_path)
        set_index = {item.set_id for item in iter_set_records(catalogue_root, language=language)}
        set_index |= set(_approved_supplemental_set_ids(catalogue_root, language=language))
        cards_dir = catalogue_root / "catalog" / "pokemon" / language / "cards"
        for set_id in sorted(set_index):
            path = cards_dir / f"{set_id}.json"
            if not path.exists():
                continue
            rel = f"catalog/pokemon/{language}/cards/{path.name}"
            snapshot.source_hashes[rel] = sha256_file(path)
    counts = {language: 0 for language in SUPPORTED_LANGUAGES}
    for record in iter_catalogue_cards(catalogue_root):
        counts[record.language] = counts.get(record.language, 0) + 1
        snapshot.total_cards += 1
        snapshot.per_language_counts = counts
    return snapshot
