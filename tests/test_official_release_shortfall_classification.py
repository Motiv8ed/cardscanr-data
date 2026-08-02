from __future__ import annotations

from pathlib import Path

from cardscanr_worldwide.official_release_shortfall_classification import (
    classify_official_release_shortfalls,
)
from cardscanr_worldwide.schema import connect


def test_classifies_unexplained_shortfall(tmp_path: Path) -> None:
    database = tmp_path / "catalogue.sqlite"
    connection = connect(str(database))
    connection.execute(
        "insert into source_provider values ('tcgdex-cards-database','t','community','u','metadata_only','a','t',null)"
    )
    connection.execute(
        "insert into import_run values ('r','tcgdex-cards-database','completed','u','s','{}','{}','n','n',null)"
    )
    connection.execute(
        "insert into source_snapshot values ('snap','tcgdex-cards-database','r','u','s',null,1,'n','u')"
    )
    connection.execute(
        "insert into source_record values ('src','tcgdex-cards-database','r','snap','set','x',null,'u','s','{}',null)"
    )
    connection.execute(
        "insert into series values ('ser','tcgdex-cards-database','ser','src','Series','{}')"
    )
    connection.execute(
        "insert into card_set values ('set','ser','tcgdex-cards-database','x','src','Set','{}','main',10,'{}','{}')"
    )
    connection.execute(
        "insert into set_release values ('rel','set','en','INTL','Set','x',null,10,'verified','src')"
    )
    connection.commit()
    connection.close()

    result = classify_official_release_shortfalls(database)
    assert result["counts"]["releases"] == 1
    connection = connect(str(database))
    row = connection.execute(
        "select issue_class,status,externally_unavoidable from unresolved_item"
    ).fetchone()
    assert tuple(row) == ("official_count_shortfall", "blocked_external", 1)
    connection.close()
