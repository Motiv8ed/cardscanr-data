import json
import sqlite3

from cardscanr_worldwide.card_image_validation import apply_results, register_candidates
from cardscanr_worldwide.schema import connect


def test_verified_card_image_resolves_missing_registry_item(tmp_path) -> None:
    database = tmp_path / "catalogue.sqlite"
    connection = connect(str(database))
    connection.execute("insert into source_provider values ('p','P','official','https://example.test','link_only',null,null,null)")
    connection.execute("insert into import_run values ('r','p','completed','input',null,'{}','{}','now','now',null)")
    connection.execute("insert into source_snapshot values ('ss','p','r','input','sha',null,1,'now','raw')")
    connection.execute("insert into source_record values ('sr','p','r','ss','card','card-1',null,'https://example.test/1','sha','{}',null)")
    connection.execute("insert into series values ('series','p','series','sr','Series','{}')")
    connection.execute("insert into card_set values ('set','series','p','set','sr','Set','{}','main',1,'null','{}')")
    connection.execute("insert into set_release values ('release','set','ja','JP','Set','S',null,1,'verified','sr')")
    connection.execute("insert into card_design values ('design','pokemon','Card','[]','key')")
    connection.execute("insert into card_printing values ('printing','design','release','sr','1','card-1',null,null,null,null,null,'[]',null,'[]','[]','[]','verified','{}')")
    connection.execute("insert into card_variant values ('variant','printing','unspecified',null,null,null,null,0,'{}','unknown')")
    connection.execute("insert into card_image_candidate values ('candidate','variant','sr','p','display','https://example.test/1.png','link_only','candidate')")
    connection.execute("insert into provider_entity_mapping values ('p','card','card-1','card_variant','variant','direct','verified','sr','{}')")
    connection.execute("insert into unresolved_item values ('u','card_variant','variant','ja','JP','missing_card_image','missing','{}','open',0)")
    connection.commit()
    connection.close()
    checkpoint = tmp_path / "checkpoint.sqlite"
    register_candidates(database, checkpoint)
    progress = sqlite3.connect(checkpoint)
    progress.execute(
        """update assets set status='pass',attempts=1,attempted_at='now',http_status=200,
           content_type='image/png',byte_size=12,sha256='abc',cache_path='cache.png',result_json=?,error=null""",
        (json.dumps({"width": 100, "height": 120, "format": "PNG", "sha256": "abc"}),),
    )
    progress.commit()
    progress.close()
    assert apply_results(database, checkpoint) == {"pass": 1, "applied": 1}
    connection = sqlite3.connect(database)
    assert connection.execute("select validation_status from card_image_candidate").fetchone() == ("verified",)
    assert connection.execute("select status from unresolved_item").fetchone() == ("resolved",)


def test_known_pokellector_watermark_stays_blocked(tmp_path) -> None:
    database = tmp_path / "catalogue.sqlite"
    connection = connect(str(database))
    provider = "pokellector-english-gap-evidence"
    connection.execute("insert into source_provider values (?,?, 'community_corroboration','https://example.test','permission_pending',null,null,null)",(provider,"P"))
    connection.execute("insert into import_run values ('r',?,'completed','input',null,'{}','{}','now','now',null)",(provider,))
    connection.execute("insert into source_snapshot values ('ss',?,'r','input','sha',null,1,'now','raw')",(provider,))
    connection.execute("insert into source_record values ('sr',?,'r','ss','card','card-1',null,'page','sha','{}',null)",(provider,))
    connection.execute("insert into series values ('series',?,'series','sr','Series','{}')",(provider,))
    connection.execute("insert into card_set values ('set','series',?,'set','sr','Set','{}','main',1,'null','{}')",(provider,))
    connection.execute("insert into set_release values ('release','set','en','US','Set','S',null,1,'verified','sr')")
    connection.execute("insert into card_design values ('design','pokemon','Card','[]','key')")
    connection.execute("insert into card_printing values ('printing','design','release','sr','1','card-1',null,null,null,null,null,'[]',null,'[]','[]','[]','verified','{}')")
    connection.execute("insert into card_variant values ('variant','printing','unspecified',null,null,null,null,0,'{}','unknown')")
    connection.execute("insert into card_image_candidate values ('candidate','variant','sr',?,'display','https://example.test/1.png','permission_pending','candidate')",(provider,))
    connection.execute("insert into provider_entity_mapping values (?,'card','card-1','card_variant','variant','direct','verified','sr','{}')",(provider,))
    connection.execute("insert into unresolved_item values ('u','card_variant','variant','en','US','card_image_identity_review','missing','{}','open',0)")
    connection.commit(); connection.close()
    checkpoint = tmp_path / "checkpoint.sqlite"; register_candidates(database, checkpoint, [provider])
    progress=sqlite3.connect(checkpoint)
    progress.execute("""update assets set status='pass',attempts=1,attempted_at='now',http_status=200,
      content_type='image/png',byte_size=12,sha256='abc',cache_path='cache.png',result_json='{}',error=null""")
    progress.commit(); progress.close()
    result=apply_results(database,checkpoint)
    assert result["known_watermark_blockers"] == 1
    connection=sqlite3.connect(database)
    assert connection.execute("select validation_status from card_image_candidate").fetchone() == ("blocked",)
    assert connection.execute("select status,externally_unavoidable from unresolved_item").fetchone() == ("blocked_external",1)
    assert connection.execute("select status from image_validation_result").fetchone() == ("warning",)
