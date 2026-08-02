import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_scope_contains_all_tcgdex_languages_and_required_regions() -> None:
    scope = json.loads((ROOT / "config/worldwide_catalogue_scope.json").read_text(encoding="utf-8"))
    languages = {item["code"] for item in scope["languages"]}
    tcgdex = {"en", "fr", "es", "es-mx", "it", "pt", "pt-br", "pt-pt", "de", "nl", "pl", "ru",
               "ja", "ko", "zh-tw", "id", "th", "zh-cn"}
    assert tcgdex <= languages
    by_language = {item["code"]: item for item in scope["languages"]}
    assert by_language["pt-br"]["officially_printed"] is True
    assert by_language["pt-pt"]["officially_printed"] is False
    assert by_language["pt-pt"]["evidence_url"].startswith("https://support.pokemon.com/")
    regions = {item["code"] for item in scope["regions"]}
    assert {"JP", "KR", "CN", "TW", "HK", "TH", "ID", "BR", "MX", "PT", "INTL"} <= regions


def test_scope_enumerates_required_sealed_categories() -> None:
    scope = json.loads((ROOT / "config/worldwide_catalogue_scope.json").read_text(encoding="utf-8"))
    product_types = set(scope["sealed_product_types"])
    assert {"booster_pack", "booster_pack_art", "booster_box", "elite_trainer_box", "tin",
            "theme_deck", "blister", "collection_box", "case", "display_carton"} <= product_types


def test_source_registry_separates_metadata_and_image_rights() -> None:
    registry = json.loads((ROOT / "config/worldwide_source_registry.json").read_text(encoding="utf-8"))
    providers = {provider["id"]: provider for provider in registry["providers"]}
    assert providers["tcgdex-cards-database"]["rights_status"] == "approved_for_mirror"
    assert providers["tcgdex-assets"]["rights_status"] == "permission_pending"
    assert providers["pokemontcg-images"]["rights_status"] == "permission_pending"
    assert all(provider["rights_status"] != "unknown" for provider in providers.values())
