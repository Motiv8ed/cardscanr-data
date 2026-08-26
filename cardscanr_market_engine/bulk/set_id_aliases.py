"""Resolve CardScanR set codes to provider-specific identifiers."""
from __future__ import annotations

import re

from ..fingerprints import normalize_text
from .static_price_index import resolve_static_set_id

# CardScanR canonical set id -> TCGdex set id (when they differ).
_TCGDEX_SET_ALIASES: dict[str, str] = {
    "sv8pt5": "sv08.5",
    "sv3pt5": "sv03.5",
    "sv4pt5": "sv04.5",
    "sv5pt5": "sv05.5",
    "sv6pt5": "sv06.5",
    "sv7pt5": "sv07.5",
    "sv9pt5": "sv09.5",
}


def resolve_tcgdex_set_id(set_code: str | None) -> str | None:
    resolved = resolve_static_set_id(set_code)
    if not resolved:
        return None
    key = normalize_text(resolved).replace(".", "")
    aliased = _TCGDEX_SET_ALIASES.get(key)
    if aliased:
        return aliased
    # Heuristic: sv8pt5 style -> sv08.5 when not explicitly mapped.
    match = re.fullmatch(r"sv(\d+)pt(\d+)", key)
    if match:
        era, part = match.groups()
        return f"sv{int(era):02d}.{part}"
    return resolved
