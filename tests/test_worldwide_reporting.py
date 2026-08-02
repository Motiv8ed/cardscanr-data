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
