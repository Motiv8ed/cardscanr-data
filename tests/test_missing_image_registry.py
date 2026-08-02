import json
import sqlite3
import zipfile

from cardscanr_worldwide.missing_image_registry import import_registry
from cardscanr_worldwide.schema import connect


def test_import_registry_requires_exact_url_identity_and_preserves_evidence(tmp_path) -> None:
    database = tmp_path / "catalogue.sqlite"
    connection = connect(str(database))
    connection.execute(
        "insert into source_provider values ('assets', 'Assets', 'dataset', null, 'permission_pending', null, null, null)"
    )
    connection.execute(
        "insert into source_provider values ('cards', 'Cards', 'dataset', null, 'metadata_only', null, null, null)"
    )
    connection.execute(
        "insert into import_run values ('run', 'cards', 'completed', 'x', 'sha', '{}', '{}', 'now', 'now', null)"
    )
    connection.execute(
        "insert into source_snapshot values ('snap', 'cards', 'run', 'x', 'sha', null, 1, 'now', 'x')"
    )
    connection.execute(
        "insert into source_record values ('record', 'cards', 'run', 'snap', 'card', 'one', null, 'x', 'sha', '{}', null)"
    )
    connection.execute(
        "insert into series values ('series', 'cards', 'SERIES', 'record', 'Series', '{}')"
    )
    connection.execute(
        "insert into card_set values ('set', 'series', 'cards', 'S1', 'record', 'Set', '{}', 'main', 1, '{}', '{}')"
    )
    connection.execute(
        "insert into set_release values ('release', 'set', 'ja', 'JP', 'Set', 'S1', null, 1, 'source', 'record')"
    )
    connection.execute("insert into card_design values ('design', 'card', 'Name', '[]', 'name')")
    connection.execute(
        "insert into card_printing values ('printing', 'design', 'release', 'record', '001', '001', null, null, null, null, null, '[]', null, '[]', '[]', '[]', 'source', '{}')"
    )
    connection.execute(
        "insert into card_variant values ('variant', 'printing', 'standard', null, null, null, null, 0, '{}', 'recognized')"
    )
    for role, url in (("display", "https://example/high.webp"), ("thumbnail", "https://example/low.webp")):
        connection.execute(
            "insert into card_image_candidate values (?, 'variant', 'record', 'assets', ?, ?, 'permission_pending', 'candidate')",
            (role, role, url),
        )
    connection.commit()
    connection.close()

    record = {
        "canonical_identity": "card|S1|001|ja", "canonical_card_id": "card|S1|001",
        "language": "ja", "region": "Japan", "phase9_category": "permanent_failure",
        "failure_reason": "download HTTP 404", "collision_risks": [],
        "required_replacement_evidence": ["exact identity"], "provenance_stream": "original",
        "previously_attempted_sources": [
            {"provider": "example", "url": "https://example/high.webp", "status": 404},
            {"provider": "example", "url": "https://example/low.webp", "status": 404},
        ],
    }
    payload = {"schema_version": 1, "identity_count": 1, "records": [record]}
    package = tmp_path / "missing.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("snapshot/UNRESOLVED_2907_INVENTORY.json", json.dumps(payload))

    result = import_registry(database, package)
    assert result == {"candidate_urls_marked_missing": 2, "records": 1, "missing_card_image": 1}
    connection = sqlite3.connect(database)
    assert connection.execute("select distinct validation_status from card_image_candidate").fetchall() == [("missing",)]
    issue = connection.execute(
        "select entity_id,language_code,region_code,status,externally_unavoidable,evidence_json from unresolved_item"
    ).fetchone()
    assert issue[:5] == ("variant", "ja", "JP", "open", 0)
    evidence = json.loads(issue[5])
    assert evidence["failure_reason"] == "download HTTP 404"
    assert len(evidence["previously_attempted_sources"]) == 2
