import sqlite3

from cardscanr_worldwide.japanese_missing_image_reconciliation import PROVIDER_ID, reconcile
from cardscanr_worldwide.schema import connect


def test_reconcile_uses_exact_set_and_collector_not_card_name(tmp_path) -> None:
    database = tmp_path / "catalogue.sqlite"
    connection = connect(str(database))
    connection.execute("insert into source_provider values (?,?,?,?,?,?,?,?)",
                       (PROVIDER_ID, "Official", "official", "https://example.test", "metadata_only", None, None, None))
    connection.execute("insert into import_run values ('r',?,'completed','in',null,'{}','{}','now','now',null)", (PROVIDER_ID,))
    connection.execute("insert into source_snapshot values ('ss',?,'r','in','sha',null,1,'now','raw')", (PROVIDER_ID,))
    connection.execute("insert into source_record values ('src',?,'r','ss','card','official-1',null,'https://example.test/card','sha','{}',null)", (PROVIDER_ID,))
    connection.execute("insert into series values ('series',?,'official','src','Series','{}')", (PROVIDER_ID,))
    connection.execute("insert into card_set values ('set','series',?,'OFF','src','Set','{}','main',10,'null','{}')", (PROVIDER_ID,))
    connection.execute("insert into set_release values ('official-release','set','ja','JP','基本拡張パック','OFF',null,10,'verified','src')")
    connection.execute("insert into card_design values ('design','pokemon','Official name','[]','official-1')")
    connection.execute("insert into card_printing values ('printing','design','official-release','src','1','official-1',null,null,null,null,null,'[]',null,'[]','[]','[]','verified','{}')")
    connection.execute("insert into card_variant values ('official-variant','printing','unspecified',null,null,null,null,0,'{}','unknown')")
    connection.execute("insert into card_localisation values ('printing','ja','ガス','', '[]','official')")
    connection.execute("insert into card_image_candidate values ('official-image','official-variant','src',?,'display','https://example.test/card.png','link_only','candidate')", (PROVIDER_ID,))
    connection.execute("insert into provider_entity_mapping values (?, 'card','official-1','card_variant','official-variant','official_depiction','verified','src','{}')", (PROVIDER_ID,))
    connection.execute("insert into card_set values ('target-set','series',?,'E1','src','Set','{}','main',10,'null','{}')", (PROVIDER_ID,))
    connection.execute("insert into set_release values ('target-release','target-set','ja','JP','基本 拡張パック','E1',null,10,'verified','src')")
    connection.execute("insert into card_printing values ('target-printing','design','target-release','src','001','target',null,null,null,null,null,'[]',null,'[]','[]','[]','verified','{}')")
    connection.execute("insert into card_variant values ('target-variant','target-printing','unspecified',null,null,null,null,0,'{}','unknown')")
    connection.execute("insert into card_localisation values ('target-printing','ja','Koffing','', '[]','official')")
    connection.execute("insert into unresolved_item values ('u','card_variant','target-variant','ja','JP','missing_card_image','missing','{}','open',0)")
    connection.commit()
    connection.close()
    report = reconcile(database)
    assert report["exact_candidates"] == 1
    connection = sqlite3.connect(database)
    assert connection.execute("select source_url from card_image_candidate where card_variant_id='target-variant'").fetchone() == (
        "https://example.test/card.png",
    )
    assert connection.execute("select status from unresolved_item where id='u'").fetchone() == ("open",)
