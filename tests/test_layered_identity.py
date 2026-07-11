import json
from pathlib import Path

import jsonschema

from cardscanr_global_catalogue.layered_identity import catalogue_identity, image_identity, image_safe, layered_classification, physical_variant, validate_assignment


def record(**changes):
    value={"language":"en","region":"GLOBAL","canonicalSetId":"set:a","printedCollectorNumber":"001","normalizedCollectorNumber":"1","providerCardIds":{"tcgdex":"a-1"},"providerSetIds":{"tcgdex":"a"},"nativeCardName":"Card","cardVariant":"unspecified","designations":[],"imageProvenance":[{"provider":"tcgdex","sourceUrl":"https://example.invalid/a"}]}
    value.update(changes); return value


def test_exact_catalogue_is_independent_of_physical_variant():
    r=record(); assert catalogue_identity(r)=="exact_catalogue_record"; assert physical_variant(r)=="shared_front_variant_unresolved"


def test_shared_front_is_image_safe():
    x=layered_classification(record()); assert x["imageIdentityState"]=="exact_card_front_image" and x["imageSafe"]


def test_stamped_and_edition_specific_variants_block():
    for label in ("stamped promo", "1st edition"):
        r=record(designations=[label]); v=physical_variant(r); assert v=="variant_specific_unresolved"; assert image_identity(r,"exact_catalogue_record",v)=="image_variant_ambiguous"


def test_wrong_collector_set_language_and_region_rejected():
    r=record()
    base={"language":"en","region":"GLOBAL","canonicalSetId":"set:a","normalizedCollectorNumber":"1"}
    for key,bad in (("language","ja"),("region","JP"),("canonicalSetId","set:b"),("normalizedCollectorNumber","2")):
        image=dict(base); image[key]=bad; assert not validate_assignment(r,image)


def test_provider_duplicate_and_no_name_only_matching():
    assert catalogue_identity(record(),duplicate=True)=="provider_duplicate"
    assert catalogue_identity({"nativeCardName":"Card"})=="ambiguous_catalogue_record"


def test_permission_is_independent_of_identity():
    x=layered_classification(record(permission="pending_human_review")); assert x["imageSafe"]


def test_canary_eligibility_uses_image_safe_state():
    assert image_safe("exact_catalogue_record","shared_front_image")
    assert not image_safe("probable_catalogue_record","shared_front_image")


def test_layered_record_validates_against_contract_schema():
    schema=json.loads((Path(__file__).parents[1]/"data/contracts/image_identity_eligibility_schema.json").read_text(encoding="utf-8"))
    value={"canonicalPrintingId":"card:1",**layered_classification(record())}
    jsonschema.Draft202012Validator(schema).validate(value)
