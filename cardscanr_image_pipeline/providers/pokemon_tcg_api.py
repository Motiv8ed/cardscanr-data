from __future__ import annotations

from ..identity import normalize_local_card_number
from ..models import CardImageIdentity, ProviderImageCandidate
from ..paths import local_card_number_candidates
from .base import ImageProvider

POKEMON_TCG_IMAGE_BASE = "https://images.pokemontcg.io"


class PokemonTcgApiImageProvider:
    provider_id = "pokemon_tcg_api"

    def resolve(self, identity: CardImageIdentity) -> ProviderImageCandidate | None:
        if identity.language != "en":
            return None
        if identity.image_source == "pokewallet":
            return None

        if identity.image_source == "pokemon_tcg_api" and identity.catalogue_image_small and identity.catalogue_image_large:
            return ProviderImageCandidate(
                provider=self.provider_id,
                source_url_thumb=identity.catalogue_image_small,
                source_url_display=identity.catalogue_image_large,
                provider_card_id=_pokemon_tcg_api_card_id(identity),
                provider_set_id=identity.set_id,
                match_basis="catalogue_pokemon_tcg_api_urls",
            )

        provider_card_id = _pokemon_tcg_api_card_id(identity)
        if provider_card_id:
            parsed = _parse_pokemon_tcg_api_id(provider_card_id)
            if parsed:
                set_id, number = parsed
                if _identity_matches_pokemon_tcg_api(identity, set_id=set_id, number=number):
                    return _candidate_from_set_number(identity, provider_card_id, set_id, number, "provider_card_id")

        if identity.image_source == "pokemon_tcg_api" and identity.catalogue_image_small and identity.catalogue_image_large:
            if _catalogue_urls_match_identity(identity):
                return ProviderImageCandidate(
                    provider=self.provider_id,
                    source_url_thumb=identity.catalogue_image_small,
                    source_url_display=identity.catalogue_image_large,
                    provider_card_id=provider_card_id,
                    provider_set_id=identity.set_id,
                    match_basis="catalogue_pokemon_tcg_api_urls",
                )

        for local_id in local_card_number_candidates(identity.local_card_number):
            if not _identity_matches_pokemon_tcg_api(identity, set_id=identity.set_id, number=local_id):
                continue
            if identity.image_source not in {None, "", "pokemon_tcg_api"}:
                continue
            synthetic_id = f"{identity.set_id}-{local_id}"
            return _candidate_from_set_number(identity, synthetic_id, identity.set_id, local_id, "set_local_number")
        return None


def _pokemon_tcg_api_card_id(identity: CardImageIdentity) -> str | None:
    value = identity.provider_ids.get("pokemonTcgApi") or identity.provider_ids.get("pokemonTcgApiId")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _parse_pokemon_tcg_api_id(card_id: str) -> tuple[str, str] | None:
    text = str(card_id or "").strip()
    if "-" not in text:
        return None
    set_id, number = text.split("-", 1)
    if not set_id or not number:
        return None
    return set_id, number


def _identity_matches_pokemon_tcg_api(identity: CardImageIdentity, *, set_id: str, number: str) -> bool:
    if identity.set_id and set_id and identity.set_id.lower() != set_id.lower():
        return False
    expected = {normalize_local_card_number(item) for item in local_card_number_candidates(identity.local_card_number)}
    expected.add(identity.collector_number)
    return number in expected or normalize_local_card_number(number) in expected


def _catalogue_urls_match_identity(identity: CardImageIdentity) -> bool:
    for url in (identity.catalogue_image_small, identity.catalogue_image_large):
        if not url:
            continue
        if identity.set_id.lower() not in url.lower():
            return False
        if identity.local_card_number and identity.local_card_number not in url:
            if identity.local_card_number.isdigit() and identity.local_card_number not in url.split("/"):
                return False
    return True


def _candidate_from_set_number(
    identity: CardImageIdentity,
    provider_card_id: str,
    set_id: str,
    number: str,
    match_basis: str,
) -> ProviderImageCandidate:
    thumb = f"{POKEMON_TCG_IMAGE_BASE}/{set_id}/{number}.png"
    display = f"{POKEMON_TCG_IMAGE_BASE}/{set_id}/{number}_hires.png"
    return ProviderImageCandidate(
        provider="pokemon_tcg_api",
        source_url_thumb=thumb,
        source_url_display=display,
        provider_card_id=provider_card_id,
        provider_set_id=set_id,
        match_basis=match_basis,
    )
