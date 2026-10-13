#!/usr/bin/env python3
from __future__ import annotations

import global_image_staging_acquire as g


def test_choose_url_tcgdex_allowed() -> None:
    url, policy, reason = g.choose_url(
        {
            "normalizedDisplayUrl": "https://assets.tcgdex.net/de/bw/bw10/100/high.webp",
            "language": "de",
        }
    )
    assert reason is None
    assert url.endswith("high.webp")
    assert policy is not None
    assert policy["provider"] == "tcgdex"
    assert policy["productionPromotionAllowed"] is False


def test_choose_url_pokewallet_discovery_only() -> None:
    url, policy, reason = g.choose_url(
        {
            "normalizedDisplayUrl": "https://api.pokewallet.io/images/pk_abc?size=high",
        }
    )
    assert url is None
    assert policy is None
    assert reason == "pokewallet_authenticated_rehost_unclear"


def test_choose_url_scrydex_written_auth() -> None:
    url, policy, reason = g.choose_url(
        {
            "normalizedDisplayUrl": "https://images.scrydex.com/pokemon/sv1-1/high.png",
        }
    )
    assert reason is None
    assert url is not None
    assert policy is not None
    assert policy["licenceConfirmed"] is True
    assert policy["productionPromotionAllowed"] is True


def test_safe_seg_and_magic() -> None:
    assert " " not in g.safe_seg("a b/c")
    assert g.magic_ok(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
    assert not g.magic_ok(b"not-an-image")


def test_validate_url_identity_ok() -> None:
    issues = g.validate_url_identity(
        {
            "language": "de",
            "providerSetId": "bw10",
            "providerCardId": "bw10-100",
            "printedCollectorNumber": "100",
        },
        "https://assets.tcgdex.net/de/bw/bw10/100/high.webp",
    )
    assert issues == []


def test_validate_url_identity_language_mismatch() -> None:
    issues = g.validate_url_identity(
        {
            "language": "fr",
            "providerSetId": "bw10",
            "printedCollectorNumber": "100",
        },
        "https://assets.tcgdex.net/de/bw/bw10/100/high.webp",
    )
    assert any(i.startswith("language_mismatch") for i in issues)


def test_normalize_collector() -> None:
    assert g.normalize_collector("001/165") == "1"
    assert g.normalize_collector("020") == "20"


def test_validate_url_identity_hyphenated_set_ok() -> None:
    issues = g.validate_url_identity(
        {
            "language": "de",
            "providerSetId": "P-A",
            "providerCardId": "P-A-010",
            "printedCollectorNumber": "010",
        },
        "https://assets.tcgdex.net/de/tcgp/P-A/010/high.webp",
    )
    assert issues == []


def test_validate_url_identity_es419_esmx_alias() -> None:
    issues = g.validate_url_identity(
        {
            "language": "es-419",
            "providerSetId": "sv1",
            "providerCardId": "sv1-1",
            "printedCollectorNumber": "1",
        },
        "https://assets.tcgdex.net/es-mx/sv/sv1/1/high.webp",
    )
    assert issues == []
