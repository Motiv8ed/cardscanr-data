#!/usr/bin/env python3
from __future__ import annotations

import finalize_pokecardex_zh_hans_reconcile as fin
import pokecardex_zh_hans_acquire as acq
from cardscanr_global_catalogue.contracts import build_printing_record, canonicalize_language


def test_staging_key_energy_uses_sort_token() -> None:
    row = {
        "providerSetId": "NRGY",
        "providerVersionId": 1,
        "printedCollectorNumber": "—",
        "sort": 12,
    }
    assert "s12" in fin.staging_key_for(row)
    assert acq.collector_staging_token(fin.meta_card_dict(row)) == "s12"


def test_variant_map_covers_known_ids() -> None:
    assert fin.VERSION_TO_CARD_VARIANT[1] == "unspecified"
    assert fin.VERSION_TO_CARD_VARIANT[2] == "reverse_holo"
    assert fin.VERSION_TO_CARD_VARIANT[10] == "reverse_masterball"


def test_build_printing_record_zh_hans_pokecardex() -> None:
    assert canonicalize_language("zh-cn") == "zh-Hans"
    rec = build_printing_record(
        source_language="zh-cn",
        provider="pokecardex",
        provider_set_id="CSV1C",
        provider_card_id="CSV1C-1-v1",
        native_set_name="Eternal Birth",
        native_card_name="Sprigatito",
        collector_number="001/198",
        official_set_total=198,
        release_date=None,
        image_url=None,
        serie_id="SV",
        serie_name="Scarlet & Violet",
        variant="unspecified",
    )
    assert rec["language"] == "zh-Hans"
    assert rec["region"] == "CN"
    assert rec["providerSetIds"]["pokecardex"] == "CSV1C"
    assert rec["cardVariant"] == "unspecified"
    assert rec["canonicalPrintingId"].endswith("|unspecified")
    assert "set:" in rec["canonicalBaseId"]


def test_variant_shares_base_differs_printing() -> None:
    base = build_printing_record(
        source_language="zh-cn",
        provider="pokecardex",
        provider_set_id="CSV1C",
        provider_card_id="CSV1C-1-v1",
        native_set_name="Eternal Birth",
        native_card_name="Sprigatito",
        collector_number="1",
        official_set_total=198,
        release_date=None,
        image_url=None,
        serie_id=None,
        serie_name=None,
        variant="unspecified",
    )
    rev = build_printing_record(
        source_language="zh-cn",
        provider="pokecardex",
        provider_set_id="CSV1C",
        provider_card_id="CSV1C-1-v2",
        native_set_name="Eternal Birth",
        native_card_name="Sprigatito",
        collector_number="1",
        official_set_total=198,
        release_date=None,
        image_url=None,
        serie_id=None,
        serie_name=None,
        variant="reverse_holo",
    )
    assert base["canonicalBaseId"] == rev["canonicalBaseId"]
    assert base["canonicalPrintingId"] != rev["canonicalPrintingId"]
