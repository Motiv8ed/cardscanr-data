import json
import sqlite3

from cardscanr_worldwide.product_gap_evidence_import import import_evidence
from cardscanr_worldwide.schema import connect


def test_product_gap_evidence_is_idempotent_and_resolves_archive_gap(tmp_path) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({
        "schema_version": 1,
        "provider": {"id": "gap", "name": "Gap", "homepage": "https://example.test",
                     "attribution": "Test evidence"},
        "records": [{
            "provider_record_id": "missing:style", "related_archive_record_id": "missing",
            "name": "Corroborated Box", "product_type": "collection_box", "release_date": "2024-01-02",
            "contents": [{"type": "booster_pack", "name": "Booster packs", "quantity": 4}],
            "sources": [{"url": "https://example.test/official", "role": "official_identity"}],
        }],
    }), encoding="utf-8")
    database = tmp_path / "catalogue.sqlite"
    connection = connect(str(database))
    connection.execute(
        "insert into unresolved_item values ('u','source_product','missing','en','US','official_archive_collection_error','x','{}','open',0)"
    )
    connection.commit()
    connection.close()
    assert import_evidence(database, evidence) == {"products": 1, "product_contents": 1}
    assert import_evidence(database, evidence) == {"products": 1, "product_contents": 1}
    connection = sqlite3.connect(database)
    assert connection.execute("select count(*) from sealed_product").fetchone()[0] == 1
    assert connection.execute("select count(*) from product_content").fetchone()[0] == 1
    assert connection.execute("select verification_status from sealed_product").fetchone()[0] == "corroborated"
    assert connection.execute("select status from unresolved_item where id='u'").fetchone()[0] == "resolved"

