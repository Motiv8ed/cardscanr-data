from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote


SCHEMA_VERSION = "1.0.0"
GAME = "pokemon"
REGION_GLOBAL = "GLOBAL"
REGION_MULTI = "MULTI"


class AmbiguousLanguageError(ValueError):
    """Raised when a legacy language value cannot be mapped without guessing."""


@dataclass(frozen=True)
class LanguageDefinition:
    language: str
    english_name: str
    native_name: str
    default_region: str
    release_territories: tuple[str, ...]
    legacy_aliases: tuple[str, ...] = ()
    tcgdex_codes: tuple[str, ...] = ()
    officially_printed: bool = True
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "englishName": self.english_name,
            "nativeName": self.native_name,
            "defaultRegion": self.default_region,
            "releaseTerritories": list(self.release_territories),
            "legacyAliases": list(self.legacy_aliases),
            "providerCodes": {"tcgdex": list(self.tcgdex_codes)},
            "officiallyPrinted": self.officially_printed,
            "notes": list(self.notes),
        }


LANGUAGE_DEFINITIONS: tuple[LanguageDefinition, ...] = (
    LanguageDefinition("en", "English", "English", REGION_GLOBAL, (), ("eng",), ("en",)),
    LanguageDefinition("ja", "Japanese", "日本語", "JP", ("JP",), ("jp", "jap"), ("ja",)),
    LanguageDefinition("zh-Hans", "Chinese (Simplified)", "简体中文", "CN", ("CN",), ("zh-cn", "chs"), ("zh-cn",)),
    LanguageDefinition(
        "zh-Hant",
        "Chinese (Traditional)",
        "繁體中文",
        REGION_MULTI,
        ("TW", "HK"),
        ("zh-tw", "cht"),
        ("zh-tw",),
        notes=("TCGdex does not distinguish Taiwan and Hong Kong release territories; region remains MULTI.",),
    ),
    LanguageDefinition("ko", "Korean", "한국어", "KR", ("KR",), ("kr", "kor"), ("ko",)),
    LanguageDefinition("th", "Thai", "ไทย", "TH", ("TH",), (), ("th",)),
    LanguageDefinition("id", "Indonesian", "Bahasa Indonesia", "ID", ("ID",), (), ("id",)),
    LanguageDefinition("fr", "French", "Français", "FR", ("FR",), ("fre", "fra"), ("fr",)),
    LanguageDefinition("de", "German", "Deutsch", "DE", ("DE",), ("ger", "deu"), ("de",)),
    LanguageDefinition("it", "Italian", "Italiano", "IT", ("IT",), ("ita",), ("it",)),
    LanguageDefinition("es", "Spanish (Spain)", "Español", "ES", ("ES",), ("spa",), ("es",)),
    LanguageDefinition(
        "es-419",
        "Spanish (Latin America)",
        "Español (Latinoamérica)",
        "LATAM",
        (),
        ("es-mx", "es-latam"),
        ("es-mx",),
        notes=("TCGdex labels this catalogue es-mx while its status page describes Latin America.",),
    ),
    LanguageDefinition("pt-BR", "Portuguese (Brazil)", "Português (Brasil)", "BR", ("BR",), ("pt-br", "por-br"), ("pt-br",)),
    LanguageDefinition(
        "pt-PT",
        "Portuguese (Portugal)",
        "Português (Portugal)",
        "PT",
        ("PT",),
        ("pt-pt", "por-pt"),
        ("pt-pt",),
        notes=("Registered even when a provider currently exposes zero records.",),
    ),
    LanguageDefinition("nl", "Dutch", "Nederlands", "NL", ("NL",), ("dut", "nld"), ("nl",)),
    LanguageDefinition("pl", "Polish", "Polski", "PL", ("PL",), ("pol",), ("pl",)),
    LanguageDefinition("ru", "Russian", "Русский", "RU", ("RU",), ("rus",), ("ru",)),
)

LANGUAGE_BY_TAG = {item.language: item for item in LANGUAGE_DEFINITIONS}
TCGDEX_LANGUAGE_MAP = {
    code.casefold(): item.language
    for item in LANGUAGE_DEFINITIONS
    for code in item.tcgdex_codes
}
LEGACY_LANGUAGE_MAP = {
    alias.casefold(): item.language
    for item in LANGUAGE_DEFINITIONS
    for alias in item.legacy_aliases
}
for _definition in LANGUAGE_DEFINITIONS:
    LEGACY_LANGUAGE_MAP[_definition.language.casefold()] = _definition.language

AMBIGUOUS_LEGACY_LANGUAGES = {
    "zh": "Legacy zh combines scripts and regions; inspect providerLanguage or source provenance.",
    "chn": "PokéWallet chn does not prove Simplified versus Traditional Chinese.",
    "chi": "Legacy bibliographic Chinese code does not identify script or release territory.",
    "zho": "ISO Chinese macrolanguage code does not identify script or release territory.",
    "pt": "Portuguese without a region does not distinguish Brazil from Portugal.",
}


def canonicalize_language(value: str, *, provider: str | None = None) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("language is required")
    folded = raw.casefold()
    if provider == "tcgdex" and folded in TCGDEX_LANGUAGE_MAP:
        return TCGDEX_LANGUAGE_MAP[folded]
    if folded in AMBIGUOUS_LEGACY_LANGUAGES:
        raise AmbiguousLanguageError(AMBIGUOUS_LEGACY_LANGUAGES[folded])
    canonical = LEGACY_LANGUAGE_MAP.get(folded)
    if canonical is None:
        raise ValueError(f"unsupported language value: {value!r}")
    return canonical


def language_definition(value: str, *, provider: str | None = None) -> LanguageDefinition:
    return LANGUAGE_BY_TAG[canonicalize_language(value, provider=provider)]


def region_for_language(value: str, *, provider: str | None = None) -> str:
    return language_definition(value, provider=provider).default_region


def release_territories_for_language(value: str, *, provider: str | None = None) -> list[str]:
    return list(language_definition(value, provider=provider).release_territories)


_DIGIT_GROUP = re.compile(r"\d+")
_SPACE_AROUND_SEPARATORS = re.compile(r"\s*([/.-])\s*")
_UNSAFE_ID = re.compile(r"[\x00-\x1f|]+")


def normalize_collector_number(value: str) -> str:
    raw = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not raw:
        return ""
    raw = _SPACE_AROUND_SEPARATORS.sub(r"\1", raw)
    raw = re.sub(r"\s+", "", raw)

    def normalize_digits(match: re.Match[str]) -> str:
        return str(int(match.group(0)))

    return _DIGIT_GROUP.sub(normalize_digits, raw).upper()


def identity_token(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = _UNSAFE_ID.sub("-", text)
    if not text:
        raise ValueError("identity token cannot be empty")
    return quote(text, safe="-._~:")


def canonical_set_id(
    *,
    language: str,
    region: str,
    provider: str,
    provider_set_id: str,
    game: str = GAME,
) -> str:
    canonical_language = canonicalize_language(language)
    return "|".join(
        (
            identity_token(game.casefold()),
            identity_token(canonical_language),
            identity_token(region.upper()),
            f"{identity_token(provider.casefold())}:{identity_token(provider_set_id)}",
        )
    )


def canonical_base_id(
    *,
    language: str,
    region: str,
    canonical_set: str,
    collector_number: str,
    game: str = GAME,
) -> str:
    canonical_language = canonicalize_language(language)
    normalized = normalize_collector_number(collector_number)
    if not normalized:
        raise ValueError("collector_number is required for exact identity")
    set_digest = hashlib.sha256(canonical_set.encode("utf-8")).hexdigest()[:16]
    return "|".join(
        (
            identity_token(game.casefold()),
            identity_token(canonical_language),
            identity_token(region.upper()),
            f"set:{set_digest}",
            identity_token(normalized),
        )
    )


def canonical_printing_id(*, canonical_base: str, variant: str) -> str:
    normalized_variant = identity_token(str(variant or "").strip().casefold())
    return f"{canonical_base}|{normalized_variant}"


def sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_printing_record(
    *,
    source_language: str,
    provider: str,
    provider_set_id: str,
    provider_card_id: str,
    native_set_name: str,
    native_card_name: str,
    collector_number: str,
    official_set_total: int | None,
    release_date: str | None,
    image_url: str | None,
    serie_id: str | None,
    serie_name: str | None,
    variant: str = "unspecified",
) -> dict[str, Any]:
    language = canonicalize_language(source_language, provider=provider)
    definition = LANGUAGE_BY_TAG[language]
    region = definition.default_region
    set_id = canonical_set_id(
        language=language,
        region=region,
        provider=provider,
        provider_set_id=provider_set_id,
    )
    base_id = canonical_base_id(
        language=language,
        region=region,
        canonical_set=set_id,
        collector_number=collector_number,
    )
    printing_id = canonical_printing_id(canonical_base=base_id, variant=variant)
    aliases = sorted(
        {
            item
            for item in (
                native_card_name.strip(),
                native_set_name.strip(),
                str(collector_number).strip(),
                normalize_collector_number(collector_number),
                provider_set_id.strip(),
            )
            if item
        },
        key=lambda item: (item.casefold(), item),
    )
    provenance = {
        "provider": provider,
        "providerCardId": provider_card_id,
        "providerSetId": provider_set_id,
        "sourceLanguage": source_language,
    }
    image_provenance: list[dict[str, Any]] = []
    if image_url:
        source_url = image_url
        image_details: dict[str, Any] = {}
        if provider == "tcgdex":
            image_base = image_url.rstrip("/")
            source_url = f"{image_base}/high.webp"
            image_details = {
                "providerAssetBaseUrl": image_base,
                "thumbSourceUrl": f"{image_base}/low.webp",
                "displaySourceUrl": f"{image_base}/high.webp",
            }
        image_provenance.append(
            {
                "provider": provider,
                "sourceUrl": source_url,
                **image_details,
                "state": "pending_review",
                "rehostPermission": "pending_human_review",
            }
        )
    return {
        "schemaVersion": SCHEMA_VERSION,
        "canonicalPrintingId": printing_id,
        "canonicalBaseId": base_id,
        "canonicalArtworkId": None,
        "game": GAME,
        "language": language,
        "region": region,
        "releaseTerritories": list(definition.release_territories),
        "canonicalSetId": set_id,
        "providerSetIds": {provider: provider_set_id},
        "nativeSetName": native_set_name or provider_set_id,
        "englishSetName": native_set_name if language == "en" else None,
        "printedCollectorNumber": str(collector_number),
        "normalizedCollectorNumber": normalize_collector_number(collector_number),
        "officialSetTotal": official_set_total,
        "cardVariant": variant,
        "rarity": None,
        "releaseDate": release_date,
        "nativeCardName": native_card_name,
        "englishCardName": native_card_name if language == "en" else None,
        "searchAliases": aliases,
        "providerCardIds": {provider: provider_card_id},
        "regulationMark": None,
        "designations": [],
        "imageProvenance": image_provenance,
        "metadataProvenance": [
            {
                **provenance,
                "serieId": serie_id,
                "serieName": serie_name,
            }
        ],
        "verificationState": (
            "provisional_region_multi_variant_unresolved"
            if region == REGION_MULTI
            else "provisional_variant_unresolved"
        ),
        "sourceRecordHash": sha256_json(provenance),
    }


def build_set_record(
    *,
    source_language: str,
    provider: str,
    provider_set_id: str,
    native_set_name: str,
    official_total: int | None,
    total: int | None,
    release_date: str | None,
    serie_id: str | None,
    serie_name: str | None,
) -> dict[str, Any]:
    language = canonicalize_language(source_language, provider=provider)
    definition = LANGUAGE_BY_TAG[language]
    canonical_id = canonical_set_id(
        language=language,
        region=definition.default_region,
        provider=provider,
        provider_set_id=provider_set_id,
    )
    return {
        "schemaVersion": SCHEMA_VERSION,
        "canonicalSetId": canonical_id,
        "game": GAME,
        "language": language,
        "region": definition.default_region,
        "releaseTerritories": list(definition.release_territories),
        "providerSetIds": {provider: provider_set_id},
        "nativeSetName": native_set_name or provider_set_id,
        "englishSetName": native_set_name if language == "en" else None,
        "officialSetTotal": official_total,
        "total": total,
        "releaseDate": release_date,
        "providerSeriesId": serie_id,
        "nativeSeriesName": serie_name,
        "searchAliases": sorted({provider_set_id, native_set_name} - {""}),
        "metadataProvenance": [{"provider": provider, "providerSetId": provider_set_id}],
        "verificationState": (
            "provider_verified_region_multi"
            if definition.default_region == REGION_MULTI
            else "provider_verified"
        ),
    }


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    replace_file_with_retry(temporary, path)


def write_jsonl_atomic(path: Path, rows: Iterable[dict[str, Any]]) -> tuple[int, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    digest = hashlib.sha256()
    count = 0
    with temporary.open("wb") as handle:
        for row in rows:
            line = (
                json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode("utf-8")
            handle.write(line)
            digest.update(line)
            count += 1
    replace_file_with_retry(temporary, path)
    return count, digest.hexdigest()


def replace_file_with_retry(
    temporary: Path,
    destination: Path,
    *,
    attempts: int = 8,
) -> None:
    for attempt in range(attempts):
        try:
            temporary.replace(destination)
            return
        except PermissionError:
            if attempt + 1 >= attempts:
                raise
            time.sleep(min(0.5, 0.025 * (2**attempt)))


def language_registry_payload() -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "standard": "BCP-47-compatible application tags with region stored separately",
        "languages": [item.to_dict() for item in LANGUAGE_DEFINITIONS],
        "ambiguousLegacyAliases": [
            {"alias": alias, "reason": reason, "automaticMapping": None}
            for alias, reason in sorted(AMBIGUOUS_LEGACY_LANGUAGES.items())
        ],
        "migrationPolicy": {
            "destructiveRewrite": False,
            "retainLegacyIds": True,
            "aliasTableRequired": True,
        },
    }

