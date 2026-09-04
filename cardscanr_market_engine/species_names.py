"""Trusted Japanese → English Pokémon species name resolution.

Uses a static map derived from PokeAPI ``pokemon_species_names``
(local_language_id 1 → 9). Does not invent names or accept generic aliases.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from .catalogue_identity import is_generic_alias
from .fingerprints import normalize_text

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MAP_PATH = ROOT / "data" / "pokemon_species_names_ja_en.json"

# Trainer/owner possessive: "ホップのウールー", "Nのシンボラー", "リーリエのキュワワー"
_POSSESSIVE_NO_RE = re.compile(r"^(.+?)の(.+)$")
_SPECIES_SUFFIXES = ("VSTAR", "VMAX", "LV.X", "GX", "EX", "ex", "V")


def _clean(value: object) -> str:
    return " ".join(str(value or "").strip().split())


@lru_cache(maxsize=1)
def load_ja_en_species_map(path: str | None = None) -> dict[str, str]:
    map_path = Path(path) if path else DEFAULT_MAP_PATH
    if not map_path.is_file():
        return {}
    try:
        payload = json.loads(map_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    names = payload.get("names") if isinstance(payload, dict) else None
    if not isinstance(names, dict):
        return {}
    cleaned: dict[str, str] = {}
    for ja, en in names.items():
        ja_text = _clean(ja)
        en_text = _clean(en)
        if not ja_text or not en_text or is_generic_alias(en_text):
            continue
        cleaned[ja_text] = en_text
    return cleaned


def _format_species_with_suffix(mapped: str, suffix: str) -> str:
    if suffix.lower() == "ex":
        return f"{mapped} ex"
    return f"{mapped} {suffix}"


def _resolve_with_suffix_strip(text: str, mapping: dict[str, str]) -> str | None:
    for suffix in _SPECIES_SUFFIXES:
        candidates: list[str] = []
        for sep in (" ", "　", "-"):
            token = f"{sep}{suffix}"
            if text.endswith(token):
                candidates.append(text[: -len(token)].strip())
        # Glued JP forms such as ブラッキーex (no separator).
        if text.endswith(suffix) and len(text) > len(suffix):
            candidates.append(text[: -len(suffix)].strip())
        for trimmed in candidates:
            if not trimmed or trimmed == text:
                continue
            mapped = mapping.get(trimmed)
            if mapped:
                return _format_species_with_suffix(mapped, suffix)
    return None


def _resolve_after_possessive_strip(text: str, mapping: dict[str, str]) -> str | None:
    match = _POSSESSIVE_NO_RE.match(text)
    if not match:
        return None
    remainder = _clean(match.group(2))
    if not remainder:
        return None
    direct = mapping.get(remainder)
    if direct:
        return direct
    return _resolve_with_suffix_strip(remainder, mapping)


def resolve_english_species_name(japanese_name: object, *, map_path: str | None = None) -> str | None:
    """Return canonical English species name for a Japanese card/species name."""
    text = _clean(japanese_name)
    if not text or is_generic_alias(text):
        return None
    mapping = load_ja_en_species_map(map_path)
    direct = mapping.get(text)
    if direct:
        return direct
    suffixed = _resolve_with_suffix_strip(text, mapping)
    if suffixed:
        return suffixed
    # Safe possessive/trainer strip only: XのSpecies → Species when remainder maps.
    possessive = _resolve_after_possessive_strip(text, mapping)
    if possessive:
        return possessive
    return None


def species_resolution_diagnostics(japanese_name: object, *, map_path: str | None = None) -> dict[str, Any]:
    resolved = resolve_english_species_name(japanese_name, map_path=map_path)
    return {
        "inputNameNormalized": normalize_text(japanese_name) or None,
        "resolvedEnglishName": resolved,
        "mapLoaded": bool(load_ja_en_species_map(map_path)),
        "mapSize": len(load_ja_en_species_map(map_path)),
    }
