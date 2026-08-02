from __future__ import annotations

from pathlib import Path

from cardscanr_worldwide.product_image_gap_classification import (
    classify_product_image_gaps,
    classify_variant,
)
from cardscanr_worldwide.schema import connect


def test_classify_variant_buckets() -> None:
    assert classify_variant("pokemontcg-data", "theme_deck", "Zap!", 0, None) == (
        "historical_theme_deck_no_image_source"
    )
    assert classify_variant("pokemon-cn-official", "booster_pack", "X", 1, "warning") == (
        "china_product_image_transient_only"
    )
    assert classify_variant("pokemon-asia-id-official", "booster_pack", "Pack A", 0, None) == (
        "asia_expansion_sku_no_pack_art_url"
    )
    assert classify_variant("pokemon-asia-sg-official", "promotional_pack", "P", 0, None) == (
        "asia_local_product_gallery_unavailable"
    )
    assert classify_variant(
        "pokemon-asia-th-products-official", "starter_deck", "Deck", 1, "fail"
    ) == "asia_gallery_invalid_or_placeholder_asset"
    assert classify_variant(
        "pokemon-asia-id-products-official", "official_product", "Isi produk:", 0, None
    ) == "product_parser_false_positive"


def test_registers_unresolved_items(tmp_path: Path) -> None:
    database = tmp_path / "catalogue.sqlite"
    connection = connect(str(database))
    connection.execute(
        "insert into source_provider values ('pokemontcg-data','p','community','u','metadata_only','a','t',null)"
    )
    connection.execute(
        "insert into import_run values ('r','pokemontcg-data','completed','u','s','{}','{}','n','n',null)"
    )
    connection.execute(
        "insert into source_snapshot values ('snap','pokemontcg-data','r','u','s',null,1,'n','u')"
    )
    connection.execute(
        "insert into source_record values ('src','pokemontcg-data','r','snap','product','x',null,'u','s','{}',null)"
    )
    connection.execute(
        "insert into sealed_product values ('sp','pokemontcg-data','x','src','Zap!','theme_deck','verified','{}')"
    )
    connection.execute(
        "insert into sealed_product_variant values ('v','sp','en','INTL','Zap!','standard',null,'{}')"
    )
    connection.commit()
    connection.close()

    result = classify_product_image_gaps(database)
    assert result["variants_without_pass_image"] == 1
    assert result["counts"]["class_historical_theme_deck_no_image_source"] == 1

    connection = connect(str(database))
    row = connection.execute(
        "select issue_class,status,externally_unavoidable from unresolved_item"
    ).fetchone()
    assert tuple(row) == ("historical_theme_deck_no_image_source", "blocked_external", 1)
    connection.close()
