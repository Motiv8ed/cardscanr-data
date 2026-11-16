from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.ebay_scale.audit_report import load_audit_rows, summarise_sweeps, write_report
from cardscanr_market_engine.ebay_scale.config import should_write_set_sweep_live_price
from cardscanr_market_engine.ebay_scale.price_write_gate import apply_set_sweep_pricing_batch
from cardscanr_market_engine.ebay_scale.set_sweep_matcher import SetSweepCatalogueCard
from cardscanr_market_engine.ebay_scale.shadow_audit import (
    append_audit_log,
    build_set_sweep_audit_rows,
    record_set_sweep_shadow_audit,
)
from cardscanr_market_engine.ebay_scale.validation_cards import select_validation_per_card_ids

FIXTURE = ROOT / "tests" / "fixtures" / "ebay_scale" / "sv3_set_sweep_fixture.html"


def _catalogue() -> list[SetSweepCatalogueCard]:
    return [
        SetSweepCatalogueCard(
            catalogue_card_id="charizard-ex",
            card_name="Charizard ex",
            normalized_card_name="charizard ex",
            set_name="Obsidian Flames",
            set_code="sv3",
            collector_number="199/197",
            rarity="Ultra Rare",
        ),
        SetSweepCatalogueCard(
            catalogue_card_id="pidgey",
            card_name="Pidgey",
            normalized_card_name="pidgey",
            set_name="Obsidian Flames",
            set_code="sv3",
            collector_number="162/197",
            rarity="Common",
        ),
    ]


class ShadowAuditRowTests(unittest.TestCase):
    def test_audit_row_fields(self) -> None:
        html = FIXTURE.read_text(encoding="utf-8")
        per_card = {"charizard-ex": 82.0, "pidgey": 1.25}
        rows, _stats = build_set_sweep_audit_rows(
            sweep_id="test-sweep",
            set_code="sv3",
            set_name="Obsidian Flames",
            html=html,
            catalogue=_catalogue(),
            per_card_prices=per_card,
            validation_card_ids={"charizard-ex"},
        )
        self.assertGreaterEqual(len(rows), 2)
        charizard = next(r for r in rows if r.catalogue_card_id == "charizard-ex")
        self.assertEqual(charizard.rarity, "Ultra Rare")
        self.assertIsNotNone(charizard.set_sweep_median)
        self.assertEqual(charizard.per_card_price, 82.0)
        self.assertGreater(charizard.abs_diff_aud or 0, 0)
        self.assertTrue(charizard.validation_per_card_required)
        self.assertTrue(charizard.matched_listings)
        self.assertTrue(charizard.matched_listings[0].item_id.isdigit())
        self.assertIn("too_few_comps", charizard.suspected_mismatch_reasons)

    def test_append_jsonl_and_csv(self) -> None:
        html = FIXTURE.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            audit_dir = Path(tmp)
            rows, _ = build_set_sweep_audit_rows(
                sweep_id="sw1",
                set_code="sv3",
                set_name="Obsidian Flames",
                html=html,
                catalogue=_catalogue(),
                per_card_prices={"charizard-ex": 80.0, "pidgey": 1.0},
                validation_card_ids=set(),
            )
            path = append_audit_log(rows, audit_dir=audit_dir)
            self.assertTrue(path.exists())
            self.assertTrue((audit_dir / "set_sweep_audit.csv").exists())
            line = path.read_text(encoding="utf-8").strip().splitlines()[0]
            payload = json.loads(line)
            self.assertEqual(payload["setCode"], "sv3")


class ShadowNoWriteTests(unittest.TestCase):
    def test_shadow_records_without_live_write(self) -> None:
        html = FIXTURE.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["EBAY_SCALE_AUDIT_DIR"] = tmp
            with mock.patch.dict(
                os.environ,
                {
                    "EBAY_SCALE_ENABLED": "true",
                    "EBAY_SCALE_SET_SWEEP": "true",
                    "EBAY_SCALE_SHADOW_AUDIT": "true",
                    "EBAY_SCALE_SET_SWEEP_LIVE": "false",
                },
                clear=False,
            ):
                self.assertFalse(should_write_set_sweep_live_price())
                result = apply_set_sweep_pricing_batch(
                    sweep_id="trial-1",
                    set_code="sv3",
                    set_name="Obsidian Flames",
                    html=html,
                    catalogue=_catalogue(),
                    per_card_prices={"charizard-ex": 80.0, "pidgey": 1.0},
                )
                self.assertFalse(result["livePriceWrite"])
                self.assertFalse(result["livePricesApplied"])
                self.assertTrue(result["audit"]["recorded"])
                rows, _ = build_set_sweep_audit_rows(
                    sweep_id="x",
                    set_code="sv3",
                    set_name="Obsidian Flames",
                    html=html,
                    catalogue=_catalogue(),
                    per_card_prices={"charizard-ex": 80.0, "pidgey": 1.0},
                    validation_card_ids=set(),
                )
                self.assertTrue(all(not r.live_price_written for r in rows))
                self.assertTrue(all(not r.live_price_write_allowed for r in rows))

    def test_live_write_only_when_explicit(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "EBAY_SCALE_ENABLED": "true",
                "EBAY_SCALE_SET_SWEEP": "true",
                "EBAY_SCALE_SET_SWEEP_LIVE": "true",
            },
            clear=False,
        ):
            self.assertTrue(should_write_set_sweep_live_price())

    def test_shadow_off_skips_log(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            for k in list(os.environ):
                if k.startswith("EBAY_SCALE"):
                    del os.environ[k]
            out = record_set_sweep_shadow_audit(
                sweep_id="x",
                set_code="sv3",
                set_name="Obsidian Flames",
                html="<html></html>",
                catalogue=[],
                per_card_prices={},
                validation_card_ids=set(),
            )
            self.assertFalse(out["recorded"])


class ValidationCardSelectionTests(unittest.TestCase):
    def test_picks_top_value_cards(self) -> None:
        cats = _catalogue()
        refs = {"charizard-ex": 85.0, "pidgey": 1.0}
        picked = select_validation_per_card_ids(cats, reference_prices=refs, max_cards=1)
        self.assertEqual(picked, {"charizard-ex"})


class AuditReportTests(unittest.TestCase):
    def test_report_agreement_rates(self) -> None:
        since = datetime.now(timezone.utc) - timedelta(hours=1)
        rows = [
            {
                "sweepId": "s1",
                "setCode": "sv3",
                "setSweepMedian": 10.0,
                "perCardPrice": 11.0,
                "pctDiff": 9.09,
                "absDiffAud": 1.0,
                "matchedListings": [{"title": "T1"}],
                "suspectedMismatch": False,
                "recordedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            },
            {
                "sweepId": "s1",
                "setCode": "sv3",
                "setSweepMedian": 50.0,
                "perCardPrice": 10.0,
                "pctDiff": 400.0,
                "absDiffAud": 40.0,
                "matchedListings": [{"title": "Lot of cards"}],
                "suspectedMismatch": True,
                "suspectedMismatchReasons": ["outlier_vs_per_card_pipeline"],
                "recordedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            },
        ]
        summary = summarise_sweeps(rows)
        block = summary["perSweep"][0]
        self.assertEqual(block["agreementWithin15Pct"], 1)
        self.assertEqual(block["agreementWithin30Pct"], 1)
        self.assertEqual(summary["suspectedMismatchReasons"]["outlier_vs_per_card_pipeline"], 1)

    def test_cli_writes_markdown(self) -> None:
        html = FIXTURE.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            audit_dir = Path(tmp) / "audit"
            report_dir = Path(tmp) / "reports"
            audit_dir.mkdir()
            rows, _ = build_set_sweep_audit_rows(
                sweep_id="rep-sweep",
                set_code="sv3",
                set_name="Obsidian Flames",
                html=html,
                catalogue=_catalogue(),
                per_card_prices={"charizard-ex": 80.0, "pidgey": 1.2},
                validation_card_ids=set(),
            )
            append_audit_log(rows, audit_dir=audit_dir)
            with mock.patch(
                "cardscanr_market_engine.ebay_scale.audit_report.audit_reports_dir",
                return_value=report_dir,
            ):
                out = write_report(
                    since=None,
                    jsonl_path=audit_dir / "set_sweep_audit.jsonl",
                )
            self.assertTrue(out.exists())
            text = out.read_text(encoding="utf-8")
            self.assertIn("Obsidian", text)
            loaded = load_audit_rows(since=None, jsonl_path=audit_dir / "set_sweep_audit.jsonl")
            self.assertGreaterEqual(len(loaded), 2)


if __name__ == "__main__":
    unittest.main()