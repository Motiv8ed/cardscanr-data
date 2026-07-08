from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent
PUBLIC_V1_DIR = ROOT / "public" / "v1"
IMAGE_CARDS_MANIFEST_PATH = ROOT / "data" / "images" / "cards-manifest.json"
IMAGE_CARDS_MANIFEST_PUBLIC_URL = "/v1/images/cards-manifest.json"
