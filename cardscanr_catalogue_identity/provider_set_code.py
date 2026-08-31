#!/usr/bin/env python3
"""Structured provider set-code parsing — no destructive generic prefix stripping."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Provider header: SWSH08: Fusion Strike, SV02: Paldea Evolved, SM Ultra Prism (no colon variant handled separately)
_PROVIDER_HEADER = re.compile(
    r"^(?P<code>(?:SWSH|SV|SM|XY|BW|ME|SWSH|SVE)\d+)\s*:\s*(?P<name>.+)$",
    re.I,
)

# Trainer Gallery subset detection
_TRAINER_GALLERY = re.compile(r"trainer\s+gallery", re.I)

# Explicit era+name without numeric code (must NOT collapse to Base Set -> base1)
_ERA_BASE_SET = re.compile(
    r"^(?P<era>SM|XY|SV|SWSH|BW)\s+(?:-\s*)?Base\s+Set$",
    re.I,
)


@dataclass(frozen=True)
class ProviderSetParsed:
    original: str
    provider_code: str | None
    display_name: str
    subset_label: str | None
    parse_ok: bool
    reason: str


def parse_provider_set_name(raw: str) -> ProviderSetParsed:
    text = str(raw or "").strip()
    if not text:
        return ProviderSetParsed("", None, "", None, False, "empty")

    m = _PROVIDER_HEADER.fullmatch(text)
    if m:
        code = m.group("code").upper()
        name = m.group("name").strip()
        subset = None
        if _TRAINER_GALLERY.search(name):
            subset = "Trainer Gallery"
            # strip trailing subset from display for main-set name compare
            name = _TRAINER_GALLERY.sub("", name).strip(" -")
        return ProviderSetParsed(
            original=text,
            provider_code=code,
            display_name=name,
            subset_label=subset,
            parse_ok=True,
            reason="provider_code_colon_name",
        )

    # Era base set without numeric code — e.g. "SM Base Set"
    eb = _ERA_BASE_SET.fullmatch(text)
    if eb:
        era = eb.group("era").upper()
        return ProviderSetParsed(
            original=text,
            provider_code=era,
            display_name="Base Set",
            subset_label=None,
            parse_ok=True,
            reason="era_base_set_without_numeric_code",
        )

    return ProviderSetParsed(
        original=text,
        provider_code=None,
        display_name=text,
        subset_label=None,
        parse_ok=False,
        reason="unparsed_provider_set_name",
    )


def _swsh_numeric_to_set_id(num: int, *, trainer_gallery: bool) -> str:
    if trainer_gallery:
        return f"swsh{num}tg"
    return f"swsh{num}"


def _sv_numeric_to_set_id(num: int) -> str:
    return f"sv{num}"


def resolve_provider_code_to_canonical(parsed: ProviderSetParsed) -> dict | None:
    """Map structured provider parse to canonical CardScanR set id when deterministic."""
    if not parsed.parse_ok:
        return None

    code = (parsed.provider_code or "").upper()
    tg = parsed.subset_label == "Trainer Gallery"

    # SWSH08, SWSH01, etc.
    m = re.fullmatch(r"SWSH(\d+)", code)
    if m:
        num = int(m.group(1))
        target = _swsh_numeric_to_set_id(num, trainer_gallery=tg)
        return {
            "canonicalSetId": target,
            "productType": "TRAINER_GALLERY" if tg else "MAIN_EXPANSION",
            "evidence": "PROVEN",
            "reason": f"structured_provider_code:{code}{':TG' if tg else ''}->{target}",
            "matchPolicy": "SET_FIRST_IN_SET_CARD_MATCH",
            "providerCode": code,
            "displayName": parsed.display_name,
            "subsetLabel": parsed.subset_label,
        }

    # SV02, SV10, etc.
    m = re.fullmatch(r"SV(\d+)", code)
    if m:
        num = int(m.group(1))
        target = _sv_numeric_to_set_id(num)
        return {
            "canonicalSetId": target,
            "productType": "MAIN_EXPANSION",
            "evidence": "PROVEN",
            "reason": f"structured_provider_code:{code}->{target}",
            "matchPolicy": "SET_FIRST_IN_SET_CARD_MATCH",
            "providerCode": code,
            "displayName": parsed.display_name,
        }

    # SM Base Set (era without number) -> sm1, NOT base1
    if parsed.reason == "era_base_set_without_numeric_code" and code == "SM":
        return {
            "canonicalSetId": "sm1",
            "productType": "MAIN_EXPANSION",
            "evidence": "PROVEN",
            "reason": "SM_Base_Set_maps_to_sm1_not_base1",
            "matchPolicy": "SET_FIRST_IN_SET_CARD_MATCH",
            "providerCode": code,
            "displayName": parsed.display_name,
        }

    # XY/SM numbered expansion codes if present later
    m = re.fullmatch(r"SM(\d+)", code)
    if m:
        return None  # no sm2-style ids in canonical; fail closed

    return None
