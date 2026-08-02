from __future__ import annotations

import json
from pathlib import Path

from cardscanr_worldwide.regional_blockers import classify_derived_regional_blockers
from cardscanr_worldwide.regional_skeletons import import_regional_skeletons
from cardscanr_worldwide.schema import connect
from cardscanr_worldwide.tcgdex import canonical_json


def test_classifies_only_derived_gaps_and_normalizes_portuguese_scope(tmp_path: Path) -> None:
    database = tmp_path / "catalogue.sqlite"
    connection = connect(str(database))
    connection.execute(
        "insert into source_provider values ('tcgdex-cards-database','TCGdex','open_dataset','u','approved_for_mirror','a','t','v')"
    )
    connection.execute(
        "insert into import_run values ('r','tcgdex-cards-database','completed','u','s','{}','{}','n','n',null)"
    )
    connection.execute(
        "insert into source_snapshot values ('snap','tcgdex-cards-database','r','u','s','v',1,'n','u')"
    )
    payload = canonical_json({"name": {"en": "Set", "pt": "Colecao"}})
    connection.execute(
        "insert into source_record values ('src','tcgdex-cards-database','r','snap','set','set',null,'p','s',?,null)",
        (payload,),
    )
    connection.execute("insert into series values ('ser','tcgdex-cards-database','ser','src','Series','{}')")
    connection.execute("insert into card_set values ('set','ser','tcgdex-cards-database','set','src','Set','{}','main',2,'{}','{}')")
    connection.execute("insert into set_release values ('en','set','en','INTL','Set','set','2020-01-01',2,'verified','src')")
    connection.execute("insert into set_release values ('pt','set','pt','INTL','Colecao','set','2020-01-01',2,'verified','src')")
    for number in ("1", "2"):
        connection.execute("insert into card_design values (?, 'pokemon', ?, '[]', ?)", (f"d{number}", f"Card {number}", f"key{number}"))
        connection.execute(
            "insert into card_printing values (?,?,?,?,?,?,null,null,'Pokemon',null,null,'[]',null,'[]','[]','[]','verified','{}')",
            (f"p{number}", f"d{number}", "en", "src", number, number),
        )
    connection.commit()
    connection.close()

    assert import_regional_skeletons(database, ["pt"])["printings"] == 2
    result = classify_derived_regional_blockers(database)
    assert result["normalization"]["set_releases"] == 1
    assert result["classification"]["items"] == 6

    connection = connect(str(database))
    assert connection.execute("select region_code from set_release where id='pt'").fetchone()[0] == "BR"
    assert connection.execute(
        "select count(*) from unresolved_item where status='blocked_external' and externally_unavoidable=1"
    ).fetchone()[0] == 6
    evidence = json.loads(connection.execute("select evidence_json from unresolved_item limit 1").fetchone()[0])
    assert evidence["external_blocker"]["official_database_report"].endswith("OFFICIAL_LOCALIZED_DATABASE_BLOCKER_20260802.md")

