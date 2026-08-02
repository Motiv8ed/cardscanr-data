from pathlib import Path

from cardscanr_worldwide.reporting import build_report, markdown
from cardscanr_worldwide.schema import connect


def test_empty_staging_report_runs_integrity_gates(tmp_path: Path) -> None:
    database = tmp_path / "catalogue.sqlite"
    connect(str(database)).close()
    report = build_report(database)
    assert report["integrity"]["sqlite_integrity_check"] == "ok"
    assert report["integrity"]["foreign_key_failure_count"] == 0
    assert report["counts"]["card_printing"] == 0
    assert report["counts"]["image_acquisition_attempt"] == 0
    assert report["counts"]["image_validation_result"] == 0
    assert report["counts"]["publication_run"] == 0
    assert len(report["database_sha256"]) == 64
    expected = {row["language_code"]: row for row in report["expected_language_matrix"]}
    assert expected["ko"]["inventory_status"] == "enumerated_zero_printings"
    assert expected["pt-pt"]["expected_regions"] == ["PT"]
    gates = report["publication_gates"]
    assert gates["missing_core_provenance"] == 0
    assert gates["orphan_product_contents"] == 0
    assert gates["external_blocker_state_mismatches"] == 0
    assert gates["officially_printed_languages_without_records"] > 0
    rendered = markdown(report)
    assert "not a completion declaration" in rendered


def test_regional_variant_blocker_may_be_attached_to_printing(tmp_path: Path) -> None:
    database = tmp_path / "catalogue.sqlite"
    connection = connect(str(database))
    connection.execute("insert into source_provider values ('p','p','official','u','metadata_only','a','t',null)")
    connection.execute("insert into import_run values ('r','p','completed','u','s','{}','{}','n','n',null)")
    connection.execute("insert into source_snapshot values ('snap','p','r','u','s',null,1,'n','u')")
    connection.execute("insert into source_record values ('src','p','r','snap','card','x',null,'u','s','{}',null)")
    connection.execute("insert into series values ('ser','p','ser','src','Series','{}')")
    connection.execute("insert into card_set values ('set','ser','p','set','src','Set','{}','main',1,'{}','{}')")
    connection.execute("insert into set_release values ('rel','set','en','US','Set','set',null,1,'verified','src')")
    connection.execute("insert into card_design values ('d','pokemon','Card','[]','key')")
    connection.execute("insert into card_printing values ('print','d','rel','src','1','1',null,null,null,null,null,'[]',null,'[]','[]','[]','provisional','{}')")
    connection.execute("insert into card_variant values ('variant','print','regional-variant-unclassified',null,null,null,null,0,'{}','unknown')")
    connection.execute("insert into unresolved_item values ('u','card_printing','print','en','US','regional_variant_unclassified','x','{}','blocked_external',1)")
    connection.commit()
    connection.close()
    assert build_report(database)["publication_gates"]["unclassified_regional_variants"] == 0
