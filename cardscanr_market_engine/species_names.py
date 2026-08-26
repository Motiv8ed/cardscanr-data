"""Trusted Japanese → English Pokémon species name resolution.

Uses a static map derived from PokeAPI ``pokemon_species_names``
(local_language_id 1 → 9). Does not invent names or accept generic aliases.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from .catalogue_identity import is_generic_alias
from .fingerprints import normalize_text

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MAP_PATH = ROOT / "data" / "pokemon_species_names_ja_en.json"


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


def resolve_english_species_name(japanese_name: object, *, map_path: str | None = None) -> str | None:
    """Return canonical English species name for a Japanese card/species name."""
    text = _clean(japanese_name)
    if not text or is_generic_alias(text):
        return None
    mapping = load_ja_en_species_map(map_path)
    direct = mapping.get(text)
    if direct:
        return direct
    # Strip common trainer/suffix tokens that leave a base species name.
    for suffix in ("ex", "EX", "V", "VMAX", "VSTAR", "GX", "LV.X"):
        trimmed = text
        for sep in (" ", "　", "-"):
            if trimmed.endswith(f"{sep}{suffix}"):
                trimmed = trimmed[: -len(suffix) - len(sep)].strip()
                break
        if trimmed != text:
            mapped = mapping.get(trimmed)
            if mapped:
                return f"{mapped} {suffix}" if suffix.lower() != "ex" else f"{mapped} ex"
    return None


def species_resolution_diagnostics(japanese_name: object, *, map_path: str | None = None) -> dict[str, Any]:
    resolved = resolve_english_species_name(japanese_name, map_path=map_path)
    return {
        "inputNameNormalized": normalize_text(japanese_name) or None,
        "resolvedEnglishName": resolved,
        "mapLoaded": bool(load_ja_en_species_map(map_path)),
        "mapSize": len(load_ja_en_species_map(map_path)),
    }
