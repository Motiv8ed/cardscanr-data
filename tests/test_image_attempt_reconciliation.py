from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from cardscanr_worldwide.image_attempt_reconciliation import reconcile_tcgdex_english_missing_images
from cardscanr_worldwide.schema import connect


def _source(connection: sqlite3.Connection, provider: str, suffix: str) -> str:
    run = f"run-{suffix}"
    snapshot = f"snapshot-{suffix}"
    source = f"source-{suffix}"
    connection.execute(
        "insert into import_run values (?,?, 'completed','fixture',null,'{}','{}','now','now',null)",
        (run, provider),
    )
    connection.execute(
        "insert into source_snapshot values (?,?,?,'fixture',?,null,1,'now','fixture')",
        (snapshot, provider, run, suffix * 64),
    )
    connection.execute(
        "insert into source_record values (?,?,?,?, 'card',?,null,'fixture',?,'{}',null)",
        (source, provider, run, snapshot, f"record-{suffix}", suffix * 64),
    )
    return source


def _printing(
    connection: sqlite3.Connection,
    *,
    provider: str,
    source: str,
    prefix: str,
    set_name: str,
    collector: str,
    card_name: str,
) -> str:
    connection.execute("insert into series values (?,?,?,?,?,?)", (f"series-{prefix}", provider, prefix, source, prefix, "{}"))
    connection.execute(
        "insert into card_set values (?,?,?,?,?,?,?,?,?,?,?)",
        (f"set-{prefix}", f"series-{prefix}", provider, prefix, source, set_name, "{}", "promo", 1, "{}", "{}"),
    )
    connection.execute(
        "insert into set_release values (?,?,?,?,?,?,?,?,?,?)",
        (f"release-{prefix}", f"set-{prefix}", "en", "INTL", set_name, prefix, None, 1, "source", source),
    )
    connection.execute("insert into card_design values (?,?,?,?,?)", (f"design-{prefix}", "card", card_name, "[]", f"design-{prefix}"))
    connection.execute(
        "insert into card_printing values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"printing-{prefix}", f"design-{prefix}", f"release-{prefix}", source, collector, collector,
         None, None, "Pokemon", None, None, "[]", None, "[]", "[]", "[]", "source", "{}"),
    )
    connection.execute(
        "insert into card_variant values (?,?, 'unspecified',null,null,null,null,0,'{}','unknown')",
        (f"variant-{prefix}", f"printing-{prefix}"),
    )
    connection.execute(
        "insert into card_localisation values (?, 'en', ?, null, '{}', 'source')",
        (f"printing-{prefix}", card_name),
    )
    return f"variant-{prefix}"


def test_exact_tcgdex_http_404_is_preserved_without_resolving_gap(tmp_path: Path) -> None:
    database = tmp_path / "catalogue.sqlite"
    connection = connect(str(database))
    for provider, rights in (
        ("pokemontcg-data", "metadata_only"),
        ("tcgdex-cards-database", "approved_for_mirror"),
        ("tcgdex-assets", "permission_pending"),
    ):
        connection.execute(
            "insert into source_provider values (?,?, 'dataset',null,?,null,null,null)",
            (provider, provider, rights),
        )
    pokemon_source = _source(connection, "pokemontcg-data", "a")
    tcgdex_source = _source(connection, "tcgdex-cards-database", "b")
    target = _printing(
        connection, provider="pokemontcg-data", source=pokemon_source, prefix="target",
        set_name="McDonald's Collection 2014", collector="1", card_name="Weedle",
    )
    unmatched = _printing(
        connection, provider="pokemontcg-data", source=pokemon_source, prefix="oddish",
        set_name="Scarlet & Violet Black Star Promos", collector="102", card_name="Oddish",
    )
    tcgdex_variant = _printing(
        connection, provider="tcgdex-cards-database", source=tcgdex_source, prefix="2014xy",
        set_name="McDonald's Collection 2014", collector="001", card_name="Weedle",
    )
    url = "https://assets.tcgdex.net/en/mc/2014xy/1/high.webp"
    connection.execute(
        "insert into card_image_candidate values ('tcgdex-image',?,?,?,?,?,?,?)",
        (tcgdex_variant, tcgdex_source, "tcgdex-assets", "display", url, "permission_pending", "candidate"),
    )
    for issue_id, variant in (("issue-match", target), ("issue-unmatched", unmatched)):
        connection.execute(
            "insert into unresolved_item values (?, 'card_variant', ?, 'en', 'INTL', ?, 'review', '{}', 'needs_review', 0)",
            (issue_id, variant, "card_image_identity_review"),
        )
    connection.commit(); connection.close()

    result = reconcile_tcgdex_english_missing_images(
        database,
        {url: {"method": "GET", "status": 404, "content_type": "image/webp", "body_bytes_observed": 127}},
        observed_at="2026-08-02T02:00:00+00:00",
        require_status=404,
    )

    assert result["exact_matches"] == 1
    assert result["unmatched"] == 1
    connection = sqlite3.connect(database)
    assert connection.execute("select outcome,http_status from image_acquisition_attempt").fetchone() == ("not_found", 404)
    assert connection.execute("select status from image_validation_result").fetchone() == ("fail",)
    assert connection.execute(
        "select validation_status from card_image_candidate where card_variant_id=? and provider_id='tcgdex-assets'",
        (target,),
    ).fetchone() == ("missing",)
    assert connection.execute("select status from unresolved_item where id='issue-match'").fetchone() == ("needs_review",)
    evidence = json.loads(connection.execute("select evidence_json from unresolved_item where id='issue-unmatched'").fetchone()[0])
    assert evidence["tcgdex_exact_crosswalk"]["status"] == "no_exact_match"
    assert connection.execute("pragma foreign_key_check").fetchall() == []
    connection.close()
