from __future__ import annotations

import re
from typing import Any

from ..identity import normalize_local_card_number, promotion_metadata, provider_ids_from_card
from ..models import CardImageIdentity, ProviderImageCandidate
from ..paths import local_card_number_candidates

POKEWALLET_API_BASE = "https://api.pokewallet.io"
_POKEWALLET_ID_PATTERN = re.compile(r"^pk_[0-9a-f]+$", re.IGNORECASE)


class PokeWalletAmbiguousMatchError(ValueError):
    pass


class PokeWalletImageProvider:
    provider_id = "pokewallet"

    def resolve(self, identity: CardImageIdentity) -> ProviderImageCandidate | None:
        card_context = identity.source_card
        if isinstance(card_context, dict):
            return self._resolve_from_card(identity, card_context)
        return self._resolve_from_identity(identity)

    def _resolve_from_identity(self, identity: CardImageIdentity) -> ProviderImageCandidate | None:
        provider_card_id = _pokewallet_provider_id(identity.provider_ids)
        if not provider_card_id:
            return None
        if identity.image_source != "pokewallet":
            return None
        return self._build_candidate(identity, provider_card_id, identity.catalogue_image_small, identity.catalogue_image_large)

    def _resolve_from_card(self, identity: CardImageIdentity, card: dict[str, Any]) -> ProviderImageCandidate | None:
        provider_ids = provider_ids_from_card(card)
        provider_card_id = _pokewallet_provider_id(provider_ids)
        image_source = str(card.get("imageSource") or card.get("providerImageSource") or "").strip()
        if image_source != "pokewallet":
            return None
        if not provider_card_id:
            return None

        promo = promotion_metadata(card)
        validation_error = _validate_pokewallet_identity(identity, card, provider_card_id, promo)
        if validation_error:
            raise PokeWalletAmbiguousMatchError(validation_error)

        small = _optional_url(card.get("imageSmall") or card.get("imageUrlSmall"))
        large = _optional_url(card.get("imageLarge") or card.get("imageUrlLarge"))
        if not small or not large:
            return None
        if not _urls_reference_provider(provider_card_id, small, large):
            return None
        if not _urls_match_set_identity(identity, promo, small, large):
            return None

        return ProviderImageCandidate(
            provider=self.provider_id,
            source_url_thumb=small,
            source_url_display=large,
            provider_card_id=provider_card_id,
            provider_set_id=str(promo.get("providerSetId") or identity.provider_set_id or identity.set_id),
            match_basis="pokewallet_catalogue_identity",
        )

    def _build_candidate(
        self,
        identity: CardImageIdentity,
        provider_card_id: str,
        small: str | None,
        large: str | None,
    ) -> ProviderImageCandidate | None:
        if not small or not large:
            small = _pokewallet_image_url(provider_card_id, "low")
            large = _pokewallet_image_url(provider_card_id, "high")
        if not _urls_reference_provider(provider_card_id, small, large):
            return None
        return ProviderImageCandidate(
            provider=self.provider_id,
            source_url_thumb=small,
            source_url_display=large,
            provider_card_id=provider_card_id,
            provider_set_id=identity.provider_set_id or identity.set_id,
            match_basis="pokewallet_provider_id",
        )


def _pokewallet_provider_id(provider_ids: dict[str, Any]) -> str | None:
    value = provider_ids.get("pokewallet")
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if not _POKEWALLET_ID_PATTERN.match(text):
        return None
    return text


def _pokewallet_image_url(provider_card_id: str, size: str) -> str:
    return f"{POKEWALLET_API_BASE}/images/{provider_card_id}?size={size}"


def _optional_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _urls_reference_provider(provider_card_id: str, small: str, large: str) -> bool:
    token = provider_card_id.lower()
    return token in small.lower() and token in large.lower()


def _urls_match_set_identity(
    identity: CardImageIdentity,
    promo: dict[str, Any],
    small: str,
    large: str,
) -> bool:
    provider_set_id = promo.get("providerSetId")
    if provider_set_id is not None and str(provider_set_id) != str(identity.set_id):
        if str(promo.get("providerSetCode") or "") != str(identity.set_id):
            return False
    for local_id in local_card_number_candidates(identity.local_card_number):
        if local_id and local_id in small and local_id in large:
            return True
        if identity.collector_number and identity.collector_number in small:
            return True
    return bool(identity.collector_number and identity.collector_number in small)


def _validate_pokewallet_identity(
    identity: CardImageIdentity,
    card: dict[str, Any],
    provider_card_id: str,
    promo: dict[str, Any],
) -> str | None:
    if identity.language not in {"en", "jp"}:
        return "unsupported_language"
    if not identity.set_id:
        return "missing_set_id"
    if not identity.collector_number:
        return "missing_collector_number"
    provider_set_id = promo.get("providerSetId")
    if provider_set_id is not None:
        provider_set_text = str(provider_set_id)
        if provider_set_text != str(identity.set_id) and str(promo.get("providerSetCode") or "") != str(identity.set_id):
            return "provider_set_id_mismatch"
    promo_card_id = promo.get("providerCardId")
    if isinstance(promo_card_id, str) and promo_card_id.strip() and promo_card_id.strip() != provider_card_id:
        return "conflicting_provider_card_id"
    promo_identity_key = promo.get("identityKey")
    if isinstance(promo_identity_key, str) and promo_identity_key.strip():
        expected_parts = [
            identity.language,
            str(identity.set_id),
            identity.collector_number,
        ]
        if not all(part in promo_identity_key for part in expected_parts[:2]):
            return "promotion_identity_key_mismatch"
    duplicate_ids = {
        value.strip()
        for value in (
            provider_card_id,
            str(promo.get("providerCardId") or "").strip(),
            str(card.get("providerIds", {}).get("pokewallet") or "").strip(),
        )
        if value
    }
    if len(duplicate_ids) > 1:
        return "ambiguous_provider_card_id"
    return None


def extract_pokewallet_identity(card: dict[str, Any]) -> dict[str, Any]:
    provider_ids = provider_ids_from_card(card)
    promo = promotion_metadata(card)
    return {
        "providerCardId": _pokewallet_provider_id(provider_ids),
        "providerSetId": promo.get("providerSetId"),
        "providerSetCode": promo.get("providerSetCode"),
        "imageSource": card.get("imageSource"),
        "collectorNumber": card.get("collectorNumber"),
        "setId": card.get("setId"),
        "language": card.get("language"),
    }
