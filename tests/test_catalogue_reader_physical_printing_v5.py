from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cardscanr_catalogue_identity import IDENTITY_MODEL_VERSION
from cardscanr_search_index.catalogue_reader import (
    DEFAULT_NUMBERING_POLICY,
    SetRecord,
    iter_catalogue_cards,
    load_numbering_policies,
)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class CatalogueReaderPhysicalPrintingV5Tests(unittest.TestCase):
    def test_set_record_default_numbering_policy(self) -> None:
        record = SetRecord(
            set_id="x",
            language="en",
            name="X",
            normalized_set_name="x",
            total=None,
            printed_total=None,
            release_date=None,
            ptcgo_code=None,
            series=None,
        )
        self.assertEqual(record.numbering_policy, DEFAULT_NUMBERING_POLICY)

    def test_policy_aware_ids_persisted_version_and_supplemental_quarantine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            catalogue_root = Path(tmp) / "v1"
            en_root = catalogue_root / "catalog" / "pokemon" / "en"
            policy_path = Path(tmp) / "numbering_policy_registry.json"
            _write_json(
                policy_path,
                {
                    "setPolicies": {
                        "defaultMainExpansion": {
                            "numberingPolicy": "SEQUENTIAL_FRACTION"
                        },
                        "cel25c": {
                            "numberingPolicy": "ORIGINAL_REPRINT_NUMBERING"
                        },
                    }
                },
            )
            self.assertEqual(
                load_numbering_policies(policy_path),
                {"cel25c": "ORIGINAL_REPRINT_NUMBERING"},
            )
            _write_json(
                en_root / "sets.json",
                {
                    "sets": [
                        {
                            "id": "cel25c",
                            "name": "Celebrations: Classic Collection",
                            "printedTotal": 25,
                            "total": 25,
                        }
                    ]
                },
            )
            _write_json(
                en_root / "cards" / "cel25c.json",
                {
                    "schemaVersion": "1.0.0",
                    "setId": "cel25c",
                    "language": "en",
                    "cards": [
                        {
                            "canonicalBaseId": "pokemon|en|cel25c|15-102|venusaur",
                            "setId": "cel25c",
                            "language": "en",
                            "collectorNumber": "15/102",
                            "name": "Venusaur",
                        },
                        {
                            "canonicalBaseId": "pokemon|en|cel25c|2|persisted",
                            "setId": "cel25c",
                            "language": "en",
                            "collectorNumber": "2",
                            "name": "Persisted",
                            "identityModelVersion": IDENTITY_MODEL_VERSION,
                            "physicalPrintingId": "persisted-physical-id",
                        },
                        {
                            "canonicalBaseId": "pokemon|en|cel25c|3|stale",
                            "setId": "cel25c",
                            "language": "en",
                            "collectorNumber": "3",
                            "name": "Stale",
                            "identityModelVersion": "physical-printing-v0",
                            "physicalPrintingId": "stale-physical-id",
                        },
                    ],
                },
            )
            _write_json(
                en_root / "approved_supplemental_sets.json",
                {
                    "sets": [
                        {
                            "id": "supp",
                            "name": "Supplemental",
                            "printedTotal": 10,
                            "total": 10,
                            "searchInclusion": "approved_supplemental",
                            "languageVerified": True,
                            "physicalProductVerified": True,
                            "rosterVerified": True,
                            "identityModelVersion": IDENTITY_MODEL_VERSION,
                            "authorityEvidenceId": "test-authority",
                        }
                    ]
                },
            )
            _write_json(
                en_root / "cards" / "supp.json",
                {
                    "schemaVersion": "1.0.0",
                    "setId": "supp",
                    "language": "en",
                    "cards": [
                        {
                            "canonicalBaseId": "pokemon|en|supp|1|good",
                            "setId": "supp",
                            "language": "en",
                            "collectorNumber": "1",
                            "name": "Good",
                            "baseCardReference": "pokemon|en|base1|1|good",
                            "printingClass": "PRIZE_PACK",
                            "productFamily": "Prize Pack Series One",
                            "stampType": "prize_pack",
                        },
                        {
                            "canonicalBaseId": "pokemon|en|other|2|wrong-set",
                            "setId": "other",
                            "language": "en",
                            "collectorNumber": "2",
                            "name": "Wrong Set",
                        },
                        {
                            "canonicalBaseId": "pokemon|jp|supp|3|wrong-language",
                            "setId": "supp",
                            "language": "jp",
                            "collectorNumber": "3",
                            "name": "Wrong Language",
                        },
                    ],
                },
            )

            records = list(
                iter_catalogue_cards(
                    catalogue_root,
                    languages=("en",),
                    numbering_policy_path=policy_path,
                )
            )
            by_id = {record.canonical_base_id: record for record in records}
            self.assertEqual(len(records), 4)
            self.assertEqual(
                by_id[
                    "pokemon|en|cel25c|15-102|venusaur"
                ].physical_printing_id,
                "physical-printing-v1|en|cel25c|15/102|normal",
            )
            self.assertEqual(
                by_id["pokemon|en|cel25c|15-102|venusaur"].identity_model_version,
                IDENTITY_MODEL_VERSION,
            )
            self.assertEqual(
                by_id["pokemon|en|cel25c|2|persisted"].physical_printing_id,
                "persisted-physical-id",
            )
            self.assertEqual(
                by_id["pokemon|en|cel25c|2|persisted"].identity_model_version,
                IDENTITY_MODEL_VERSION,
            )
            self.assertNotEqual(
                by_id["pokemon|en|cel25c|3|stale"].physical_printing_id,
                "stale-physical-id",
            )
            self.assertEqual(
                by_id["pokemon|en|cel25c|3|stale"].identity_model_version,
                IDENTITY_MODEL_VERSION,
            )
            supplemental = by_id["pokemon|en|supp|1|good"]
            self.assertEqual(supplemental.printing_class, "PRIZE_PACK")
            self.assertEqual(supplemental.product_family, "Prize Pack Series One")
            self.assertEqual(supplemental.stamp_type, "prize_pack")
            self.assertEqual(
                supplemental.base_card_reference,
                "pokemon|en|base1|1|good",
            )
            self.assertIn("printingClass:prize_pack", supplemental.variant_signature or "")
            self.assertIn("productVariant:prize_pack_series_one", supplemental.variant_signature or "")
            self.assertIn("stampType:prize_pack", supplemental.variant_signature or "")
            self.assertIn("pokemon|en|supp|1|good", by_id)
            self.assertNotIn("pokemon|en|other|2|wrong-set", by_id)
            self.assertNotIn("pokemon|jp|supp|3|wrong-language", by_id)


if __name__ == "__main__":
    unittest.main()
