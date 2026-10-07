from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from tools.repair_en_catalogue_pokewallet_duplicates import repair_catalogue


def _write_catalogue(root: Path, cards: list[dict]) -> tuple[Path, Path]:
    en_root = root / "catalog" / "pokemon" / "en"
    cards_dir = en_root / "cards"
    cards_dir.mkdir(parents=True)
    sets = {
        "sets": [
            {"id": "base1", "name": "Base", "printedTotal": 100, "total": 100},
            {"id": "base01", "name": "Clone candidate", "printedTotal": 100, "total": 100},
        ],
        "setCount": 2,
        "cardCount": len(cards) + 1,
    }
    base_doc = {
        "schemaVersion": "1.0.0",
        "setId": "base1",
        "language": "en",
        "cardCount": len(cards),
        "cards": cards,
    }
    clone_doc = {
        "schemaVersion": "1.0.0",
        "setId": "base01",
        "language": "en",
        "cardCount": 1,
        "cards": [
            {
                "canonicalBaseId": "pokemon|en|base01|1|alpha",
                "setId": "base01",
                "language": "en",
                "collectorNumber": "1",
                "name": "Alpha",
            }
        ],
    }
    sets_path = en_root / "sets.json"
    clone_path = cards_dir / "base01.json"
    sets_path.write_text(json.dumps(sets), encoding="utf-8")
    (cards_dir / "base1.json").write_text(json.dumps(base_doc), encoding="utf-8")
    clone_path.write_text(json.dumps(clone_doc), encoding="utf-8")
    return en_root, clone_path


class ApprovedPlanDedupTests(unittest.TestCase):
    def test_apply_requires_approved_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            en_root, _ = _write_catalogue(Path(tmp), [])
            with self.assertRaisesRegex(SystemExit, "--apply requires --approved-plan"):
                repair_catalogue(
                    en_root=en_root,
                    apply=True,
                    report_path=Path(tmp) / "report.json",
                    numbering_policy_registry_path=None,
                )

    def test_only_exact_approved_pair_is_removed_and_clone_is_preserved(self) -> None:
        cards = [
            {
                "canonicalBaseId": "pokemon|en|base1|1|alpha",
                "setId": "base1",
                "language": "en",
                "collectorNumber": "1",
                "name": "Alpha",
                "imageSource": "pokemon_tcg_api",
            },
            {
                "canonicalBaseId": "pokemon|en|base1|001|alpha",
                "setId": "base1",
                "language": "en",
                "collectorNumber": "001/100",
                "name": "Alpha",
                "imageSource": "pokewallet",
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            en_root, clone_path = _write_catalogue(root, cards)
            sets_path = en_root / "sets.json"
            sets_before = sets_path.read_bytes()
            clone_before = clone_path.read_bytes()
            dry_report = root / "dry.json"
            report = repair_catalogue(
                en_root=en_root,
                apply=False,
                report_path=dry_report,
                numbering_policy_registry_path=None,
            )
            pairs = report["candidatePlan"]["candidatePairs"]
            self.assertEqual(len(pairs), 1)
            self.assertFalse(pairs[0]["identityEvidence"]["provider_equivalence"])
            self.assertFalse(pairs[0]["dedupeAuthorized"])
            self.assertTrue(all(not item["autoDeleteAllowed"] for item in report["cloneSets"]))

            approved_pair = copy.deepcopy(pairs[0])
            approved_pair["identityEvidence"]["provider_equivalence"] = True
            approved_path = root / "approved.json"
            approved_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": "dedup-approved-plan-v5",
                        "approvedPairs": [approved_pair],
                    }
                ),
                encoding="utf-8",
            )
            apply_report = repair_catalogue(
                en_root=en_root,
                apply=True,
                report_path=root / "apply.json",
                approved_plan_path=approved_path,
                numbering_policy_registry_path=None,
            )
            base_doc = json.loads(
                (en_root / "cards" / "base1.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(base_doc["cards"]), 1)
            self.assertEqual(
                base_doc["cards"][0]["canonicalBaseId"],
                "pokemon|en|base1|1|alpha",
            )
            self.assertEqual(apply_report["summary"]["duplicateCardsRemoved"], 1)
            self.assertEqual(sets_path.read_bytes(), sets_before)
            self.assertEqual(clone_path.read_bytes(), clone_before)

    def test_fuzzy_name_match_cannot_authorize_deletion(self) -> None:
        cards = [
            {
                "canonicalBaseId": "pokemon|en|base1|1|drowsee",
                "setId": "base1",
                "language": "en",
                "collectorNumber": "1",
                "name": "Drowsee",
            },
            {
                "canonicalBaseId": "pokemon|en|base1|001|drowzee",
                "setId": "base1",
                "language": "en",
                "collectorNumber": "001/100",
                "name": "Drowzee",
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            en_root, _ = _write_catalogue(root, cards)
            report = repair_catalogue(
                en_root=en_root,
                apply=False,
                report_path=root / "dry.json",
                numbering_policy_registry_path=None,
            )
            pair = copy.deepcopy(report["candidatePlan"]["candidatePairs"][0])
            self.assertTrue(pair["identityEvidence"]["fuzzyNamesCompatible"])
            self.assertFalse(pair["identityEvidence"]["exactNameFingerprint"])
            pair["identityEvidence"]["provider_equivalence"] = True
            approved_path = root / "approved.json"
            approved_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": "dedup-approved-plan-v5",
                        "approvedPairs": [pair],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SystemExit, "exact name fingerprint"):
                repair_catalogue(
                    en_root=en_root,
                    apply=True,
                    report_path=root / "apply.json",
                    approved_plan_path=approved_path,
                    numbering_policy_registry_path=None,
                )


if __name__ == "__main__":
    unittest.main()
