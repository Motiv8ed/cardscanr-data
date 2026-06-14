import importlib.util
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = ROOT / "tools"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TOOLS_DIR))


def load_tool_module(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS_DIR / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


build_price_cache = load_tool_module("build_price_cache")
pokewallet_foundation = load_tool_module("build_pokewallet_catalog_foundation")
promotion = load_tool_module("promote_provider_catalog_to_app_catalog")


def test_english_catalogue_card_exports_image_url_aliases() -> None:
    record = build_price_cache.build_catalog_card_record(
        {
            "id": "sv10-62",
            "name": "Arrokuda",
            "number": "62",
            "images": {
                "small": "https://images.pokemontcg.io/sv10/62.png",
                "large": "https://images.pokemontcg.io/sv10/62_hires.png",
            },
        },
        "sv10",
        "Destined Rivals",
    )

    assert record["imageUrl"] == "https://images.pokemontcg.io/sv10/62.png"
    assert record["imageUrlSmall"] == record["imageSmall"]
    assert record["imageUrlLarge"] == record["imageLarge"]
    assert record["providerImageSource"] == "pokemon_tcg_api"


def test_japanese_tcgdex_catalogue_card_exports_image_url_aliases() -> None:
    record = build_price_cache.build_japanese_catalog_card_record(
        {"id": "M3-032", "localId": "032", "name": "Espurr"},
        "M3",
        "Nihil Zero",
        "M",
    )

    assert record["imageUrl"] == "https://assets.tcgdex.net/ja/M/M3/032/low.webp"
    assert record["imageUrlSmall"] == record["imageSmall"]
    assert record["imageUrlLarge"] == record["imageLarge"]
    assert record["providerImageSource"] == "tcgdex"


def test_pokewallet_provider_record_exports_absolute_image_urls() -> None:
    set_item = pokewallet_foundation.ProviderSet(
        set_id="24711",
        set_code="m5",
        name="Abyss Eye",
        language="jap",
        app_language="jp",
        card_count=81,
        release_date=None,
    )
    record = pokewallet_foundation.provider_card_record(
        {
            "id": "pk_test",
            "card_info": {
                "set_id": "24711",
                "set_code": "m5",
                "set_name": "Abyss Eye",
                "name": "Espurr",
                "clean_name": "Espurr",
                "card_number": "032/080",
            },
        },
        set_item,
        {},
    )

    assert record["imageUrl"] == "https://api.pokewallet.io/images/pk_test?size=low"
    assert record["imageUrlSmall"] == "https://api.pokewallet.io/images/pk_test?size=low"
    assert record["imageUrlLarge"] == "https://api.pokewallet.io/images/pk_test?size=high"
    assert record["providerImageSource"] == "pokewallet_api_image_endpoint"


def test_pokewallet_promotion_preserves_image_url_aliases() -> None:
    provider_record = promotion.ProviderRecord(
        language="jp",
        path=ROOT / "public" / "v1" / "provider-catalog" / "pokewallet" / "cards" / "jp" / "24711.json",
        file_set_id="24711",
        file_set_code="m5",
        file_set_name="Abyss Eye",
        card={
            "providerCardId": "pk_test",
            "providerSetId": "24711",
            "providerSetCode": "m5",
            "providerSetName": "Abyss Eye",
            "cardScanRLanguage": "jp",
            "providerLanguage": "jap",
            "cleanName": "Espurr",
            "cardNumber": "032/080",
            "imageUrlSmall": "https://api.pokewallet.io/images/pk_test?size=low",
            "imageUrlLarge": "https://api.pokewallet.io/images/pk_test?size=high",
            "providerImageSource": "pokewallet_api_image_endpoint",
        },
    )

    candidate, reason = promotion.build_candidate(
        provider_record,
        app_set_map={},
        enabled_languages={"jp"},
    )
    assert reason == "promotable"
    assert candidate is not None

    app_card = promotion.build_app_card(candidate)
    assert app_card["imageUrl"] == "https://api.pokewallet.io/images/pk_test?size=low"
    assert app_card["imageUrlSmall"] == app_card["imageSmall"]
    assert app_card["imageUrlLarge"] == app_card["imageLarge"]
    assert app_card["providerImageSource"] == "pokewallet_api_image_endpoint"
