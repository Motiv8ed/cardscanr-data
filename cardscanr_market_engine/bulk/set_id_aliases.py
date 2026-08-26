"""Canonical CardScanR set identity → provider set identifiers."""
from __future__ import annotations

import re

from ..fingerprints import normalize_text

# CardScanR shorthand / legacy set codes → static price file stem (under prices/current/).
_STATIC_SET_ALIASES: dict[str, str] = {
    "obf": "sv3",
    "sv03": "sv3",
    "evs": "swsh7",
    "meg": "me1",
    "pfl": "me2",
    "me4": "me04",  # no static file yet; TCGdex backfill target
    "sv8pt5": "sv8pt5",
    "sv3pt5": "sv3pt5",
    "sv9": "sv09",
    "m3": "24600",
    "pokemon-asia-my-official:set:me01": "me1",
    "pokemon-asia-my-official:set:sv3.5": "sv3pt5",
    "pokemon-asia-my-official:set:25th": "cel25",
    "pokemon-asia-my-official:set:pgo": "pgo",
    "pokemon-asia-my-official:set:mep": "mep",
    "sv08a": "23909",
}

# CardScanR canonical/static stem → TCGdex set id.
_TCGDEX_SET_ALIASES: dict[str, str] = {
    "sv8pt5": "sv08.5",
    "sv3pt5": "sv03.5",
    "sv4pt5": "sv04.5",
    "sv5pt5": "sv05.5",
    "sv6pt5": "sv06.5",
    "sv7pt5": "sv07.5",
    "sv9pt5": "sv09.5",
    "obf": "sv03",
    "sv03": "sv03",
    "sv3": "sv03",
    "evs": "swsh7",
    "me1": "me01",
    "meg": "me01",
    "me2": "me02",
    "pfl": "me02",
    "me3": "me03",
    "me4": "me04",
    "me04": "me04",
    "sv09": "sv09",
    "sv9": "sv09",
    "sv05": "sv05",
    "sv08": "sv08",
    "sv10": "sv10",
    "sv01": "sv01",
    "sv04": "sv04",
    "base1": "base1",
    "swsh1": "swsh1",
    "sm115": "sm115",
    "pokemon-asia-my-official:set:me01": "me01",
    "pokemon-asia-my-official:set:sv3.5": "sv03.5",
    "pokemon-asia-my-official:set:25th": "cel25",
    "pokemon-asia-my-official:set:pgo": "swsh10.5",
    "pokemon-asia-my-official:set:mep": "mep",
    "m3": "24600",
    "1433": "1433",
    "1375": "1375",
}

# Numeric JP catalogue ids → PokeWallet provider set id (often same string).
_POKEWALLET_SET_ALIASES: dict[str, str] = {
    "24173": "24173",
    "24600": "24600",
    "23909": "23909",
    "sv08a": "23909",
    "sv09": "24173",  # JP Battle Partners uses numeric provider id
}


def _normalize_set_key(set_code: str | None) -> str | None:
    code = str(set_code or "").strip()
    if not code:
        return None
    for prefix in (
        "tcgdex:international:set:",
        "pokemon-asia-my-official:set:",
        "tcgdex:",
    ):
        if code.startswith(prefix):
            code = code.split(":")[-1]
    return code or None


def resolve_static_set_id(set_code: str | None, *, language: str | None = None) -> str | None:
    raw = str(set_code or "").strip()
    if not raw:
        return None
    normalized_full = normalize_text(raw)
    if normalized_full in _STATIC_SET_ALIASES:
        return _STATIC_SET_ALIASES[normalized_full]
    resolved = _normalize_set_key(set_code)
    if not resolved:
        return None
    lang = normalize_text(language or "")
    key = normalize_text(resolved).replace(".", "")
    if lang in {"ja", "jp"} and key in {"sv09", "sv9"}:
        return "24173"
    return _STATIC_SET_ALIASES.get(key, resolved)


def resolve_tcgdex_set_id(set_code: str | None, *, language: str | None = None) -> str | None:
    static_id = resolve_static_set_id(set_code, language=language)
    if not static_id:
        return None
    raw = normalize_text(str(set_code or ""))
    if raw in _TCGDEX_SET_ALIASES:
        return _TCGDEX_SET_ALIASES[raw]
    key = normalize_text(static_id).replace(".", "")
    aliased = _TCGDEX_SET_ALIASES.get(key)
    if aliased:
        return aliased
    match = re.fullmatch(r"sv(\d+)pt(\d+)", key)
    if match:
        era, part = match.groups()
        return f"sv{int(era):02d}.{part}"
    match = re.fullmatch(r"me(\d+)", key)
    if match:
        return f"me{int(match.group(1)):02d}"
    return static_id


def resolve_pokewallet_set_id(set_code: str | None, *, language: str = "en") -> str | None:
    static_id = resolve_static_set_id(set_code)
    if not static_id:
        return None
    key = normalize_text(static_id)
    lang = normalize_text(language)
    if key in _POKEWALLET_SET_ALIASES:
        return _POKEWALLET_SET_ALIASES[key]
    if lang in {"ja", "jp"} and key.isdigit():
        return key
    return None


def is_synthetic_set_code(set_code: str | None, set_name: str | None = None) -> bool:
    code = normalize_text(set_code)
    name = normalize_text(set_name)
    if code in {"smoke-test", "smoke_test"}:
        return True
    if "smoke test" in name:
        return True
    return False
