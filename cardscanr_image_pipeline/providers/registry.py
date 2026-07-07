from __future__ import annotations

from .base import ImageProvider
from .pokemon_tcg_api import PokemonTcgApiImageProvider
from .pokewallet import PokeWalletImageProvider
from .tcgdex import TcgdexImageProvider


def build_default_provider_chain(language: str) -> list[ImageProvider]:
    providers: list[ImageProvider] = [TcgdexImageProvider()]
    if language == "en":
        providers.append(PokemonTcgApiImageProvider())
    providers.append(PokeWalletImageProvider())
    return providers
