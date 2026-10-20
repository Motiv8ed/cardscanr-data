#!/usr/bin/env python3
"""Regression tests for EN/JP catalogue pack build tooling."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cardscanr_search_index.catalogue_packs import build_all_packs
from cardscanr_search_index.global_builder import SCHEMA_VERSION
from cardscanr_search_index.v1_to_global_pack_source import (
    convert_v1_search_to_global_pack_source,
    pack_set_id_from_canonical_base_id,
)
from tools.build_production_en_jp_packs import (
    require_nonzero_production_language_counts,
    resolve_public_base_url,
)


def _init_min_global_source(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
        CREATE TABLE sets(
          set_id TEXT PRIMARY KEY,
          language TEXT NOT NULL,
          region TEXT NOT NULL,
          set_name TEXT NOT NULL,
          normalized_set_name TEXT NOT NULL,
          release_date TEXT
        ) WITHOUT ROWID;
        CREATE TABLE cards(
          canonical_printing_id TEXT PRIMARY KEY,
          canonical_base_id TEXT NOT NULL UNIQUE,
          canonical_set_id TEXT NOT NULL,
          set_id TEXT NOT NULL,
          language TEXT NOT NULL,
          region TEXT NOT NULL,
          native_card_name TEXT NOT NULL,
          english_card_name TEXT,
          native_set_name TEXT NOT NULL,
          english_set_name TEXT,
          printed_collector_number TEXT NOT NULL,
          collector_number TEXT NOT NULL,
          normalized_collector_number TEXT NOT NULL,
          canonical_english_name TEXT,
          localized_name TEXT,
          set_name TEXT NOT NULL,
          normalized_canonical_name TEXT NOT NULL,
          normalized_localized_name TEXT NOT NULL,
          normalized_set_name TEXT NOT NULL,
          set_code TEXT,
          provider_set_aliases TEXT NOT NULL,
          aliases TEXT NOT NULL,
          rarity TEXT,
          regulation_mark TEXT,
          promo_status INTEGER NOT NULL,
          release_date TEXT,
          image_thumbnail_url TEXT,
          image_display_url TEXT,
          image_provider TEXT,
          image_state TEXT NOT NULL,
          mirror_permission_status TEXT,
          provider_card_id TEXT,
          provider_set_id TEXT,
          thumbnail_url TEXT,
          large_image_url TEXT,
          image_source TEXT,
          image_cached INTEGER NOT NULL,
          provider_ids_json TEXT NOT NULL,
          provider_set_codes_json TEXT NOT NULL,
          native_name_status TEXT NOT NULL,
          canonical_card_name TEXT
        ) WITHOUT ROWID;
        CREATE TABLE card_aliases(
          canonical_base_id TEXT NOT NULL,
          normalized_alias TEXT NOT NULL,
          alias_type TEXT NOT NULL,
          PRIMARY KEY(canonical_base_id,normalized_alias)
        ) WITHOUT ROWID;
        CREATE VIRTUAL TABLE cards_fts USING fts5(
          canonical_base_id UNINDEXED,
          native_card_name,
          english_card_name,
          native_set_name,
          english_set_name,
          printed_collector_number,
          normalized_collector_number,
          set_code,
          aliases,
          tokenize='unicode61 remove_diacritics 2'
        );
        CREATE TABLE sealed_products(
          product_variant_id TEXT PRIMARY KEY,
          canonical_product_id TEXT NOT NULL,
          language TEXT,
          region TEXT NOT NULL,
          local_name TEXT NOT NULL,
          canonical_name TEXT NOT NULL,
          normalized_local_name TEXT NOT NULL,
          normalized_canonical_name TEXT NOT NULL,
          product_type TEXT NOT NULL,
          release_date TEXT,
          verification_status TEXT NOT NULL,
          attributes_json TEXT NOT NULL,
          image_url TEXT,
          image_provider TEXT,
          image_role TEXT,
          image_state TEXT NOT NULL,
          mirror_permission_status TEXT,
          provider_product_ids_json TEXT NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE sealed_product_contents(
          product_variant_id TEXT NOT NULL,
          ordinal INTEGER NOT NULL,
          content_kind TEXT NOT NULL,
          entity_id TEXT,
          description TEXT,
          quantity INTEGER NOT NULL,
          attributes_json TEXT NOT NULL,
          PRIMARY KEY(product_variant_id,ordinal)
        ) WITHOUT ROWID;
        CREATE VIRTUAL TABLE sealed_products_fts USING fts5(
          product_variant_id UNINDEXED,
          local_name,
          canonical_name,
          product_type,
          tokenize='unicode61 remove_diacritics 2'
        );
        """
    )
    conn.execute(
        "INSERT INTO meta(key,value) VALUES(?,?)",
        ("schema_version", SCHEMA_VERSION),
    )
    return conn


def _insert_card(
    conn: sqlite3.Connection,
    *,
    language: str,
    set_id: str,
    number: str,
    name: str,
) -> None:
    base_id = f"pokemon|{language}|{set_id}|{number}|{name.lower()}"
    conn.execute(
        """
        INSERT INTO cards(
          canonical_printing_id, canonical_base_id, canonical_set_id, set_id,
          language, region, native_card_name, english_card_name, native_set_name,
          english_set_name, printed_collector_number, collector_number,
          normalized_collector_number, canonical_english_name, localized_name,
          set_name, normalized_canonical_name, normalized_localized_name,
          normalized_set_name, set_code, provider_set_aliases, aliases, rarity,
          regulation_mark, promo_status, release_date, image_thumbnail_url,
          image_display_url, image_provider, image_state, mirror_permission_status,
          provider_card_id, provider_set_id, thumbnail_url, large_image_url,
          image_source, image_cached, provider_ids_json, provider_set_codes_json,
          native_name_status, canonical_card_name
        ) VALUES (
          ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
        )
        """,
        (
            base_id,
            base_id,
            set_id,
            set_id,
            language,
            language,
            name,
            name,
            set_id,
            set_id,
            number,
            number,
            number,
            name,
            name,
            set_id,
            name.lower(),
            name.lower(),
            set_id.lower(),
            set_id,
            "[]",
            "",
            None,
            None,
            0,
            None,
            None,
            None,
            None,
            "missing",
            None,
            None,
            None,
            None,
            None,
            None,
            0,
            "{}",
            "[]",
            "trusted",
            name,
        ),
    )


def _insert_sealed(
    conn: sqlite3.Connection,
    *,
    product_id: str,
    language: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO sealed_products(
          product_variant_id, canonical_product_id, language, region,
          local_name, canonical_name, normalized_local_name, normalized_canonical_name,
          product_type, release_date, verification_status, attributes_json,
          image_url, image_provider, image_role, image_state,
          mirror_permission_status, provider_product_ids_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            product_id,
            product_id,
            language,
            language or "und",
            f"{product_id} local",
            f"{product_id} canonical",
            product_id,
            product_id,
            "booster_box",
            None,
            "verified",
            "{}",
            None,
            None,
            None,
            "missing",
            None,
            "{}",
        ),
    )
    conn.execute(
        """
        INSERT INTO sealed_product_contents(
          product_variant_id, ordinal, content_kind, entity_id,
          description, quantity, attributes_json
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (product_id, 0, "booster_pack", None, "packs", 36, "{}"),
    )


def _write_mixed_source(path: Path) -> Path:
    conn = _init_min_global_source(path)
    try:
        for set_id, language in (("en1", "en"), ("ja1", "ja"), ("ko1", "ko")):
            conn.execute(
                "INSERT INTO sets VALUES (?,?,?,?,?,?)",
                (set_id, language, language, set_id, set_id, None),
            )
            _insert_card(conn, language=language, set_id=set_id, number="1", name=f"Card{language}")
        _insert_sealed(conn, product_id="sealed-en", language="en")
        _insert_sealed(conn, product_id="sealed-ko", language="ko")
        _insert_sealed(conn, product_id="sealed-null", language=None)
        conn.commit()
    finally:
        conn.close()
    return path


def _write_empty_sealed_source(path: Path) -> Path:
    conn = _init_min_global_source(path)
    try:
        conn.execute(
            "INSERT INTO sets VALUES (?,?,?,?,?,?)",
            ("en1", "en", "en", "en1", "en1", None),
        )
        _insert_card(conn, language="en", set_id="en1", number="1", name="Pikachu")
        conn.commit()
    finally:
        conn.close()
    return path


def _init_v1_cards_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE cards(
          canonical_base_id TEXT PRIMARY KEY,
          language TEXT NOT NULL,
          set_id TEXT NOT NULL,
          set_name TEXT,
          normalized_set_name TEXT,
          collector_number TEXT,
          normalized_collector_number TEXT,
          physical_printing_id TEXT,
          localized_name TEXT,
          canonical_english_name TEXT,
          search_aliases_json TEXT,
          provider_set_codes_json TEXT,
          normalized_canonical_name TEXT,
          normalized_localized_name TEXT,
          rarity TEXT,
          release_date TEXT,
          set_release_date TEXT,
          thumbnail_url TEXT,
          large_image_url TEXT,
          image_source TEXT,
          image_cached INTEGER,
          provider_ids_json TEXT
        )
        """
    )


def _insert_v1_card(
    conn: sqlite3.Connection,
    *,
    language: str,
    set_id: str,
    set_name: str,
    collector: str,
    slug: str,
    localized_name: str,
    english_name: str,
) -> None:
    base_id = f"pokemon|{language}|{set_id}|{collector}|{slug}"
    conn.execute(
        """
        INSERT INTO cards VALUES (
          ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
        )
        """,
        (
            base_id,
            language,
            set_id,
            set_name,
            set_name.lower(),
            collector,
            collector,
            None,
            localized_name,
            english_name,
            "[]",
            "[]",
            english_name.lower(),
            localized_name.lower(),
            None,
            None,
            None,
            None,
            None,
            None,
            0,
            "{}",
        ),
    )


def _write_min_v1_db(path: Path, *, languages: tuple[str, ...] = ("en", "jp")) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    try:
        _init_v1_cards_table(conn)
        for lang in languages:
            set_id = f"{lang}-set"
            _insert_v1_card(
                conn,
                language=lang,
                set_id=set_id,
                set_name=set_id,
                collector="1",
                slug="card",
                localized_name="Card",
                english_name="Card",
            )
        conn.commit()
    finally:
        conn.close()
    return path


def _write_neo1_en_jp_v1_db(path: Path) -> Path:
    """Shared provider code neo1 for EN+JP (production collision case)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    try:
        _init_v1_cards_table(conn)
        _insert_v1_card(
            conn,
            language="en",
            set_id="neo1",
            set_name="Neo Genesis",
            collector="001/111",
            slug="ampharos",
            localized_name="Ampharos",
            english_name="Ampharos",
        )
        _insert_v1_card(
            conn,
            language="jp",
            set_id="neo1",
            set_name="Gold, Silver, to a New World...",
            collector="001",
            slug="chikorita",
            localized_name="Chikorita",
            english_name="Chikorita",
        )
        conn.commit()
    finally:
        conn.close()
    return path


def _temp_dir() -> tempfile.TemporaryDirectory[str]:
    # Windows may briefly retain SQLite handles after close.
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


class CataloguePackLanguageFilterTests(unittest.TestCase):
    def test_core_and_sealed_respect_language_filter(self) -> None:
        with _temp_dir() as tmp:
            root = Path(tmp)
            source = _write_mixed_source(root / "source.sqlite")
            manifest = build_all_packs(
                source_db=source,
                output_dir=root / "out",
                languages=("en", "ja"),
                include_sealed=True,
                catalogue_release_id="test-filter",
            )
            pack_ids = {p["packId"] for p in manifest["packs"]}
            self.assertEqual(pack_ids, {"core", "en", "ja", "sealed-products"})
            self.assertNotIn("ko", pack_ids)

            core = root / "out" / "sqlite" / "core.sqlite"
            with sqlite3.connect(core) as conn:
                set_langs = {
                    row[0]
                    for row in conn.execute("SELECT language FROM sets")
                }
            self.assertEqual(set_langs, {"en", "ja"})

            sealed = root / "out" / "sqlite" / "sealed-products.sqlite"
            with sqlite3.connect(sealed) as conn:
                product_langs = {
                    row[0]
                    for row in conn.execute("SELECT language FROM sealed_products")
                }
                content_count = conn.execute(
                    "SELECT COUNT(*) FROM sealed_product_contents"
                ).fetchone()[0]
            self.assertEqual(product_langs, {"en"})
            self.assertEqual(content_count, 1)

    def test_default_install_ids_match_built_packs(self) -> None:
        with _temp_dir() as tmp:
            root = Path(tmp)
            source = _write_mixed_source(root / "source.sqlite")
            manifest = build_all_packs(
                source_db=source,
                output_dir=root / "out",
                languages=("ja",),
                include_sealed=False,
                catalogue_release_id="test-defaults",
            )
            built = [p["packId"] for p in manifest["packs"]]
            self.assertEqual(built, ["core", "ja"])
            self.assertEqual(manifest["defaultInstallPackIds"], ["core"])
            self.assertNotIn("en", manifest["defaultInstallPackIds"])
            self.assertNotIn("sealed-products", manifest["defaultInstallPackIds"])

    def test_empty_sealed_omitted_from_manifest(self) -> None:
        with _temp_dir() as tmp:
            root = Path(tmp)
            source = _write_empty_sealed_source(root / "source.sqlite")
            manifest = build_all_packs(
                source_db=source,
                output_dir=root / "out",
                languages=("en",),
                include_sealed=True,
                catalogue_release_id="test-empty-sealed",
            )
            pack_ids = [p["packId"] for p in manifest["packs"]]
            self.assertEqual(pack_ids, ["core", "en"])
            self.assertEqual(manifest["defaultInstallPackIds"], ["core", "en"])
            self.assertNotIn("sealed-products", pack_ids)


class V1ToGlobalJpAliasTests(unittest.TestCase):
    def test_jp_alias_keeps_japanese_cards(self) -> None:
        with _temp_dir() as tmp:
            root = Path(tmp)
            v1 = _write_min_v1_db(root / "v1.sqlite", languages=("en", "jp"))
            out = root / "global.sqlite"
            report = convert_v1_search_to_global_pack_source(
                v1_db=v1,
                output_db=out,
                languages=("en", "jp"),
            )
            self.assertEqual(report["languages"], ["en", "ja"])
            self.assertEqual(report["languageCounts"].get("en"), 1)
            self.assertEqual(report["languageCounts"].get("ja"), 1)
            with sqlite3.connect(out) as conn:
                langs = {
                    row[0] for row in conn.execute("SELECT language FROM cards")
                }
            self.assertEqual(langs, {"en", "ja"})

    def test_neo1_en_jp_keeps_language_specific_set_rows(self) -> None:
        """Shared provider code neo1 must not let EN win the sets PK."""
        self.assertEqual(
            pack_set_id_from_canonical_base_id(
                "pokemon|en|neo1|001/111|ampharos",
                pack_lang="en",
                raw_set_id="neo1",
            ),
            "pokemon|en|neo1",
        )
        self.assertEqual(
            pack_set_id_from_canonical_base_id(
                "pokemon|jp|neo1|001|chikorita",
                pack_lang="ja",
                raw_set_id="neo1",
            ),
            "pokemon|jp|neo1",
        )
        # Short ids must not be truncated by rsplit; use language-qualified fallback.
        self.assertEqual(
            pack_set_id_from_canonical_base_id(
                "pokemon|jp|neo1",
                pack_lang="ja",
                raw_set_id="neo1",
            ),
            "pokemon|ja|neo1",
        )
        self.assertEqual(
            pack_set_id_from_canonical_base_id(
                "pokemon|jp|neo1|001",
                pack_lang="ja",
                raw_set_id="neo1",
            ),
            "pokemon|ja|neo1",
        )
        with _temp_dir() as tmp:
            root = Path(tmp)
            v1 = _write_neo1_en_jp_v1_db(root / "v1.sqlite")
            source = root / "global.sqlite"
            report = convert_v1_search_to_global_pack_source(
                v1_db=v1,
                output_db=source,
                languages=("en", "ja"),
            )
            self.assertEqual(report["setCount"], 2)
            self.assertEqual(report["languageCounts"].get("en"), 1)
            self.assertEqual(report["languageCounts"].get("ja"), 1)

            with sqlite3.connect(source) as conn:
                set_rows = {
                    (row[0], row[1], row[2])
                    for row in conn.execute(
                        "SELECT set_id, language, set_name FROM sets"
                    )
                }
                card_set_ids = {
                    (row[0], row[1])
                    for row in conn.execute(
                        "SELECT language, set_id FROM cards"
                    )
                }
            self.assertEqual(
                set_rows,
                {
                    ("pokemon|en|neo1", "en", "Neo Genesis"),
                    (
                        "pokemon|jp|neo1",
                        "ja",
                        "Gold, Silver, to a New World...",
                    ),
                },
            )
            self.assertEqual(
                card_set_ids,
                {
                    ("en", "pokemon|en|neo1"),
                    ("ja", "pokemon|jp|neo1"),
                },
            )

            manifest = build_all_packs(
                source_db=source,
                output_dir=root / "out",
                languages=("en", "ja"),
                include_sealed=True,
                catalogue_release_id="test-neo1",
            )
            pack_ids = {p["packId"] for p in manifest["packs"]}
            self.assertEqual(pack_ids, {"core", "en", "ja"})
            # Empty sealed source is omitted; not a false-positive sealed default.
            self.assertNotIn("sealed-products", pack_ids)
            self.assertEqual(
                set(manifest["defaultInstallPackIds"]),
                {"core", "en"},
            )

            ja_pack = root / "out" / "sqlite" / "ja.sqlite"
            with sqlite3.connect(ja_pack) as conn:
                ja_sets = {
                    row[0]
                    for row in conn.execute("SELECT set_id FROM sets")
                }
                orphan_cards = conn.execute(
                    """
                    SELECT COUNT(*) FROM cards c
                    LEFT JOIN sets s ON s.set_id = c.set_id
                    WHERE s.set_id IS NULL
                    """
                ).fetchone()[0]
                ja_set_lang = conn.execute(
                    "SELECT language FROM sets WHERE set_id = ?",
                    ("pokemon|jp|neo1",),
                ).fetchone()
            self.assertEqual(ja_sets, {"pokemon|jp|neo1"})
            self.assertEqual(orphan_cards, 0)
            self.assertEqual(ja_set_lang[0], "ja")


class ProductionPackGateTests(unittest.TestCase):
    def test_reject_incomplete_language_counts(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            require_nonzero_production_language_counts(
                {
                    "languages": ["en", "ja"],
                    "languageCounts": {"en": 10, "ja": 0},
                }
            )
        self.assertIn("incomplete production source", str(ctx.exception))

    def test_accept_nonzero_language_counts(self) -> None:
        require_nonzero_production_language_counts(
            {
                "languages": ["en", "ja"],
                "languageCounts": {"en": 1, "ja": 2},
            }
        )

    def test_missing_config_falls_back(self) -> None:
        with _temp_dir() as tmp:
            missing = Path(tmp) / "missing.json"
            self.assertEqual(resolve_public_base_url(missing), "https://assets.cardscanr.com")
            self.assertEqual(resolve_public_base_url(None), "https://assets.cardscanr.com")

    def test_config_without_asset_keys_falls_back(self) -> None:
        """Absent asset keys (not invalid values) may use the default host."""
        with _temp_dir() as tmp:
            cfg = Path(tmp) / "no-asset-keys.json"
            cfg.write_text(
                json.dumps(
                    {
                        "r2Bucket": "cardscanr-catalog",
                        "pagesBaseUrl": "https://example.pages.dev",
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(resolve_public_base_url(cfg), "https://assets.cardscanr.com")

    def test_invalid_config_raises(self) -> None:
        with _temp_dir() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text("{not-json", encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                resolve_public_base_url(bad)
            self.assertIn("invalid publication config", str(ctx.exception))

    def test_explicit_invalid_asset_url_raises_by_key_presence(self) -> None:
        """Key presence wins over truthiness; empty/null/http must not fall back."""
        cases = (
            {"r2CatalogueAssetsBaseUrl": ""},
            {"r2CatalogueAssetsBaseUrl": None},
            {"r2CatalogueAssetsBaseUrl": "http://insecure.example"},
            {"r2CatalogueAssetsBaseUrl": 123},
            {"r2PublicBaseUrl": ""},
            {"r2PublicBaseUrl": "http://insecure.example"},
            # Prefer assets key even when public base is valid HTTPS.
            {
                "r2CatalogueAssetsBaseUrl": "http://bad.example",
                "r2PublicBaseUrl": "https://ok.example",
            },
        )
        with _temp_dir() as tmp:
            for index, payload in enumerate(cases):
                cfg = Path(tmp) / f"bad-url-{index}.json"
                body = {
                    "r2Bucket": "cardscanr-catalog",
                    "pagesBaseUrl": "https://example.pages.dev",
                    **payload,
                }
                cfg.write_text(json.dumps(body), encoding="utf-8")
                with self.assertRaises(ValueError, msg=f"case={payload}") as ctx:
                    resolve_public_base_url(cfg)
                self.assertIn("asset base URL must be https", str(ctx.exception))

    def test_valid_config_override(self) -> None:
        with _temp_dir() as tmp:
            cfg = Path(tmp) / "ok.json"
            cfg.write_text(
                json.dumps(
                    {
                        "r2Bucket": "cardscanr-catalog",
                        "pagesBaseUrl": "https://example.pages.dev",
                        "r2CatalogueAssetsBaseUrl": "https://cdn.example.test",
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(resolve_public_base_url(cfg), "https://cdn.example.test")

    def test_manifest_classification_is_size_only_not_completeness(self) -> None:
        """Documented false-positive: PASS means size limits, not catalogue fullness.

        Production tooling rejects empty language counts before build_all_packs.
        build_all_packs itself still classifies PASS solely from public size
        limits, so callers must keep the convert-report gate.
        """
        with _temp_dir() as tmp:
            root = Path(tmp)
            source = _write_empty_sealed_source(root / "source.sqlite")
            manifest = build_all_packs(
                source_db=source,
                output_dir=root / "out",
                languages=("en",),
                include_sealed=True,
                catalogue_release_id="test-pass-size-only",
            )
            self.assertEqual(manifest["classification"], "PASS")
            self.assertEqual(manifest["sizeLimitViolations"], [])
            # Completeness is enforced by require_nonzero_production_language_counts,
            # not by classification. Empty convert reports must still raise:
            with self.assertRaises(ValueError):
                require_nonzero_production_language_counts(
                    {"languages": ["en", "ja"], "languageCounts": {"en": 0, "ja": 0}}
                )


if __name__ == "__main__":
    unittest.main()
