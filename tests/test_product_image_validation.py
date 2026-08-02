import io
import json
import sqlite3

from PIL import Image

from cardscanr_worldwide.product_image_validation import apply_results, inspect_image, register_candidates
from cardscanr_worldwide.schema import connect


def test_inspect_image_hashes_and_rejects_html() -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (128, 96), (20, 80, 140)).save(buffer, format="PNG")
    result = inspect_image(buffer.getvalue())
    assert (result["width"], result["height"], result["format"]) == (128, 96, "PNG")
    assert len(result["sha256"]) == 64
    assert len(result["average_hash"]) == 16
    assert len(result["difference_hash"]) == 16
    assert len(result["perceptual_hash"]) == 16
    try:
        inspect_image(b"<!doctype html><title>blocked</title>")
    except ValueError as error:
        assert "not an image" in str(error)
    else:
        raise AssertionError("HTML response was accepted as an image")


def test_apply_transient_result_preserves_rights_and_is_not_app_eligible(tmp_path) -> None:
    database = tmp_path / "catalogue.sqlite"
    connection = connect(str(database))
    connection.execute("insert into source_provider values ('p','P','official','https://example.test','link_only',null,null,null)")
    connection.execute("insert into import_run values ('r','p','completed','input',null,'{}','{}','now','now',null)")
    connection.execute("insert into source_snapshot values ('ss','p','r','input','sha',null,1,'now','raw')")
    connection.execute("insert into source_record values ('sr','p','r','ss','sealed_product','x',null,'https://example.test/x','sha','{}',null)")
    connection.execute("insert into sealed_product values ('sp','p','x','sr','Box','collection_box','verified','{}')")
    connection.execute("insert into sealed_product_variant values ('spv','sp','en','US','Box','standard',null,'{}')")
    connection.execute("insert into product_image_candidate values ('pic','spv','sr','p','display','https://example.test/x.png','link_only','candidate','{}')")
    connection.commit()
    connection.close()
    checkpoint = tmp_path / "checkpoint.sqlite"
    source_checkpoint = tmp_path / "source.sqlite"
    source = sqlite3.connect(source_checkpoint)
    source.execute("create table products(parsed_json text)")
    source.execute("insert into products values (?)", (json.dumps({"images": [{
        "canonical_url": "https://example.test/x.png",
        "source_url": "https://example.test/x.png?auth_key=temporary",
    }]}),))
    source.commit()
    source.close()
    register_candidates(database, checkpoint, source_checkpoint)
    progress = sqlite3.connect(checkpoint)
    assert progress.execute("select fetch_url from assets").fetchone() == (
        "https://example.test/x.png?auth_key=temporary",
    )
    progress.execute(
        """update assets set status='pass',attempts=1,attempted_at='now',http_status=200,
           content_type='image/png',byte_size=12,sha256='abc',cache_path='cache.png',result_json=?,error=null""",
        (json.dumps({"width": 100, "height": 120, "format": "PNG", "sha256": "abc"}),),
    )
    progress.commit()
    progress.close()
    assert apply_results(database, checkpoint) == {"pass": 1, "applied": 1}
    connection = sqlite3.connect(database)
    assert connection.execute("select rights_status,validation_status from product_image_candidate").fetchone() == (
        "link_only", "acquired_transient"
    )
    assert connection.execute("select status from image_validation_result").fetchone() == ("warning",)
    assert connection.execute("select outcome from image_acquisition_attempt").fetchone() == ("acquired",)
