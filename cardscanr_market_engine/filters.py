from __future__ import annotations

import re
from statistics import median
from typing import Any

from .fingerprints import normalize_collector_number, normalize_market_variant, normalize_name, normalize_text
from .models import EvaluatedComp, MarketPriceKey, SoldComp

REJECTION_PATTERNS: dict[str, tuple[str, ...]] = {
    "lot_or_bundle": (
        " lot ",
        " lot of ",
        " bundle ",
        " bulk ",
        " playset ",
        " collection ",
        " card lot ",
        " holo lot ",
        " mixed lot ",
    ),
    "variation_or_pick": (
        " choose your card ",
        " choose your own ",
        " you pick ",
        " you-pick ",
        " pick your card ",
        " pick your own ",
        " select your card ",
        " complete your set ",
        " card singles pick ",
        " all pokemon pick ",
        " variation listing ",
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
MIRROR_MASTERBALL_HOLO_RE = re.compile(r"\b(?:mirror[\s-]+holo|master\s*ball|masterball)\b", flags=re.IGNORECASE)
MULTI_CARD_COUNT_RE = re.compile(
    r"\b(?:x\s*(?:10|[2-9][0-9]|[1-9][0-9]{2,})|(?:10|[2-9][0-9]|[1-9][0-9]{2,})\s*(?:x|pcs?|cards?))\b",
    flags=re.IGNORECASE,
)
JAPANESE_TEXT_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]")
KOREAN_TEXT_RE = re.compile(r"[\uac00-\ud7af]")
RAW_ALIAS_FIELDS = (
    "english_card_name",
    "englishCardName",
    "english_name",
    "englishName",
    "name_en",
    "nameEn",
    "en_name",
    "enName",
    "canonical_english_name",
    "canonicalEnglishName",
    "canonical_card_name",
    "canonicalCardName",
)
MIN_INCLUDED_SCORE = 0.65
SNIPPET_IDENTITY_BOUNDARY_RE = re.compile(
    r"\s+(?:"
    r"opens\s+in\s+a\s+new\s+window\s+or\s+tab|"
    r"pre-owned|brand\s+new|"
    r"(?:au|us|gbp|cad|\$|£)\s*\$?\d|"
    r"buy\s+it\s+now|best\s+offer|"
    r"\+\s*(?:au|us|gbp|cad|\$|£)?\s*\$?\d|"
    r"delivery|shipping|postage|free\s+returns|"
    r"view\s+similar\s+active\s+items|sell\s+one\s+like\s+this"
    r")\b",
    flags=re.IGNORECASE,
)
SOLD_PREFIX_RE = re.compile(
    r"^sold\s+(?:[0-9]{1,2}\s+[A-Za-z]{3,9}\s+[0-9]{4}|[A-Za-z]{3,9}\s+[0-9]{1,2},\s+[0-9]{4})\s*",
    flags=re.IGNORECASE,
)


def _bounded_score(value: float) -> float:
    return max(0.0, min(round(value, 4), 1.0))


def _contains_any(haystack: str, needles: tuple[str, ...]) -> bool:
    return any(needle in haystack for needle in needles)


def _clean(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def _metadata_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return " ".join(_metadata_text(item) for pair in value.items() for item in pair)
    if isinstance(value, (list, tuple, set)):
        return " ".join(_metadata_text(item) for item in value)
    return _clean(value)


def _evidence_text(comp: SoldComp) -> str:
    raw = comp.raw_metadata
    parts = [
        comp.title,
        comp.condition_text,
        raw.get("rawTextSnippet"),
        raw.get("conditionText"),
        raw.get("itemSpecificsText"),
        raw.get("item_specifics_text"),
        raw.get("itemSpecifics"),
        raw.get("item_specifics"),
        raw.get("specifics"),
    ]
    return " ".join(_clean(part) for part in parts if _clean(part))


def _padded_normalized_evidence(comp: SoldComp) -> str:
    return f" {normalize_text(_evidence_text(comp))} "


def _extract_identity_prefix_from_snippet(value: object) -> str:
    text = SOLD_PREFIX_RE.sub("", _clean(value))
    if not text:
        return ""
    match = SNIPPET_IDENTITY_BOUNDARY_RE.search(text)
    if match:
        text = text[: match.start()]
    return _clean(text)


def _identity_text(comp: SoldComp) -> str:
    raw = comp.raw_metadata
    parts = [
        comp.title,
        raw.get("identityTitle"),
        raw.get("identity_title"),
        raw.get("titleLikeText"),
        raw.get("title_like_text"),
        _extract_identity_prefix_from_snippet(raw.get("rawTextSnippet")),
    ]
    return " ".join(_clean(part) for part in parts if _clean(part))


def _padded_normalized_identity(comp: SoldComp) -> str:
    return f" {normalize_text(_identity_text(comp))} "


def _language_family(value: object) -> str:
    normalized = normalize_text(value).replace("_", "-")
    if normalized in {"jp", "ja", "jpn", "japanese"}:
        return "jp"
    if normalized in {"kr", "ko", "kor", "korean", "korea"}:
        return "kr"
    if normalized in {"en", "eng", "english"}:
        return "en"
    if normalized in {"zh", "cn", "chn", "chinese", "zh-cn", "zh-tw"}:
        return "zh"
    return normalized


def _has_explicit_language(text: str, terms: tuple[str, ...]) -> bool:
    return any(re.search(term, text, flags=re.IGNORECASE) for term in terms)


def _language_reject_reason(price_key: MarketPriceKey, comp: SoldComp) -> str | None:
    requested = _language_family(price_key.language)
    evidence = _evidence_text(comp)
    japanese = _has_explicit_language(
        evidence,
        (
            r"\bjapanese\b",
            r"\bjapan\b",
            r"\bjpn\b",
            r"\bjp\b",
        ),
    ) or bool(JAPANESE_TEXT_RE.search(evidence))
    korean = _has_explicit_language(evidence, (r"\bkorean\b", r"\bkorea\b", r"\bkor\b", r"\bkr\b")) or bool(KOREAN_TEXT_RE.search(evidence))
    chinese = _has_explicit_language(
        evidence,
        (
            r"\bchinese\b",
            r"\bchina\b",
            r"\bchn\b",
            r"\bcn\b",
            r"\bsimplified chinese\b",
            r"\btraditional chinese\b",
            r"\btaiwan\b",
            r"\bhong kong\b",
            r"\bhk\b",
        ),
    )
    english = _has_explicit_language(evidence, (r"\benglish\b", r"\beng\b"))
    if requested == "jp":
        if korean or chinese or english:
            return "wrong_language"
    elif requested == "kr":
        if japanese or chinese or english:
            return "wrong_language"
    elif requested == "en":
        allows_cross_language = bool(price_key.raw.get("allow_cross_language_fallback") or price_key.raw.get("includeEnglishEquivalent"))
        if not allows_cross_language and (japanese or korean or chinese):
            return "wrong_language"
    return None


def _is_lot_or_bundle(normalized_evidence: str) -> bool:
    if _contains_any(normalized_evidence, REJECTION_PATTERNS["lot_or_bundle"]):
        return True
    if re.search(r"\b(?:lot|bulk|bundle)s?\b", normalized_evidence, flags=re.IGNORECASE):
        return True
    if MULTI_CARD_COUNT_RE.search(normalized_evidence) and re.search(r"\b(?:cards?|pokemon|holo|non holo|reverse)\b", normalized_evidence, flags=re.IGNORECASE):
        return True
    return False


def _alias_candidates(raw: dict[str, Any]) -> list[object]:
    candidates: list[object] = []
    for field in RAW_ALIAS_FIELDS:
        if field in raw:
            candidates.append(raw.get(field))
    aliases = raw.get("aliases")
    if isinstance(aliases, dict):
        for field in RAW_ALIAS_FIELDS:
            if field in aliases:
                candidates.append(aliases.get(field))
        for field in ("en", "EN"):
            if aliases.get(field):
                candidates.append(aliases.get(field))
    return candidates


def _canonical_card_name(price_key: MarketPriceKey) -> str:
    for candidate in _alias_candidates(price_key.raw):
        text = _clean(candidate)
        if text:
            return text
    normalized = _clean(price_key.normalized_card_name).replace("_", " ")
    if normalized and normalized != "unknown":
        return normalized
    return _clean(price_key.card_name)


def _canonical_card_names(price_key: MarketPriceKey) -> tuple[str, ...]:
    names: list[str] = []
    canonical = _canonical_card_name(price_key)
    if canonical:
        names.append(canonical)
    original = _clean(price_key.card_name)
    if original:
        names.append(original)
    normalized = _clean(price_key.normalized_card_name).replace("_", " ")
    if normalized and normalized != "unknown":
        names.append(normalized)
    normalized_names = []
    for name in names:
        text = normalize_name(name).replace("_", " ")
        if text and text not in normalized_names:
            normalized_names.append(text)
    return tuple(normalized_names)


def _requested_collector_parts(price_key: MarketPriceKey) -> tuple[str, str, bool]:
    requested = normalize_collector_number(price_key.collector_number)
    short = requested.split("/", 1)[0] if "/" in requested else requested
    return requested, short, bool("/" in requested)


def _collector_reference_variants(price_key: MarketPriceKey) -> set[str]:
    requested, short, has_full_number = _requested_collector_parts(price_key)
    if not requested:
        return set()
    variants = {requested.lower()}
    if has_full_number:
        total = requested.split("/", 1)[1]
        hyphen = f"{short}-{total}"
        variants.add(hyphen.lower())
        set_code = normalize_text(price_key.set_code or "").replace(" ", "")
        if set_code:
            variants.add(f"{hyphen}-{set_code}".lower())
            variants.add(f"{set_code} {short}".lower())
            variants.add(f"{set_code}-{short}".lower())
    return variants


def _collector_key(value: object) -> str:
    text = normalize_collector_number(str(value).replace("-", "/"))
    parts = []
    for part in text.split("/"):
        parts.append(re.sub(r"^0+(\d)", r"\1", part))
    return "/".join(parts)


def _collector_equivalent(left: object, right: object) -> bool:
    left_key = _collector_key(left)
    right_key = _collector_key(right)
    return bool(left_key and right_key and left_key == right_key)


def _detected_collector_numbers(normalized_title: str) -> tuple[set[str], set[str]]:
    full_numbers = {
        normalize_collector_number(match)
        for match in re.findall(r"\b([A-Za-z]*\d+[A-Za-z]*/\d+[A-Za-z]*)\b", normalized_title, flags=re.IGNORECASE)
    }
    hyphen_numbers = {
        normalize_collector_number(match.replace("-", "/"))
        for match in re.findall(r"\b(\d+[A-Za-z]*-\d+[A-Za-z]*)(?:-[A-Za-z0-9-]+)?\b", normalized_title, flags=re.IGNORECASE)
    }
    short_numbers = {
        normalize_collector_number(match)
        for match in re.findall(r"(?:#\s*)?\b([A-Za-z]*\d+[A-Za-z]*)\b", normalized_title, flags=re.IGNORECASE)
    }
    full_numbers |= hyphen_numbers
    full_numbers.discard("")
    short_numbers.discard("")
    return full_numbers, short_numbers


def _collector_number_match_info(price_key: MarketPriceKey, normalized_title: str) -> dict[str, Any]:
    requested, short, has_full_number = _requested_collector_parts(price_key)
    if not requested:
        return {"matches": True, "quality": "not_requested", "requested": "", "detected": []}
    requested_lower = requested.lower()
    if any(variant in normalized_title for variant in _collector_reference_variants(price_key)):
        return {"matches": True, "quality": "full" if has_full_number else "short", "requested": requested, "detected": [requested]}
    full_numbers, short_numbers = _detected_collector_numbers(normalized_title)
    detected = sorted(full_numbers | short_numbers)
    if any(_collector_equivalent(requested, value) for value in full_numbers):
        return {"matches": True, "quality": "full", "requested": requested, "detected": detected}
    if not has_full_number and any(_collector_equivalent(requested, value) for value in short_numbers):
        return {"matches": True, "quality": "short", "requested": requested, "detected": detected}
    if has_full_number and short and any(_collector_equivalent(short, value) for value in short_numbers):
        return {"matches": True, "quality": "short_from_full", "requested": requested, "detected": detected}
    set_code = normalize_text(price_key.set_code or "").replace(" ", "")
    if set_code and short:
        set_short_re = re.compile(rf"\b{re.escape(set_code)}(?:-[a-z0-9]+)?[\s-]+0*{re.escape(short.lstrip('0') or short)}\b", flags=re.IGNORECASE)
        if set_short_re.search(normalized_title):
            return {"matches": True, "quality": "short_from_full", "requested": requested, "detected": detected}
    if not detected:
        return {"matches": False, "quality": "missing", "requested": requested, "detected": []}
    return {"matches": False, "quality": "conflict", "requested": requested, "detected": detected}


def _collector_number_matches(price_key: MarketPriceKey, normalized_title: str) -> bool:
    return bool(_collector_number_match_info(price_key, normalized_title)["matches"])


def _card_name_matches(price_key: MarketPriceKey, normalized_title: str) -> bool:
    names = _canonical_card_names(price_key)
    return not names or any(name in normalized_title for name in names)


def _set_code_conflicts(price_key: MarketPriceKey, normalized_title: str) -> bool:
    requested = normalize_text(price_key.set_code or "")
    if not requested:
        return False
    detected = set(re.findall(r"\b(?:sv|swsh|sm|xy|bw|base)\s*0?\d+\b", normalized_title, flags=re.IGNORECASE))
    normalized_detected = {normalize_text(value).replace(" ", "") for value in detected}
    return bool(normalized_detected and requested.replace(" ", "") not in normalized_detected)


def _set_identity_match_info(price_key: MarketPriceKey, normalized_title: str) -> dict[str, Any]:
    set_name = normalize_name(price_key.set_name).replace("_", " ")
    set_code = normalize_text(price_key.set_code or "").replace(" ", "")
    normalized_compact = normalized_title.replace(" ", "")
    code_match = bool(set_code and set_code in normalized_compact)
    name_match = bool(set_name and set_name in normalized_title)
    conflict = _set_code_conflicts(price_key, normalized_title)
    if code_match:
        quality = "set_code"
    elif name_match:
        quality = "set_name"
    elif conflict:
        quality = "conflict"
    else:
        quality = "missing"
    return {
        "matches": code_match or name_match,
        "quality": quality,
        "conflict": conflict,
        "requested_set_name": set_name,
        "requested_set_code": set_code,
    }


def _collector_number_references(normalized_title: str) -> set[str]:
    references = {
        normalize_collector_number(match)
        for match in re.findall(r"\b([A-Za-z]*\d+[A-Za-z]*/\d+[A-Za-z]*)\b", normalized_title, flags=re.IGNORECASE)
    }
    references.update(
        normalize_collector_number(match.replace("-", "/"))
        for match in re.findall(r"\b(\d+[A-Za-z]*-\d+[A-Za-z]*)(?:-[A-Za-z0-9-]+)?\b", normalized_title, flags=re.IGNORECASE)
    )
    references.discard("")
    return references


def _has_many_card_numbers(price_key: MarketPriceKey, normalized_title: str) -> bool:
    requested, _short, _has_full_number = _requested_collector_parts(price_key)
    detected = _collector_number_references(normalized_title)
    detected = {value for value in detected if not _collector_equivalent(value, requested)}
    return len(detected) >= 1


def detect_listing_variant(title: str) -> str:
    normalized_title = normalize_text(title)
    if REVERSE_HOLO_RE.search(normalized_title):
        return "reverse_holo"
    if NON_HOLO_RE.search(normalized_title):
        return "non_holo"
    if MIRROR_MASTERBALL_HOLO_RE.search(normalized_title):
        return "holo"
    if HOLO_RE.search(normalized_title):
        return "holo"
    return "non_holo"


def _variant_reject_reason(price_key: MarketPriceKey, comp: SoldComp) -> str | None:
    requested = normalize_market_variant(price_key.variant)
    detected = detect_listing_variant(_evidence_text(comp))
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
    normalized_title = _padded_normalized_identity(comp)
    score = 0.0
    if _card_name_matches(price_key, normalized_title):
        score += 0.35
    collector_info = _collector_number_match_info(price_key, normalized_title)
    if collector_info["quality"] == "full":
        score += 0.3
    elif collector_info["quality"] == "short":
        score += 0.3
    elif collector_info["quality"] == "short_from_full":
        score += 0.22
    elif collector_info["quality"] == "not_requested":
        score += 0.08
    set_info = _set_identity_match_info(price_key, normalized_title)
    if set_info["quality"] == "set_code":
        score += 0.15
    elif set_info["quality"] == "set_name":
        score += 0.12
    requested_variant = normalize_market_variant(price_key.variant)
    if requested_variant == "raw" and " raw " in normalized_title:
        score += 0.1
    elif requested_variant != "raw" and detect_listing_variant(comp.title) == requested_variant:
        score += 0.1
    if comp.raw_metadata.get("url_quality") == "direct_item" or "/itm/" in comp.listing_url:
        score += 0.05
    return _bounded_score(score)


def _reject_reason(price_key: MarketPriceKey, comp: SoldComp) -> str | None:
    normalized_evidence = _padded_normalized_evidence(comp)
    normalized_identity = _padded_normalized_identity(comp)
    if comp.currency.upper() != price_key.currency.upper():
        return "currency_mismatch"
    language_rejection = _language_reject_reason(price_key, comp)
    if language_rejection:
        return language_rejection
    variant_rejection = _variant_reject_reason(price_key, comp)
    if variant_rejection:
        return variant_rejection
    if comp.raw_metadata.get("priceRangeListing") or comp.raw_metadata.get("price_range_listing"):
        return "price_range_or_variation_listing"
    price_text = f" {normalize_text(comp.raw_metadata.get('priceText', ''))} "
    if re.search(r"\d[\d,]*(?:\.\d{1,2})?\s+(?:to|-)\s+\D*\d", price_text, flags=re.IGNORECASE):
        return "price_range_or_variation_listing"
    if _contains_any(normalized_evidence, REJECTION_PATTERNS["variation_or_pick"]):
        return "price_range_or_variation_listing"
    if _is_lot_or_bundle(normalized_evidence):
        return "likely_bundle_lot"
    if _contains_any(normalized_evidence, REJECTION_PATTERNS["proxy_or_custom"]):
        return "proxy_or_custom"
    if _contains_any(normalized_evidence, REJECTION_PATTERNS["digital"]):
        return "digital"
    if _contains_any(normalized_evidence, REJECTION_PATTERNS["oversized"]):
        return "oversized_or_jumbo"
    if price_key.variant != "graded" and (
        _contains_any(normalized_evidence, GRADED_TERMS) or _contains_any(f" {normalize_text(comp.condition_text)} ", GRADED_TERMS)
    ):
        return "graded_for_raw_request"
    if price_key.variant not in {"sealed", "product"} and _contains_any(normalized_evidence, REJECTION_PATTERNS["sealed_product"]):
        return "sealed_product_for_single_card_request"
    if not _collector_number_matches(price_key, normalized_identity):
        return "wrong_collector_number"
    if _set_code_conflicts(price_key, normalized_identity):
        return "wrong_set"
    if not _card_name_matches(price_key, normalized_identity):
        return "wrong_card_name"
    if _has_many_card_numbers(price_key, normalized_identity):
        return "multiple_card_numbers"
    if score_comp(price_key, comp) < MIN_INCLUDED_SCORE:
        return "weak_evidence_match"
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
    requested_card = normalize_text(str(raw.get("requestedCanonicalCardName") or raw.get("requestedCardName", "")))
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
        metadata.setdefault("requestedCanonicalCardName", normalize_text(_canonical_card_name(price_key)))
        metadata.setdefault("requestedCollectorNumber", normalize_collector_number(price_key.collector_number))
        normalized_evidence = _padded_normalized_evidence(comp)
        normalized_identity = _padded_normalized_identity(comp)
        requested_variant = normalize_market_variant(price_key.variant)
        detected_variant = detect_listing_variant(_evidence_text(comp))
        variant_match = requested_variant == "raw" or requested_variant == detected_variant
        collector_info = _collector_number_match_info(price_key, normalized_identity)
        set_info = _set_identity_match_info(price_key, normalized_identity)
        likely_pick = _contains_any(normalized_evidence, REJECTION_PATTERNS["variation_or_pick"])
        likely_lot = _is_lot_or_bundle(normalized_evidence)
        metadata.setdefault("collector_number_identity_text", _identity_text(comp)[:500])
        metadata.setdefault("requested_variant", requested_variant)
        metadata.setdefault("detected_variant", detected_variant)
        metadata.setdefault("variant_match", variant_match)
        metadata.setdefault("variant_warning", rejection_reason if rejection_reason and rejection_reason.startswith(("wrong_variant_", "weak_variant_")) else None)
        metadata.setdefault("likely_pick_your_card", likely_pick)
        metadata.setdefault("likely_bundle_lot", likely_lot)
        metadata.setdefault("language_rejection", "wrong_language" if rejection_reason == "wrong_language" else None)
        metadata.setdefault("collector_number_match", bool(collector_info["matches"]))
        metadata.setdefault("collector_number_match_quality", collector_info["quality"])
        metadata.setdefault("detected_collector_numbers", collector_info["detected"])
        metadata.setdefault("set_name_match", bool(set_info["matches"]))
        metadata.setdefault("set_match_quality", set_info["quality"])
        metadata.setdefault("set_code_conflict", bool(set_info["conflict"]))
        metadata.setdefault("card_name_match", _card_name_matches(price_key, normalized_identity))
        metadata.setdefault("canonical_card_name_candidates", list(_canonical_card_names(price_key)))
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
