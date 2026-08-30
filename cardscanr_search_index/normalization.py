from __future__ import annotations

import re
import unicodedata

_FRACTION_PATTERN = re.compile(r"^(\d+)\s*/\s*(\d+)$")
_PREFIXED_FRACTION_PATTERN = re.compile(
    r"^([A-Za-z]+)?(\d+)\s*/\s*([A-Za-z]+)?(\d+)$"
)
_DIGIT_GROUP = re.compile(r"\d+")
_SPACE_AROUND_SEPARATORS = re.compile(r"\s*([/.-])\s*")
_PUNCT_TO_SPACE = re.compile(r"[\u2019\u2018'`´’‘]+")
_DASH_VARIANTS = re.compile(r"[\u2010\u2011\u2012\u2013\u2014\u2212-]+")
_NON_ALNUM = re.compile(r"[^\w\s]+", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
_EMBEDDED_COLLECTOR = re.compile(
    r"(?<![A-Za-z0-9])((?:[A-Za-z]{1,4})?\d{1,4}\s*/\s*(?:[A-Za-z]{1,4})?\d{1,4}|\d{1,4})(?![A-Za-z0-9])"
)
_COLLECTOR_QUERY = re.compile(r"^[\w./-]+$", re.UNICODE)


def nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", str(text or ""))


def normalize_whitespace(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def normalize_punctuation(text: str) -> str:
    value = nfkc(text)
    value = _PUNCT_TO_SPACE.sub(" ", value)
    value = _DASH_VARIANTS.sub("-", value)
    value = _NON_ALNUM.sub(" ", value)
    return normalize_whitespace(value)


def normalize_search_text(text: str) -> str:
    value = normalize_punctuation(text)
    return value.casefold()


def normalize_ascii_token(text: str) -> str:
    value = nfkc(text).translate(_FULLWIDTH_DIGITS)
    value = normalize_punctuation(value)
    return value.casefold()


def normalize_collector_number(value: str) -> str:
    """Normalize collector numbers losslessly for search identity.

    Strips leading zeros from *every* digit group while preserving letter
    prefixes/suffixes (TG01/TG30 → tg1/tg30, SV001 → sv1, 024/086 → 24/86).
    """
    raw = nfkc(value).translate(_FULLWIDTH_DIGITS).strip()
    if not raw:
        return ""
    raw = _SPACE_AROUND_SEPARATORS.sub(r"\1", raw)
    raw = re.sub(r"\s+", "", raw)
    stripped = _DIGIT_GROUP.sub(lambda match: str(int(match.group(0))), raw)
    return stripped.casefold()


def split_collector_number(value: str) -> tuple[str, str | None]:
    """Return (numerator, optional denominator) after normalization."""
    normalized = normalize_collector_number(value)
    if not normalized:
        return "", None
    if "/" not in normalized:
        return normalized, None
    left, right = normalized.split("/", 1)
    return left, right or None


def is_collector_number_query(value: str) -> bool:
    raw = nfkc(value).translate(_FULLWIDTH_DIGITS).strip()
    if not raw:
        return False
    if raw.isdigit():
        return True
    compact = re.sub(r"\s+", "", raw)
    if _FRACTION_PATTERN.match(compact):
        return True
    if _PREFIXED_FRACTION_PATTERN.match(compact):
        if len(compact) <= 12 and any(ch.isdigit() for ch in compact):
            return True
    if len(raw) > 12:
        return False
    if not _COLLECTOR_QUERY.match(raw):
        return False
    digit_count = sum(1 for ch in raw if ch.isdigit())
    if digit_count == 0:
        return len(raw) <= 4
    return len(raw) <= 8


def extract_collector_number_from_query(value: str) -> str | None:
    """Pull a collector-number token out of a mixed name/set/number query."""
    raw = nfkc(value).translate(_FULLWIDTH_DIGITS).strip()
    if not raw:
        return None
    if is_collector_number_query(raw):
        return raw
    matches = list(_EMBEDDED_COLLECTOR.finditer(raw))
    if not matches:
        return None
    fractions = [match.group(1) for match in matches if "/" in match.group(1)]
    if fractions:
        return fractions[-1]
    return matches[-1].group(1)


def collector_number_variants(value: str) -> list[str]:
    raw = nfkc(value).translate(_FULLWIDTH_DIGITS).strip()
    if not raw:
        return []
    variants = {raw, raw.casefold()}
    normalized = normalize_collector_number(raw)
    variants.add(normalized)
    local, total = split_collector_number(raw)
    if local:
        variants.add(local)
    if local and total:
        variants.add(f"{local}/{total}")
    if raw.isdigit():
        variants.add(raw.zfill(3))
        variants.add(raw.lstrip("0") or "0")
    match = _FRACTION_PATTERN.match(re.sub(r"\s+", "", raw))
    if match:
        left = match.group(1)
        right = match.group(2)
        variants.add(f"{left}/{right}")
        variants.add(f"{left.lstrip('0') or '0'}/{right.lstrip('0') or '0'}")
        variants.add(f"{left.zfill(3)}/{right.zfill(3)}")
    return sorted({item for item in variants if item})


def normalize_set_name(text: str) -> str:
    return normalize_search_text(text)


def is_latin_text(text: str) -> bool:
    if not text:
        return False
    for char in text:
        if char.isalpha() and ord(char) > 127:
            return False
    return any(char.isalpha() for char in text)


def build_search_aliases(
    *,
    name: str,
    normalized_name: str,
    localized_name: str | None,
    display_name: str | None,
    original_name: str | None,
    set_name: str,
    set_code: str | None,
    provider_set_id: str | None,
    collector_number: str,
) -> list[str]:
    aliases: set[str] = set()
    for candidate in (name, normalized_name, localized_name, display_name, original_name):
        if candidate:
            aliases.add(candidate.strip())
            aliases.add(normalize_search_text(candidate))
    if set_name:
        aliases.add(set_name.strip())
        aliases.add(normalize_set_name(set_name))
    if set_code:
        aliases.add(set_code.strip())
        aliases.add(set_code.strip().casefold())
    if provider_set_id:
        aliases.add(str(provider_set_id).strip())
    for variant in collector_number_variants(collector_number):
        aliases.add(variant)
    # Romanized aliases only from trusted Latin catalogue fields.
    for candidate in (display_name, original_name):
        if candidate and is_latin_text(candidate) and candidate.strip() != (localized_name or "").strip():
            aliases.add(candidate.strip())
            aliases.add(normalize_search_text(candidate))
    return sorted(alias for alias in aliases if alias)
