import json
from pathlib import Path

from cardscanr_worldwide.corrections import apply_corrections
from cardscanr_worldwide.schema import connect


def test_correction_requires_expected_value_and_preserves_provenance(tmp_path: Path) -> None:
    database = tmp_path / "staging.sqlite"
    registry = tmp_path / "corrections.json"
    registry.write_text(json.dumps({"schema_version":1,"corrections":[{
        "id":"fix","entity_type":"card_printing","entity_id":"print","field":"collector_number",
        "expected_value":"60","corrected_value":"80","reason":"evidence","evidence":["https://example.test"]
    }]}),encoding="utf-8")
    con=connect(str(database))
    con.execute("insert into source_provider values ('p','P','open_dataset',null,'approved_for_mirror',null,null,null)")
    con.execute("insert into import_run values ('r','p','completed','x',null,'{}','{}','2026',null,null)")
    con.execute("insert into source_snapshot values ('sn','p','r','x','"+'a'*64+"',null,1,'2026','x')")
    con.execute("insert into source_record values ('src','p','r','sn','card','c',null,'x','"+'b'*64+"','{}',null)")
    con.execute("insert into series values ('ser','p','s','src','S','{}')")
    con.execute("insert into card_set values ('set','ser','p','set','src','S','{}','main',1,'{}','{}')")
    con.execute("insert into set_release values ('rel','set','en','INTL','S','s',null,1,'source','src')")
    con.execute("insert into card_design values ('d','pokemon','Card','[]','d')")
    con.execute("insert into card_printing values ('print','d','rel','src','60','k',null,null,null,null,null,'[]',null,'[]','[]','[]','source','{}')")
    con.commit();con.close()

    assert apply_corrections(database,registry)=={"applied":1}
    con=connect(str(database))
    assert tuple(con.execute("select collector_number,verification_status from card_printing").fetchone()) == ("80","corroborated")
    assert con.execute("select count(*) from source_record where provider_id='cardscanr-catalogue-corrections'").fetchone()[0]==1
    con.close()
