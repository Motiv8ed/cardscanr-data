from __future__ import annotations

import hashlib
import json
import random
import requests
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .catalogue import DEFAULT_CATALOGUE_ROOT, load_json, load_set_index
from .identity import identity_from_catalogue_card
from .matching import classify_sample_bucket, resolve_provider_with_trace
from .processing import pokewallet_request_headers
from .models import CardImageIdentity
from .tcgdex_serie_cache import load_tcgdex_set_serie_map

SAMPLE_MANIFEST_VERSION = "1.0.0"
SAMPLE_SEED = 20260708
SAMPLE_BUCKETS: dict[str, int] = {
    "en_tcgdex": 25,
    "en_pokemon_tcg_api": 15,
    "en_pokewallet": 10,
    "jp_tcgdex": 20,
    "jp_pokewallet": 25,
}
EDGE_CASE_COUNT = 5
RUNTIME_DIR = Path("reports/runtime")
EDGE_CASE_FIXTURES: list[tuple[str, str]] = [
    ("leading_zero_collector", "pokemon|jp|SV10|001|クヌギダマ"),
    ("fraction_collector_number", "pokemon|jp|23598|001/073|tropius_001_073"),
    ("promotion_provider_set_id", "pokemon|en|1430|1/130|dialga"),
    ("duplicate_name_charizard", "pokemon|en|base1|4|charizard"),
    ("jp_pokewallet_only", "pokemon|jp|23598|002/073|foongus"),
]
SUPPLEMENTARY_EDGE_FIXTURES: list[tuple[str, str]] = [
    ("duplicate_name_charizard", "pokemon|en|xy12|11|charizard"),
    ("alpha_collector_number", "pokemon|en|swsh45sv|SV001|rowlet"),
    ("fraction_collector_number", "pokemon|en|1430|3/130|electivire"),
    ("promotion_provider_set_id", "pokemon|en|1430|2/130|dusknoir"),
    ("jp_pokewallet_only", "pokemon|jp|23598|003/073|amoonguss"),
]
TARGET_MANIFEST_COUNT = sum(SAMPLE_BUCKETS.values()) + len(SUPPLEMENTARY_EDGE_FIXTURES)


@dataclass(frozen=True)
class SampleManifestEntry:
    bucket: str
    edgeCaseTag: str | None
    canonicalBaseId: str
    language: str
    setId: str
    collectorNumber: str
    imageSource: str | None
    provider: str | None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def manifest_path() -> Path:
    return RUNTIME_DIR / "image_pipeline_stage2_sample_100.json"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fast_bucket(identity: CardImageIdentity, card: dict[str, Any], tcgdx_sets_en: set[str], tcgdx_sets_jp: set[str]) -> str | None:
    source = str(card.get("imageSource") or "")
    language = identity.language
    if language == "en" and source == "pokewallet":
        return "en_pokewallet"
    if language == "jp" and source == "pokewallet":
        return "jp_pokewallet"
    if language == "jp" and source == "tcgdex":
        return "jp_tcgdex"
    if language == "en" and source == "pokemon_tcg_api":
        return "en_pokemon_tcg_api"
    if language == "en" and source == "tcgdex":
        return "en_tcgdex"
    return None


def _edge_case_tag(identity: CardImageIdentity, card: dict[str, Any]) -> str | None:
    collector = identity.collector_number
    name = str(card.get("normalizedName") or "")
    promo = card.get("promotionMetadata") if isinstance(card.get("promotionMetadata"), dict) else {}
    if collector.startswith("0") and collector.replace("/", "").isdigit():
        return "leading_zero_collector"
    if "/" in collector:
        return "fraction_collector_number"
    if any(ch.isalpha() for ch in collector):
        return "alpha_collector_number"
    if promo.get("providerSetId"):
        return "promotion_provider_set_id"
    if name == "charizard":
        return "duplicate_name_charizard"
    if card.get("imageSource") == "pokewallet" and identity.language == "jp":
        return "jp_pokewallet_only"
    return None


def _iter_all_cards(catalogue_root: Path = DEFAULT_CATALOGUE_ROOT) -> list[tuple[CardImageIdentity, dict[str, Any]]]:
    cards: list[tuple[CardImageIdentity, dict[str, Any]]] = []
    for language in ("en", "jp"):
        set_index = load_set_index(catalogue_root, game="pokemon", language=language)
        cards_dir = catalogue_root / "catalog" / "pokemon" / language / "cards"
        if not cards_dir.exists():
            continue
        for path in sorted(cards_dir.glob("*.json"), key=lambda item: item.name.lower()):
            payload = load_json(path)
            if not isinstance(payload, dict):
                continue
            set_meta = set_index.get(path.stem, {"id": path.stem})
            for card in payload.get("cards") or []:
                if not isinstance(card, dict):
                    continue
                identity = identity_from_catalogue_card(card, set_meta=set_meta)
                if identity.canonical_base_id:
                    cards.append((identity, card))
    return cards


def source_urls_reachable(identity: CardImageIdentity, card: dict[str, Any], *, session: requests.Session) -> bool:
    resolution = resolve_provider_with_trace(identity, source_card=card)
    if resolution.ambiguous or resolution.candidate is None:
        return False
    candidate = resolution.candidate
    for url in (candidate.source_url_display, candidate.source_url_thumb):
        if not url:
            continue
        try:
            response = session.get(
                url,
                timeout=20,
                stream=True,
                headers=pokewallet_request_headers(url),
            )
            if response.status_code == 200 and int(response.headers.get("Content-Length") or 1) > 0:
                response.close()
                return True
            response.close()
        except requests.RequestException:
            continue
    return False


def build_stratified_sample(
    *,
    catalogue_root: Path = DEFAULT_CATALOGUE_ROOT,
    seed: int = SAMPLE_SEED,
) -> dict[str, Any]:
    tcgdx_sets_en = set(load_tcgdex_set_serie_map("en").keys())
    tcgdx_sets_jp = set(load_tcgdex_set_serie_map("jp").keys())
    all_cards = _iter_all_cards(catalogue_root)
    cards_by_id = {identity.canonical_base_id: (identity, card) for identity, card in all_cards}
    grouped: dict[str, list[tuple[CardImageIdentity, dict[str, Any]]]] = {key: [] for key in SAMPLE_BUCKETS}

    for identity, card in all_cards:
        bucket = _fast_bucket(identity, card, tcgdx_sets_en, tcgdx_sets_jp)
        if bucket in grouped:
            grouped[bucket].append((identity, card))

    rng = random.Random(seed)
    selected: list[SampleManifestEntry] = []
    selected_ids: set[str] = set()
    bucket_counts = {key: 0 for key in SAMPLE_BUCKETS}
    session = requests.Session()

    for edge_tag, canonical_id in EDGE_CASE_FIXTURES:
        item = cards_by_id.get(canonical_id)
        if item is None:
            raise RuntimeError(f"Edge fixture not found: {canonical_id}")
        identity, card = item
        resolution = resolve_provider_with_trace(identity, source_card=card)
        if resolution.ambiguous or resolution.candidate is None:
            raise RuntimeError(f"Edge fixture not matchable: {canonical_id}")
        bucket = classify_sample_bucket(identity, source_card=card, resolution=resolution)
        if bucket not in SAMPLE_BUCKETS:
            raise RuntimeError(f"Edge fixture bucket missing for {canonical_id}: {bucket}")
        if not source_urls_reachable(identity, card, session=session):
            raise RuntimeError(f"Edge fixture source URL unreachable: {canonical_id}")
        selected.append(
            SampleManifestEntry(
                bucket=bucket,
                edgeCaseTag=edge_tag,
                canonicalBaseId=identity.canonical_base_id,
                language=identity.language,
                setId=identity.set_id,
                collectorNumber=identity.collector_number,
                imageSource=identity.image_source,
                provider=resolution.candidate.provider,
            )
        )
        selected_ids.add(identity.canonical_base_id)
        bucket_counts[bucket] += 1

    if len(selected) != EDGE_CASE_COUNT:
        raise RuntimeError("Edge fixtures were not fully applied")

    for bucket, count in SAMPLE_BUCKETS.items():
        pool = grouped.get(bucket, [])
        rng.shuffle(pool)
        for identity, card in pool:
            if bucket_counts[bucket] >= count:
                break
            if identity.canonical_base_id in selected_ids:
                continue
            resolution = resolve_provider_with_trace(identity, source_card=card)
            if resolution.ambiguous or resolution.candidate is None:
                continue
            if classify_sample_bucket(identity, source_card=card, resolution=resolution) != bucket:
                continue
            if not source_urls_reachable(identity, card, session=session):
                continue
            selected.append(
                SampleManifestEntry(
                    bucket=bucket,
                    edgeCaseTag=None,
                    canonicalBaseId=identity.canonical_base_id,
                    language=identity.language,
                    setId=identity.set_id,
                    collectorNumber=identity.collector_number,
                    imageSource=identity.image_source,
                    provider=resolution.candidate.provider,
                )
            )
            selected_ids.add(identity.canonical_base_id)
            bucket_counts[bucket] += 1
        if bucket_counts[bucket] < count:
            raise RuntimeError(f"Unable to fill bucket {bucket}: needed {count}, found {bucket_counts[bucket]}")

    if len(selected) != sum(SAMPLE_BUCKETS.values()):
        raise RuntimeError(f"Unexpected manifest size {len(selected)}")

    for edge_tag, canonical_id in SUPPLEMENTARY_EDGE_FIXTURES:
        if canonical_id in selected_ids:
            continue
        item = cards_by_id.get(canonical_id)
        if item is None:
            raise RuntimeError(f"Supplementary edge fixture not found: {canonical_id}")
        identity, card = item
        resolution = resolve_provider_with_trace(identity, source_card=card)
        if resolution.ambiguous or resolution.candidate is None:
            raise RuntimeError(f"Supplementary edge fixture not matchable: {canonical_id}")
        bucket = classify_sample_bucket(identity, source_card=card, resolution=resolution) or "supplementary_edge"
        selected.append(
            SampleManifestEntry(
                bucket=bucket,
                edgeCaseTag=edge_tag,
                canonicalBaseId=identity.canonical_base_id,
                language=identity.language,
                setId=identity.set_id,
                collectorNumber=identity.collector_number,
                imageSource=identity.image_source,
                provider=resolution.candidate.provider,
            )
        )
        selected_ids.add(identity.canonical_base_id)

    if len(selected) != TARGET_MANIFEST_COUNT:
        raise RuntimeError(f"Unexpected manifest size {len(selected)}; expected {TARGET_MANIFEST_COUNT}")

    manifest = {
        "schemaVersion": SAMPLE_MANIFEST_VERSION,
        "generatedAtUtc": utc_now_iso(),
        "seed": seed,
        "cardCount": len(selected),
        "buckets": SAMPLE_BUCKETS,
        "edgeCaseCount": EDGE_CASE_COUNT,
        "entries": [asdict(entry) for entry in selected],
    }
    return manifest


def write_sample_manifest(manifest: dict[str, Any], *, path: Path | None = None) -> Path:
    target = path or manifest_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest["sha256"] = sha256_file(target)
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def load_sample_manifest(path: Path | None = None) -> dict[str, Any]:
    target = path or manifest_path()
    return json.loads(target.read_text(encoding="utf-8"))


def identities_for_manifest(
    manifest: dict[str, Any],
    *,
    catalogue_root: Path = DEFAULT_CATALOGUE_ROOT,
) -> list[CardImageIdentity]:
    wanted = {entry["canonicalBaseId"] for entry in manifest.get("entries") or []}
    found: dict[str, CardImageIdentity] = {}
    for language in ("en", "jp"):
        set_index = load_set_index(catalogue_root, game="pokemon", language=language)
        cards_dir = catalogue_root / "catalog" / "pokemon" / language / "cards"
        if not cards_dir.exists():
            continue
        for path in cards_dir.glob("*.json"):
            payload = load_json(path)
            if not isinstance(payload, dict):
                continue
            set_meta = set_index.get(path.stem, {"id": path.stem})
            for card in payload.get("cards") or []:
                if not isinstance(card, dict):
                    continue
                identity = identity_from_catalogue_card(card, set_meta=set_meta)
                if identity.canonical_base_id in wanted:
                    found[identity.canonical_base_id] = identity
    ordered: list[CardImageIdentity] = []
    for entry in manifest.get("entries") or []:
        card_id = entry["canonicalBaseId"]
        if card_id not in found:
            raise KeyError(f"Manifest card not found in catalogue: {card_id}")
        ordered.append(found[card_id])
    return ordered
