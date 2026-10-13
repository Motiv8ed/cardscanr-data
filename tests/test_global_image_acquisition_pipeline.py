from __future__ import annotations

import importlib.util
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools" / "global_image_acquisition_pipeline.py"
SPEC = importlib.util.spec_from_file_location("gia", TOOLS)
assert SPEC and SPEC.loader
gia = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gia)


def test_choose_url_permits_tcgdex() -> None:
    url, policy, reason = gia.choose_url(
        {
            "normalizedDisplayUrl": "https://assets.tcgdex.net/de/bw/bw10/100/high.webp",
        }
    )
    assert reason is None
    assert url.endswith("high.webp")
    assert policy and policy["provider"] == "tcgdex"


def test_choose_url_blocks_official_asia() -> None:
    url, policy, reason = gia.choose_url(
        {
            "normalizedDisplayUrl": "https://asia.pokemon-card.com/id/assets/card.png",
        }
    )
    assert url is None and policy is None
    assert reason and reason.startswith("discovery_only:")


def test_magic_ok() -> None:
    assert gia.magic_ok(b"\x89PNG\r\n\x1a\n" + b"\0" * 8)
    assert not gia.magic_ok(b"notanimage")


def test_scrydex_rights_tag() -> None:
    _, policy, _ = gia.choose_url(
        {"normalizedDisplayUrl": "https://images.scrydex.com/pokemon/sv1-1/high.png"}
    )
    assert policy and "scrydex_written_authorization" in policy["rightsStatus"]
