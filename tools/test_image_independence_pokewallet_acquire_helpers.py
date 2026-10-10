#!/usr/bin/env python3
"""Unit tests for PokéWallet acquire helpers (no live network / no secrets)."""

from __future__ import annotations

import image_independence_pokewallet_acquire as pw


def test_extract_pk() -> None:
    assert pw.extract_pk("pk_abc123def") == "pk_abc123def"
    assert (
        pw.extract_pk("https://api.pokewallet.io/images/pk_deadbeef?size=high")
        == "pk_deadbeef"
    )
    assert pw.extract_pk("not-an-id") is None
    print("ok test_extract_pk")


def test_target_from_unresolved() -> None:
    row = {
        "canonical_card_id": "pokemon|en|1455|004/009|rockets_scizor_4_winner",
        "language": "en",
        "set_id": "1455",
        "collector_number": "004/009",
    }
    card = {
        "name": "Rocket's Scizor",
        "setId": "1455",
        "collectorNumber": "004/009",
        "providerIds": {"pokewallet": "pk_477a12f735a1aff3be867f3fd5d5b0864bb495d13fc1777f5e2cc8aac13bff3739de55638a3d7e799cb5abd718"},
        "imageLarge": "https://api.pokewallet.io/images/pk_477a12f735a1aff3be867f3fd5d5b0864bb495d13fc1777f5e2cc8aac13bff3739de55638a3d7e799cb5abd718?size=high",
        "imageSource": "pokewallet",
    }
    t = pw.target_from_unresolved(row, card)
    assert t is not None
    assert t.pk_id.startswith("pk_")
    assert "size=high" in t.image_url
    ok, basis = pw.verify_identity(t, card)
    assert ok and basis == "catalogue_pokewallet_exact_match"
    print("ok test_target_from_unresolved")


def test_verify_rejects_mismatch() -> None:
    row = {
        "canonical_card_id": "pokemon|en|1455|004/009|x",
        "language": "en",
        "set_id": "1455",
        "collector_number": "004/009",
    }
    card = {
        "setId": "1455",
        "collectorNumber": "999/009",
        "providerIds": {"pokewallet": "pk_aaa"},
        "imageLarge": "https://api.pokewallet.io/images/pk_aaa?size=high",
    }
    t = pw.target_from_unresolved(row, card)
    assert t is not None
    ok, reason = pw.verify_identity(t, card)
    assert not ok and reason == "collector_mismatch"
    print("ok test_verify_rejects_mismatch")


def test_budget_helpers() -> None:
    data = {"day": pw.utc_day(), "hour": pw.utc_hour(), "dayCount": 0, "hourCount": 0}
    ok, _ = pw.budget_ok(data)
    assert ok
    data["hourCount"] = pw.HOUR_SAFE
    ok, reason = pw.budget_ok(data)
    assert not ok and reason == "hour_budget_exhausted"
    print("ok test_budget_helpers")


def test_key_fingerprint_stable() -> None:
    fp = pw.key_fingerprint("pk_test_example_not_real")
    assert len(fp) == 12
    assert fp == pw.key_fingerprint("pk_test_example_not_real")
    print("ok test_key_fingerprint_stable")


if __name__ == "__main__":
    test_extract_pk()
    test_target_from_unresolved()
    test_verify_rejects_mismatch()
    test_budget_helpers()
    test_key_fingerprint_stable()
    print("all_pass")
