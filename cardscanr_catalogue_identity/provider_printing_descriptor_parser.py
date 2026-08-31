#!/usr/bin/env python3
"""Structured provider card-name parser for physical-printing descriptors (V5.3).

Understands:
  CARD_NAME [COLLECTOR] [SET/PRODUCT CONTEXT] [EVENT CONTEXT] [PHYSICAL DESCRIPTORS]

Descriptors and contexts are peeled as trailing structures only. Legitimate card
titles such as Target Whistle / League Staff never receive false descriptors.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from . import normalize_card_name, parse_collector_number

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

# Event / championship contexts (longest first). Captured separately from base name.
_EVENT_CONTEXT_SPECS: tuple[tuple[str, str], ...] = (
    (r"world\s+championships?\s+20\d{2}", "world_championships"),
    (r"worlds?\s+20\d{2}", "worlds"),
    (r"worlds?\s+\d{2}\b", "worlds"),  # Worlds 11 → 2011-style short year
    (r"national\s+championships?", "national_championships"),
    (r"regional\s+championships?", "regional_championships"),
    (r"state\s+championships?", "state_championships"),
    (r"city\s+championships?", "city_championships"),
    (r"origins?\s+game\s+fair", "origins_game_fair"),
    (r"team\s+plasma", "team_plasma"),
    (r"eb\s+games", "eb_games"),
)

_EVENT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(rf"^(?P<head>.+?)\s+(?P<ctx>{pattern})$", re.I), token)
    for pattern, token in _EVENT_CONTEXT_SPECS
)

# Product/set context fragments often left after collector strip.
_PRODUCT_CONTEXT_SPECS: tuple[tuple[str, str], ...] = (
    (r"sm\s+unbroken\s+bonds", "sm_unbroken_bonds"),
    (r"sm\s+unified\s+minds", "sm_unified_minds"),
    (r"sm\s+cosmic\s+eclipse", "sm_cosmic_eclipse"),
    (r"xy\s+evolutions", "xy_evolutions"),
    (r"black\s+and\s+white", "black_and_white"),
    (r"sword\s+(?:&|and)\s+shield", "sword_and_shield"),
    (r"scarlet\s+(?:&|and)\s+violet", "scarlet_and_violet"),
)

_PRODUCT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(rf"^(?P<head>.+?)\s+(?P<ctx>{pattern})$", re.I), token)
    for pattern, token in _PRODUCT_CONTEXT_SPECS
)

_PROMO_COLLECTOR = re.compile(r"^(?P<head>.+?)\s+(?P<collector>[A-Za-z]{2,6}\d+[A-Za-z]?)$")
_SPACED_FRACTION = re.compile(
    r"^(?P<head>.+?)\s+(?P<n1>\d{1,3})\s+(?P<n2>\d{2,3})$"
)
_SLASH_FRACTION = re.compile(
    r"^(?P<head>.+?)\s+(?P<collector>\d{1,3}/\d{2,3})$"
)

_GENERIC_SINGLE_TOKENS = frozenset(
    {"staff", "target", "league", "championship", "exclusive", "stamped"}
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
    event_context: tuple[str, ...] = ()
    product_context: tuple[str, ...] = ()
    base_name_parse_clean: bool = False
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


def _peel_suffix(
    text: str,
    patterns: tuple[tuple[re.Pattern[str], str], ...],
) -> tuple[str, list[str]]:
    remaining = text.strip()
    found_rev: list[str] = []
    progressed = True
    while progressed and remaining:
        progressed = False
        for item in patterns:
            pattern = item[0]
            token = item[1]
            match = pattern.match(remaining)
            if not match:
                continue
            head = match.group("head").strip()
            if not head:
                continue
            remaining = head
            found_rev.append(token)
            progressed = True
            break
    return remaining, list(reversed(found_rev))


def _peel_suffix_descriptors(text: str) -> tuple[str, list[str], dict[str, list[str]]]:
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


def _normalize_worlds_short_year(raw: str, token: str) -> str:
    """Map 'Worlds 11' → worlds_2011 style token when short year is present."""
    if token != "worlds":
        return token
    match = re.search(r"worlds?\s+(\d{2})\b", raw, re.I)
    if match and not re.search(r"worlds?\s+20\d{2}", raw, re.I):
        year = int(match.group(1))
        return f"worlds_20{year:02d}"
    match = re.search(r"worlds?\s+(20\d{2})\b", raw, re.I)
    if match:
        return f"worlds_{match.group(1)}"
    match = re.search(r"world\s+championships?\s+(20\d{2})", raw, re.I)
    if match:
        return f"world_championships_{match.group(1)}"
    return token


def _strip_collector(
    text: str,
    *,
    collector_number: str | None,
) -> tuple[str, str | None]:
    remaining = text.strip()
    supplied = str(collector_number or "").strip()

    # Prefer supplied collector as strong evidence for spaced fractions.
    if supplied:
        slash = supplied.replace(" ", "/")
        parsed = parse_collector_number(slash if "/" in slash else supplied)
        if parsed.parse_ok:
            # "Zeraora 60 214 ..." with collector 60/214
            spaced = _SPACED_FRACTION.match(remaining)
            if spaced and parsed.numerator is not None and parsed.denominator is not None:
                if (
                    int(spaced.group("n1")) == parsed.numerator
                    and int(spaced.group("n2")) == parsed.denominator
                ):
                    return spaced.group("head").strip(), (
                        f"{parsed.numerator}/{parsed.denominator}"
                    )
            slash_m = _SLASH_FRACTION.match(remaining)
            if slash_m:
                left = parse_collector_number(slash_m.group("collector"))
                if (
                    left.parse_ok
                    and left.numerator == parsed.numerator
                    and left.denominator == parsed.denominator
                ):
                    return slash_m.group("head").strip(), slash_m.group("collector")
            promo = _PROMO_COLLECTOR.match(remaining)
            if promo and parse_collector_number(promo.group("collector")).parse_ok:
                cand = promo.group("collector")
                if cand.casefold() == supplied.casefold() or cand.casefold() == (
                    parsed.prefix or ""
                ).casefold() + str(parsed.numerator):
                    return promo.group("head").strip(), cand

    spaced = _SPACED_FRACTION.match(remaining)
    if spaced:
        collector = f"{int(spaced.group('n1'))}/{int(spaced.group('n2'))}"
        if parse_collector_number(collector).parse_ok:
            return spaced.group("head").strip(), collector

    slash_m = _SLASH_FRACTION.match(remaining)
    if slash_m and parse_collector_number(slash_m.group("collector")).parse_ok:
        return slash_m.group("head").strip(), slash_m.group("collector")

    promo = _PROMO_COLLECTOR.match(remaining)
    if promo and parse_collector_number(promo.group("collector")).parse_ok:
        return promo.group("head").strip(), promo.group("collector")

    return remaining, None


def _looks_clean_base(base: str) -> bool:
    text = base.strip()
    if not text:
        return False
    if re.search(r"\b\d{1,3}\s+\d{2,3}\b", text):
        return False
    if re.search(r"\b[A-Za-z]{2,6}\d+[A-Za-z]?\b", text):
        return False
    if re.search(
        r"\b(worlds?|championships?|city|state|national|regional|unbroken bonds)\b",
        text,
        re.I,
    ):
        return False
    return True


def parse_provider_printing_descriptors(
    raw_name: str,
    *,
    collector_number: str | None = None,
    exact_canonical_titles: set[str] | None = None,
) -> ProviderPrintingDescriptorParse:
    """Parse provider card name into base name, contexts, and descriptors."""
    raw = str(raw_name or "").strip()
    normalized_full = normalize_card_name(raw).strip()
    title_blocklist = set(_EXACT_CARD_TITLES_NO_DESCRIPTOR)
    if exact_canonical_titles:
        title_blocklist |= {
            normalize_card_name(title).strip() for title in exact_canonical_titles
        }

    empty = ProviderPrintingDescriptorParse(
        raw_name=raw,
        base_card_name=normalized_full,
        collector_from_name=(str(collector_number).strip() if collector_number else None),
        printing_descriptors=(),
        descriptor_fields={},
        variant_signature="normal",
        meaningful_physical_descriptor=False,
        ambiguous_descriptor=False,
        descriptor_in_card_title=False,
        event_context=(),
        product_context=(),
        base_name_parse_clean=False,
        evidence={"normalizedTitle": normalized_full},
    )

    if normalized_full in title_blocklist:
        return ProviderPrintingDescriptorParse(
            **{
                **empty.to_dict(),
                "descriptor_in_card_title": True,
                "evidence": {
                    "reason": "exact_card_title_no_descriptor",
                    "normalizedTitle": normalized_full,
                },
            }
        )

    after_desc, tokens, fields = _peel_suffix_descriptors(raw)
    after_event, events = _peel_suffix(after_desc, _EVENT_PATTERNS)
    events = [_normalize_worlds_short_year(raw, token) for token in events]
    after_product, products = _peel_suffix(after_event, _PRODUCT_PATTERNS)
    base_raw, embedded_collector = _strip_collector(
        after_product, collector_number=collector_number
    )
    # Sometimes collector sits before event/product; try again on earlier remainder.
    if embedded_collector is None:
        base_raw2, embedded_collector = _strip_collector(
            after_desc, collector_number=collector_number
        )
        if embedded_collector is not None:
            # Re-peel contexts from the collector-stripped remainder.
            after_event, events = _peel_suffix(base_raw2, _EVENT_PATTERNS)
            events = [_normalize_worlds_short_year(raw, token) for token in events]
            after_product, products = _peel_suffix(after_event, _PRODUCT_PATTERNS)
            base_raw = after_product

    base_name = normalize_card_name(base_raw).strip()

    if not tokens:
        return ProviderPrintingDescriptorParse(
            **{
                **empty.to_dict(),
                "evidence": {
                    "reason": "no_structured_suffix_descriptor",
                    "normalizedTitle": normalized_full,
                },
            }
        )

    if not base_name:
        return ProviderPrintingDescriptorParse(
            **{
                **empty.to_dict(),
                "ambiguous_descriptor": True,
                "evidence": {"reason": "suffix_peel_left_empty_base"},
            }
        )

    compound = any("_" in token for token in tokens)
    has_collector = bool(embedded_collector) or bool(str(collector_number or "").strip())
    meaningful = True
    if not has_collector and not compound and not events and not products:
        if len(tokens) == 1 and tokens[0] in _GENERIC_SINGLE_TOKENS:
            meaningful = False

    if not meaningful:
        return ProviderPrintingDescriptorParse(
            **{
                **empty.to_dict(),
                "evidence": {
                    "reason": "generic_trailing_token_without_collector_structure",
                    "rejectedTokens": tokens,
                },
            }
        )

    token_tuple = tuple(tokens)
    clean = _looks_clean_base(base_name)
    ambiguous = not clean
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
        ambiguous_descriptor=ambiguous,
        descriptor_in_card_title=False,
        event_context=tuple(events),
        product_context=tuple(products),
        base_name_parse_clean=clean,
        evidence={
            "embeddedCollectorFromName": embedded_collector,
            "normalizedTitle": normalized_full,
            "parseMode": "structured_provider_name_v5_3",
            "eventContext": list(events),
            "productContext": list(products),
        },
    )


def canonical_has_descriptor_coverage(
    canonical_card: dict[str, Any] | None,
    parsed: ProviderPrintingDescriptorParse,
) -> bool:
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
    if mapping_evidence == "PROVEN" and card_evidence == "PROVEN":
        return "SUPPLEMENTAL_PHYSICAL_PRINTING_CANDIDATE", "PROVEN"
    if mapping_evidence in {"PROVEN", "STRONG_EVIDENCE"} and card_evidence in {
        "PROVEN",
        "STRONG_EVIDENCE",
    }:
        return "PHYSICAL_VARIANT_AUTHORITY_PENDING", "STRONG_EVIDENCE"
    return "PHYSICAL_VARIANT_AUTHORITY_PENDING", "UNRESOLVED"
