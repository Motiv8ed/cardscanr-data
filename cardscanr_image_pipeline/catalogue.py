from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .identity import identity_from_catalogue_card
from .models import CardImageIdentity

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOGUE_ROOT = ROOT / "public" / "v1"


def load_json(path: Path) -> Any:
    with open(path, encoding="utf-8-sig") as handle:
        return json.load(handle)


def load_set_index(catalogue_root: Path, *, game: str, language: str) -> dict[str, dict[str, Any]]:
    path = catalogue_root / "catalog" / game / language / "sets.json"
    if not path.exists():
        return {}
    payload = load_json(path)
    sets = payload.get("sets") if isinstance(payload, dict) else None
    if not isinstance(sets, list):
        return {}
    index: dict[str, dict[str, Any]] = {}
    for item in sets:
        if isinstance(item, dict) and item.get("id"):
            index[str(item["id"])] = item
    return index


def infer_serie_id_from_set_meta(set_meta: dict[str, Any]) -> str | None:
    series = set_meta.get("series")
    if not isinstance(series, str) or not series.strip():
        return None
    # TCGdex asset paths use short serie codes (e.g. SV, M, Base).
    # Catalogue stores display series names; callers can enrich via TCGdex API later.
    return series.strip()


def iter_catalogue_identities(
    catalogue_root: Path,
    *,
    game: str = "pokemon",
    languages: Iterable[str] = ("en", "jp"),
    set_id: str | None = None,
    sample_limit: int | None = None,
) -> Iterable[CardImageIdentity]:
    count = 0
    for language in languages:
        set_index = load_set_index(catalogue_root, game=game, language=language)
        cards_dir = catalogue_root / "catalog" / game / language / "cards"
        if not cards_dir.exists():
            continue
        for path in sorted(cards_dir.glob("*.json"), key=lambda item: item.name.lower()):
            if set_id and path.stem != set_id:
                continue
            payload = load_json(path)
            if not isinstance(payload, dict):
                continue
            cards = payload.get("cards")
            if not isinstance(cards, list):
                continue
            file_set_id = path.stem
            set_meta = set_index.get(file_set_id, {"id": file_set_id})
            serie_id = infer_serie_id_from_set_meta(set_meta)
            for card in cards:
                if not isinstance(card, dict):
                    continue
                if card.get("game") != game or card.get("language") != language:
                    continue
                identity = identity_from_catalogue_card(card, set_meta=set_meta, serie_id=serie_id)
                if not identity.canonical_base_id:
                    continue
                yield identity
                count += 1
                if sample_limit is not None and count >= sample_limit:
                    return
