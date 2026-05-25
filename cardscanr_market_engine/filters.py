from __future__ import annotations

import re
from statistics import median

from .fingerprints import normalize_collector_number, normalize_name, normalize_text
from .models import EvaluatedComp, MarketPriceKey, SoldComp

REJECTION_PATTERNS: dict[str, tuple[str, ...]] = {
    "lot_or_bundle": (" lot ", " bundle ", " x2 ", " x3 ", " playset ", " collection "),
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
    totals = [item.comp.total_price for item in included]
    median_total = median(totals)
    if median_total <= 0:
        return evaluated
    updated: list[EvaluatedComp] = []
    for item in evaluated:
        if not item.included_in_estimate:
            updated.append(item)
            continue
        total = item.comp.total_price
        if total > median_total * 1.8 or total < median_total * 0.55:
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


def filter_comps(price_key: MarketPriceKey, comps: list[SoldComp]) -> list[EvaluatedComp]:
    evaluated: list[EvaluatedComp] = []
    for comp in comps:
        rejection_reason = _reject_reason(price_key, comp)
        evaluated.append(
            EvaluatedComp(
                comp=comp,
                included_in_estimate=rejection_reason is None,
                rejection_reason=rejection_reason,
                match_score=score_comp(price_key, comp),
            )
        )
    return _apply_outlier_rejections(evaluated)
