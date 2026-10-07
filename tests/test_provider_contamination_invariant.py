"""Invariant: impossible set/card product phrases must not become canonical EN cards.

PROVIDER_CONTAMINATION case: xy9 Prinplup with Diamond/Pearl product naming.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

EN = Path(r"D:\cardscanr-data\public\v1\catalog\pokemon\en")


def _contamination_hits(cards: list[dict], set_id: str) -> list[dict]:
    hits = []
    for card in cards:
        name = str(card.get("name") or "")
        lower = name.casefold()
        if set_id == "xy9" and "prinplup" in lower and "diamond" in lower:
            hits.append(card)
        # Generic: era product phrase that cannot belong to this set family.
        if set_id.startswith("xy") and "diamond & pearl" in lower.replace("and", "&"):
            hits.append(card)
        if set_id.startswith("xy") and "diamond/pearl" in lower:
            hits.append(card)
    return hits


class ProviderContaminationInvariant(unittest.TestCase):
    def test_xy9_has_no_prinplup_diamond_pearl_contamination(self) -> None:
        path = EN / "cards" / "xy9.json"
        self.assertTrue(path.exists(), "xy9.json missing")
        payload = json.loads(path.read_text(encoding="utf-8"))
        cards = [c for c in (payload.get("cards") or []) if isinstance(c, dict)]
        hits = _contamination_hits(cards, "xy9")
        self.assertEqual(
            hits,
            [],
            f"PROVIDER_CONTAMINATION still present in xy9: {[h.get('id') or h.get('name') for h in hits]}",
        )

    def test_scan_indexed_sets_for_era_product_phrase_collisions(self) -> None:
        sets_doc = json.loads((EN / "sets.json").read_text(encoding="utf-8"))
        set_ids = [str(s.get("id")) for s in sets_doc.get("sets") or [] if s.get("id")]
        bad: list[str] = []
        for sid in set_ids:
            path = EN / "cards" / f"{sid}.json"
            if not path.exists():
                continue
            cards = [
                c
                for c in (json.loads(path.read_text(encoding="utf-8")).get("cards") or [])
                if isinstance(c, dict)
            ]
            for hit in _contamination_hits(cards, sid):
                bad.append(str(hit.get("id") or f"{sid}:{hit.get('name')}"))
        self.assertEqual(bad, [], f"era/product contamination identities: {bad}")


if __name__ == "__main__":
    unittest.main()
