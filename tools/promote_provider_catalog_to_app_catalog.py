#!/usr/bin/env python3
"""Promote safe Pokewallet provider catalogue records into app catalogue files."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import unicodedata
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
V1_DIR = ROOT / "public" / "v1"
PROVIDER_ROOT = V1_DIR / "provider-catalog" / "pokewallet" / "cards"
APP_ROOT = V1_DIR / "catalog" / "pokemon"
REPORTS_DIR = ROOT / "reports"
REPORT_JSON_PATH = REPORTS_DIR / "provider_to_app_promotion_latest.json"
REPORT_MD_PATH = REPORTS_DIR / "provider_to_app_promotion_gaps.md"
SCHEMA_VERSION = "1.0.0"
PROMOTION_SOURCE = "pokewallet"
PROMOTION_DETAIL_SOURCE = "pokewallet_provider_promotion"
DEFAULT_LANGUAGES = ["en", "jp"]
SUPPORTED_WITH_FLAG = {"en", "jp", "zh"}


@dataclass(frozen=True)
class ProviderRecord:
    language: str
    path: Path
    file_set_id: str
    file_set_code: str
    file_set_name: str
    card: dict[str, Any]


@dataclass(frozen=True)
class PromotionCandidate:
    provider: ProviderRecord
    app_set_id: str
    app_set_name: str
    collector_number: str
    raw_name: str
    display_name: str
    normalized_name: str
    image_small: str
    image_large: str
    variant_key: str
    identity_key: str
    canonical_base_id: str


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path: Path) -> Any:
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def try_load_json(path: Path) -> Any:
    try:
        return load_json(path)
    except (OSError, json.JSONDecodeError):
        return None


def json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write_json_if_changed(path: Path, payload: Any) -> bool:
    encoded = json_bytes(payload)
    if path.exists() and path.read_bytes() == encoded:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_bytes(encoded)
    os.replace(tmp_path, path)
    return True


def write_text_if_changed(path: Path, text: str) -> bool:
    encoded = text.encode("utf-8")
    if path.exists() and path.read_bytes() == encoded:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_bytes(encoded)
    os.replace(tmp_path, path)
    return True


def normalize_catalog_name(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    normalized = re.sub(r"[^\w]+", "_", normalized, flags=re.UNICODE).strip("_")
    normalized = re.sub(r"_+", "_", normalized)
    return normalized or "unknown"


def normalize_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", unicodedata.normalize("NFKC", str(value or "")).lower())


def normalize_number(value: Any) -> str:
    raw = unicodedata.normalize("NFKC", str(value or "")).strip()
    raw = re.sub(r"\s+", " ", raw)
    return raw


def safe_set_id(value: Any, *, language: str) -> str:
    raw = unicodedata.normalize("NFKC", str(value or "")).strip()
    raw = re.sub(r"[\\/:*?\"<>|]+", "-", raw)
    raw = re.sub(r"\s+", "-", raw)
    raw = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip(".-_")
    raw = re.sub(r"-+", "-", raw)
    if language == "en":
        raw = raw.lower()
    return raw


def provider_endpoint_url(endpoint: Any) -> str | None:
    raw = str(endpoint or "").strip()
    if not raw:
        return None
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    base = os.getenv("POKEWALLET_IMAGE_BASE_URL", "https://api.pokewallet.io").rstrip("/")
    if raw.startswith("/"):
        return f"{base}{raw}"
    return f"{base}/{raw}"


def selected_languages(raw: str | None, *, include_zh: bool) -> list[str]:
    values = [item.strip().lower() for item in str(raw or "en,jp").split(",") if item.strip()]
    if include_zh and "zh" not in values:
        values.append("zh")
    result: list[str] = []
    for value in values:
        if value in SUPPORTED_WITH_FLAG and value not in result:
            result.append(value)
    return result or list(DEFAULT_LANGUAGES)


def iter_provider_records(languages: list[str] | None = None) -> list[ProviderRecord]:
    wanted = set(languages) if languages else None
    records: list[ProviderRecord] = []
    if not PROVIDER_ROOT.exists():
        return records
    for language_dir in sorted([item for item in PROVIDER_ROOT.iterdir() if item.is_dir()], key=lambda item: item.name):
        language = language_dir.name.lower()
        if wanted is not None and language not in wanted:
            continue
        for path in sorted(language_dir.glob("*.json"), key=lambda item: item.name.lower()):
            payload = try_load_json(path)
            if not isinstance(payload, dict):
                continue
            cards = payload.get("cards")
            if not isinstance(cards, list):
                continue
            file_set_id = str(payload.get("providerSetId") or path.stem).strip()
            file_set_code = str(payload.get("providerSetCode") or "").strip()
            file_set_name = str(payload.get("providerSetName") or file_set_code or file_set_id).strip()
            for card in cards:
                if isinstance(card, dict):
                    records.append(
                        ProviderRecord(
                            language=language,
                            path=path,
                            file_set_id=file_set_id,
                            file_set_code=file_set_code,
                            file_set_name=file_set_name,
                            card=card,
                        )
                    )
    return records


def load_app_sets(language: str) -> dict[str, Any]:
    path = APP_ROOT / language / "sets.json"
    payload = try_load_json(path)
    if isinstance(payload, dict):
        return payload
    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAtUtc": now_utc(),
        "game": "pokemon",
        "language": language,
        "catalogueStatus": "not_built_yet",
        "cardsAvailable": False,
        "sets": [],
        "source": PROMOTION_SOURCE,
        "notes": [],
        "setCount": 0,
        "cardCount": 0,
        "partialSetCount": 0,
        "failedSetCount": 0,
        "failedSetIds": [],
    }


def load_app_card_files(language: str) -> dict[str, dict[str, Any]]:
    cards_dir = APP_ROOT / language / "cards"
    result: dict[str, dict[str, Any]] = {}
    if not cards_dir.exists():
        return result
    for path in sorted(cards_dir.glob("*.json"), key=lambda item: item.name.lower()):
        payload = try_load_json(path)
        if isinstance(payload, dict):
            result[path.stem] = payload
    return result


def app_card_count(language: str) -> int:
    total = 0
    for payload in load_app_card_files(language).values():
        cards = payload.get("cards")
        if isinstance(cards, list):
            total += len([card for card in cards if isinstance(card, dict)])
    return total


def provider_card_count(language: str) -> int:
    return len(iter_provider_records([language]))


def build_app_set_token_map(app_sets: dict[str, Any]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    sets = app_sets.get("sets")
    if not isinstance(sets, list):
        return mapping
    for item in sets:
        if not isinstance(item, dict):
            continue
        set_id = str(item.get("id") or "").strip()
        if not set_id:
            continue
        tokens = {
            normalize_token(set_id),
            normalize_token(item.get("name")),
            normalize_token(item.get("ptcgoCode")),
        }
        for token in tokens:
            if token:
                mapping.setdefault(token, set_id)
    return mapping


def build_existing_identity_indexes(languages: list[str]) -> tuple[set[str], set[str], set[str]]:
    identity_keys: set[str] = set()
    position_keys: set[str] = set()
    pokewallet_ids: set[str] = set()
    for language in languages:
        for set_id, payload in load_app_card_files(language).items():
            cards = payload.get("cards")
            if not isinstance(cards, list):
                continue
            for card in cards:
                if not isinstance(card, dict):
                    continue
                collector = normalize_number(card.get("collectorNumber"))
                normalized_name = str(card.get("normalizedName") or normalize_catalog_name(card.get("name"))).strip()
                variant_key = variant_identity(card.get("availableVariants"))
                identity_keys.add(make_identity_key(language, set_id, collector, normalized_name, variant_key))
                if collector:
                    position_keys.add(make_position_key(language, set_id, collector))
                provider_ids = card.get("providerIds")
                if isinstance(provider_ids, dict):
                    pokewallet = provider_ids.get("pokewallet")
                    if isinstance(pokewallet, str) and pokewallet.strip():
                        pokewallet_ids.add(pokewallet.strip())
    return identity_keys, position_keys, pokewallet_ids


def variant_identity(value: Any) -> str:
    if isinstance(value, list):
        parts = sorted(str(item).strip().lower() for item in value if str(item).strip())
        return ",".join(parts) if parts else "normal"
    if isinstance(value, str) and value.strip():
        return value.strip().lower()
    return "normal"


def make_identity_key(language: str, set_id: str, collector: str, normalized_name: str, variant_key: str) -> str:
    return "|".join([language, set_id, collector_identity_key(collector), normalized_name, variant_key or "normal"])


def make_position_key(language: str, set_id: str, collector: str) -> str:
    return "|".join([language, set_id, collector_identity_key(collector)])


def collector_identity_key(value: Any) -> str:
    raw = normalize_number(value).lower()
    compact = re.sub(r"\s+", "", raw)
    if re.fullmatch(r"\d+", compact or ""):
        return compact.lstrip("0") or "0"
    return raw


def display_name_from_provider(raw_name: str, set_code: str, collector_number: str) -> str:
    value = str(raw_name or "").strip()
    if not value:
        return ""
    suffix = re.search(r"\s+\(([^()]*)\)\s*$", value)
    if suffix:
        inner = suffix.group(1)
        token_hits = 0
        if set_code and normalize_token(set_code) and normalize_token(set_code) in normalize_token(inner):
            token_hits += 1
        if collector_number and normalize_token(collector_number) and normalize_token(collector_number) in normalize_token(inner):
            token_hits += 1
        if token_hits:
            stripped = value[: suffix.start()].strip()
            if stripped:
                return stripped
    return value


def provider_set_identity(record: ProviderRecord, app_set_map: dict[str, str]) -> tuple[str | None, str | None, str]:
    card = record.card
    identity_basis = card.get("imageCacheIdentityBasis") if isinstance(card.get("imageCacheIdentityBasis"), dict) else {}
    raw_code = (
        card.get("providerSetCode")
        or identity_basis.get("setId")
        or record.file_set_code
        or card.get("providerSetName")
        or record.file_set_name
        or ""
    )
    raw_name = card.get("providerSetName") or record.file_set_name or raw_code
    provider_set_id = card.get("providerSetId") or record.file_set_id
    for value in [raw_code, raw_name, provider_set_id]:
        token = normalize_token(value)
        if token and token in app_set_map:
            return app_set_map[token], str(raw_name or app_set_map[token]), "mapped_existing_set"
    fallback = raw_code or raw_name
    safe_id = safe_set_id(fallback, language=record.language)
    if not safe_id:
        return None, None, "invalid_or_unknown_set"
    return safe_id, str(raw_name or safe_id), "provider_set_identity"


def build_candidate(
    record: ProviderRecord,
    *,
    app_set_map: dict[str, str],
    enabled_languages: set[str],
) -> tuple[PromotionCandidate | None, str]:
    card = record.card
    language = str(card.get("cardScanRLanguage") or record.language or "").strip().lower()
    if language not in enabled_languages:
        return None, "unsupported_language"
    if language not in SUPPORTED_WITH_FLAG:
        return None, "unsupported_language"

    provider_card_id = str(card.get("providerCardId") or "").strip()
    if not provider_card_id:
        return None, "missing_provider_card_id"

    app_set_id, app_set_name, set_reason = provider_set_identity(record, app_set_map)
    if not app_set_id or not app_set_name:
        return None, set_reason

    collector_number = normalize_number(
        (card.get("imageCacheIdentityBasis") or {}).get("collectorNumber")
        if isinstance(card.get("imageCacheIdentityBasis"), dict)
        else card.get("cardNumber")
    )
    if not collector_number:
        collector_number = normalize_number(card.get("cardNumber"))
    if not collector_number:
        return None, "missing_collector_number"

    raw_name = str(card.get("cleanName") or card.get("name") or "").strip()
    if not raw_name:
        return None, "missing_name"
    display_name = display_name_from_provider(raw_name, str(card.get("providerSetCode") or record.file_set_code), collector_number)
    if not display_name:
        return None, "missing_name"
    normalized_name = normalize_catalog_name(display_name)
    if not normalized_name or normalized_name == "unknown":
        return None, "missing_name"

    image_small = provider_endpoint_url(card.get("imageEndpointLow") or card.get("imageEndpoint"))
    image_large = provider_endpoint_url(card.get("imageEndpointHigh") or card.get("imageEndpoint"))
    if not image_small or not image_large:
        return None, "missing_image_url"

    variant_key = variant_identity(card.get("variants"))
    identity_key = make_identity_key(language, app_set_id, collector_number, normalized_name, variant_key)
    canonical_base_id = f"pokemon|{language}|{app_set_id}|{collector_number}|{normalized_name}"
    return (
        PromotionCandidate(
            provider=record,
            app_set_id=app_set_id,
            app_set_name=app_set_name,
            collector_number=collector_number,
            raw_name=raw_name,
            display_name=display_name,
            normalized_name=normalized_name,
            image_small=image_small,
            image_large=image_large,
            variant_key=variant_key,
            identity_key=identity_key,
            canonical_base_id=canonical_base_id,
        ),
        "promotable",
    )


def build_app_card(candidate: PromotionCandidate) -> dict[str, Any]:
    card = candidate.provider.card
    language = candidate.provider.language
    provider_ids = {
        "pokemonTcgApi": None,
        "tcgdex": None,
        "pokewallet": card.get("providerCardId"),
    }
    external_ids = {
        "pokemonTcgApiId": None,
        "tcgdexCardId": None,
        "tcgplayerProductId": None,
        "pricechartingId": None,
    }
    base = {
        "canonicalBaseId": candidate.canonical_base_id,
        "game": "pokemon",
        "language": language,
        "setId": candidate.app_set_id,
        "setName": candidate.app_set_name,
        "collectorNumber": candidate.collector_number,
        "name": candidate.display_name,
        "displayName": candidate.display_name,
        "originalName": candidate.raw_name,
        "normalizedName": candidate.normalized_name,
        "rarity": card.get("rarity"),
        "hp": None,
        "imageSmall": candidate.image_small,
        "imageLarge": candidate.image_large,
        "imageSource": PROMOTION_SOURCE,
        "imageCached": False,
        "providerIds": provider_ids,
        "pricingReferences": {
            "tcgplayerAvailable": False,
            "cardmarketAvailable": bool(card.get("hasCardmarketFields")),
            "pokewalletAvailable": bool(card.get("hasPriceFields")),
        },
        "externalIds": external_ids,
        "availableVariants": sorted(str(item) for item in card.get("variants", []) if str(item).strip())
        if isinstance(card.get("variants"), list)
        else [],
        "promotionMetadata": {
            "source": PROMOTION_DETAIL_SOURCE,
            "provider": "pokewallet",
            "providerCardId": card.get("providerCardId"),
            "providerLanguage": card.get("providerLanguage"),
            "providerSetId": card.get("providerSetId") or candidate.provider.file_set_id,
            "providerSetCode": card.get("providerSetCode") or candidate.provider.file_set_code,
            "providerSetName": card.get("providerSetName") or candidate.provider.file_set_name,
            "providerFile": candidate.provider.path.relative_to(ROOT).as_posix(),
            "identityKey": candidate.identity_key,
            "variantKey": candidate.variant_key,
            "confidence": "provider_catalog_identity",
        },
    }
    if language == "en":
        base.update(
            {
                "supertype": None,
                "supertypes": [],
                "subtypes": [],
                "types": [],
                "artist": None,
                "illustrator": None,
            }
        )
    else:
        base.update(
            {
                "category": None,
                "supertype": None,
                "supertypes": [],
                "subtypes": [],
                "types": [],
                "illustrator": None,
            }
        )
    return base


def catalogue_card_sort_key(card: dict[str, Any]) -> tuple[str, str, str]:
    collector = normalize_number(card.get("collectorNumber"))
    numeric = re.sub(r"\D+", "", collector)
    numeric_key = numeric.zfill(8) if numeric else "99999999"
    return (numeric_key, collector.lower(), str(card.get("normalizedName") or card.get("name") or "").lower())


def set_sort_key(item: dict[str, Any]) -> tuple[str, str]:
    return (str(item.get("id") or "").lower(), str(item.get("name") or "").lower())


def card_file_payload(language: str, set_id: str, set_name: str, cards: list[dict[str, Any]], existing: dict[str, Any] | None) -> dict[str, Any]:
    source = existing.get("source") if isinstance(existing, dict) and existing.get("source") else PROMOTION_SOURCE
    if not existing:
        source = PROMOTION_SOURCE
    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAtUtc": (existing or {}).get("generatedAtUtc") or now_utc(),
        "game": "pokemon",
        "language": language,
        "setId": set_id,
        "setName": set_name,
        "source": source,
        "catalogueStatus": "built",
        "cardCount": len(cards),
        "cards": cards,
    }


def update_sets_payload(
    language: str,
    sets_payload: dict[str, Any],
    card_files: dict[str, dict[str, Any]],
    promoted_set_ids: set[str],
) -> dict[str, Any]:
    sets = [dict(item) for item in sets_payload.get("sets", []) if isinstance(item, dict)]
    by_id = {str(item.get("id")): item for item in sets if item.get("id")}
    for set_id in sorted(promoted_set_ids, key=str.lower):
        card_payload = card_files.get(set_id, {})
        if set_id not in by_id:
            by_id[set_id] = {
                "id": set_id,
                "name": card_payload.get("setName") or set_id,
                "series": None,
                "printedTotal": None,
                "total": card_payload.get("cardCount"),
                "releaseDate": None,
                "updatedAt": None,
                "ptcgoCode": None,
                "symbolUrl": None,
                "logoUrl": None,
                "imageSource": PROMOTION_SOURCE,
                "imageCached": False,
                "promotionSource": PROMOTION_DETAIL_SOURCE,
            }
        else:
            by_id[set_id].setdefault("promotionSource", PROMOTION_DETAIL_SOURCE)

    existing_order = [str(item.get("id")) for item in sets if item.get("id")]
    new_ids = sorted([set_id for set_id in by_id if set_id not in existing_order], key=str.lower)
    ordered_sets = [by_id[set_id] for set_id in existing_order if set_id in by_id] + [by_id[set_id] for set_id in new_ids]
    total_cards = 0
    for payload in card_files.values():
        cards = payload.get("cards")
        if isinstance(cards, list):
            total_cards += len(cards)
    source = sets_payload.get("source") or ("pokemon_tcg_api" if language == "en" else "tcgdex" if language == "jp" else PROMOTION_SOURCE)
    updated = dict(sets_payload)
    updated.update(
        {
            "schemaVersion": SCHEMA_VERSION,
            "generatedAtUtc": sets_payload.get("generatedAtUtc") or now_utc(),
            "game": "pokemon",
            "language": language,
            "catalogueStatus": "partial_built" if promoted_set_ids or ordered_sets else "not_built_yet",
            "cardsAvailable": bool(card_files),
            "sets": ordered_sets,
            "source": source,
            "notes": sets_payload.get("notes") if isinstance(sets_payload.get("notes"), list) else [],
            "setCount": len(ordered_sets),
            "cardCount": total_cards,
            "partialSetCount": int(sets_payload.get("partialSetCount") or 0),
            "failedSetCount": int(sets_payload.get("failedSetCount") or 0),
            "failedSetIds": sets_payload.get("failedSetIds") if isinstance(sets_payload.get("failedSetIds"), list) else [],
            "promotionSummary": {
                "source": PROMOTION_DETAIL_SOURCE,
                "promotedSetCount": len(promoted_set_ids),
                "lastPromotionAtUtc": now_utc(),
            },
        }
    )
    return updated


def summarize_duplicate_groups(candidates: list[PromotionCandidate]) -> tuple[set[str], list[dict[str, Any]]]:
    groups: dict[str, list[PromotionCandidate]] = defaultdict(list)
    for candidate in candidates:
        groups[candidate.identity_key].append(candidate)
    duplicate_keys = {key for key, items in groups.items() if len(items) > 1}
    summaries: list[dict[str, Any]] = []
    for key in sorted(duplicate_keys):
        items = groups[key]
        first = items[0]
        summaries.append(
            {
                "identityKey": key,
                "language": first.provider.language,
                "setId": first.app_set_id,
                "collectorNumber": first.collector_number,
                "normalizedName": first.normalized_name,
                "count": len(items),
                "providerCardIds": sorted(str(item.provider.card.get("providerCardId")) for item in items),
            }
        )
    summaries.sort(key=lambda item: (-int(item["count"]), str(item["identityKey"])))
    return duplicate_keys, summaries


def analyse_provider_to_app(languages: list[str], *, include_zh: bool = False) -> dict[str, Any]:
    enabled = set(languages)
    if include_zh:
        enabled.add("zh")
    all_provider_languages = sorted([item.name for item in PROVIDER_ROOT.iterdir() if item.is_dir()]) if PROVIDER_ROOT.exists() else []
    app_languages = sorted([item.name for item in APP_ROOT.iterdir() if item.is_dir()]) if APP_ROOT.exists() else []
    all_languages = sorted(set(all_provider_languages) | set(app_languages) | enabled)
    app_set_maps = {language: build_app_set_token_map(load_app_sets(language)) for language in all_languages}
    existing_identity_keys, existing_position_keys, existing_pokewallet_ids = build_existing_identity_indexes(all_languages)

    raw_candidates: list[PromotionCandidate] = []
    per_provider: list[dict[str, Any]] = []
    reason_counts: Counter[str] = Counter()
    reason_counts_by_language: dict[str, Counter[str]] = defaultdict(Counter)
    provider_counts: Counter[str] = Counter()
    represented_counts: Counter[str] = Counter()
    top_gap_counter: Counter[str] = Counter()
    missing_set_mappings: Counter[str] = Counter()
    missing_collectors: Counter[str] = Counter()

    for record in iter_provider_records(all_provider_languages):
        language = record.language
        provider_counts[language] += 1
        candidate, reason = build_candidate(record, app_set_map=app_set_maps.get(language, {}), enabled_languages=enabled)
        provider_id = str(record.card.get("providerCardId") or "").strip()
        represented = False
        identity_key = None
        if provider_id and provider_id in existing_pokewallet_ids:
            represented = True
        if candidate:
            identity_key = candidate.identity_key
            raw_candidates.append(candidate)
            if identity_key in existing_identity_keys:
                represented = True
            if make_position_key(language, candidate.app_set_id, candidate.collector_number) in existing_position_keys:
                represented = True
        if represented:
            represented_counts[language] += 1
            reason = "already_represented"
        reason_counts[reason] += 1
        reason_counts_by_language[language][reason] += 1
        if reason != "already_represented":
            label = f"{language}|{record.file_set_code or record.file_set_id or 'unknown'}|{record.file_set_name or 'unknown'}"
            top_gap_counter[label] += 1
        if reason in {"invalid_or_unknown_set", "missing_set_mapping"}:
            missing_set_mappings[f"{language}|{record.file_set_code or record.file_set_id or 'unknown'}"] += 1
        if reason == "missing_collector_number":
            missing_collectors[f"{language}|{record.file_set_code or record.file_set_id or 'unknown'}"] += 1
        per_provider.append(
            {
                "language": language,
                "providerCardId": provider_id or None,
                "providerSetId": record.card.get("providerSetId") or record.file_set_id,
                "providerSetCode": record.card.get("providerSetCode") or record.file_set_code,
                "providerSetName": record.card.get("providerSetName") or record.file_set_name,
                "cardNumber": record.card.get("cardNumber"),
                "name": record.card.get("cleanName") or record.card.get("name"),
                "identityKey": identity_key,
                "represented": represented,
                "reason": reason,
            }
        )

    duplicate_keys, duplicate_groups = summarize_duplicate_groups(raw_candidates)
    for item in per_provider:
        if item.get("reason") == "promotable" and item.get("identityKey") in duplicate_keys:
            item["reason"] = "duplicate_candidate"
            reason_counts["promotable"] -= 1
            reason_counts["duplicate_candidate"] += 1
            language = str(item.get("language"))
            reason_counts_by_language[language]["promotable"] -= 1
            reason_counts_by_language[language]["duplicate_candidate"] += 1

    app_counts = {language: app_card_count(language) for language in all_languages}
    provider_count_map = {language: provider_counts.get(language, 0) for language in all_languages}
    not_represented = {
        language: max(0, provider_count_map.get(language, 0) - represented_counts.get(language, 0))
        for language in all_languages
    }
    blocked_reasons = {
        key: value
        for key, value in sorted(reason_counts.items())
        if value and key not in {"already_represented", "promotable"}
    }
    blocked_reasons_by_language = {
        language: {
            key: value
            for key, value in sorted(counter.items())
            if value and key not in {"already_represented", "promotable"}
        }
        for language, counter in sorted(reason_counts_by_language.items())
    }
    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAtUtc": now_utc(),
        "languagesRequested": languages,
        "includeZh": include_zh,
        "providerCardCountByLanguage": provider_count_map,
        "appCatalogueCardCountByLanguage": app_counts,
        "providerCardsAlreadyRepresentedByLanguage": dict(sorted(represented_counts.items())),
        "providerCardsNotRepresentedByLanguage": dict(sorted(not_represented.items())),
        "statusReasonCounts": {key: value for key, value in sorted(reason_counts.items()) if value},
        "statusReasonCountsByLanguage": {
            language: {key: value for key, value in sorted(counter.items()) if value}
            for language, counter in sorted(reason_counts_by_language.items())
        },
        "blockedReasonCounts": blocked_reasons,
        "blockedReasonCountsByLanguage": blocked_reasons_by_language,
        "duplicateCandidates": duplicate_groups[:100],
        "top20GapSets": [
            {"language": key.split("|", 2)[0], "providerSetCode": key.split("|", 2)[1], "providerSetName": key.split("|", 2)[2], "count": count}
            for key, count in top_gap_counter.most_common(20)
        ],
        "topMissingSetMappings": [
            {"key": key, "count": count} for key, count in missing_set_mappings.most_common(20)
        ],
        "topMissingCollectorNumbers": [
            {"key": key, "count": count} for key, count in missing_collectors.most_common(20)
        ],
        "providerRecords": per_provider,
    }


def build_markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Provider To App Promotion",
        "",
        f"- generatedAtUtc: {report.get('generatedAtUtc')}",
        f"- status: {report.get('status', 'report')}",
        f"- languages: {', '.join(report.get('languagesProcessed') or report.get('languagesRequested') or [])}",
        "",
        "## Counts",
    ]
    for language, count in sorted((report.get("providerCardCountByLanguage") or {}).items()):
        app_count = (report.get("afterAppCatalogueCardCountByLanguage") or report.get("appCatalogueCardCountByLanguage") or {}).get(language, 0)
        lines.append(f"- {language}: provider={count} app={app_count}")
    lines.append("")
    lines.append("## Blocked Reasons")
    for reason, count in sorted((report.get("blockedReasonCounts") or {}).items(), key=lambda item: (-int(item[1]), item[0])):
        lines.append(f"- {reason}: {count}")
    lines.append("")
    lines.append("## Top Gaps")
    for item in report.get("top20GapSets", []):
        set_name = item.get("providerSetName") or item.get("setName") or item.get("providerSetCode") or item.get("providerSetId") or "unknown"
        set_code = item.get("providerSetCode") or item.get("providerSetId") or "unknown"
        lines.append(f"- {item.get('language', 'unknown')} {set_code} ({set_name}): {item.get('count', 0)}")
    lines.append("")
    return "\n".join(lines)


def promote(languages: list[str], *, include_zh: bool, dry_run: bool, write_reports: bool) -> dict[str, Any]:
    enabled = set(languages)
    if include_zh:
        enabled.add("zh")
    before_counts = {language: app_card_count(language) for language in sorted(enabled)}
    app_sets = {language: load_app_sets(language) for language in sorted(enabled)}
    app_card_files = {language: load_app_card_files(language) for language in sorted(enabled)}
    app_set_maps = {language: build_app_set_token_map(app_sets[language]) for language in sorted(enabled)}
    existing_identity_keys, existing_position_keys, existing_pokewallet_ids = build_existing_identity_indexes(sorted(enabled))

    candidate_records: list[PromotionCandidate] = []
    blocked: list[dict[str, Any]] = []
    provider_counts: Counter[str] = Counter()
    already_represented: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    reason_counts_by_language: dict[str, Counter[str]] = defaultdict(Counter)

    for record in iter_provider_records(sorted(enabled)):
        language = record.language
        provider_counts[language] += 1
        candidate, reason = build_candidate(record, app_set_map=app_set_maps.get(language, {}), enabled_languages=enabled)
        provider_id = str(record.card.get("providerCardId") or "").strip()
        represented = bool(provider_id and provider_id in existing_pokewallet_ids)
        if candidate and candidate.identity_key in existing_identity_keys:
            represented = True
        if candidate and make_position_key(language, candidate.app_set_id, candidate.collector_number) in existing_position_keys:
            represented = True
        if represented:
            already_represented[language] += 1
            reason = "already_represented"
        if candidate and reason == "promotable":
            candidate_records.append(candidate)
        elif reason != "already_represented":
            reason_counts[reason] += 1
            reason_counts_by_language[language][reason] += 1
            blocked.append(
                {
                    "language": language,
                    "providerCardId": provider_id or None,
                    "providerSetId": record.card.get("providerSetId") or record.file_set_id,
                    "providerSetCode": record.card.get("providerSetCode") or record.file_set_code,
                    "cardNumber": record.card.get("cardNumber"),
                    "name": record.card.get("cleanName") or record.card.get("name"),
                    "reason": reason,
                }
            )

    duplicate_keys, duplicate_groups = summarize_duplicate_groups(candidate_records)
    promoted_candidates: list[PromotionCandidate] = []
    seen_candidate_keys: set[str] = set()
    for candidate in sorted(candidate_records, key=lambda item: (item.identity_key, str(item.provider.card.get("providerCardId")))):
        if candidate.identity_key in duplicate_keys:
            reason_counts["duplicate_candidate"] += 1
            reason_counts_by_language[candidate.provider.language]["duplicate_candidate"] += 1
            blocked.append(
                {
                    "language": candidate.provider.language,
                    "providerCardId": candidate.provider.card.get("providerCardId"),
                    "providerSetId": candidate.provider.card.get("providerSetId") or candidate.provider.file_set_id,
                    "providerSetCode": candidate.provider.card.get("providerSetCode") or candidate.provider.file_set_code,
                    "cardNumber": candidate.collector_number,
                    "name": candidate.raw_name,
                    "reason": "duplicate_candidate",
                    "identityKey": candidate.identity_key,
                }
            )
            continue
        if candidate.identity_key in seen_candidate_keys:
            continue
        promoted_candidates.append(candidate)
        seen_candidate_keys.add(candidate.identity_key)

    promoted_by_language: Counter[str] = Counter()
    promoted_set_ids: dict[str, set[str]] = defaultdict(set)
    changed_files: list[str] = []
    for candidate in promoted_candidates:
        language = candidate.provider.language
        set_id = candidate.app_set_id
        existing_payload = app_card_files[language].get(set_id)
        existing_cards = []
        if isinstance(existing_payload, dict) and isinstance(existing_payload.get("cards"), list):
            existing_cards = [card for card in existing_payload["cards"] if isinstance(card, dict)]
        new_card = build_app_card(candidate)
        merged_cards = existing_cards + [new_card]
        merged_cards.sort(key=catalogue_card_sort_key)
        payload = card_file_payload(language, set_id, candidate.app_set_name, merged_cards, existing_payload)
        app_card_files[language][set_id] = payload
        promoted_by_language[language] += 1
        promoted_set_ids[language].add(set_id)

    for language in sorted(enabled):
        if promoted_set_ids.get(language):
            app_sets[language] = update_sets_payload(language, app_sets[language], app_card_files[language], promoted_set_ids[language])

    if not dry_run:
        for language in sorted(enabled):
            for set_id in sorted(promoted_set_ids.get(language, set()), key=str.lower):
                path = APP_ROOT / language / "cards" / f"{set_id}.json"
                if write_json_if_changed(path, app_card_files[language][set_id]):
                    changed_files.append(path.relative_to(ROOT).as_posix())
            if promoted_set_ids.get(language):
                path = APP_ROOT / language / "sets.json"
                if write_json_if_changed(path, app_sets[language]):
                    changed_files.append(path.relative_to(ROOT).as_posix())

    after_counts = {language: app_card_count(language) for language in sorted(enabled)}
    if dry_run:
        after_counts = {language: before_counts.get(language, 0) + promoted_by_language.get(language, 0) for language in sorted(enabled)}

    top_gap_counter: Counter[str] = Counter()
    for item in blocked:
        if item["reason"] == "already_represented":
            continue
        key = f"{item['language']}|{item.get('providerSetCode') or item.get('providerSetId') or 'unknown'}"
        top_gap_counter[key] += 1

    report = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAtUtc": now_utc(),
        "status": "dry_run" if dry_run else "ok",
        "dryRun": dry_run,
        "languagesProcessed": sorted(enabled),
        "includeZh": include_zh,
        "providerCardCountByLanguage": {language: provider_counts.get(language, 0) for language in sorted(enabled)},
        "beforeAppCatalogueCardCountByLanguage": before_counts,
        "afterAppCatalogueCardCountByLanguage": after_counts,
        "providerCardsAlreadyRepresentedByLanguage": dict(sorted(already_represented.items())),
        "promotedCountByLanguage": dict(sorted(promoted_by_language.items())),
        "blockedCountByLanguage": {
            language: sum(reason_counts_by_language[language].values()) for language in sorted(enabled)
        },
        "blockedReasonCounts": {key: value for key, value in sorted(reason_counts.items()) if value},
        "blockedReasonCountsByLanguage": {
            language: {key: value for key, value in sorted(reason_counts_by_language[language].items()) if value}
            for language in sorted(enabled)
        },
        "topDuplicateGroups": duplicate_groups[:20],
        "topMissingSetMappings": [
            {"key": key, "count": count}
            for key, count in Counter(
                f"{item['language']}|{item.get('providerSetCode') or item.get('providerSetId') or 'unknown'}"
                for item in blocked
                if item["reason"] in {"missing_set_mapping", "invalid_or_unknown_set"}
            ).most_common(20)
        ],
        "topMissingCollectorNumbers": [
            {"key": key, "count": count}
            for key, count in Counter(
                f"{item['language']}|{item.get('providerSetCode') or item.get('providerSetId') or 'unknown'}"
                for item in blocked
                if item["reason"] == "missing_collector_number"
            ).most_common(20)
        ],
        "top20GapSets": [
            {"language": key.split("|", 1)[0], "providerSetCode": key.split("|", 1)[1], "count": count}
            for key, count in top_gap_counter.most_common(20)
        ],
        "blockedRecordsSample": sorted(blocked, key=lambda item: (item["language"], item["reason"], str(item.get("providerSetCode")), str(item.get("cardNumber"))))[:500],
        "filesChanged": changed_files,
    }
    if write_reports and not dry_run:
        write_json_if_changed(REPORT_JSON_PATH, report)
        write_text_if_changed(REPORT_MD_PATH, build_markdown_report(report))
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Promote safe provider catalogue cards into app catalogue files.")
    parser.add_argument("--languages", default="en,jp", help="Comma-separated languages to promote.")
    parser.add_argument("--include-zh", action="store_true", help="Allow ZH promotion when explicitly requested.")
    parser.add_argument("--dry-run", action="store_true", help="Report planned promotions without writing app catalogue files.")
    parser.add_argument("--no-report", action="store_true", help="Do not write runtime reports.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    languages = selected_languages(args.languages, include_zh=args.include_zh)
    if "zh" in languages and not args.include_zh:
        languages = [language for language in languages if language != "zh"]
    report = promote(languages, include_zh=args.include_zh, dry_run=args.dry_run, write_reports=not args.no_report)
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
