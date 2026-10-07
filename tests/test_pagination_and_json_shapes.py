#!/usr/bin/env python3
"""Pagination completeness + upstream JSON-shape regression tests."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_price_cache as price_cache  # noqa: E402
import build_pokewallet_catalog_foundation as pokewallet  # noqa: E402


class PaginationCompletenessTests(unittest.TestCase):
    def test_fetches_every_page_until_total_count(self) -> None:
        pages = {
            1: {"data": [{"id": f"a{i}"} for i in range(250)], "totalCount": 501, "pageSize": 250, "count": 250},
            2: {"data": [{"id": f"b{i}"} for i in range(250)], "totalCount": 501, "pageSize": 250, "count": 250},
            3: {"data": [{"id": "c0"}], "totalCount": 501, "pageSize": 250, "count": 1},
        }

        def fake_get(_endpoint: str, params: dict | None = None) -> dict:
            page = int((params or {}).get("page") or 1)
            return pages[page]

        with mock.patch.object(price_cache, "pokemon_tcg_get", side_effect=fake_get):
            records, total, pages_fetched = price_cache.fetch_pokemon_tcg_paginated(
                "/cards", page_size=250, require_complete=True
            )

        self.assertEqual(total, 501)
        self.assertEqual(pages_fetched, 3)
        self.assertEqual(len(records), 501)

    def test_raises_when_truncated_before_total_count(self) -> None:
        def fake_get(_endpoint: str, params: dict | None = None) -> dict:
            page = int((params or {}).get("page") or 1)
            if page == 1:
                return {"data": [{"id": "x"} for _ in range(250)], "totalCount": 400, "pageSize": 250}
            return {"data": [], "totalCount": 400, "pageSize": 250}

        with mock.patch.object(price_cache, "pokemon_tcg_get", side_effect=fake_get):
            with self.assertRaises(RuntimeError) as ctx:
                price_cache.fetch_pokemon_tcg_paginated(
                    "/cards", page_size=250, require_complete=True
                )
        self.assertIn("incomplete", str(ctx.exception).lower())


class PokewalletJsonShapeTests(unittest.TestCase):
    def test_set_detail_accepts_cards_key(self) -> None:
        set_obj, cards = pokewallet.set_detail_cards(
            {"set": {"id": "me4", "name": "Chaos Rising"}, "cards": [{"id": "1"}, {"id": "2"}]}
        )
        self.assertEqual(set_obj.get("id"), "me4")
        self.assertEqual(len(cards), 2)

    def test_set_detail_accepts_data_key(self) -> None:
        set_obj, cards = pokewallet.set_detail_cards(
            {"set": {"id": "me4"}, "data": [{"id": "1"}, "skip", {"id": "2"}]}
        )
        self.assertEqual(len(cards), 2)
        self.assertEqual(cards[0]["id"], "1")

    def test_set_detail_rejects_unknown_nonempty_shape(self) -> None:
        with self.assertRaises(ValueError):
            pokewallet.set_detail_cards({"set": {"id": "x"}, "results": [{"id": "1"}]})

    def test_set_detail_empty_when_truly_empty(self) -> None:
        set_obj, cards = pokewallet.set_detail_cards({"set": {"id": "x"}})
        self.assertEqual(set_obj.get("id"), "x")
        self.assertEqual(cards, [])

    def test_list_items_finds_top_level_shapes(self) -> None:
        payload = {"sets": [{"id": "a"}, {"id": "b"}]}
        items = pokewallet.list_items(payload, "sets", "data", "cards")
        self.assertEqual([i["id"] for i in items], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
