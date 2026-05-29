from __future__ import annotations

import re
from statistics import median

from .fingerprints import normalize_collector_number, normalize_name, normalize_text
from .models import EvaluatedComp, MarketPriceKey, SoldComp

REJECTION_PATTERNS: dict[str, tuple[str, ...]] = {
    "lot_or_bundle": (" lot ", " bundle ", " x2 ", " x3 ", " playset ", " collection "),
    "variation_or_pick": (
        " choose your card ",
        " you pick ",
        " pick your card ",
        " singles common ",
        " holo/reverse/ex ",
        " reverse/holo/ex ",
    ),
    "proxy_or_custom": (" proxy ", " custom ", " fan art ", " fanart ", " alter "),
    "digital": (" digital ", " online code ", " ptcgo ", " code card "),
    "sealed_product": (" booster box ", " elite trainer box ", " etb ", " blister ", " booster pack ", " tin "),
}
GRADED_TERMS = (" psa ", " bgs ", " cgc ", " sgc ", " graded ", " slab ")


def _bounded_score(value: float) -> float:
    return max(0.0, min(round(value, 4), 1.0))


def _contains_any(haystack: str, needles: tuple[str, ...]) -> bool:
    return any(needle in haystack for needle in needles)


def _collector_number_matches(price_key: MarketPriceKey, normalized_title: str) -> bool:
    requested = normalize_collector_number(price_key.collector_number)
    if not requested:
        return True
    requested_lower = requested.lower()
    if requested_lower in normalized_title:
        return True
    detected = {
        normalize_collector_number(match)
        for match in re.findall(r"(?:#\s*)?([A-Za-z]*\d+[A-Za-z]*)(?:/\d+)?", normalized_title, flags=re.IGNORECASE)
    }
    detected.discard("")
    if not detected:
        return True
    return requested in detected


def score_comp(price_key: MarketPriceKey, comp: SoldComp) -> float:
    normalized_title = f" {normalize_text(comp.title)} "
    score = 0.35
    if normalize_name(price_key.normalized_card_name or price_key.card_name).replace("_", " ") in normalized_title:
        score += 0.35
    if price_key.set_code and normalize_text(price_key.set_code) in normalized_title:
        score += 0.1
    elif normalize_name(price_key.set_name).replace("_", " ") in normalized_title:
        score += 0.05
    if normalize_collector_number(price_key.collector_number).lower() in normalized_title:
        score += 0.1
    if price_key.variant == "raw" and " raw " in normalized_title:
        score += 0.1
    return _bounded_score(score)


def _reject_reason(price_key: MarketPriceKey, comp: SoldComp) -> str | None:
    normalized_title = f" {normalize_text(comp.title)} "
    if comp.currency.upper() != price_key.currency.upper():
        return "currency_mismatch"
    if comp.raw_metadata.get("priceRangeListing") or comp.raw_metadata.get("price_range_listing"):
        return "price_range_or_variation_listing"
    price_text = f" {normalize_text(comp.raw_metadata.get('priceText', ''))} "
    if re.search(r"\d[\d,]*(?:\.\d{1,2})?\s+(?:to|-)\s+\D*\d", price_text, flags=re.IGNORECASE):
        return "price_range_or_variation_listing"
    if _contains_any(normalized_title, REJECTION_PATTERNS["variation_or_pick"]):
        return "price_range_or_variation_listing"
    if _contains_any(normalized_title, REJECTION_PATTERNS["lot_or_bundle"]):
        return "lot_or_bundle"
    if _contains_any(normalized_title, REJECTION_PATTERNS["proxy_or_custom"]):
        return "proxy_or_custom"
    if _contains_any(normalized_title, REJECTION_PATTERNS["digital"]):
        return "digital"
    if price_key.variant != "graded" and (
        _contains_any(normalized_title, GRADED_TERMS) or _contains_any(f" {normalize_text(comp.condition_text)} ", GRADED_TERMS)
    ):
        return "graded_for_raw_request"
    if price_key.variant not in {"sealed", "product"} and _contains_any(normalized_title, REJECTION_PATTERNS["sealed_product"]):
        return "sealed_product_for_single_card_request"
    if not _collector_number_matches(price_key, normalized_title):
        return "wrong_collector_number"
    return None


def _apply_outlier_rejections(evaluated: list[EvaluatedComp]) -> list[EvaluatedComp]:
    included = [item for item in evaluated if item.included_in_estimate]
    if len(included) < 4:
        return evaluated
    item_prices = [item.comp.sold_price for item in included]
    median_item = median(item_prices)
    if median_item <= 0:
        return evaluated
    updated: list[EvaluatedComp] = []
    for item in evaluated:
        if not item.included_in_estimate:
            updated.append(item)
            continue
        item_price = item.comp.sold_price
        item_outlier = item_price > median_item * 1.8 or item_price < median_item * 0.55
        if item_price < median_item * 0.55 and _exact_card_match_for_evaluated(item):
            extreme_low_item_outlier = item_price < median_item * 0.15
            if not extreme_low_item_outlier:
                updated.append(item)
                continue
        if item_outlier:
            updated.append(
                EvaluatedComp(
                    comp=item.comp,
                    included_in_estimate=False,
                    rejection_reason="obvious_outlier",
                    match_score=item.match_score,
                )
            )
            continue
        updated.append(item)
    return updated


def _exact_card_match_for_evaluated(item: EvaluatedComp) -> bool:
    # Exact-match protection is intentionally conservative here: it prevents free-shipping
    # exact comps from being rejected only because landed prices are higher elsewhere.
    raw = item.comp.raw_metadata
    title_text = normalize_text(item.comp.title)
    requested_card = normalize_text(str(raw.get("requestedCardName", "")))
    requested_number = normalize_collector_number(str(raw.get("requestedCollectorNumber", ""))).lower()
    if requested_card and requested_number:
        return item.match_score >= 0.85 and requested_card in title_text and requested_number in title_text
    return item.match_score >= 0.85


def filter_comps(price_key: MarketPriceKey, comps: list[SoldComp]) -> list[EvaluatedComp]:
    evaluated: list[EvaluatedComp] = []
    for comp in comps:
        rejection_reason = _reject_reason(price_key, comp)
        metadata = dict(comp.raw_metadata)
        metadata.setdefault("requestedCardName", normalize_text(price_key.card_name))
        metadata.setdefault("requestedCollectorNumber", normalize_collector_number(price_key.collector_number))
        comp_for_eval = SoldComp(
            source_listing_id=comp.source_listing_id,
            title=comp.title,
            sold_price=comp.sold_price,
            shipping_price=comp.shipping_price,
            total_price=comp.total_price,
            currency=comp.currency,
            sold_date=comp.sold_date,
            listing_url=comp.listing_url,
            condition_text=comp.condition_text,
            raw_metadata=metadata,
        )
        evaluated.append(
            EvaluatedComp(
                comp=comp_for_eval,
                included_in_estimate=rejection_reason is None,
                rejection_reason=rejection_reason,
                match_score=score_comp(price_key, comp_for_eval),
            )
        )
    return _apply_outlier_rejections(evaluated)
