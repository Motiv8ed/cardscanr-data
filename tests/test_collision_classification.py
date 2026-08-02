from pathlib import Path

from cardscanr_worldwide.collision_classification import classify_collisions
from cardscanr_worldwide.schema import connect


def test_distinct_provider_rows_are_classified_and_number_mismatch_is_reviewed(tmp_path: Path) -> None:
    database = tmp_path / "staging.sqlite"
    con = connect(str(database))
    con.execute("insert into source_provider values ('pokemontcg-data','P','open_dataset',null,'approved_for_mirror',null,null,null)")
    con.execute("insert into import_run values ('r','pokemontcg-data','completed','x',null,'{}','{}','2026',null,null)")
    con.execute("insert into source_snapshot values ('sn','pokemontcg-data','r','x','" + "a"*64 + "',null,1,'2026','x')")
    for i, provider_id in enumerate(("set-1", "set-2"), 1):
        con.execute("insert into source_record values (?,?,?,?,?,?,?,?,?,?,null)",
                    (f"src{i}","pokemontcg-data","r","sn","card",provider_id,None,"x",str(i)*64,
                     '{"id":"'+provider_id+'","number":"1"}'))
    con.execute("insert into series values ('ser','pokemontcg-data','ser','src1','S','{}')")
    con.execute("insert into card_set values ('set','ser','pokemontcg-data','set','src1','S','{}','main',2,'{}','{}')")
    con.execute("insert into set_release values ('rel','set','en','INTL','S','set',null,2,'source','src1')")
    for i in (1,2):
        con.execute("insert into card_design values (?,?,?,'[]',?)",(f"d{i}",'pokemon',f"Name{i}",f"d{i}"))
        con.execute("insert into card_printing values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"p{i}",f"d{i}",'rel',f"src{i}",'1',f"k{i}",None,None,None,None,None,'[]',None,'[]','[]','[]','source','{}'))
    con.commit();con.close()

    counters = classify_collisions(database)

    assert counters == {"groups": 1, "needs_review": 1, "printing_rows": 2}
    con = connect(str(database))
    row = con.execute("select status,evidence_json from unresolved_item").fetchone()
    assert row["status"] == "needs_review"
    assert "provider_id_collector_number_conflict" in row["evidence_json"]
    con.close()

