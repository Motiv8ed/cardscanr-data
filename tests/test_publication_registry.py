from __future__ import annotations

import sqlite3
from pathlib import Path

from cardscanr_worldwide.publication_export import export_bundle
from cardscanr_worldwide.publication_registry import register_bundle
from cardscanr_worldwide.schema import connect


def test_register_bundle_verifies_and_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "catalogue.sqlite"
    connect(str(database)).close()
    export_bundle(database, tmp_path / "bundles", "v1")
    bundle = tmp_path / "bundles" / "v1"

    first = register_bundle(database, bundle)
    second = register_bundle(database, bundle)

    assert first == second
    assert first["status"] == "canary"
    connection = sqlite3.connect(database)
    assert connection.execute("select version,status,rollback_retained from publication_run").fetchone() == ("v1", "canary", 1)
    assert connection.execute("select count(*) from publication_artifact").fetchone()[0] == 14
    assert connection.execute("pragma foreign_key_check").fetchall() == []
    connection.close()
