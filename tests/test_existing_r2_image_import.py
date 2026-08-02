from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from cardscanr_worldwide.existing_r2_image_import import import_existing_r2_manifests
from cardscanr_worldwide.schema import connect


def test_existing_r2_manifest_maps_exact_provider_id_and_preserves_unmatched(tmp_path: Path) -> None:
    database = tmp_path / "catalogue.sqlite"
    connection = connect(str(database))
    connection.execute("insert into source_provider values ('pokemontcg-data','p','dataset',null,'approved_for_mirror',null,null,null)")
    connection.execute("insert into import_run values ('run','pokemontcg-data','completed','x',null,'{}','{}','now','now',null)")
    connection.execute("insert into source_snapshot values ('snap','pokemontcg-data','run','x','sha',null,1,'now','x')")
    connection.execute("insert into source_record values ('src','pokemontcg-data','run','snap','card','set-1',null,'x','sha','{}',null)")
    connection.execute("insert into series values ('series','pokemontcg-data','series','src','Series','{}')")
    connection.execute("insert into card_set values ('set','series','pokemontcg-data','set','src','Set','{}','main',1,'{}','{}')")
    connection.execute("insert into set_release values ('release','set','en','INTL','Set','SET',null,1,'source','src')")
    connection.execute("insert into card_design values ('design','card','Name','[]','design')")
    connection.execute("insert into card_printing values ('printing','design','release','src','1','1',null,null,null,null,null,'[]',null,'[]','[]','[]','source','{}')")
    connection.execute("insert into card_variant values ('variant','printing','unspecified',null,null,null,null,0,'{}','recognized')")
    connection.execute("insert into card_localisation values ('printing','en','Name',null,'{}','source')")
    connection.commit(); connection.close()
    row = {
        "card_id": "set-1", "set_id": "set", "language": "en", "variant": "standard",
        "source_provider": "images.example", "source_url": "https://source/image.png",
        "source_card_identifier": "pokemon|en|set|1|name", "rights_status": "approved_for_mirror",
        "r2_bucket": "bucket", "r2_display_key": "cards/display.webp", "r2_thumbnail_key": "cards/thumb.webp",
        "public_display_url": "https://cdn/cards/display.webp", "public_thumbnail_url": "https://cdn/cards/thumb.webp",
        "content_sha256": "a" * 64, "width": 700, "height": 1000, "byte_size": 123,
        "mime_type": "image/webp", "verification_status": "verified", "publicVerifySkipped": False,
        "uploaded_at": "2026-01-01T00:00:00Z",
    }
    unmatched = {**row, "card_id": "unknown", "source_card_identifier": None,
                 "content_sha256": "b" * 64, "public_display_url": "https://cdn/unknown/display.webp",
                 "public_thumbnail_url": "https://cdn/unknown/thumb.webp"}
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps([row, unmatched]), encoding="utf-8")

    result = import_existing_r2_manifests(database, [manifest])

    assert result["mapped_en"] == 1
    assert result["identity_review"] == 1
    connection = sqlite3.connect(database)
    assert connection.execute("select count(*) from card_image_candidate where validation_status='verified'").fetchone()[0] == 2
    assert connection.execute("select count(*) from image_validation_result where status='pass'").fetchone()[0] == 2
    assert connection.execute("select count(*) from image_acquisition_attempt where outcome='acquired'").fetchone()[0] == 1
    assert connection.execute("select status from unresolved_item").fetchone() == ("needs_review",)
    assert connection.execute("pragma foreign_key_check").fetchall() == []
    connection.close()
