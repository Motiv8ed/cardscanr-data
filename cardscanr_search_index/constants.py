from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOGUE_ROOT = ROOT / "public" / "v1"
SEARCH_OUTPUT_DIR = DEFAULT_CATALOGUE_ROOT / "catalog" / "pokemon" / "search"
DATABASE_BASENAME = "catalog_search_v1.sqlite"
MANIFEST_BASENAME = "catalog_search_v1.manifest.json"
SHA256_BASENAME = "catalog_search_v1.sha256"
PREVIOUS_DATABASE_BASENAME = "catalog_search_v1.previous.sqlite"
SEARCH_INDEX_SCHEMA_VERSION = "1.1.0"
CATALOGUE_SCHEMA_VERSION = "1.0.0"
GENERATOR_VERSION = "1.1.0"
SUPPORTED_LANGUAGES = ("en", "jp")
MINIMUM_COMPATIBLE_APP_VERSION = "1.0.0+21"
MINIMUM_COMPATIBLE_APP_VERSION_STATUS = "resolved"
