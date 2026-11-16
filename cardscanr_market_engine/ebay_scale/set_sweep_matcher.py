from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..filters import _reject_reason, _set_identity_match_info
from ..fingerprints import normalize_collector_number, normalize_name, normalize_text
from ..models import MarketPriceKey, SoldComp
from .sold_row_html import SoldResultsRow


@dataclass(frozen=True)
class SetSweepMatch:
    catalogue_card_id: str
    card_name: str
    collector_number: str
    confidence: str  # "high" | "ambiguous"
    rejection_reason: str | None = None


@dataclass(frozen=True)
class SetSweepCatalogueCard:
    catalogue_card_id: str
    card_name: str
    normalized_card_name: str
    set_name: str
    set_code: str
    collector_number: str
    language: str = "en"
    variant: str = "raw"
    condition: str = "raw"
    rarity: str = ""


def _price_key_for_card(card: SetSweepCatalogueCard) -> MarketPriceKey:
    return MarketPriceKey(
        id=f"scale-{card.catalogue_card_id}",
        game="pokemon",
        card_name=card.card_name,
        normalized_card_name=card.normalized_card_name,
        set_name=card.set_name,
        set_code=card.set_code,
        collector_number=card.collector_number,
        language=card.language,
        variant=card.variant,
        condition=card.condition,
        market_country="AU",
        currency="AUD",
        fingerprint=f"scale|{card.catalogue_card_id}",
        raw={"catalogue_card_id": card.catalogue_card_id},
    )


def _sold_comp_from_row(row: SoldResultsRow, *, index: int) -> SoldComp:
    price = row.sold_price or 0.0
    shipping = row.postage or 0.0
    return SoldComp(
        source_listing_id=f"sweep-fixture-{index}",
        title=row.title,
        sold_price=price,
        shipping_price=shipping,
        total_price=price + shipping,
        currency="AUD",
        sold_date=None,
        listing_url=row.listing_url,
        condition_text="",
        raw_metadata={"bestOffer": row.best_offer, "soldDateText": row.sold_date},
    )


def match_sold_row_to_catalogue(
    row: SoldResultsRow,
    *,
    catalogue: list[SetSweepCatalogueCard],
    index: int = 0,
) -> SetSweepMatch | None:
    """Match one sold title to at most one catalogue card; drop ambiguous rows."""
    normalized_title = normalize_text(row.title)
    candidates: list[tuple[SetSweepCatalogueCard, str]] = []
    for card in catalogue:
        price_key = _price_key_for_card(card)
        reject = _reject_reason(price_key, _sold_comp_from_row(row, index=index))
        if reject:
            continue
        name_ok = normalize_name(card.normalized_card_name).replace("_", " ") in normalized_title or (
            normalize_name(card.card_name).replace("_", " ") in normalized_title
        )
        number = normalize_collector_number(card.collector_number)
        compact_title = normalized_title.replace(" ", "")
        number_ok = bool(number) and number.replace("/", "") in compact_title.replace("/", "")
        set_info = _set_identity_match_info(price_key, normalized_title)
        set_ok = bool(set_info.get("matches"))
        if name_ok and number_ok and set_ok:
            candidates.append((card, "high"))
        elif name_ok and number_ok:
            candidates.append((card, "ambiguous"))

    if not candidates:
        return None
    if len({c[0].catalogue_card_id for c in candidates}) > 1:
        return None
    card, confidence = candidates[0]
    return SetSweepMatch(
        catalogue_card_id=card.catalogue_card_id,
        card_name=card.card_name,
        collector_number=card.collector_number,
        confidence=confidence if confidence == "high" else "ambiguous",
    )


def classify_unmatched_rejection(
    row: SoldResultsRow,
    *,
    sample_card: SetSweepCatalogueCard,
    index: int = 0,
) -> str:
    """Map a row to a filter rejection reason for sweep stats."""
    price_key = _price_key_for_card(sample_card)
    reject = _reject_reason(price_key, _sold_comp_from_row(row, index=index))
    return reject or "unmatched"