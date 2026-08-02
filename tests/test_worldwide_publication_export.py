from __future__ import annotations

import json
from pathlib import Path

import pytest

from cardscanr_worldwide.publication_export import export_bundle
from cardscanr_worldwide.schema import connect
from cardscanr_worldwide.tcgdex import canonical_json


def test_publication_export_preserves_missing_native_name_and_variants(tmp_path: Path) -> None:
    database = tmp_path / "staging.sqlite"
    connection = connect(str(database))
    connection.execute("insert into source_provider values ('p','Provider','open_dataset',null,'approved_for_mirror',null,null,null)")
    connection.execute("insert into import_run values ('r','p','completed','fixture',null,'{}','{}','2026-01-01',null,null)")
    connection.execute("insert into source_snapshot values ('sn','p','r','fixture','" + "a" * 64 + "',null,1,'2026-01-01','fixture')")
    connection.execute("insert into source_record values ('src','p','r','sn','card','c1',null,'fixture','" + "b" * 64 + "','{}',null)")
    connection.execute("insert into series values ('ser','p','ser','src','Series','{}')")
    connection.execute("insert into card_set values ('set','ser','p','set','src','Set','{}','main',1,'{}','{}')")
    connection.execute("insert into set_release values ('rel','set','nl','INTL','Basis Set','base1','1999-01-01',1,'source','src')")
    connection.execute("insert into card_design values ('design','pokemon','Pikachu','[25]','design')")
    connection.execute("insert into card_printing values ('print','design','rel','src','58','58',null,null,'Pokemon',null,40,'[\"Lightning\"]',null,'[]','[]','[]','provisional','{}')")
    connection.execute("insert into card_variant values ('variant','print','unspecified',null,null,null,null,0,'{}','unknown')")
    connection.execute("insert into sealed_product values ('product','p','prod','src','Box','collection_box','verified','{}')")
    connection.execute("insert into sealed_product_variant values ('pv','product','nl','INTL','Box','standard',null,'{}')")
    connection.commit(); connection.close()

    manifest = export_bundle(database, tmp_path / "bundles", "v-test")

    card = json.loads((tmp_path / "bundles/v-test/cards.jsonl").read_text(encoding="utf-8"))
    assert card["nativeCardName"] is None
    assert card["nativeNameStatus"] == "missing"
    assert card["canonicalCardName"] == "Pikachu"
    variant = json.loads((tmp_path / "bundles/v-test/card_variants.jsonl").read_text(encoding="utf-8"))
    assert variant["canonicalVariantId"] == "variant"
    assert manifest["outputs"]["products.jsonl"]["rows"] == 1
    assert manifest["integrity"]["foreignKeyFailures"] == 0

    with pytest.raises(FileExistsError):
        export_bundle(database, tmp_path / "bundles", "v-test")

