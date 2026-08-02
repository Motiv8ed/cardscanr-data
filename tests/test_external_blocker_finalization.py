from __future__ import annotations

import json
from pathlib import Path

from cardscanr_worldwide.external_blocker_finalization import finalize_external_blockers
from cardscanr_worldwide.schema import connect


def test_classifies_selected_classes_but_not_active_collection(tmp_path: Path) -> None:
    database = tmp_path / "catalogue.sqlite"
    connection = connect(str(database))
    connection.execute(
        "insert into unresolved_item values ('a','card_printing','a','zh-cn','CN','missing_official_local_name','x','{}','open',0)"
    )
    connection.execute(
        "insert into unresolved_item values ('b','source_card','b','th','TH','official_detail_not_collected','x','{}','open',0)"
    )
    connection.execute(
        "insert into unresolved_item values ('c','card_variant','c','ja','JP','missing_card_image','x','{}','open',0)"
    )
    connection.commit()
    connection.close()
    result = finalize_external_blockers(database)
    assert result["classification"]["items"] == 1
    connection = connect(str(database))
    assert connection.execute("select status from unresolved_item where id='a'").fetchone()[0] == "blocked_external"
    assert connection.execute("select status from unresolved_item where id='b'").fetchone()[0] == "open"
    assert connection.execute("select status from unresolved_item where id='c'").fetchone()[0] == "open"
    result = finalize_external_blockers(database, include_missing_card_images=True)
    assert result["classification"]["issue_missing_card_image"] == 1
    evidence = json.loads(connection.execute("select evidence_json from unresolved_item where id='a'").fetchone()[0])
    assert evidence["external_blocker"]["classification_policy"] == "evidence_exhausted_no_inference"

