#!/usr/bin/env python3
"""Variant-safe Pokémon collector / canonical identity helpers.

Canonical identity is NOT collector-number alone and NOT display-name alone.
It is a structured key:

  language | set_id | collector_position_key | name_fingerprint | variant_token

Where:
  - collector_position_key is produced by parse_collector_number()
  - name_fingerprint is accent/punct tolerant but only a compatibility signal
    inside a position — never the sole identity
  - variant_token captures intentional subset/finish markers when present

Collector parser extracts:
  prefix, numerator, denominator_prefix, denominator, suffix, original

Known-safe collapses (same physical printing representations):
  024/086 ↔ 24
  SV1/SV94 ↔ SV1 ↔ SV001
  TG01/TG30 ↔ TG01
  GG01/GG70 ↔ GG01

Unknown formats fail closed (parse_ok=False) rather than inventing identity.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from typing import Any

_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
_DIGIT_GROUP = re.compile(r"\d+")
_SPACE = re.compile(r"\s+")

# Pure numeric or numeric/numeric: 24, 024/086
_PURE = re.compile(r"^(?P<num>\d+)(?:/(?P<den>\d+))?$")
# Prefixed local[/prefixed total]: SV1, SV1/SV94, SV001/SV122, TG01/TG30, GG01/GG70, RC1/RC32
_PREFIXED_FRAC = re.compile(
    r"^(?P<p1>[A-Za-z]+)(?P<n1>\d+)(?:/(?P<p2>[A-Za-z]+)?(?P<n2>\d+))?$"
)
# Prefixed + letter suffix: SM103a, SM104a (promo letter variants)
_PREFIXED_SUFFIX = re.compile(
    r"^(?P<p1>[A-Za-z]+)(?P<n1>\d+)(?P<suf>[A-Za-z]+)(?:/(?P<p2>[A-Za-z]+)?(?P<n2>\d+))?$"
)
# Numeric + letter suffix [/denominator]: 98a, 148a/168, 182b/214, 002a/131
_NUM_SUFFIX_FRAC = re.compile(
    r"^(?P<num>\d+)(?P<suf>[A-Za-z]+)(?:/(?P<den>\d+))?$"
)

@dataclass(frozen=True)
class ParsedCollector:
    original: str
    parse_ok: bool
    prefix: str | None
    numerator: int | None
    denominator_prefix: str | None
    denominator: int | None
    suffix: str | None
    position_key: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _prep(value: object) -> str:
    raw = unicodedata.normalize("NFKC", str(value or ""))
    raw = raw.translate(_FULLWIDTH_DIGITS).strip()
    raw = _SPACE.sub("", raw)
    return raw


def parse_collector_number(value: object) -> ParsedCollector:
    """Parse a printed collector number into structured parts.

    Fail-closed: unknown formats keep a normalized opaque position_key and
    set parse_ok=False so callers can refuse unsafe collapses.
    """
    original = str(value or "").strip()
    raw = _prep(value)
    if not raw:
        return ParsedCollector(
            original=original,
            parse_ok=False,
            prefix=None,
            numerator=None,
            denominator_prefix=None,
            denominator=None,
            suffix=None,
            position_key="",
            reason="empty",
        )

    m = _PURE.fullmatch(raw)
    if m:
        num = int(m.group("num"))
        den = int(m.group("den")) if m.group("den") else None
        return ParsedCollector(
            original=original,
            parse_ok=True,
            prefix=None,
            numerator=num,
            denominator_prefix=None,
            denominator=den,
            suffix=None,
            position_key=str(num),
            reason="pure_numeric_or_fraction",
        )

    m = _PREFIXED_FRAC.fullmatch(raw)
    if m:
        p1 = m.group("p1").upper()
        n1 = int(m.group("n1"))
        p2 = m.group("p2").upper() if m.group("p2") else None
        n2 = int(m.group("n2")) if m.group("n2") else None
        # Fail closed if both sides of slash present but prefixes conflict
        # and both are non-empty different letters (e.g. SV1/TG30 nonsense).
        if p2 and p2 != p1 and n2 is not None:
            return ParsedCollector(
                original=original,
                parse_ok=False,
                prefix=p1,
                numerator=n1,
                denominator_prefix=p2,
                denominator=n2,
                suffix=None,
                position_key=f"{p1.lower()}{n1}/{p2.lower()}{n2}",
                reason="conflicting_prefix_fraction",
            )
        return ParsedCollector(
            original=original,
            parse_ok=True,
            prefix=p1,
            numerator=n1,
            denominator_prefix=(p2 or p1) if n2 is not None else None,
            denominator=n2,
            suffix=None,
            position_key=f"{p1.lower()}{n1}",
            reason="prefixed_local_or_fraction",
        )

    m = _PREFIXED_SUFFIX.fullmatch(raw)
    if m:
        p1 = m.group("p1").upper()
        n1 = int(m.group("n1"))
        suf = m.group("suf").lower()
        p2 = m.group("p2").upper() if m.group("p2") else None
        n2 = int(m.group("n2")) if m.group("n2") else None
        if p2 and p2 != p1 and n2 is not None:
            return ParsedCollector(
                original=original,
                parse_ok=False,
                prefix=p1,
                numerator=n1,
                denominator_prefix=p2,
                denominator=n2,
                suffix=suf,
                position_key=f"{p1.lower()}{n1}{suf}",
                reason="conflicting_prefix_suffix_fraction",
            )
        return ParsedCollector(
            original=original,
            parse_ok=True,
            prefix=p1,
            numerator=n1,
            denominator_prefix=(p2 or p1) if n2 is not None else None,
            denominator=n2,
            suffix=suf,
            # Distinct from bare SM103 — letter suffix is part of identity.
            position_key=f"{p1.lower()}{n1}{suf}",
            reason="prefixed_letter_suffix",
        )

    m = _NUM_SUFFIX_FRAC.fullmatch(raw)
    if m:
        num = int(m.group("num"))
        suf = m.group("suf").lower()
        den = int(m.group("den")) if m.group("den") else None
        return ParsedCollector(
            original=original,
            parse_ok=True,
            prefix=None,
            numerator=num,
            denominator_prefix=None,
            denominator=den,
            suffix=suf,
            # 148a/168 and 148a share position_key 148a; distinct from 148 and 148b.
            position_key=f"{num}{suf}",
            reason="numeric_letter_suffix_or_fraction",
        )

    # Opaque fallback — do not invent collapses.
    opaque = _DIGIT_GROUP.sub(lambda match: str(int(match.group(0))), raw).casefold()
    return ParsedCollector(
        original=original,
        parse_ok=False,
        prefix=None,
        numerator=None,
        denominator_prefix=None,
        denominator=None,
        suffix=None,
        position_key=opaque,
        reason="unknown_format_opaque",
    )


def collector_position_key(value: object) -> str:
    return parse_collector_number(value).position_key


# Back-compat alias used across repair/promote tooling.
def collector_identity_key(value: object) -> str:
    return collector_position_key(value)


def normalize_card_name(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold().replace("'", "").replace("’", "").replace("-", " ")
    # Common TCG orthography variants before stripping punctuation.
    text = text.replace("&", " and ")
    text = re.sub(r"\blv\.?\s*x\b", " lvx ", text)
    text = re.sub(r"\bexp\.?\s*all\b", " exp all ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    # Drop provider-appended collector fragments BEFORE trailing-number cleanup:
    # "Tate and Liza 148a 168", "Pokegear 30 182b 214",
    # "Garbodor 51a 145 Cosmos Holo" (keep finish words).
    text = re.sub(
        r"\s+\d+[a-z](?:\s+\d+)?(?=\s+(cosmos|holo|reverse|full|art|stamp|promo)\b|$)",
        "",
        text,
    ).strip()
    text = re.sub(r"\s+\d+[a-z]\s+\d+$", "", text).strip()
    text = re.sub(r"\s+\d+[a-z]$", "", text).strip()
    # Drop trailing bare numbers often appended by provider dumps ("Clefable 1",
    # "Professors Research 189 198"). Loop until stable so multi-number tails clear.
    while True:
        nxt = re.sub(r"\s+\d+$", "", text).strip()
        if nxt == text:
            break
        text = nxt
    # Drop trailing forme labels that sometimes appear only on one provider.
    text = re.sub(
        r"\s+(land forme|sky forme|full art|secret|illustration rare|secret rare)$",
        "",
        text,
    ).strip()
    # Unit Energy abbreviation expansions used by some providers.
    text = re.sub(r"\bunit energy grw\b", "unit energy grassfirewater", text)
    text = re.sub(r"\bunit energy lpm\b", "unit energy lightningpsychicmetal", text)
    text = re.sub(r"\bunit energy fdy\b", "unit energy fightingdarknessfairy", text)
    return text

def names_compatible(left: object, right: object) -> bool:
    """Compatibility gate only — never sole identity."""
    a = normalize_card_name(left)
    b = normalize_card_name(right)
    if not a or not b:
        return False
    if a == b:
        return True
    if a in b or b in a:
        return True
    ta, tb = set(a.split()), set(b.split())
    if ta and tb and (len(ta & tb) / max(len(ta), len(tb))) >= 0.6:
        return True
    # OCR / provider typo tolerance for near-identical single tokens
    # (Drowsee↔Drowzee, Exeggcutor↔Exeggutor) without collapsing distinct names.
    if abs(len(a) - len(b)) <= 2:
        # Token-wise compare ignoring order for short names.
        ca, cb = a.replace(" ", ""), b.replace(" ", "")
        if ca and cb:
            matches = sum(1 for x, y in zip(ca, cb) if x == y)
            ratio = matches / max(len(ca), len(cb))
            if ratio >= 0.85 and min(len(ca), len(cb)) >= 5:
                return True
            # Single insertion/deletion/substitution
            if _edit_distance_leq(ca, cb, 2):
                return True
    return False


def _edit_distance_leq(left: str, right: str, limit: int) -> bool:
    if abs(len(left) - len(right)) > limit:
        return False
    prev = list(range(len(right) + 1))
    for i, ca in enumerate(left, start=1):
        curr = [i]
        min_row = i
        for j, cb in enumerate(right, start=1):
            cost = 0 if ca == cb else 1
            val = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
            curr.append(val)
            if val < min_row:
                min_row = val
        if min_row > limit:
            return False
        prev = curr
    return prev[-1] <= limit


def name_fingerprint(value: object) -> str:
    return normalize_card_name(value).replace(" ", "_")


def variant_token(card: dict[str, Any] | None = None, *, explicit: object = None) -> str:
    if explicit not in (None, ""):
        return normalize_card_name(explicit).replace(" ", "_") or "normal"
    card = card or {}
    for key in ("variant", "variantType", "finish", "raritySubtype", "subset"):
        value = card.get(key)
        if isinstance(value, str) and value.strip():
            return normalize_card_name(value).replace(" ", "_") or "normal"
        if isinstance(value, list) and value:
            joined = ",".join(str(v) for v in value if str(v).strip())
            if joined:
                return normalize_card_name(joined).replace(" ", "_") or "normal"
    return "normal"


def canonical_identity_key(
    *,
    language: str,
    set_id: str,
    collector_number: object,
    name: object,
    card: dict[str, Any] | None = None,
    require_parse_ok: bool = False,
) -> str:
    """Strongest safe identity key for one language+set printing.

    Distinct physical printings that share a collector local (cel25c #15)
    remain distinct because name_fingerprint differs.
    True provider duplicates (API 24 vs PW 024/086, same name) collide.
    """
    parsed = parse_collector_number(collector_number)
    if require_parse_ok and not parsed.parse_ok:
        raise ValueError(
            f"unsafe collector format for canonical identity: {collector_number!r} ({parsed.reason})"
        )
    return "|".join(
        [
            str(language or "").casefold(),
            str(set_id or "").casefold(),
            parsed.position_key,
            name_fingerprint(name),
            variant_token(card),
        ]
    )


def collector_fraction_key(value: object) -> str:
    """Richest safe collector identity for matching within a mapped set/product.

    Includes denominator when present so 001/165 is never equivalent to bare 1
    from an unrelated product for *cross-set* purposes. Within one mapped set,
    use collectors_compatible() which allows bare↔fraction collapse when the
    denominator matches the set's known printed total or the peer's fraction.
    """
    parsed = parse_collector_number(value)
    if not parsed.parse_ok:
        return parsed.position_key
    parts: list[str] = []
    if parsed.prefix:
        parts.append(parsed.prefix.lower())
    if parsed.numerator is not None:
        num = str(parsed.numerator)
        if parsed.suffix:
            num = f"{num}{parsed.suffix.lower()}"
        parts.append(num)
    if parsed.denominator is not None:
        den = str(parsed.denominator)
        if parsed.denominator_prefix:
            den = f"{parsed.denominator_prefix.lower()}{den}"
        parts.append(den)
    return "/".join(parts) if parts else parsed.position_key


def collectors_compatible(
    left: object,
    right: object,
    *,
    set_printed_total: int | None = None,
    require_same_set: bool = True,
) -> bool:
    """Collector compatibility for cards already proven to share set/product.

    require_same_set=True documents that callers MUST already have set-first
    proof; this function never authorizes cross-set equivalence by itself.
    """
    if not require_same_set:
        raise ValueError("collectors_compatible refuses cross-set use; map set first")
    a = parse_collector_number(left)
    b = parse_collector_number(right)
    if not a.parse_ok or not b.parse_ok:
        # Opaque formats: only exact casefold equality of originals.
        return str(left or "").casefold().strip() == str(right or "").casefold().strip()
    if a.prefix != b.prefix:
        return False
    if a.numerator != b.numerator:
        return False
    if (a.suffix or "") != (b.suffix or ""):
        return False
    # Both have denominators: must agree (and prefixes if present).
    if a.denominator is not None and b.denominator is not None:
        if a.denominator != b.denominator:
            return False
        if (a.denominator_prefix or a.prefix) != (b.denominator_prefix or b.prefix):
            # Allow None vs same prefix
            ap = a.denominator_prefix or a.prefix
            bp = b.denominator_prefix or b.prefix
            if ap and bp and ap != bp:
                return False
        return True
    # One bare, one fraction: allow when denominator matches set printed total
    # or when the fraction side is the only denominator present.
    den = a.denominator if a.denominator is not None else b.denominator
    if den is None:
        return True  # both bare, same numerator/suffix/prefix
    if set_printed_total is not None and den == set_printed_total:
        return True
    # Bare vs fraction without set total: not enough for PROVEN; caller may
    # still treat as STRONG if other signals agree.
    return False


def evidence_definition(level: str) -> str:
    """Canonical evidence vocabulary for catalogue reconciliation."""
    return {
        "PROVEN": (
            "set/product identity proven AND physical card/printing identity proven"
        ),
        "STRONG_EVIDENCE": (
            "multiple compatible evidence signals but incomplete authority"
        ),
        "HEURISTIC": "useful hypothesis only — not safe to merge/promote",
        "UNRESOLVED": "identity is not safe to merge/promote",
    }.get(level, "unknown_evidence_level")


def classify_pair(
    kept: dict[str, Any],
    dropped: dict[str, Any],
    *,
    language: str,
    set_id: str,
) -> str:
    """Classify a deleted row relative to the retained winner in the same set."""
    kept_pos = collector_position_key(kept.get("collectorNumber"))
    dropped_pos = collector_position_key(dropped.get("collectorNumber"))
    if not kept_pos or not dropped_pos:
        return "UNKNOWN"
    if kept_pos != dropped_pos:
        # Over-broad pass should only delete within a position group; treat as unknown.
        return "UNKNOWN"
    if names_compatible(kept.get("name"), dropped.get("name")):
        # Same position + compatible name ⇒ true duplicate representation.
        return "TRUE_DUPLICATE"
    # Same position + incompatible name ⇒ legitimate distinct printing collapsed.
    return "FALSE_MERGE"


_VARIANT_SIGNATURE_FIELDS = (
    "variant",
    "variantType",
    "finish",
    "raritySubtype",
    "subset",
    "printingClass",
    "stampType",
    "cardSize",
    "productVariant",
    "deckVariant",
)


def _normalize_variant_part(value: object) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, list):
        parts = [str(v).strip() for v in value if str(v).strip()]
        if not parts:
            return None
        value = ",".join(parts)
    text = normalize_card_name(str(value)).replace(" ", "_")
    return text or None


def variant_signature(card: dict[str, Any] | None = None, *, explicit: object = None) -> str:
    """Composite deterministic variant identity from all populated identity fields."""
    if explicit not in (None, ""):
        return _normalize_variant_part(explicit) or "normal"
    card = card or {}
    parts: list[str] = []
    for key in _VARIANT_SIGNATURE_FIELDS:
        norm = _normalize_variant_part(card.get(key))
        if norm:
            parts.append(f"{key}:{norm}")
    if not parts:
        return "normal"
    return "|".join(sorted(parts))


IDENTITY_MODEL_VERSION = "physical-printing-v1"

# Stamp / finish tokens that are also printingClass values.
_PRINTING_CLASS_FROM_STAMP = {
    "staff",
    "prerelease",
    "prerelease_staff",
    "prize_pack",
    "shadowless",
    "first_edition",
    "jumbo",
    "cosmos_holo",
    "cracked_ice",
    "world_championship",
    "trainer_kit",
    "product_exclusive",
}

# Set-level product labels that must not enter variantSignature when the
# physicalProductId already encodes the promo/product family.
_SET_LEVEL_PRODUCT_VARIANTS = {
    "black_star_promo",
    "black_star_promos",
    "promo",
    "promos",
}


def normalize_physical_printing_card_view(
    card: dict[str, Any] | None = None,
    *,
    descriptor_tokens: list[str] | None = None,
    product_type: object = None,
) -> dict[str, Any]:
    """Single canonical card-view builder for variantSignature + physicalPrintingId.

    All candidate audits, fixtures, catalogue reads, and staging proofs must use
    this (or physical_printing_id which calls it) so dimensions are never omitted
    or duplicated across paths.
    """
    view: dict[str, Any] = dict(card or {})
    tokens = [str(t).strip().casefold().replace(" ", "_") for t in (descriptor_tokens or []) if str(t).strip()]

    for token in tokens:
        if token in _PRINTING_CLASS_FROM_STAMP or "holo" in token:
            view.setdefault("printingClass", token)
            if token in {"staff", "prerelease_staff", "prerelease", "prize_pack"}:
                view.setdefault("stampType", token)
        elif token:
            view.setdefault("stampType", token)

    # Mirror stamp into printingClass when stamp alone names the printing class.
    stamp = _normalize_variant_part(view.get("stampType"))
    if stamp and stamp in _PRINTING_CLASS_FROM_STAMP:
        view.setdefault("printingClass", stamp)

    pc = _normalize_variant_part(view.get("printingClass"))
    if pc and pc in {"staff", "prerelease_staff", "prerelease", "prize_pack"}:
        view.setdefault("stampType", pc)

    # productFamily may feed productVariant once; drop set-level promo noise.
    if view.get("productFamily") and not view.get("productVariant"):
        view["productVariant"] = view.get("productFamily")
    if product_type not in (None, "") and not view.get("productVariant"):
        view["productVariant"] = product_type

    pv = _normalize_variant_part(view.get("productVariant"))
    if pv in _SET_LEVEL_PRODUCT_VARIANTS:
        view.pop("productVariant", None)
    pf = _normalize_variant_part(view.get("productFamily"))
    if pf in _SET_LEVEL_PRODUCT_VARIANTS:
        view.pop("productFamily", None)

    return view


def identity_collector_key(
    collector_number: object,
    *,
    numbering_policy: str = "SEQUENTIAL_FRACTION",
    set_printed_total: int | None = None,
) -> str:
    """Numbering-policy-aware collector identity for physical printing keys.

    For SEQUENTIAL_FRACTION with known set printedTotal, bare and matching
    fraction forms collapse to the numerator (24 == 024/086 when total=86).
    A contradictory denominator is preserved and flagged via fraction key.

    Prefixed promo/TG/SV policies use position_key so BW40 stays ``bw40``
    (never ``bw/40``) across every execution path.
    """
    policy = str(numbering_policy or "SEQUENTIAL_FRACTION")
    if policy in ("ORIGINAL_REPRINT_NUMBERING", "LETTERED_VARIANT", "MULTI_DENOMINATOR"):
        return collector_fraction_key(collector_number)
    if policy in (
        "PROMO_PREFIX",
        "PREFIXED_SEQUENTIAL",
        "TRAINER_GALLERY",
        "SHINY_VAULT",
        "GALARIAN_GALLERY",
        "ENERGY_SERIES",
    ):
        parsed = parse_collector_number(collector_number)
        if not parsed.parse_ok:
            return parsed.position_key or str(collector_number or "").casefold().strip()
        return parsed.position_key

    parsed = parse_collector_number(collector_number)
    if not parsed.parse_ok:
        return parsed.position_key or str(collector_number or "").casefold().strip()

    if policy == "SEQUENTIAL_FRACTION":
        if parsed.denominator is not None and set_printed_total is not None:
            if parsed.denominator != set_printed_total:
                return collector_fraction_key(collector_number)
            if parsed.numerator is not None:
                suf = (parsed.suffix or "").lower()
                pref = (parsed.prefix or "").lower()
                core = f"{parsed.numerator}{suf}"
                return f"{pref}{core}" if pref else core
        if parsed.denominator is not None and set_printed_total is None:
            return collector_fraction_key(collector_number)
        if parsed.numerator is not None:
            suf = (parsed.suffix or "").lower()
            pref = (parsed.prefix or "").lower()
            core = f"{parsed.numerator}{suf}"
            return f"{pref}{core}" if pref else core
    return parsed.position_key


def physical_printing_id(
    *,
    language: str,
    set_id: str,
    collector_number: object,
    card: dict[str, Any] | None = None,
    numbering_policy: str = "SEQUENTIAL_FRACTION",
    set_printed_total: int | None = None,
    identity_model_version: str = IDENTITY_MODEL_VERSION,
    descriptor_tokens: list[str] | None = None,
    product_type: object = None,
) -> str:
    """Stable physical printing identity — non-redundant, versioned.

    Format: v1|language|set_id|collector_key|variant_signature

    ``set_id`` MUST be a stable CardScanR physicalProductId / canonical set id,
    never a raw numeric provider orphan id.
    """
    card = card or {}
    persisted = str(card.get("physicalPrintingId") or "").strip()
    persisted_ver = str(card.get("identityModelVersion") or "").strip()
    if persisted and persisted_ver == identity_model_version:
        return persisted

    if str(set_id or "").strip().isdigit():
        raise ValueError(
            f"physical_printing_id refuses numeric provider set id {set_id!r}; "
            "resolve via physical product identity registry first"
        )

    collector_key = identity_collector_key(
        collector_number,
        numbering_policy=numbering_policy,
        set_printed_total=set_printed_total,
    )
    card_for_sig = normalize_physical_printing_card_view(
        card,
        descriptor_tokens=descriptor_tokens,
        product_type=product_type,
    )
    sig = variant_signature(card_for_sig)
    return "|".join(
        [
            identity_model_version,
            str(language or "").casefold(),
            str(set_id or "").casefold(),
            collector_key,
            sig,
        ]
    )


def assert_classification_evidence(classification: str, evidence: str) -> None:
    """Every classification containing PROVEN must carry PROVEN evidence."""
    cls = str(classification or "")
    if "PROVEN" in cls and evidence != "PROVEN":
        raise ValueError(
            f"classification {classification!r} requires evidence PROVEN, got {evidence!r}"
        )


def destructive_dedup_allowed(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    set_id: str,
    numbering_policy: str = "SEQUENTIAL_FRACTION",
    set_printed_total: int | None = None,
    provider_equivalence: bool = False,
) -> bool:
    """Automatic destructive dedup requires exact physical-printing equivalence."""
    if str(left.get("setId") or set_id) != str(right.get("setId") or set_id):
        return False
    if not provider_equivalence:
        return False
    lk = identity_collector_key(
        left.get("collectorNumber"),
        numbering_policy=numbering_policy,
        set_printed_total=set_printed_total,
    )
    rk = identity_collector_key(
        right.get("collectorNumber"),
        numbering_policy=numbering_policy,
        set_printed_total=set_printed_total,
    )
    if lk != rk:
        return False
    if variant_signature(left) != variant_signature(right):
        return False
    if not names_compatible(left.get("name"), right.get("name")):
        return False
    return True
