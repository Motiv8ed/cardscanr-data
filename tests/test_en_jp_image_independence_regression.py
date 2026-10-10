"""Regression gate: every EN/JA catalogue card must have local master + CardScanR CDN imagery.

Fails closed when new catalogue cards are added without verified independence bindings.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
CATALOGUE = ROOT / "public" / "v1" / "catalog" / "pokemon"
MASTER = ROOT / "data" / "images" / "master"
CDN_HOST = "cardscanr-images.andygore149.workers.dev"


def _master_lang(folder: str) -> str:
    return "ja" if folder == "jp" else folder


def _is_cdn(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:  # noqa: BLE001
        return False
    return host == CDN_HOST


def _index_master_canonicals() -> set[str]:
    out: set[str] = set()
    if not MASTER.is_dir():
        return out
    for sidecar in MASTER.rglob("asset.json"):
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        cid = str(meta.get("canonicalBaseId") or "").strip()
        display = sidecar.parent / "display.webp"
        public = str(meta.get("publicUrl") or "").strip()
        if cid and display.is_file() and _is_cdn(public):
            out.add(cid)
    return out


class EnJpImageIndependenceRegressionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.master_canonicals = _index_master_canonicals()
        cls.failures: list[str] = []
        cls.total = 0
        for folder in ("en", "jp"):
            cards_dir = CATALOGUE / folder / "cards"
            if not cards_dir.is_dir():
                cls.failures.append(f"missing_catalogue_dir:{cards_dir}")
                continue
            for path in sorted(cards_dir.glob("*.json")):
                data = json.loads(path.read_text(encoding="utf-8"))
                for card in data.get("cards") or []:
                    if not isinstance(card, dict):
                        continue
                    cls.total += 1
                    cid = str(card.get("canonicalBaseId") or "").strip()
                    src = str(card.get("imageSource") or "")
                    large = str(
                        card.get("imageUrlLarge")
                        or card.get("imageLarge")
                        or card.get("imageUrl")
                        or ""
                    )
                    small = str(
                        card.get("imageUrlSmall")
                        or card.get("imageSmall")
                        or card.get("imageUrl")
                        or ""
                    )
                    reasons: list[str] = []
                    if not cid:
                        reasons.append("empty_canonical")
                    if src != "cardscanr_cdn":
                        reasons.append(f"imageSource={src or 'empty'}")
                    if not _is_cdn(large):
                        reasons.append("large_not_cdn")
                    if not _is_cdn(small):
                        reasons.append("small_not_cdn")
                    if not card.get("imageCached"):
                        reasons.append("imageCached_false")
                    if cid and cid not in cls.master_canonicals:
                        reasons.append("missing_local_master_or_cdn_publicUrl")
                    if reasons:
                        cls.failures.append(
                            f"{folder}/{path.stem}|{card.get('normalizedName')}|{cid}|"
                            + ",".join(reasons)
                        )

    def test_catalogue_dirs_exist(self) -> None:
        self.assertTrue((CATALOGUE / "en" / "cards").is_dir())
        self.assertTrue((CATALOGUE / "jp" / "cards").is_dir())

    def test_expected_scale(self) -> None:
        # Guard against accidental empty/partial catalogue checkout.
        self.assertGreaterEqual(self.total, 68011)

    def test_no_en_jp_cards_missing_master_and_cdn(self) -> None:
        if self.failures:
            sample = "\n".join(self.failures[:40])
            self.fail(
                f"{len(self.failures)} / {self.total} EN/JA cards lack local-master + "
                f"verified CDN imagery.\nFirst failures:\n{sample}"
            )

    def test_master_index_covers_catalogue_scale(self) -> None:
        self.assertGreaterEqual(len(self.master_canonicals), 68011)


if __name__ == "__main__":
    unittest.main()
