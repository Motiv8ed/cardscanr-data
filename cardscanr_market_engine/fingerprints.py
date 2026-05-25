from __future__ import annotations

import re
import unicodedata


def normalize_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def normalize_name(value: object) -> str:
    text = normalize_text(value)
    text = re.sub(r"[^\w]+", "_", text, flags=re.UNICODE).strip("_")
    text = re.sub(r"_+", "_", text)
    return text or "unknown"


def normalize_collector_number(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().upper()
    text = re.sub(r"\s+", "", text)
    return text


def build_market_price_fingerprint(
    *,
    game: object,
    language: object,
    set_code: object,
    set_name: object,
    collector_number: object,
    card_name: object,
    variant: object,
    condition: object,
    market_country: object,
    currency: object,
) -> str:
    set_identity = normalize_text(set_code) or normalize_name(set_name)
    parts = [
        normalize_text(game) or "unknown",
        normalize_text(language) or "unknown",
        set_identity,
        normalize_collector_number(collector_number) or "-",
        normalize_name(card_name),
        normalize_text(variant) or "raw",
        normalize_text(condition) or "unknown",
        normalize_text(market_country) or "unknown",
        normalize_text(currency) or "usd",
    ]
    return "|".join(parts)


def fingerprint_from_price_key(price_key: object) -> str:
    return build_market_price_fingerprint(
        game=getattr(price_key, "game", ""),
        language=getattr(price_key, "language", ""),
        set_code=getattr(price_key, "set_code", ""),
        set_name=getattr(price_key, "set_name", ""),
        collector_number=getattr(price_key, "collector_number", ""),
        card_name=getattr(price_key, "normalized_card_name", "") or getattr(price_key, "card_name", ""),
        variant=getattr(price_key, "variant", ""),
        condition=getattr(price_key, "condition", ""),
        market_country=getattr(price_key, "market_country", ""),
        currency=getattr(price_key, "currency", ""),
    )
