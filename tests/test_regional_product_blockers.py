from __future__ import annotations

import json
from pathlib import Path

from cardscanr_worldwide.regional_product_blockers import register_regional_product_blockers
from cardscanr_worldwide.schema import connect


def test_registers_missing_product_region_and_ignores_unprinted_language(tmp_path: Path) -> None:
    database = tmp_path / "catalogue.sqlite"
    scope = tmp_path / "scope.json"
    scope.write_text(json.dumps({"languages": [
        {"code": "en", "officially_printed": True, "regional_variants": ["US", "GB"]},
        {"code": "pt-pt", "officially_printed": False, "regional_variants": ["PT"]},
    ]}), encoding="utf-8")
    connection = connect(str(database))
    connection.execute("insert into source_provider values ('p','p','official','u','metadata_only','a','t',null)")
    connection.execute("insert into import_run values ('r','p','completed','u','s','{}','{}','n','n',null)")
    connection.execute("insert into source_snapshot values ('snap','p','r','u','s',null,1,'n','u')")
    connection.execute("insert into source_record values ('src','p','r','snap','product','x',null,'u','s','{}',null)")
    connection.execute("insert into sealed_product values ('sp','p','x','src','Product','box','verified','{}')")
    connection.execute("insert into sealed_product_variant values ('v','sp','en','US','Product','standard',null,'{}')")
    connection.commit()
    connection.close()
    result = register_regional_product_blockers(database, scope)
    assert result["expected_language_regions"] == 2
    assert result["covered_language_regions"] == 1
    connection = connect(str(database))
    row = connection.execute("select language_code,region_code,status,externally_unavoidable from unresolved_item").fetchone()
    assert tuple(row) == ("en", "GB", "blocked_external", 1)

