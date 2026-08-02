from __future__ import annotations

import json
from pathlib import Path

from cardscanr_worldwide.asia_unparsed_product_page_classification import (
    classify_asia_unparsed_product_pages,
)
from cardscanr_worldwide.schema import connect


def test_marks_special_card_archive_nonblocking(tmp_path: Path) -> None:
    database = tmp_path / "catalogue.sqlite"
    connection = connect(str(database))
    page = "https://asia.pokemon-card.com/id/archive/special/card/s5/index.html"
    connection.execute(
        """insert into unresolved_item values (
             'u1','sealed_product_page',?,?,?,'official_product_page_unparsed',?,?, 'needs_review',0)""",
        (page, "id", "ID", "summary", json.dumps({"page_url": page})),
    )
    connection.commit()
    connection.close()

    result = classify_asia_unparsed_product_pages(database)
    assert result["counts"]["non_product_archive_pages"] == 1
    connection = connect(str(database))
    row = connection.execute("select status,externally_unavoidable from unresolved_item").fetchone()
    assert tuple(row) == ("classified_nonblocking", 0)
    connection.close()
