from __future__ import annotations

from functools import lru_cache
from typing import Any

import requests

from .models import CardImageIdentity

TCGDEX_API_BASE = "https://api.tcgdex.net/v2"
_SERIE_MAPS: dict[str, dict[str, str]] = {}


def tcgdex_api_language(language: str) -> str:
    return "ja" if language in {"jp", "ja"} else language


def serie_from_tcgdex_asset_url(url: str | None) -> str | None:
    if not url or "assets.tcgdex.net" not in url:
        return None
    parts = [part for part in url.split("/") if part]
    for index, part in enumerate(parts):
        if part.endswith("tcgdex.net") and len(parts) > index + 2:
            return parts[index + 2]
    return None


def load_tcgdex_set_serie_map(language: str) -> dict[str, str]:
    api_language = tcgdex_api_language(language)
    if api_language in _SERIE_MAPS:
        return _SERIE_MAPS[api_language]
    mapping: dict[str, str] = {}
    try:
        response = requests.get(f"{TCGDEX_API_BASE}/{api_language}/sets", timeout=60)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError):
        _SERIE_MAPS[api_language] = mapping
        return mapping
    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            set_id = str(item["id"])
            serie_id = serie_from_tcgdex_asset_url(str(item.get("logo") or ""))
            if not serie_id:
                serie = item.get("serie")
                if isinstance(serie, dict):
                    serie_id = serie.get("id")
                elif isinstance(serie, str):
                    serie_id = serie
            if serie_id:
                mapping[set_id] = str(serie_id)
    _SERIE_MAPS[api_language] = mapping
    return mapping


@lru_cache(maxsize=2048)
def fetch_tcgdex_serie_id(language: str, set_id: str) -> str | None:
    mapping = load_tcgdex_set_serie_map(language)
    if set_id in mapping:
        return mapping[set_id]
    api_language = tcgdex_api_language(language)
    try:
        response = requests.get(f"{TCGDEX_API_BASE}/{api_language}/sets/{set_id}", timeout=20)
        if response.status_code != 200:
            return None
        payload = response.json()
    except (requests.RequestException, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    serie = payload.get("serie")
    if isinstance(serie, dict) and serie.get("id"):
        serie_id = str(serie["id"])
    elif isinstance(serie, str) and serie.strip():
        serie_id = serie.strip()
    else:
        return None
    mapping[set_id] = serie_id
    _SERIE_MAPS[api_language] = mapping
    return serie_id


def enrich_identity_serie_id(identity: CardImageIdentity) -> CardImageIdentity:
    if identity.serie_id:
        return identity
    serie_id = fetch_tcgdex_serie_id(identity.language, identity.set_id)
    if not serie_id:
        return identity
    from dataclasses import replace

    return replace(identity, serie_id=serie_id)
