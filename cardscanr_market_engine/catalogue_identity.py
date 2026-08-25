from __future__ import annotations

import re

from .fingerprints import normalize_collector_number, normalize_name, normalize_text

INTERNAL_SET_PREFIXES = ("pokemon-asia-",)
INTERNAL_SET_MARKER = ":set:"
GENERIC_ALIAS_VALUES = frozenset({"unknown", "n/a", "na", "-", "none", "null"})


def is_generic_alias(value: object) -> bool:
    text = normalize_text(value)
    return not text or text in GENERIC_ALIAS_VALUES


def is_internal_set_code(value: object) -> bool:
    text = normalize_text(value)
    if not text:
        return False
    lowered = text.lower()
    if any(lowered.startswith(prefix) for prefix in INTERNAL_SET_PREFIXES):
        return True
    if INTERNAL_SET_MARKER in lowered:
        return True
    compact = lowered.replace(" ", "").replace("_", "")
    if re.fullmatch(r"\d{4,}", compact):
        return True
    return False


def is_internal_catalogue_collector_number(value: object, *, set_code: object = None) -> bool:
    text = normalize_collector_number(value)
    if not text:
        return False
    if "/" in text:
        return False
    if re.fullmatch(r"\d{4,}", text):
        return True
    if is_internal_set_code(set_code) and re.fullmatch(r"\d+", text):
        return True
    return False


def external_set_code_hint(set_code: object) -> str:
    text = str(set_code or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if INTERNAL_SET_MARKER in lowered:
        hint = text.rsplit(":", 1)[-1].strip()
        return hint.upper() if hint else ""
    if is_internal_set_code(text):
        return ""
    return text.upper()


def compact_set_search_name(set_name: object) -> str:
    text = " ".join(str(set_name or "").strip().split())
    if not text:
        return ""
    text = re.split(r"\brelease\s+date\b", text, flags=re.IGNORECASE)[0].strip()
    text = re.sub(r"\b\d{1,2}-\d{1,2}-\d{4}\b", "", text).strip(" -")
    normalized = normalize_name(text).replace("_", " ")
    words = normalized.split()
    if len(words) >= 4:
        half = len(words) // 2
        if words[:half] == words[half : half * 2]:
            normalized = " ".join(words[:half])
    return normalized


def searchable_set_label(set_name: object, set_code: object) -> str:
    compact = compact_set_search_name(set_name)
    if compact and not is_generic_alias(compact):
        return compact
    hint = external_set_code_hint(set_code)
    if hint:
        return hint
    code = str(set_code or "").strip()
    if code and not is_internal_set_code(code):
        return code.upper()
    return ""


def searchable_collector_number(value: object, *, set_code: object = None) -> str:
    if is_internal_catalogue_collector_number(value, set_code=set_code):
        return ""
    return normalize_collector_number(value)


def uses_catalogue_collector_identity(price_key: object) -> bool:
    return is_internal_catalogue_collector_number(
        getattr(price_key, "collector_number", ""),
        set_code=getattr(price_key, "set_code", None),
    )


def allows_regional_listing_language(price_key: object) -> bool:
    language = normalize_text(getattr(price_key, "language", ""))
    set_code = getattr(price_key, "set_code", None)
    if language in {"en", "eng", "english"} and is_internal_set_code(set_code):
        return True
    return bool(getattr(price_key, "raw", {}).get("allow_cross_language_fallback"))
