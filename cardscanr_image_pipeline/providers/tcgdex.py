from __future__ import annotations

from ..identity import normalize_local_card_number
from ..models import CardImageIdentity, ProviderImageCandidate
from ..paths import local_card_number_candidates
from .base import ImageProvider

TCGDEX_ASSET_BASE = "https://assets.tcgdex.net"
TCGDEX_LANGUAGE_MAP = {
    "en": "en",
    "jp": "ja",
    "ja": "ja",
    "zh": "zh-tw",
}


def tcgdex_api_language(language: str) -> str:
    return TCGDEX_LANGUAGE_MAP.get(language.lower(), language.lower())


def build_tcgdex_image_url(
    *,
    language: str,
    serie_id: str,
    set_id: str,
    local_id: str,
    quality: str,
) -> str:
    api_lang = tcgdex_api_language(language)
    local = str(local_id or "").strip()
    return f"{TCGDEX_ASSET_BASE}/{api_lang}/{serie_id}/{set_id}/{local}/{quality}.webp"


def parse_tcgdex_card_id(card_id: str) -> tuple[str, str] | None:
    text = str(card_id or "").strip()
    if "-" not in text:
        return None
    set_id, local_id = text.rsplit("-", 1)
    if not set_id or not local_id:
        return None
    return set_id, local_id


class TcgdexImageProvider:
    provider_id = "tcgdex"

    def resolve(self, identity: CardImageIdentity) -> ProviderImageCandidate | None:
        if identity.image_source == "pokewallet":
            return None
        if identity.image_source == "pokemon_tcg_api" and not _tcgdex_card_id(identity):
            return None
        if identity.image_source == "tcgdex" and identity.catalogue_image_small and identity.catalogue_image_large:
            provider_card_id = _tcgdex_card_id(identity)
            return ProviderImageCandidate(
                provider=self.provider_id,
                source_url_thumb=identity.catalogue_image_small,
                source_url_display=identity.catalogue_image_large,
                provider_card_id=provider_card_id,
                provider_set_id=identity.set_id,
                match_basis="catalogue_tcgdex_urls",
            )
        provider_card_id = _tcgdex_card_id(identity)
        if provider_card_id:
            parsed = parse_tcgdex_card_id(provider_card_id)
            if parsed:
                set_id, local_id = parsed
                if _identity_matches_tcgdex(identity, set_id=set_id, local_id=local_id):
                    return _candidate_from_ids(identity, provider_card_id, set_id, local_id, "provider_card_id")

        if identity.image_source == "tcgdex" and identity.catalogue_image_small and identity.catalogue_image_large:
            if _catalogue_urls_match_identity(identity):
                return ProviderImageCandidate(
                    provider=self.provider_id,
                    source_url_thumb=identity.catalogue_image_small,
                    source_url_display=identity.catalogue_image_large,
                    provider_card_id=provider_card_id,
                    provider_set_id=identity.set_id,
                    match_basis="catalogue_tcgdex_urls",
                )

        serie_id = identity.serie_id
        if serie_id:
            for local_id in local_card_number_candidates(identity.local_card_number):
                if not _identity_matches_tcgdex(identity, set_id=identity.set_id, local_id=local_id):
                    continue
                thumb = build_tcgdex_image_url(
                    language=identity.language,
                    serie_id=serie_id,
                    set_id=identity.set_id,
                    local_id=local_id,
                    quality="low",
                )
                display = build_tcgdex_image_url(
                    language=identity.language,
                    serie_id=serie_id,
                    set_id=identity.set_id,
                    local_id=local_id,
                    quality="high",
                )
                synthetic_id = f"{identity.set_id}-{local_id}"
                return ProviderImageCandidate(
                    provider=self.provider_id,
                    source_url_thumb=thumb,
                    source_url_display=display,
                    provider_card_id=synthetic_id,
                    provider_set_id=identity.set_id,
                    match_basis="set_local_number",
                )
        return None


def _tcgdex_card_id(identity: CardImageIdentity) -> str | None:
    value = identity.provider_ids.get("tcgdex") or identity.provider_ids.get("tcgdexCardId")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _identity_matches_tcgdex(identity: CardImageIdentity, *, set_id: str, local_id: str) -> bool:
    if identity.set_id and set_id and identity.set_id.lower() != set_id.lower():
        return False
    expected_locals = {normalize_local_card_number(item) for item in local_card_number_candidates(identity.local_card_number)}
    expected_locals.add(identity.local_card_number)
    return normalize_local_card_number(local_id) in expected_locals or local_id in expected_locals


def _catalogue_urls_match_identity(identity: CardImageIdentity) -> bool:
    for url in (identity.catalogue_image_small, identity.catalogue_image_large):
        if not url:
            continue
        if identity.set_id.lower() not in url.lower():
            return False
        if identity.local_card_number and identity.local_card_number not in url:
            padded = identity.local_card_number.zfill(3) if identity.local_card_number.isdigit() else identity.local_card_number
            if padded not in url:
                return False
    return True


def _candidate_from_ids(
    identity: CardImageIdentity,
    provider_card_id: str,
    set_id: str,
    local_id: str,
    match_basis: str,
) -> ProviderImageCandidate | None:
    serie_id = identity.serie_id
    if not serie_id and identity.catalogue_image_small and identity.catalogue_image_large:
        return ProviderImageCandidate(
            provider="tcgdex",
            source_url_thumb=identity.catalogue_image_small,
            source_url_display=identity.catalogue_image_large,
            provider_card_id=provider_card_id,
            provider_set_id=set_id,
            match_basis=match_basis,
        )
    if not serie_id:
        return None
    return ProviderImageCandidate(
        provider="tcgdex",
        source_url_thumb=build_tcgdex_image_url(
            language=identity.language,
            serie_id=serie_id,
            set_id=set_id,
            local_id=local_id,
            quality="low",
        ),
        source_url_display=build_tcgdex_image_url(
            language=identity.language,
            serie_id=serie_id,
            set_id=set_id,
            local_id=local_id,
            quality="high",
        ),
        provider_card_id=provider_card_id,
        provider_set_id=set_id,
        match_basis=match_basis,
    )
