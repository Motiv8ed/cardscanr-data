#!/usr/bin/env python3
"""Extract physical-printing descriptors from provider card names (V5.2).

Descriptors are accepted only as known provider *suffix* structures after an
optional embedded collector token — never as generic mid-title word matches.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from . import normalize_card_name, parse_collector_number

# Longest-first suffix descriptor patterns (trailing metadata only).
_SUFFIX_DESCRIPTOR_SPECS: tuple[tuple[str, str, str], ...] = (
    (r"prerelease\s+staff", "prerelease_staff", "stampType"),
    (r"worlds\s+staff", "worlds_staff", "stampType"),
    (r"league\s+winner", "league_winner", "stampType"),
    (r"pokemon\s+center\s+exclusive", "pokemon_center_exclusive", "stampType"),
    (r"pok[eé]mon\s+center\s+exclusive", "pokemon_center_exclusive", "stampType"),
    (r"cracked\s+ice\s+holo(?:foil)?", "cracked_ice_holo", "printingClass"),
    (r"cosmos?\s+holo(?:foil)?", "cosmos_holo", "printingClass"),
    (r"cosmo\s+holo(?:foil)?", "cosmos_holo", "printingClass"),
    (r"reverse\s+holo(?:foil)?", "reverse_holo", "printingClass"),
    (r"target\s+non[\s-]?holo(?:foil)?", "target_non_holo", "stampType"),
    (r"non[\s-]?holo(?:foil)?", "non_holo", "printingClass"),
    (r"1st\s+edition", "first_edition", "printingClass"),
    (r"first\s+edition", "first_edition", "printingClass"),
    (r"prerelease", "prerelease", "printingClass"),
    (r"shadowless", "shadowless", "printingClass"),
    (r"stamped", "stamped", "stampType"),
    (r"staff", "staff", "stampType"),
    (r"target", "target", "stampType"),
    (r"championship", "championship", "stampType"),
)

_SUFFIX_DESCRIPTOR_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = tuple(
    (re.compile(rf"^(?P<head>.+?)\s+(?P<desc>{pattern})$", re.I), token, field_key)
    for pattern, token, field_key in _SUFFIX_DESCRIPTOR_SPECS
)

_TRAILING_COLLECTOR = re.compile(
    r"^(?P<head>.+?)\s+(?P<collector>[A-Za-z]{2,6}\d+[A-Za-z]?)$"
)

_GENERIC_SINGLE_TOKENS = frozenset(
    {
        "staff",
        "target",
        "league",
        "championship",
        "exclusive",
        "stamped",
    }
)

_EXACT_CARD_TITLES_NO_DESCRIPTOR = frozenset(
    {
        "target whistle",
        "pokemon league headquarters",
        "pokémon league headquarters",
        "league staff",
        "league center trainer",
        "champions festival 2015",
        "champions festival 2016",
        "champions festival 2017",
        "champions festival 2018",
        "champions festival 2019",
        "champions festival 2022",
    }
)


@dataclass(frozen=True)
class ProviderPrintingDescriptorParse:
    raw_name: str
    base_card_name: str
    collector_from_name: str | None
    printing_descriptors: tuple[str, ...]
    descriptor_fields: dict[str, list[str]]
    variant_signature: str
    meaningful_physical_descriptor: bool
    ambiguous_descriptor: bool
    descriptor_in_card_title: bool
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _build_variant_signature(
    tokens: tuple[str, ...],
    fields: dict[str, list[str]],
) -> str:
    if not tokens:
        return "normal"
    parts: list[str] = []
    for key in sorted(fields):
        values = sorted(set(fields[key]))
        parts.append(f"{key}:{','.join(values)}")
    mapped = {value for values in fields.values() for value in values}
    extras = [token for token in tokens if token not in mapped]
    if extras:
        parts.append(f"descriptor:{','.join(sorted(extras))}")
    return "|".join(parts) if parts else "normal"


def _peel_suffix_descriptors(text: str) -> tuple[str, list[str], dict[str, list[str]]]:
    """Peel known descriptor suffixes from the end only (repeatable)."""
    remaining = text.strip()
    tokens_rev: list[str] = []
    fields: dict[str, list[str]] = {}
    progressed = True
    while progressed and remaining:
        progressed = False
        for pattern, token, field_key in _SUFFIX_DESCRIPTOR_PATTERNS:
            match = pattern.match(remaining)
            if not match:
                continue
            head = match.group("head").strip()
            if not head:
                continue
            remaining = head
            tokens_rev.append(token)
            fields.setdefault(field_key, [])
            if token not in fields[field_key]:
                fields[field_key].append(token)
            progressed = True
            break
    tokens = list(reversed(tokens_rev))
    for key in fields:
        fields[key] = list(reversed(fields[key]))
    return remaining, tokens, fields


def _strip_trailing_collector(text: str) -> tuple[str, str | None]:
    match = _TRAILING_COLLECTOR.match(text.strip())
    if not match:
        return text.strip(), None
    candidate = match.group("collector").strip()
    if not parse_collector_number(candidate).parse_ok:
        return text.strip(), None
    return match.group("head").strip(), candidate


def parse_provider_printing_descriptors(
    raw_name: str,
    *,
    collector_number: str | None = None,
    exact_canonical_titles: set[str] | None = None,
) -> ProviderPrintingDescriptorParse:
    """Parse provider card name into base name and trailing printing descriptors."""
    raw = str(raw_name or "").strip()
    normalized_full = normalize_card_name(raw).strip()
    title_blocklist = set(_EXACT_CARD_TITLES_NO_DESCRIPTOR)
    if exact_canonical_titles:
        title_blocklist |= {
            normalize_card_name(title).strip() for title in exact_canonical_titles
        }

    if normalized_full in title_blocklist:
        return ProviderPrintingDescriptorParse(
            raw_name=raw,
            base_card_name=normalized_full,
            collector_from_name=(
                str(collector_number).strip() if collector_number else None
            ),
            printing_descriptors=(),
            descriptor_fields={},
            variant_signature="normal",
            meaningful_physical_descriptor=False,
            ambiguous_descriptor=False,
            descriptor_in_card_title=True,
            evidence={
                "reason": "exact_card_title_no_descriptor",
                "normalizedTitle": normalized_full,
            },
        )

    # 1) Peel trailing known descriptor suffixes from the full provider name.
    after_desc, tokens, fields = _peel_suffix_descriptors(raw)
    # 2) Then strip an embedded trailing collector token from the remainder.
    base_raw, embedded_collector = _strip_trailing_collector(after_desc)
    base_name = normalize_card_name(base_raw).strip()

    if not tokens:
        return ProviderPrintingDescriptorParse(
            raw_name=raw,
            base_card_name=normalized_full,
            collector_from_name=(
                embedded_collector
                or (str(collector_number).strip() if collector_number else None)
            ),
            printing_descriptors=(),
            descriptor_fields={},
            variant_signature="normal",
            meaningful_physical_descriptor=False,
            ambiguous_descriptor=False,
            descriptor_in_card_title=False,
            evidence={
                "reason": "no_structured_suffix_descriptor",
                "normalizedTitle": normalized_full,
            },
        )

    if not base_name:
        return ProviderPrintingDescriptorParse(
            raw_name=raw,
            base_card_name=normalized_full,
            collector_from_name=(
                embedded_collector
                or (str(collector_number).strip() if collector_number else None)
            ),
            printing_descriptors=(),
            descriptor_fields={},
            variant_signature="normal",
            meaningful_physical_descriptor=False,
            ambiguous_descriptor=True,
            descriptor_in_card_title=False,
            evidence={"reason": "suffix_peel_left_empty_base"},
        )

    # Structured gate: require collector embedding OR a compound descriptor.
    # Reject bare titles like "League Staff" / lone "Target …" card names.
    compound = any("_" in token for token in tokens)
    has_collector = bool(embedded_collector) or bool(str(collector_number or "").strip())
    meaningful = True
    if not has_collector and not compound:
        if len(tokens) == 1 and tokens[0] in _GENERIC_SINGLE_TOKENS:
            meaningful = False

    if not meaningful:
        return ProviderPrintingDescriptorParse(
            raw_name=raw,
            base_card_name=normalized_full,
            collector_from_name=(
                str(collector_number).strip() if collector_number else None
            ),
            printing_descriptors=(),
            descriptor_fields={},
            variant_signature="normal",
            meaningful_physical_descriptor=False,
            ambiguous_descriptor=False,
            descriptor_in_card_title=False,
            evidence={
                "reason": "generic_trailing_token_without_collector_structure",
                "rejectedTokens": tokens,
            },
        )

    token_tuple = tuple(tokens)
    return ProviderPrintingDescriptorParse(
        raw_name=raw,
        base_card_name=base_name,
        collector_from_name=(
            embedded_collector
            or (str(collector_number).strip() if collector_number else None)
        ),
        printing_descriptors=token_tuple,
        descriptor_fields=fields,
        variant_signature=_build_variant_signature(token_tuple, fields),
        meaningful_physical_descriptor=True,
        ambiguous_descriptor=False,
        descriptor_in_card_title=False,
        evidence={
            "embeddedCollectorFromName": embedded_collector,
            "normalizedTitle": normalized_full,
            "parseMode": "suffix_structure_v5_2",
        },
    )


def canonical_has_descriptor_coverage(
    canonical_card: dict[str, Any] | None,
    parsed: ProviderPrintingDescriptorParse,
) -> bool:
    """Return True when canonical metadata already covers provider descriptors."""
    if not parsed.meaningful_physical_descriptor:
        return True
    card = canonical_card or {}
    canonical_text = " ".join(
        str(card.get(key) or "")
        for key in (
            "name",
            "variant",
            "variantType",
            "finish",
            "raritySubtype",
            "subset",
            "printingClass",
            "stampType",
            "productVariant",
        )
    ).casefold()
    for token in parsed.printing_descriptors:
        needle = token.replace("_", " ")
        if needle not in canonical_text and token not in canonical_text:
            return False
    return True


def supplemental_classification_for_descriptor_match(
    *,
    mapping_evidence: str,
    card_evidence: str,
) -> tuple[str, str]:
    """Choose supplemental class when set/collector/base match but descriptors differ."""
    if mapping_evidence == "PROVEN" and card_evidence == "PROVEN":
        return "SUPPLEMENTAL_PHYSICAL_PRINTING_CANDIDATE", "PROVEN"
    if mapping_evidence in {"PROVEN", "STRONG_EVIDENCE"} and card_evidence in {
        "PROVEN",
        "STRONG_EVIDENCE",
    }:
        return "PHYSICAL_VARIANT_AUTHORITY_PENDING", "STRONG_EVIDENCE"
    return "PHYSICAL_VARIANT_AUTHORITY_PENDING", "UNRESOLVED"
