from __future__ import annotations

import re
from statistics import median

from .fingerprints import normalize_collector_number, normalize_market_variant, normalize_name, normalize_text
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
    "proxy_or_custom": (" proxy ", " custom ", " fan art ", " fanart ", " alter ", " replica "),
    "digital": (" digital ", " online code ", " ptcgo ", " code card ", " redeem ", " download "),
    "sealed_product": (
        " booster ",
        " booster box ",
        " elite trainer box ",
        " etb ",
        " blister ",
        " booster pack ",
        " sealed ",
        " pack ",
        " tin ",
    ),
    "oversized": (" jumbo ", " oversized ", " over sized ", " giant card "),
}
GRADED_TERMS = (" psa ", " bgs ", " cgc ", " sgc ", " graded ", " slab ")
REVERSE_HOLO_RE = re.compile(
    r"\b(?:reverse[\s-]+holo|rev[\s-]+holo|holofoil[\s-]+reverse|reverse[\s-]+foil|rh)\b",
    flags=re.IGNORECASE,
)
NON_HOLO_RE = re.compile(r"\b(?:non[\s-]+holo|regular|normal)\b", flags=re.IGNORECASE)
HOLO_RE = re.compile(r"\b(?:holo|holographic|holofoil)\b", flags=re.IGNORECASE)


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


def _card_name_matches(price_key: MarketPriceKey, normalized_title: str) -> bool:
    requested = normalize_name(price_key.normalized_card_name or price_key.card_name).replace("_", " ")
    return not requested or requested in normalized_title


def _set_code_conflicts(price_key: MarketPriceKey, normalized_title: str) -> bool:
    requested = normalize_text(price_key.set_code or "")
    if not requested:
        return False
    detected = set(re.findall(r"\b(?:sv|swsh|sm|xy|bw|base)\s*0?\d+\b", normalized_title, flags=re.IGNORECASE))
    normalized_detected = {normalize_text(value).replace(" ", "") for value in detected}
    return bool(normalized_detected and requested.replace(" ", "") not in normalized_detected)


def _has_many_card_numbers(normalized_title: str) -> bool:
    detected = {
        normalize_collector_number(match)
        for match in re.findall(r"(?:#\s*)?([A-Za-z]*\d+[A-Za-z]*(?:/\d+)?)", normalized_title, flags=re.IGNORECASE)
    }
    detected.discard("")
    return len(detected) >= 4


def detect_listing_variant(title: str) -> str:
    normalized_title = normalize_text(title)
    if REVERSE_HOLO_RE.search(normalized_title):
        return "reverse_holo"
    if NON_HOLO_RE.search(normalized_title):
        return "non_holo"
    if HOLO_RE.search(normalized_title):
        return "holo"
    return "non_holo"


def _variant_reject_reason(price_key: MarketPriceKey, comp: SoldComp) -> str | None:
    requested = normalize_market_variant(price_key.variant)
    detected = detect_listing_variant(comp.title)
    if requested == "raw":
        return None
    if requested == "non_holo":
        if detected == "reverse_holo":
            return "wrong_variant_reverse_holo"
        if detected == "holo":
            return "wrong_variant_holo"
        return None
    if requested == "reverse_holo":
        return None if detected == "reverse_holo" else "weak_variant_match"
    if requested == "holo":
        if detected == "reverse_holo":
            return "wrong_variant_reverse_holo"
        return None if detected == "holo" else "weak_variant_match"
    return None


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
    requested_variant = normalize_market_variant(price_key.variant)
    if requested_variant == "raw" and " raw " in normalized_title:
        score += 0.1
    elif requested_variant != "raw" and detect_listing_variant(comp.title) == requested_variant:
        score += 0.1
    return _bounded_score(score)


def _reject_reason(price_key: MarketPriceKey, comp: SoldComp) -> str | None:
    normalized_title = f" {normalize_text(comp.title)} "
    if comp.currency.upper() != price_key.currency.upper():
        return "currency_mismatch"
    variant_rejection = _variant_reject_reason(price_key, comp)
    if variant_rejection:
        return variant_rejection
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
    if _contains_any(normalized_title, REJECTION_PATTERNS["oversized"]):
        return "oversized_or_jumbo"
    if price_key.variant != "graded" and (
        _contains_any(normalized_title, GRADED_TERMS) or _contains_any(f" {normalize_text(comp.condition_text)} ", GRADED_TERMS)
    ):
        return "graded_for_raw_request"
    if price_key.variant not in {"sealed", "product"} and _contains_any(normalized_title, REJECTION_PATTERNS["sealed_product"]):
        return "sealed_product_for_single_card_request"
    if not _collector_number_matches(price_key, normalized_title):
        return "wrong_collector_number"
    if _set_code_conflicts(price_key, normalized_title):
        return "wrong_set"
    if not _card_name_matches(price_key, normalized_title):
        return "wrong_card_name"
    if _has_many_card_numbers(normalized_title):
        return "multiple_card_numbers"
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
        normalized_title = f" {normalize_text(comp.title)} "
        requested_variant = normalize_market_variant(price_key.variant)
        detected_variant = detect_listing_variant(comp.title)
        variant_match = requested_variant == "raw" or requested_variant == detected_variant
        metadata.setdefault("requested_variant", requested_variant)
        metadata.setdefault("detected_variant", detected_variant)
        metadata.setdefault("variant_match", variant_match)
        metadata.setdefault("variant_warning", rejection_reason if rejection_reason and rejection_reason.startswith(("wrong_variant_", "weak_variant_")) else None)
        metadata.setdefault("collector_number_match", _collector_number_matches(price_key, normalized_title))
        metadata.setdefault("set_name_match", not _set_code_conflicts(price_key, normalized_title))
        metadata.setdefault("card_name_match", _card_name_matches(price_key, normalized_title))
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
