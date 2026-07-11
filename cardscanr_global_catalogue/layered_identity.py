from __future__ import annotations

from typing import Any

CATALOGUE_STATES = {"exact_catalogue_record", "probable_catalogue_record", "ambiguous_catalogue_record", "provider_duplicate", "conflicting_catalogue_record"}
VARIANT_STATES = {"exact_physical_variant", "shared_front_variant_unresolved", "variant_specific_unresolved", "physical_variant_not_applicable", "physical_variant_conflict"}
IMAGE_STATES = {"exact_variant_image", "exact_card_front_image", "shared_front_image", "image_candidate_unverified", "image_variant_ambiguous", "image_identity_conflict", "missing_image"}


def catalogue_identity(record: dict[str, Any], *, duplicate: bool = False, conflict: bool = False) -> str:
    if conflict:
        return "conflicting_catalogue_record"
    if duplicate:
        return "provider_duplicate"
    required = (record.get("language"), record.get("region"), record.get("canonicalSetId"),
                record.get("printedCollectorNumber"), record.get("normalizedCollectorNumber"),
                record.get("providerCardIds"), record.get("providerSetIds"), record.get("nativeCardName"))
    return "exact_catalogue_record" if all(required) else "ambiguous_catalogue_record"


def physical_variant(record: dict[str, Any]) -> str:
    variant = str(record.get("cardVariant") or "unspecified").casefold()
    if variant not in {"", "unspecified"}:
        return "exact_physical_variant"
    designations = " ".join(str(x).casefold() for x in record.get("designations") or [])
    if any(x in designations for x in ("stamp", "1st edition", "deck exclusive", "parallel")):
        return "variant_specific_unresolved"
    return "shared_front_variant_unresolved"


def image_identity(record: dict[str, Any], catalogue_state: str, variant_state: str) -> str:
    images = [x for x in record.get("imageProvenance") or [] if isinstance(x, dict) and x.get("sourceUrl")]
    if not images:
        return "missing_image"
    if catalogue_state != "exact_catalogue_record":
        return "image_candidate_unverified"
    if variant_state in {"variant_specific_unresolved", "physical_variant_conflict"}:
        return "image_variant_ambiguous"
    if variant_state == "exact_physical_variant":
        return "exact_variant_image"
    return "exact_card_front_image"


def image_safe(catalogue_state: str, image_state: str, *, language_match: bool = True,
               region_match: bool = True, set_match: bool = True,
               collector_match: bool = True, provider_mapping_consistent: bool = True,
               visual_conflict: bool = False) -> bool:
    return (catalogue_state == "exact_catalogue_record" and
            image_state in {"exact_variant_image", "exact_card_front_image", "shared_front_image"} and
            language_match and region_match and set_match and collector_match and
            provider_mapping_consistent and not visual_conflict)


def validate_assignment(record: dict[str, Any], image: dict[str, Any]) -> bool:
    """Reject cross-language/set/collector assignments; names are intentionally unused."""
    return all((record.get("language") == image.get("language"),
                record.get("region") == image.get("region"),
                record.get("canonicalSetId") == image.get("canonicalSetId"),
                record.get("normalizedCollectorNumber") == image.get("normalizedCollectorNumber")))


def layered_classification(record: dict[str, Any], *, duplicate: bool = False, conflict: bool = False) -> dict[str, Any]:
    cat = catalogue_identity(record, duplicate=duplicate, conflict=conflict)
    variant = physical_variant(record)
    image = image_identity(record, cat, variant)
    safe = image_safe(cat, image)
    unresolved = [key for key in ("edition", "finish", "stamp", "promoDeckSource") if not record.get(key)]
    return {"catalogueIdentityState": cat, "physicalVariantState": variant,
            "imageIdentityState": image, "imageSafe": safe,
            "unresolvedFields": unresolved,
            "blockingReason": None if safe else ("missing_image" if image == "missing_image" else image),
            "evidenceUsed": {"providerCardIds": record.get("providerCardIds"), "providerSetIds": record.get("providerSetIds"),
                             "language": record.get("language"), "region": record.get("region"),
                             "canonicalSetId": record.get("canonicalSetId"), "printedCollectorNumber": record.get("printedCollectorNumber"),
                             "normalizedCollectorNumber": record.get("normalizedCollectorNumber"), "setTotal": record.get("officialSetTotal")}}
