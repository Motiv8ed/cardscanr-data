import json
import sqlite3

from cardscanr_worldwide.product_image_fallback_import import import_fallbacks
from cardscanr_worldwide.schema import connect


def test_import_exact_archive_fallback_and_classify_icon(tmp_path) -> None:
    database = tmp_path / "catalogue.sqlite"
    connection = connect(str(database))
    connection.execute("insert into source_provider values ('p','P','archive','https://example.test','link_only',null,null,null)")
    connection.execute("insert into import_run values ('r','p','completed','input',null,'{}','{}','now','now',null)")
    connection.execute("insert into source_snapshot values ('ss','p','r','input','sha',null,1,'now','raw')")
    connection.execute("insert into source_record values ('sr','p','r','ss','sealed_product','x',null,'https://example.test/x','sha','{}',null)")
    connection.execute("insert into sealed_product values ('sp','p','x','sr','Box','collection_box','verified','{}')")
    connection.execute("insert into sealed_product_variant values ('spv','sp','en','US','Box','standard',null,'{}')")
    connection.execute("insert into product_image_candidate values ('original','spv','sr','p','display','https://example.test/missing.png','link_only','invalid','{}')")
    connection.execute("insert into product_image_candidate values ('icon','spv','sr','p','display','https://example.test/icon.gif','link_only','invalid','{}')")
    connection.commit(); connection.close()
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({
        "schema_version": 1, "provider_id": "p",
        "excluded_non_product_urls": ["https://example.test/icon.gif"],
        "fallbacks": [{"original_url": "https://example.test/missing.png",
                       "archive_url": "https://archive.test/missing.png", "archive_timestamp": "20200101",
                       "archive_digest": "digest"}],
        "additional_candidates": [{"product_provider_record_id": "x",
                                   "source_url": "https://archive.test/product.png",
                                   "match_method": "exact_slug"}],
    }), encoding="utf-8")
    assert import_fallbacks(database, evidence) == {"excluded_non_product_icons": 1,
                                                    "archive_fallback_candidates": 1,
                                                    "additional_exact_candidates": 1}
    connection = sqlite3.connect(database)
    assert connection.execute("select count(*) from product_image_candidate").fetchone()[0] == 4
    assert connection.execute("select validation_status from product_image_candidate where id='icon'").fetchone() == (
        "invalid",
    )
