#!/usr/bin/env python3
"""Structured provider set-code parsing — deterministic, no fuzzy substring."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_PROVIDER_HEADER = re.compile(
    r"^(?P<code>(?:SWSH|SV|SM|XY|BW|ME|SVE)\d+)\s*:\s*(?P<name>.+)$",
    re.I,
)
_ERA_DASH_NAME = re.compile(
    r"^(?P<era>SM|XY|SV|SWSH|BW|HGSS|DP)\s*[-:]\s*(?P<name>.+)$",
    re.I,
)
_TRAINER_GALLERY = re.compile(r"trainer\s+gallery", re.I)
_ERA_BASE_SET = re.compile(
    r"^(?P<era>SM|XY|SV|SWSH|BW)\s+(?:-\s*)?Base\s+Set$",
    re.I,
)
_SHADOWLESS = re.compile(r"base\s+set\s*\(\s*shadowless\s*\)", re.I)

# Explicit reviewed aliases: normalized provider remainder -> canonical set id
REVIEWED_NAME_ALIASES: dict[str, str] = {
    "paldean fates": "sv4pt5",
    "prismatic evolutions": "sv8pt5",
    "white flare": "rsv10pt5",
    "black bolt": "zsv10pt5",
    "shrouded fable": "sv6pt5",
    "scarlet and violet 151": "sv3pt5",
    "151": "sv3pt5",
    "scarlet and violet base set": "sv1",
    "sword and shield base set": "swsh1",
    "base set": "base1",  # only when era is empty / classic — callers must gate
}

# HGSS era bare names (provider dumps omit HS— prefix)
HGSS_BARE_ALIASES: dict[str, str] = {
    "triumphant": "hgss4",
    "unleashed": "hgss2",
    "undaunted": "hgss3",
}

# Explicit promo product aliases after collector-namespace review
PROMO_ALIASES: dict[str, dict[str, Any]] = {
    "sm promos": {
        "canonicalSetId": "smp",
        "expectedPrefixes": ("SM",),
        "productType": "BLACK_STAR_PROMO",
    },
    "xy promos": {
        "canonicalSetId": "xyp",
        "expectedPrefixes": ("XY",),
        "productType": "BLACK_STAR_PROMO",
    },
    "sv scarlet and violet promo cards": {
        "canonicalSetId": "svp",
        "expectedPrefixes": ("SVP", "SV"),
        "productType": "BLACK_STAR_PROMO",
    },
    "black and white promos": {
        "canonicalSetId": "bwp",
        "expectedPrefixes": ("BW",),
        "productType": "BLACK_STAR_PROMO",
    },
    "diamond and pearl promos": {
        "canonicalSetId": "dpp",
        "expectedPrefixes": ("DP",),
        "productType": "BLACK_STAR_PROMO",
    },
    "hgss promos": {
        "canonicalSetId": "hsp",
        "expectedPrefixes": ("HGSS", "HS"),
        "productType": "BLACK_STAR_PROMO",
    },
}

ERA_COMPATIBLE_PREFIX: dict[str, tuple[str, ...]] = {
    "SM": ("sm",),
    "XY": ("xy",),
    "SV": ("sv", "zsv", "rsv"),
    "SWSH": ("swsh",),
    "BW": ("bw",),
    "HGSS": ("hgss", "hs"),
    "DP": ("dp",),
    "ME": ("me",),
}


def normalize_set_name(name: str) -> str:
    text = str(name or "").casefold().strip()
    text = text.replace("&", " and ")
    # Normalize fancy dash variants used in HS—Unleashed etc.
    text = text.replace("—", " ").replace("–", " ").replace("-", " ")
    text = re.sub(r"^hs\s+", "", text)  # HS Unleashed -> unleashed for compare
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


@dataclass(frozen=True)
class ProviderSetParsed:
    original: str
    provider_code: str | None
    era: str | None
    display_name: str
    subset_label: str | None
    parse_ok: bool
    reason: str
    product_hint: str | None = None


def parse_provider_set_name(raw: str) -> ProviderSetParsed:
    text = str(raw or "").strip()
    if not text:
        return ProviderSetParsed("", None, None, "", None, False, "empty")

    if _SHADOWLESS.search(text):
        return ProviderSetParsed(
            original=text,
            provider_code=None,
            era=None,
            display_name="Base Set (Shadowless)",
            subset_label="Shadowless",
            parse_ok=True,
            reason="shadowless_edition",
            product_hint="SHADOWLESS_EDITION",
        )

    m = _PROVIDER_HEADER.fullmatch(text)
    if m:
        code = m.group("code").upper()
        name = m.group("name").strip()
        era = re.match(r"[A-Z]+", code)
        era_s = era.group(0) if era else None
        subset = None
        if _TRAINER_GALLERY.search(name):
            subset = "Trainer Gallery"
            name = _TRAINER_GALLERY.sub("", name).strip(" -")
        return ProviderSetParsed(
            original=text,
            provider_code=code,
            era=era_s,
            display_name=name,
            subset_label=subset,
            parse_ok=True,
            reason="provider_code_colon_name",
        )

    eb = _ERA_BASE_SET.fullmatch(text)
    if eb:
        era = eb.group("era").upper()
        return ProviderSetParsed(
            original=text,
            provider_code=era,
            era=era,
            display_name="Base Set",
            subset_label=None,
            parse_ok=True,
            reason="era_base_set_without_numeric_code",
        )

    em = _ERA_DASH_NAME.fullmatch(text)
    if em:
        era = em.group("era").upper()
        name = em.group("name").strip()
        return ProviderSetParsed(
            original=text,
            provider_code=None,
            era=era,
            display_name=name,
            subset_label=None,
            parse_ok=True,
            reason="era_dash_display_name",
        )

    # Bare HGSS expansion names
    bare = normalize_set_name(text)
    if bare in HGSS_BARE_ALIASES:
        return ProviderSetParsed(
            original=text,
            provider_code=None,
            era="HGSS",
            display_name=text.strip(),
            subset_label=None,
            parse_ok=True,
            reason="hgss_bare_name",
        )

    # Promo product names
    if bare in PROMO_ALIASES or bare.replace(":", " ") in PROMO_ALIASES:
        key = bare if bare in PROMO_ALIASES else bare.replace(":", " ")
        return ProviderSetParsed(
            original=text,
            provider_code=None,
            era=None,
            display_name=text.strip(),
            subset_label=None,
            parse_ok=True,
            reason="promo_product_name",
            product_hint="BLACK_STAR_PROMO",
        )

    return ProviderSetParsed(
        original=text,
        provider_code=None,
        era=None,
        display_name=text,
        subset_label=None,
        parse_ok=False,
        reason="unparsed_provider_set_name",
    )


def names_compatible_exact(left: str, right: str) -> bool:
    return normalize_set_name(left) == normalize_set_name(right)


def _code_target(code: str, *, trainer_gallery: bool) -> str | None:
    code = code.upper()
    m = re.fullmatch(r"SWSH(\d+)", code)
    if m:
        num = int(m.group(1))
        return f"swsh{num}tg" if trainer_gallery else f"swsh{num}"
    m = re.fullmatch(r"SV(\d+)", code)
    if m:
        return f"sv{int(m.group(1))}"
    m = re.fullmatch(r"SM(\d+)", code)
    if m:
        return f"sm{int(m.group(1))}"
    m = re.fullmatch(r"XY(\d+)", code)
    if m:
        return f"xy{int(m.group(1))}"
    m = re.fullmatch(r"ME(\d+)", code)
    if m:
        return f"me{int(m.group(1))}"
    m = re.fullmatch(r"BW(\d+)", code)
    if m:
        return f"bw{int(m.group(1))}"
    return None


def _era_compatible(era: str | None, set_id: str) -> bool:
    if not era:
        return True
    prefixes = ERA_COMPATIBLE_PREFIX.get(era.upper(), ())
    sid = set_id.casefold()
    return any(sid.startswith(p) for p in prefixes)


def resolve_with_canonical_catalog(
    parsed: ProviderSetParsed,
    *,
    sets_by_id: dict[str, dict[str, Any]],
    by_official_name: dict[str, list[str]] | None = None,
    provider_cards: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Resolve provider set with display-name verification against canonical metadata."""
    by_official_name = by_official_name or {}

    if parsed.reason == "shadowless_edition":
        return {
            "canonicalSetId": None,
            "productType": "SHADOWLESS_EDITION",
            "evidence": "STRONG_EVIDENCE",
            "reason": "base_set_shadowless_is_distinct_physical_printing",
            "matchPolicy": "PRODUCT_LOCAL_ONLY",
            "baseCardReferenceSetId": "base1",
            "displayName": parsed.display_name,
            "mappingResult": "SUPPLEMENTAL_PHYSICAL_PRINTING_CANDIDATE",
        }

    if parsed.reason == "promo_product_name":
        key = normalize_set_name(parsed.original)
        alias = PROMO_ALIASES.get(key)
        if not alias:
            return None
        target = alias["canonicalSetId"]
        if target not in sets_by_id:
            return {
                "canonicalSetId": None,
                "productType": alias["productType"],
                "evidence": "HEURISTIC",
                "reason": f"promo_alias_target_missing:{target}",
                "mappingResult": "UNRESOLVED_SET_IDENTITY",
            }
        # Collector prefix corroboration when cards provided
        prefixes = alias.get("expectedPrefixes") or ()
        if provider_cards:
            prefixed = 0
            total = 0
            for card in provider_cards:
                cn = str(card.get("collectorNumber") or "").strip().upper()
                if not cn:
                    continue
                total += 1
                if any(cn.startswith(p) for p in prefixes):
                    prefixed += 1
            if total and prefixed / total < 0.5:
                return {
                    "canonicalSetId": None,
                    "productType": alias["productType"],
                    "evidence": "HEURISTIC",
                    "reason": "promo_prefix_namespace_mismatch",
                    "mappingResult": "UNRESOLVED_SET_IDENTITY",
                    "prefixMatchRatio": round(prefixed / total, 4),
                }
        return {
            "canonicalSetId": target,
            "productType": alias["productType"],
            "evidence": "PROVEN",
            "reason": f"explicit_promo_alias:{parsed.original}->{target}",
            "matchPolicy": "SET_FIRST_IN_SET_CARD_MATCH",
            "displayName": parsed.display_name,
            "mappingResult": "PROVIDER_ALIAS_PROVEN",
        }

    if parsed.reason == "hgss_bare_name":
        target = HGSS_BARE_ALIASES.get(normalize_set_name(parsed.display_name))
        if not target or target not in sets_by_id:
            return None
        canon_name = str(sets_by_id[target].get("name") or "")
        if normalize_set_name(parsed.display_name) not in normalize_set_name(canon_name) and normalize_set_name(
            canon_name
        ).endswith(normalize_set_name(parsed.display_name)):
            pass  # HS—Triumphant endswith Triumphant after normalize
        # After normalize_set_name, HS—Triumphant -> triumphant
        if normalize_set_name(canon_name) != normalize_set_name(parsed.display_name):
            return {
                "canonicalSetId": None,
                "evidence": "HEURISTIC",
                "reason": "hgss_name_mismatch",
                "mappingResult": "PROVIDER_CODE_NAME_CONTRADICTION",
            }
        return {
            "canonicalSetId": target,
            "productType": "MAIN_EXPANSION",
            "evidence": "PROVEN",
            "reason": f"hgss_bare_alias:{parsed.display_name}->{target}",
            "matchPolicy": "SET_FIRST_IN_SET_CARD_MATCH",
            "displayName": parsed.display_name,
            "mappingResult": "PROVIDER_ALIAS_PROVEN",
        }

    if parsed.reason == "era_base_set_without_numeric_code" and (parsed.era or "") == "SM":
        if "sm1" not in sets_by_id:
            return None
        return {
            "canonicalSetId": "sm1",
            "productType": "MAIN_EXPANSION",
            "evidence": "PROVEN",
            "reason": "SM_Base_Set_maps_to_sm1_not_base1",
            "matchPolicy": "SET_FIRST_IN_SET_CARD_MATCH",
            "providerCode": parsed.provider_code,
            "displayName": parsed.display_name,
            "mappingResult": "PROVIDER_ALIAS_PROVEN",
        }

    # Numbered provider code path with display-name verification
    if parsed.provider_code and parsed.reason == "provider_code_colon_name":
        tg = parsed.subset_label == "Trainer Gallery"
        target = _code_target(parsed.provider_code, trainer_gallery=tg)
        if not target:
            return None
        if target not in sets_by_id:
            return {
                "canonicalSetId": None,
                "evidence": "HEURISTIC",
                "reason": f"provider_code_target_missing:{target}",
                "mappingResult": "UNRESOLVED_SET_IDENTITY",
                "providerCode": parsed.provider_code,
            }
        canon = sets_by_id[target]
        canon_name = str(canon.get("name") or "")
        # For Trainer Gallery, canonical name usually includes Trainer Gallery
        expected = parsed.display_name
        if tg:
            expected_tg = f"{parsed.display_name} Trainer Gallery".strip()
            if not (
                names_compatible_exact(expected, canon_name)
                or names_compatible_exact(expected_tg, canon_name)
                or normalize_set_name(canon_name).startswith(normalize_set_name(parsed.display_name))
            ):
                return {
                    "canonicalSetId": None,
                    "evidence": "HEURISTIC",
                    "reason": "PROVIDER_CODE_NAME_CONTRADICTION",
                    "mappingResult": "PROVIDER_CODE_NAME_CONTRADICTION",
                    "providerCode": parsed.provider_code,
                    "displayName": parsed.display_name,
                    "canonicalName": canon_name,
                    "proposedTarget": target,
                }
        else:
            if not names_compatible_exact(expected, canon_name):
                # Allow "Sword & Shield Base Set" vs "Sword & Shield"
                if not (
                    normalize_set_name(expected).startswith(normalize_set_name(canon_name))
                    or normalize_set_name(canon_name).startswith(normalize_set_name(expected))
                ):
                    return {
                        "canonicalSetId": None,
                        "evidence": "HEURISTIC",
                        "reason": "PROVIDER_CODE_NAME_CONTRADICTION",
                        "mappingResult": "PROVIDER_CODE_NAME_CONTRADICTION",
                        "providerCode": parsed.provider_code,
                        "displayName": parsed.display_name,
                        "canonicalName": canon_name,
                        "proposedTarget": target,
                    }
        return {
            "canonicalSetId": target,
            "productType": "TRAINER_GALLERY" if tg else "MAIN_EXPANSION",
            "evidence": "PROVEN",
            "reason": f"structured_provider_code_verified:{parsed.provider_code}->{target}",
            "matchPolicy": "SET_FIRST_IN_SET_CARD_MATCH",
            "providerCode": parsed.provider_code,
            "displayName": parsed.display_name,
            "canonicalName": canon_name,
            "mappingResult": "PROVIDER_ALIAS_PROVEN",
        }

    # Era + exact display name
    if parsed.reason == "era_dash_display_name" and parsed.era:
        rem = normalize_set_name(parsed.display_name)
        candidates: list[str] = []
        # Reviewed aliases first
        alias_id = REVIEWED_NAME_ALIASES.get(rem)
        if alias_id and alias_id in sets_by_id and _era_compatible(parsed.era, alias_id):
            candidates = [alias_id]
        else:
            for sid, metas in by_official_name.items():
                # by_official_name maps name -> list of ids
                pass
            # Prefer map name -> ids
            name_map = by_official_name
            if rem in name_map:
                candidates = [c for c in name_map[rem] if _era_compatible(parsed.era, c)]
            # Also try exact against each set name
            if not candidates:
                for sid, meta in sets_by_id.items():
                    if not _era_compatible(parsed.era, sid):
                        continue
                    if names_compatible_exact(parsed.display_name, str(meta.get("name") or "")):
                        candidates.append(sid)
        candidates = sorted(set(candidates))
        if len(candidates) == 1:
            target = candidates[0]
            return {
                "canonicalSetId": target,
                "productType": "MAIN_EXPANSION",
                "evidence": "PROVEN",
                "reason": f"era_exact_name:{parsed.era}+{parsed.display_name}->{target}",
                "matchPolicy": "SET_FIRST_IN_SET_CARD_MATCH",
                "era": parsed.era,
                "displayName": parsed.display_name,
                "canonicalName": sets_by_id[target].get("name"),
                "mappingResult": "PROVIDER_ALIAS_PROVEN",
            }
        if len(candidates) > 1:
            return {
                "canonicalSetId": None,
                "evidence": "HEURISTIC",
                "reason": "conflicting_era_name_candidates",
                "mappingResult": "UNRESOLVED_SET_IDENTITY",
                "candidates": candidates,
            }
        return None

    return None


# Back-compat thin wrapper used by older tests
def resolve_provider_code_to_canonical(
    parsed: ProviderSetParsed,
    *,
    sets_by_id: dict[str, dict[str, Any]] | None = None,
) -> dict | None:
    if sets_by_id is None:
        # Minimal resolution without name verification (tests may supply later)
        if not parsed.parse_ok:
            return None
        if parsed.provider_code and parsed.reason == "provider_code_colon_name":
            tg = parsed.subset_label == "Trainer Gallery"
            target = _code_target(parsed.provider_code, trainer_gallery=tg)
            if not target:
                return None
            return {
                "canonicalSetId": target,
                "productType": "TRAINER_GALLERY" if tg else "MAIN_EXPANSION",
                "evidence": "PROVEN",
                "reason": f"structured_provider_code:{parsed.provider_code}->{target}",
                "matchPolicy": "SET_FIRST_IN_SET_CARD_MATCH",
                "providerCode": parsed.provider_code,
                "displayName": parsed.display_name,
                "subsetLabel": parsed.subset_label,
                "nameVerification": "deferred",
            }
        if parsed.reason == "era_base_set_without_numeric_code" and parsed.era == "SM":
            return {
                "canonicalSetId": "sm1",
                "productType": "MAIN_EXPANSION",
                "evidence": "PROVEN",
                "reason": "SM_Base_Set_maps_to_sm1_not_base1",
                "matchPolicy": "SET_FIRST_IN_SET_CARD_MATCH",
            }
        return None
    return resolve_with_canonical_catalog(parsed, sets_by_id=sets_by_id)
