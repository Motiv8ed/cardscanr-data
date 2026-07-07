from __future__ import annotations

from dataclasses import dataclass

from dataclasses import replace

from .models import CardImageIdentity, ProviderImageCandidate
from .providers.pokemon_tcg_api import PokemonTcgApiImageProvider
from .providers.pokewallet import PokeWalletAmbiguousMatchError, PokeWalletImageProvider
from .providers.registry import build_default_provider_chain
from .providers.tcgdex import TcgdexImageProvider
from .tcgdex_serie_cache import enrich_identity_serie_id


@dataclass(frozen=True)
class ProviderResolution:
    candidate: ProviderImageCandidate | None
    fallback_provider: str | None
    primary_provider: str | None
    ambiguous: bool = False
    ambiguity_reason: str | None = None
    skipped_providers: tuple[str, ...] = ()


def resolve_provider_image(
    identity: CardImageIdentity,
    providers: list[ImageProvider] | None = None,
) -> tuple[ProviderImageCandidate | None, str | None]:
    resolution = resolve_provider_with_trace(identity, providers=providers)
    return resolution.candidate, resolution.fallback_provider


def resolve_provider_with_trace(
    identity: CardImageIdentity,
    *,
    providers: list[ImageProvider] | None = None,
    source_card: dict | None = None,
) -> ProviderResolution:
    chain = providers or build_default_provider_chain(identity.language)
    enriched = enrich_identity_serie_id(identity)
    if source_card is not None:
        enriched = replace(enriched, source_card=source_card)
    skipped: list[str] = []
    for index, provider in enumerate(chain):
        try:
            candidate = provider.resolve(enriched)
        except PokeWalletAmbiguousMatchError as exc:
            return ProviderResolution(
                candidate=None,
                fallback_provider=None,
                primary_provider=None,
                ambiguous=True,
                ambiguity_reason=str(exc),
                skipped_providers=tuple(skipped),
            )
        if candidate is None:
            skipped.append(provider.provider_id)
            continue
        fallback_provider = None if index == 0 else provider.provider_id
        primary_provider = chain[0].provider_id if index > 0 else provider.provider_id
        if index > 0:
            return ProviderResolution(
                candidate=candidate,
                fallback_provider=provider.provider_id,
                primary_provider=candidate.provider,
                skipped_providers=tuple(skipped),
            )
        return ProviderResolution(
            candidate=candidate,
            fallback_provider=None,
            primary_provider=candidate.provider,
            skipped_providers=tuple(skipped),
        )
    return ProviderResolution(
        candidate=None,
        fallback_provider=None,
        primary_provider=None,
        skipped_providers=tuple(skipped),
    )


def classify_sample_bucket(
    identity: CardImageIdentity,
    *,
    source_card: dict,
    resolution: ProviderResolution | None = None,
) -> str | None:
    traced = resolution or resolve_provider_with_trace(identity, source_card=source_card)
    if traced.ambiguous or traced.candidate is None:
        return None
    language = identity.language
    if identity.image_source == "pokewallet":
        return "en_pokewallet" if language == "en" else "jp_pokewallet" if language == "jp" else None
    if language == "en" and identity.image_source == "pokemon_tcg_api":
        pokemon_candidate = PokemonTcgApiImageProvider().resolve(
            replace(enrich_identity_serie_id(identity), source_card=source_card)
        )
        return "en_pokemon_tcg_api" if pokemon_candidate is not None else None
    if language == "en" and identity.image_source == "tcgdex":
        tcgdx_candidate = TcgdexImageProvider().resolve(
            replace(enrich_identity_serie_id(identity), source_card=source_card)
        )
        return "en_tcgdex" if tcgdx_candidate is not None else None
    enriched = enrich_identity_serie_id(identity)
    if source_card is not None:
        enriched = replace(enriched, source_card=source_card)
    tcgdx_candidate = TcgdexImageProvider().resolve(enriched)
    pokemon_candidate = PokemonTcgApiImageProvider().resolve(enriched) if identity.language == "en" else None
    try:
        pokewallet_candidate = PokeWalletImageProvider().resolve(enriched)
    except PokeWalletAmbiguousMatchError:
        return None
    if language == "en" and tcgdx_candidate is not None:
        return "en_tcgdex"
    if language == "en" and pokemon_candidate is not None:
        return "en_pokemon_tcg_api"
    if language == "en" and pokewallet_candidate is not None:
        return "en_pokewallet"
    if language == "jp" and tcgdx_candidate is not None:
        return "jp_tcgdex"
    if language == "jp" and pokewallet_candidate is not None:
        return "jp_pokewallet"
    return None
