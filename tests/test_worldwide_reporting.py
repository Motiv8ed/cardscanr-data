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
    rendered = markdown(report)
    assert "not a completion declaration" in rendered
