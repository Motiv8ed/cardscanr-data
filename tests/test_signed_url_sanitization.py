import json
import sqlite3

from cardscanr_worldwide.schema import connect
from cardscanr_worldwide.signed_url_sanitization import sanitize_signed_urls


def test_sanitizer_keeps_canonical_url_and_removes_signed_values(tmp_path) -> None:
    database = tmp_path / "catalogue.sqlite"
    signed = "https://image.example.test/a.png?auth_key=temporary"
    connection = connect(str(database))
    connection.execute("insert into source_provider values ('p','P','official','https://example.test','link_only',null,null,null)")
    connection.execute("insert into import_run values ('r','p','completed','input',null,'{}','{}','now','now',null)")
    connection.execute("insert into source_snapshot values ('ss','p','r','input','sha',null,1,'now','raw')")
    raw = json.dumps({"images": [{"canonical_url": "https://image.example.test/a.png", "source_url": signed}]})
    connection.execute("insert into source_record values ('sr','p','r','ss','sealed_product','x',null,'https://example.test/x','sha',?,null)", (raw,))
    connection.execute("insert into sealed_product values ('sp','p','x','sr','Box','collection_box','verified',?)", (raw,))
    connection.execute("insert into sealed_product_variant values ('spv','sp','en','US','Box','standard',null,'{}')")
    connection.execute("insert into product_image_candidate values ('pic','spv','sr','p','display','https://image.example.test/a.png','link_only','candidate',?)",
                       (json.dumps({"signed_source_url": signed, "ordinal": 0}),))
    connection.commit()
    connection.close()
    assert sanitize_signed_urls(database, "p") == {"source_records": 1, "sealed_products": 1, "image_candidates": 1}
    connection = sqlite3.connect(database)
    values = [connection.execute("select raw_payload_json from source_record").fetchone()[0],
              connection.execute("select raw_product_json from sealed_product").fetchone()[0],
              connection.execute("select attributes_json from product_image_candidate").fetchone()[0]]
    assert all("auth_key" not in value and "temporary" not in value for value in values)
    assert "https://image.example.test/a.png" in values[0]

