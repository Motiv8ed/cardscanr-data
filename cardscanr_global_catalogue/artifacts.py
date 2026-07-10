from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .contracts import (
    AMBIGUOUS_LEGACY_LANGUAGES,
    LANGUAGE_DEFINITIONS,
    SCHEMA_VERSION,
    write_json_atomic,
)


ROOT = Path(__file__).resolve().parent.parent


CANONICAL_PRINTING_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://data.cardscanr.app/schemas/canonical-pokemon-printing-v1.json",
    "title": "CardScanR canonical Pokémon TCG printing",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schemaVersion",
        "canonicalPrintingId",
        "canonicalBaseId",
        "canonicalArtworkId",
        "game",
        "language",
        "region",
        "releaseTerritories",
        "canonicalSetId",
        "providerSetIds",
        "nativeSetName",
        "englishSetName",
        "printedCollectorNumber",
        "normalizedCollectorNumber",
        "officialSetTotal",
        "cardVariant",
        "rarity",
        "releaseDate",
        "nativeCardName",
        "englishCardName",
        "searchAliases",
        "providerCardIds",
        "regulationMark",
        "designations",
        "imageProvenance",
        "metadataProvenance",
        "verificationState",
        "sourceRecordHash",
    ],
    "properties": {
        "schemaVersion": {"const": SCHEMA_VERSION},
        "canonicalPrintingId": {"type": "string", "minLength": 1},
        "canonicalBaseId": {"type": "string", "minLength": 1},
        "canonicalArtworkId": {
            "oneOf": [{"type": "string", "minLength": 1}, {"type": "null"}]
        },
        "game": {"const": "pokemon"},
        "language": {
            "enum": [definition.language for definition in LANGUAGE_DEFINITIONS]
        },
        "region": {"type": "string", "pattern": "^[A-Z][A-Z0-9_-]*$"},
        "releaseTerritories": {
            "type": "array",
            "items": {"type": "string", "pattern": "^[A-Z][A-Z0-9_-]*$"},
            "uniqueItems": True,
        },
        "canonicalSetId": {"type": "string", "minLength": 1},
        "providerSetIds": {"$ref": "#/$defs/providerIdMap"},
        "nativeSetName": {"type": "string", "minLength": 1},
        "englishSetName": {
            "oneOf": [{"type": "string", "minLength": 1}, {"type": "null"}]
        },
        "printedCollectorNumber": {"type": "string", "minLength": 1},
        "normalizedCollectorNumber": {"type": "string", "minLength": 1},
        "officialSetTotal": {
            "oneOf": [{"type": "integer", "minimum": 0}, {"type": "null"}]
        },
        "cardVariant": {"type": "string", "minLength": 1},
        "rarity": {
            "oneOf": [{"type": "string", "minLength": 1}, {"type": "null"}]
        },
        "releaseDate": {
            "oneOf": [
                {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
                {"type": "null"},
            ]
        },
        "nativeCardName": {"type": "string", "minLength": 1},
        "englishCardName": {
            "oneOf": [{"type": "string", "minLength": 1}, {"type": "null"}]
        },
        "searchAliases": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "uniqueItems": True,
        },
        "providerCardIds": {"$ref": "#/$defs/providerIdMap"},
        "regulationMark": {
            "oneOf": [{"type": "string", "minLength": 1}, {"type": "null"}]
        },
        "designations": {
            "type": "array",
            "items": {
                "enum": [
                    "promo",
                    "stamped",
                    "reverse_holo",
                    "holo",
                    "non_holo",
                    "first_edition",
                    "unlimited",
                    "other",
                ]
            },
            "uniqueItems": True,
        },
        "imageProvenance": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["provider", "sourceUrl", "state", "rehostPermission"],
                "properties": {
                    "provider": {"type": "string", "minLength": 1},
                    "sourceUrl": {"type": "string", "format": "uri"},
                    "state": {
                        "enum": [
                            "verified_r2",
                            "verified_public_provider",
                            "provider_auth_required",
                            "provider_rate_limited",
                            "provider_unavailable",
                            "source_http_404",
                            "source_http_401",
                            "source_http_403",
                            "identity_ambiguous",
                            "missing_image",
                            "pending_review",
                            "rejected_mismatch",
                            "legal_review_required",
                        ]
                    },
                    "rehostPermission": {"type": "string", "minLength": 1},
                },
                "additionalProperties": True,
            },
        },
        "metadataProvenance": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["provider", "providerCardId", "providerSetId"],
                "properties": {
                    "provider": {"type": "string", "minLength": 1},
                    "providerCardId": {"type": "string", "minLength": 1},
                    "providerSetId": {"type": "string", "minLength": 1},
                    "sourceLanguage": {"type": "string", "minLength": 1},
                },
                "additionalProperties": True,
            },
        },
        "verificationState": {"type": "string", "minLength": 1},
        "sourceRecordHash": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$",
        },
    },
    "$defs": {
        "providerIdMap": {
            "type": "object",
            "minProperties": 1,
            "additionalProperties": {"type": "string", "minLength": 1},
        }
    },
}


LANGUAGE_CONTRACT_MARKDOWN = """# Global Language and Region Contract

This contract uses canonical BCP-47-compatible language tags while storing release region separately.

## Rules

- `language` describes the printed text/script.
- `region` describes the release market only when the provider proves it.
- `releaseTerritories` lists known territories without claiming a single exact market.
- A provider language is never used as proof of region when that provider combines markets.
- Legacy IDs are retained in the provider crosswalk. Existing public IDs are not rewritten in place.
- `zh-Hant` from TCGdex remains `region=MULTI` with territories `TW` and `HK`; it must not be guessed as either market.
- `en` remains `region=GLOBAL` until stronger release evidence exists.

## Reversible aliases

- `jp` and `jap` map to `ja`.
- `zh-cn` and `chs` map to `zh-Hans`.
- `zh-tw` and `cht` map to `zh-Hant`.
- `kr` maps to `ko`.
- `es-mx` maps to `es-419`, the Unicode/BCP-47 Latin America macroregion.
- `pt-br` maps to `pt-BR`.
- `pt-pt` maps to `pt-PT`, even when the current provider exposes no set.

The values `zh`, `chn`, `chi`, `zho`, and `pt` are deliberately ambiguous and require source evidence.

## Region separation

Canonical set and printing identities include both language and region. Two records with the same translated name,
collector number, or artwork are not merged across regions. A later region split is represented as a reversible
crosswalk from the provisional `MULTI` identity; it is never a destructive rewrite.
"""


IDENTITY_CONTRACT_MARKDOWN = """# Global Canonical Printing Identity Contract

Every catalogue row represents one exact physical printing. Identity is not inferred from a card name or artwork.

## Identity hierarchy

- `canonicalSetId`: game + language + evidenced region + canonical provider set identity.
- `canonicalBaseId`: game + language + region + canonical set digest + normalized collector number.
- `canonicalPrintingId`: canonical base + evidenced physical variant.
- `canonicalArtworkId`: populated only when artwork equivalence is independently proven; otherwise `null`.

The current TCGdex set-level ingestion uses `cardVariant=unspecified`, so records remain provisional rather than
claiming that holo, reverse-holo, stamped, promo, first-edition, and other printings have been fully separated.

## Required evidence

Exact identity requires language, region where relevant, set identity, collector number, and variant evidence.
Name-only, Pokémon-only, visually similar artwork, translated set names, and matching collector numbers in another
set are never sufficient.

## Provenance and confidence

Provider card/set IDs and source language are retained. Stronger verified facts may enrich a record; weaker data
cannot overwrite them. Conflicts and one-to-many crosswalks are quarantined. `canonicalArtworkId`, rarity,
regulation mark, designation, and English aliases remain null or empty when the set response does not prove them.

## Identifier stability

Identifiers are deterministic and percent-escaped. Existing app identifiers remain available in
`provider_crosswalk.jsonl`; no production identifier is rewritten by this staging rollout.

## Image safety

An image candidate is not a verified image. Artwork is wired only after exact identity, terms, download validation,
normalization, immutable R2 upload, and object verification all pass. Provider URLs remain internal provenance.
"""


def language_registry_payload() -> dict[str, Any]:
    reversible_aliases: list[dict[str, str]] = []
    for definition in LANGUAGE_DEFINITIONS:
        for alias in definition.legacy_aliases:
            reversible_aliases.append(
                {
                    "legacyValue": alias,
                    "canonicalLanguage": definition.language,
                    "migration": "alias_only_no_destructive_rewrite",
                }
            )
    return {
        "schemaVersion": "1.0.0",
        "contract": "BCP-47-compatible language plus separate release region",
        "languages": [definition.to_dict() for definition in LANGUAGE_DEFINITIONS],
        "reversibleAliasMigration": sorted(
            reversible_aliases,
            key=lambda item: item["legacyValue"].casefold(),
        ),
        "ambiguousLegacyValues": [
            {"value": value, "reason": reason, "automaticMapping": None}
            for value, reason in sorted(AMBIGUOUS_LEGACY_LANGUAGES.items())
        ],
    }


def provider_credentials_example() -> dict[str, Any]:
    return {
        "_warning": "Place real values only in ignored config/provider_credentials.local.json. Never commit secrets.",
        "providers": {
            "pokemon_tcg_api": {"POKEMON_TCG_API_KEY": "<optional-free-api-key>"},
            "pokewallet": {"POKEWALLET_API_KEY": "<required-api-key>"},
            "scrydex": {
                "SCRYDEX_API_KEY": "<paid-api-key>",
                "SCRYDEX_TEAM_ID": "<team-id>",
            },
            "ximilar": {"XIMILAR_API_TOKEN": "<recognition-only-token>"},
        },
    }


def budget_example() -> dict[str, Any]:
    return {
        "schemaVersion": "1.0.0",
        "currency": "USD",
        "maximumPaidProviderSpend": 0,
        "maximumUnexpectedCloudflareSpend": 0,
        "allowPaidApiSubscription": False,
        "allowProviderOverage": False,
        "allowOriginalSourceImageArchive": False,
        "initialImageBatchSize": 500,
        "canaryCardsPerLanguage": 100,
        "r2": {
            "maximumWritesBeforeApproval": 0,
            "maximumNewStorageBytesBeforeApproval": 0,
        },
        "notes": [
            "Raise limits only after a written user approval.",
            "Metadata caching from an approved free provider does not consume this image/R2 budget.",
        ],
    }


def write_contract_artifacts() -> dict[str, str]:
    paths = {
        "languageRegistry": ROOT / "data" / "contracts" / "language_region_registry.json",
        "printingSchema": ROOT / "data" / "contracts" / "canonical_printing_schema.json",
        "languageContract": ROOT / "docs" / "global_language_region_contract.md",
        "identityContract": ROOT / "docs" / "global_catalogue_identity_contract.md",
        "credentialExample": ROOT / "config" / "provider_credentials.example.json",
        "budgetExample": ROOT / "config" / "global_rollout_budget.example.json",
    }
    write_json_atomic(paths["languageRegistry"], language_registry_payload())
    write_json_atomic(paths["printingSchema"], CANONICAL_PRINTING_SCHEMA)
    write_json_atomic(paths["credentialExample"], provider_credentials_example())
    write_json_atomic(paths["budgetExample"], budget_example())
    paths["languageContract"].write_text(LANGUAGE_CONTRACT_MARKDOWN, encoding="utf-8")
    paths["identityContract"].write_text(IDENTITY_CONTRACT_MARKDOWN, encoding="utf-8")
    return {
        key: path.relative_to(ROOT).as_posix()
        for key, path in paths.items()
    }


def render_credentials_example() -> str:
    return json.dumps(provider_credentials_example(), indent=2, ensure_ascii=False) + "\n"

