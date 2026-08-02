import json
from pathlib import Path

from cardscanr_search_index.global_builder import build_global_search_index, verify_global_search_index


def test_global_index_excludes_authenticated_urls_and_preserves_region(tmp_path: Path) -> None:
    cards = tmp_path / "cards.jsonl"
    images = tmp_path / "images.jsonl"
    output = tmp_path / "global.sqlite"
    card = {"canonicalPrintingId":"p1","canonicalSetId":"s1","language":"es-419","region":"LATAM","nativeCardName":"Carta","englishCardName":"Card","nativeSetName":"Set Nativo","englishSetName":"Set","printedCollectorNumber":"001","normalizedCollectorNumber":"1","providerCardIds":{"pokewallet":"secret-card"},"providerSetIds":{"pokewallet":"set1"},"searchAliases":["alias"],"designations":[],"rarity":"rare","regulationMark":"G","releaseDate":"2026-01-01"}
    image = {"canonicalPrintingId":"p1","provider":"pokewallet","authenticationRequirement":"X-API-Key","directUseTechnicalStatus":"auth_server_only","normalizedThumbnailUrl":"https://api.pokewallet.io/images/secret-card?size=low","normalizedDisplayUrl":"https://api.pokewallet.io/images/secret-card?size=high","mirrorPermissionStatus":"pending"}
    cards.write_text(json.dumps(card)+"\n",encoding="utf-8")
    images.write_text(json.dumps(image)+"\n",encoding="utf-8")
    build_global_search_index(cards_path=cards,direct_images_path=images,output_path=output)
    result=verify_global_search_index(output)
    assert result["classification"]=="PASS"
    assert result["perLanguageCounts"]=={"es-419":1}
    assert result["authenticatedUrls"]==0


def test_global_index_marks_canonical_name_fallback_as_missing_native_text(tmp_path: Path) -> None:
    cards = tmp_path / "cards.jsonl"
    images = tmp_path / "images.jsonl"
    output = tmp_path / "global.sqlite"
    card = {"canonicalPrintingId":"p2","canonicalSetId":"s2","language":"nl","region":"INTL",
            "nativeCardName":None,"nativeNameStatus":"missing","canonicalCardName":"Pikachu",
            "englishCardName":None,"nativeSetName":"Basis Set","englishSetName":"Base Set",
            "printedCollectorNumber":"58","normalizedCollectorNumber":"58","providerCardIds":{},
            "providerSetIds":{},"searchAliases":["Pikachu"],"designations":[]}
    cards.write_text(json.dumps(card)+"\n",encoding="utf-8")
    images.write_text("",encoding="utf-8")
    build_global_search_index(cards_path=cards,direct_images_path=images,output_path=output)
    import sqlite3
    with sqlite3.connect(output) as connection:
        row = connection.execute(
            "select native_card_name,native_name_status,canonical_card_name from cards"
        ).fetchone()
    assert row == ("Pikachu", "missing", "Pikachu")


def test_global_index_includes_searchable_sealed_products_and_safe_images(tmp_path: Path) -> None:
    cards = tmp_path / "cards.jsonl"; cards.write_text("", encoding="utf-8")
    images = tmp_path / "images.jsonl"; images.write_text("", encoding="utf-8")
    products = tmp_path / "products.jsonl"
    product_contents = tmp_path / "product_contents.jsonl"
    product_images = tmp_path / "product_images.jsonl"
    output = tmp_path / "global.sqlite"
    products.write_text(json.dumps({"canonicalProductId":"product-1","productVariantId":"variant-1",
        "language":"en","region":"US","localName":"Pikachu Collection","canonicalName":"Pikachu Collection",
        "productType":"collection_box","releaseDate":"2026-01-01","attributes":{},
        "verificationStatus":"verified","providerProductIds":{"official":"p1"}})+"\n",encoding="utf-8")
    product_contents.write_text(json.dumps({"productVariantId":"variant-1","ordinal":0,"contentKind":"booster_pack",
        "entityId":None,"description":"4 booster packs","quantity":4,"attributes":{}})+"\n",encoding="utf-8")
    product_images.write_text(json.dumps({"productVariantId":"variant-1","provider":"official","imageRole":"display",
        "url":"https://example.test/product.png","authenticationRequirement":"not_required",
        "directUseTechnicalStatus":"verified","mirrorPermissionStatus":"link_only"})+"\n",encoding="utf-8")
    build_global_search_index(cards_path=cards,direct_images_path=images,products_path=products,
        product_contents_path=product_contents,direct_product_images_path=product_images,output_path=output)
    result=verify_global_search_index(output)
    assert result["classification"] == "PASS"
    assert result["products"] == 1
    assert result["productContents"] == 1
    import sqlite3
    with sqlite3.connect(output) as connection:
        row=connection.execute("select local_name,image_url from sealed_products").fetchone()
        fts=connection.execute("select count(*) from sealed_products_fts where sealed_products_fts match 'Pikachu'").fetchone()[0]
    assert row == ("Pikachu Collection", "https://example.test/product.png")
    assert fts == 1
