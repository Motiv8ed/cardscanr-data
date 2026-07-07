from __future__ import annotations

import hashlib
import re
from typing import Any

from .models import CardImageIdentity

_FRACTION_PATTERN = re.compile(r"^(\d+)\s*/\s*(\d+)$")


def normalize_local_card_number(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.isdigit():
        return text.lstrip("0") or "0"
    return text


def parse_collector_number(
    collector_number: str,
    *,
    set_total: int | None = None,
    printed_total: int | None = None,
) -> tuple[str, str, int | None]:
    raw = str(collector_number or "").strip()
    if not raw:
        return "", "", set_total
    match = _FRACTION_PATTERN.match(raw)
    if match:
        local = normalize_local_card_number(match.group(1))
        try:
            total = int(match.group(2))
        except ValueError:
            total = set_total
        return raw, local, total
    return raw, normalize_local_card_number(raw), set_total or printed_total


def provider_ids_from_card(card: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    provider_ids = card.get("providerIds")
    if isinstance(provider_ids, dict):
        merged.update(provider_ids)
    external_ids = card.get("externalIds")
    if isinstance(external_ids, dict):
        merged["pokemonTcgApiId"] = external_ids.get("pokemonTcgApiId")
        merged["tcgdexCardId"] = external_ids.get("tcgdexCardId")
        merged["pokewallet"] = merged.get("pokewallet") or external_ids.get("pokewallet")
    return merged


def promotion_metadata(card: dict[str, Any]) -> dict[str, Any]:
    value = card.get("promotionMetadata")
    return value if isinstance(value, dict) else {}


def infer_serie_id_from_tcgdex_url(url: str | None) -> str | None:
    if not url or "assets.tcgdex.net" not in url:
        return None
    parts = [part for part in url.split("/") if part]
    try:
        net_index = next(index for index, part in enumerate(parts) if part.endswith("tcgdex.net"))
    except StopIteration:
        return None
    # .../tcgdex.net/{language}/{serie}/{setId}/...
    if len(parts) <= net_index + 2:
        return None
    return parts[net_index + 2]


def identity_from_catalogue_card(
    card: dict[str, Any],
    *,
    set_meta: dict[str, Any] | None = None,
    serie_id: str | None = None,
    attach_source_card: bool = True,
) -> CardImageIdentity:
    set_meta = set_meta or {}
    set_id = str(card.get("setId") or "").strip()
    collector_number = str(card.get("collectorNumber") or "").strip()
    printed_total = _optional_int(set_meta.get("printedTotal"))
    set_total = _optional_int(set_meta.get("total"))
    printed_card_number, local_card_number, parsed_total = parse_collector_number(
        collector_number,
        set_total=set_total,
        printed_total=printed_total,
    )
    promo = promotion_metadata(card)
    provider_set_id = promo.get("providerSetId")
    if provider_set_id is not None:
        provider_set_id = str(provider_set_id)
    set_code = promo.get("providerSetCode")
    if set_code is not None:
        set_code = str(set_code)
    else:
        set_code = set_id or None
    resolved_serie_id = serie_id or infer_serie_id_from_tcgdex_url(
        _optional_str(card.get("imageLarge") or card.get("imageUrlLarge"))
    )
    return CardImageIdentity(
        canonical_base_id=str(card.get("canonicalBaseId") or "").strip(),
        game=str(card.get("game") or "pokemon").strip().lower(),
        language=str(card.get("language") or "").strip().lower(),
        set_id=set_id,
        set_code=set_code,
        collector_number=collector_number,
        printed_card_number=printed_card_number,
        local_card_number=local_card_number,
        set_total=parsed_total or set_total,
        printed_total=printed_total,
        provider_set_id=provider_set_id,
        provider_ids=provider_ids_from_card(card),
        image_source=str(card.get("imageSource") or card.get("providerImageSource") or "") or None,
        catalogue_image_small=_optional_str(card.get("imageSmall") or card.get("imageUrlSmall")),
        catalogue_image_large=_optional_str(card.get("imageLarge") or card.get("imageUrlLarge")),
        serie_id=resolved_serie_id,
        source_card=card if attach_source_card else None,
    )


def identity_match_key(identity: CardImageIdentity) -> tuple[Any, ...]:
    return (
        identity.language,
        identity.provider_set_id or identity.set_id,
        identity.set_code or identity.set_id,
        identity.printed_card_number,
        identity.local_card_number,
        identity.set_total,
        identity.printed_total,
    )


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None
