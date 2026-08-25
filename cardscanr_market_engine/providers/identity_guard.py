from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any
import unicodedata

from ..catalogue_identity import is_generic_alias
from ..models import ProviderRequest


ENGLISH_MARKET_COUNTRIES = frozenset({"AU", "US", "GB", "CA"})
ENGLISH_MARKET_IDENTITY_UNAVAILABLE = "english_market_identity_unavailable"
LATIN_ALIAS_MIN_RATIO = 0.8
MOSTLY_NON_LATIN_RATIO = 0.5

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


@dataclass(frozen=True)
class ScriptAnalysis:
    latin_count: int
    non_latin_count: int
    latin_ratio: float
    non_latin_detected: bool


@dataclass(frozen=True)
class IdentityGuardResult:
    blocked: bool
    reason: str | None
    search_card_name: str
    search_name_source: str
    diagnostics: dict[str, Any]


def _clean(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def analyze_latin_ratio(value: object) -> ScriptAnalysis:
    text = unicodedata.normalize("NFKC", str(value or ""))
    latin_count = 0
    non_latin_count = 0
    non_latin_detected = False
    for char in text:
        if not char.isalpha():
            continue
        name = unicodedata.name(char, "")
        if "LATIN" in name:
            latin_count += 1
        else:
            non_latin_count += 1
            non_latin_detected = True
    total = latin_count + non_latin_count
    latin_ratio = 1.0 if total == 0 else latin_count / total
    return ScriptAnalysis(
        latin_count=latin_count,
        non_latin_count=non_latin_count,
        latin_ratio=round(latin_ratio, 3),
        non_latin_detected=non_latin_detected,
    )


def is_safe_latin_alias(value: object) -> bool:
    text = _clean(value)
    if not text or is_generic_alias(text):
        return False
    analysis = analyze_latin_ratio(text)
    return bool(analysis.latin_count > 0 and analysis.latin_ratio >= LATIN_ALIAS_MIN_RATIO)


def _safe_original_card_name(value: object) -> str:
    text = _clean(value)
    if not text:
        return ""
    analysis = analyze_latin_ratio(text)
    if analysis.non_latin_detected:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        return f"[non_latin_redacted length={len(text)} sha256={digest}]"
    return text[:80]


def _raw_alias_candidates(raw: dict[str, Any]) -> list[tuple[str, object]]:
    candidates: list[tuple[str, object]] = []
    for field in RAW_ALIAS_FIELDS:
        if field in raw:
            candidates.append((f"raw.{field}", raw.get(field)))
    aliases = raw.get("aliases")
    if isinstance(aliases, dict):
        for field in RAW_ALIAS_FIELDS:
            if field in aliases:
                candidates.append((f"raw.aliases.{field}", aliases.get(field)))
        english = aliases.get("en") or aliases.get("EN")
        if english:
            candidates.append(("raw.aliases.en", english))
    return candidates


def _english_alias_for_request(request: ProviderRequest) -> tuple[str, str] | None:
    key = request.price_key
    for source, value in _raw_alias_candidates(key.raw):
        text = _clean(value)
        if is_safe_latin_alias(text):
            return text, source
    if is_safe_latin_alias(key.card_name):
        return None
    normalized_name = _clean(key.normalized_card_name).replace("_", " ")
    if normalized_name and normalized_name != _clean(key.card_name) and is_safe_latin_alias(normalized_name):
        return normalized_name, "normalized_card_name"
    return None


def evaluate_english_market_identity(request: ProviderRequest) -> IdentityGuardResult:
    key = request.price_key
    market_country = str(request.market_country or "").upper()
    card_name = _clean(key.card_name)
    analysis = analyze_latin_ratio(card_name)
    alias = _english_alias_for_request(request)
    search_card_name = alias[0] if alias else card_name
    search_name_source = alias[1] if alias else "card_name"
    blocked = (
        market_country in ENGLISH_MARKET_COUNTRIES
        and analysis.non_latin_detected
        and analysis.latin_ratio < MOSTLY_NON_LATIN_RATIO
        and alias is None
    )
    diagnostics: dict[str, Any] = {
        "market_country": market_country,
        "marketplace": request.marketplace,
        "provider_marketplace": request.provider_marketplace_id,
        "provider_domain": request.provider_domain,
        "original_card_name": _safe_original_card_name(card_name),
        "original_card_name_redacted": bool(analysis.non_latin_detected),
        "latin_ratio": analysis.latin_ratio,
        "non_latin_detected": analysis.non_latin_detected,
        "search_name_source": search_name_source,
    }
    if blocked:
        diagnostics["blocked_reason"] = ENGLISH_MARKET_IDENTITY_UNAVAILABLE
    elif alias:
        diagnostics["english_alias_available"] = True
    return IdentityGuardResult(
        blocked=blocked,
        reason=ENGLISH_MARKET_IDENTITY_UNAVAILABLE if blocked else None,
        search_card_name=search_card_name,
        search_name_source=search_name_source,
        diagnostics=diagnostics,
    )
