import json
import sqlite3
from pathlib import Path

from cardscanr_worldwide.collectors.pokemon_asia_products import SCHEMA
from cardscanr_worldwide.pokemon_asia_products_import import import_checkpoint


def test_imports_official_product_image_and_contents(tmp_path: Path) -> None:
    checkpoint = tmp_path / "checkpoint.sqlite"
    with sqlite3.connect(checkpoint) as connection:
        connection.executescript(SCHEMA)
        connection.execute("insert into collector_runs values ('run','id','completed','now','now','{}',null)")
        connection.execute(
            "insert into pages values ('id','https://example.test/product',200,'sha',1,'text/html','raw','now',1,null)"
        )
        connection.execute(
            "insert into products values ('id','p1','https://example.test/product',0,'Booster Pack Test',"
            "'booster_pack','https://example.test/product.png',?,'sha','now')",
            (json.dumps({"contents": ["5 random cards"], "fields": {"Price": "20,000 IDR"}}),),
        )
    database = tmp_path / "worldwide.sqlite"
    result = import_checkpoint(database, checkpoint, "id")
    assert result == {"products": 1, "contents": 1, "image_candidates": 1}
    with sqlite3.connect(database) as connection:
        assert connection.execute("select product_type from sealed_product").fetchone() == ("booster_pack",)
        assert connection.execute("select rights_status from product_image_candidate").fetchone() == ("link_only",)
        assert connection.execute("pragma foreign_key_check").fetchall() == []
