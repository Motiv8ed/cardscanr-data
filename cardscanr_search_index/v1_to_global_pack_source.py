"""Convert production v1 search SQLite into a global-schema pack source.

Maps jp → ja for packed-catalogue language codes while preserving canonical
base IDs (which retain the `jp` language segment used by production identity).

Does not include CJK (zh-*/ko) or other research languages.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .global_builder import SCHEMA_VERSION

# Packed Android queries use CataloguePackLanguage: jp → ja.
_V1_TO_PACK_LANGUAGE = {
    "en": "en",
    "jp": "ja",
    "ja": "ja",
}

_PACK_REGION = {
    "en": "en",
    "ja": "jp",
}


def convert_v1_search_to_global_pack_source(
    *,
    v1_db: Path,
    output_db: Path,
    languages: tuple[str, ...] = ("en", "ja"),
) -> dict[str, object]:
    """Build a minimal global-schema SQLite suitable for catalogue_packs."""
    if output_db.exists():
        output_db.unlink()
    output_db.parent.mkdir(parents=True, exist_ok=True)

    wanted_pack_langs = {lang.lower() for lang in languages}
    # Accept either v1 `jp` or pack `ja` when filtering Japanese.
    v1_lang_filter: set[str] = set()
    for lang in wanted_pack_langs:
        if lang == "ja":
            v1_lang_filter.update({"ja", "jp"})
        else:
            v1_lang_filter.add(lang)

    source = sqlite3.connect(f"file:{v1_db.as_posix()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    dest = sqlite3.connect(output_db)
    try:
        dest.executescript(
            """
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            PRAGMA temp_store=MEMORY;
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
            CREATE INDEX idx_cards_language ON cards(language);
            CREATE INDEX idx_cards_set_collector ON cards(set_id,normalized_collector_number);
            CREATE INDEX idx_cards_name ON cards(normalized_canonical_name);
            CREATE INDEX idx_cards_localized_name ON cards(normalized_localized_name);
            CREATE INDEX idx_cards_set_name ON cards(normalized_set_name);
            CREATE INDEX idx_cards_set_name_canon ON cards(normalized_set_name,normalized_canonical_name);
            CREATE INDEX idx_cards_set_name_localized ON cards(normalized_set_name,normalized_localized_name);
            CREATE INDEX cards_language_region ON cards(language,region);
            CREATE INDEX cards_set_number ON cards(canonical_set_id,normalized_collector_number);
            CREATE INDEX cards_language_number ON cards(language,normalized_collector_number);
            CREATE INDEX cards_rarity ON cards(rarity);
            CREATE INDEX cards_regulation ON cards(regulation_mark);
            CREATE INDEX cards_promo ON cards(promo_status);
            CREATE TABLE card_aliases(
              canonical_base_id TEXT NOT NULL,
              normalized_alias TEXT NOT NULL,
              alias_type TEXT NOT NULL,
              PRIMARY KEY(canonical_base_id,normalized_alias)
            ) WITHOUT ROWID;
            CREATE INDEX idx_aliases_normalized ON card_aliases(normalized_alias);
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
            CREATE INDEX sealed_products_language_region ON sealed_products(language,region);
            CREATE INDEX sealed_products_type ON sealed_products(product_type);
            CREATE INDEX sealed_products_local_name ON sealed_products(normalized_local_name);
            CREATE INDEX sealed_products_canonical_name ON sealed_products(normalized_canonical_name);
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

        placeholders = ",".join("?" for _ in v1_lang_filter)
        rows = source.execute(
            f"""
            SELECT *
            FROM cards
            WHERE lower(language) IN ({placeholders})
            ORDER BY language, set_id, normalized_collector_number, canonical_base_id
            """,
            tuple(sorted(v1_lang_filter)),
        ).fetchall()

        set_rows: dict[str, tuple] = {}
        alias_rows: list[tuple[str, str, str]] = []
        card_inserts: list[tuple] = []
        pack_lang_counts: dict[str, int] = {}

        for row in rows:
            v1_lang = str(row["language"] or "").strip().lower()
            pack_lang = _V1_TO_PACK_LANGUAGE.get(v1_lang)
            if pack_lang is None or pack_lang not in wanted_pack_langs:
                continue

            region = _PACK_REGION.get(pack_lang, pack_lang)
            set_id = str(row["set_id"] or "").strip()
            set_name = str(row["set_name"] or "").strip() or set_id
            normalized_set_name = str(row["normalized_set_name"] or "").strip() or set_name.lower()
            collector = str(row["collector_number"] or "").strip()
            normalized_collector = str(row["normalized_collector_number"] or "").strip() or collector
            canonical_base_id = str(row["canonical_base_id"] or "").strip()
            # Pack PK is canonical_printing_id. Production v1 reuses the same
            # physical_printing_id across distinct catalogue identities, so use
            # the unique canonical_base_id as the pack printing key.
            physical_printing_id = canonical_base_id
            source_physical = str(row["physical_printing_id"] or "").strip()
            if not source_physical:
                source_physical = (
                    f"physical-printing-v1|{pack_lang}|{set_id}|{collector}|normal"
                )

            localized = str(row["localized_name"] or "").strip()
            english_name = str(row["canonical_english_name"] or "").strip()
            native_name = localized or english_name or canonical_base_id
            aliases_raw = row["search_aliases_json"]
            aliases_list: list[str] = []
            if aliases_raw:
                try:
                    decoded = json.loads(aliases_raw)
                    if isinstance(decoded, list):
                        aliases_list = [
                            str(item).strip()
                            for item in decoded
                            if str(item).strip()
                        ]
                except json.JSONDecodeError:
                    aliases_list = []
            aliases_text = " ".join(aliases_list)

            provider_set_codes = str(row["provider_set_codes_json"] or "[]")
            try:
                codes = json.loads(provider_set_codes)
                set_code = codes[0] if isinstance(codes, list) and codes else None
            except json.JSONDecodeError:
                set_code = None

            # set_id is the packs PK. Production EN/JP codes do not collide.
            if set_id not in set_rows:
                set_rows[set_id] = (
                    set_id,
                    pack_lang,
                    region,
                    set_name,
                    normalized_set_name,
                    row["set_release_date"] or row["release_date"],
                )

            card_inserts.append(
                (
                    physical_printing_id,
                    canonical_base_id,
                    set_id,
                    set_id,
                    pack_lang,
                    region,
                    native_name,
                    english_name or None,
                    set_name,
                    set_name if pack_lang == "en" else None,
                    collector,
                    collector,
                    normalized_collector,
                    english_name or None,
                    localized or None,
                    set_name,
                    str(row["normalized_canonical_name"] or "").strip() or (english_name or native_name).lower(),
                    str(row["normalized_localized_name"] or "").strip() or native_name.lower(),
                    normalized_set_name,
                    set_code,
                    provider_set_codes,
                    aliases_text,
                    row["rarity"],
                    None,
                    0,
                    row["release_date"] or row["set_release_date"],
                    row["thumbnail_url"],
                    row["large_image_url"],
                    row["image_source"],
                    "available" if row["thumbnail_url"] else "missing",
                    None,
                    None,
                    None,
                    row["thumbnail_url"],
                    row["large_image_url"],
                    row["image_source"],
                    int(row["image_cached"] or 0),
                    str(row["provider_ids_json"] or "{}"),
                    provider_set_codes,
                    "trusted" if localized else "fallback_english",
                    english_name or None,
                )
            )
            pack_lang_counts[pack_lang] = pack_lang_counts.get(pack_lang, 0) + 1

            for alias in aliases_list:
                normalized_alias = alias.lower()
                if normalized_alias:
                    alias_rows.append((canonical_base_id, normalized_alias, "search_alias"))

        dest.executemany(
            """
            INSERT OR REPLACE INTO sets(
              set_id, language, region, set_name, normalized_set_name, release_date
            ) VALUES (?,?,?,?,?,?)
            """,
            list(set_rows.values()),
        )
        dest.executemany(
            """
            INSERT OR REPLACE INTO cards(
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
            card_inserts,
        )
        # Alias PK may collide on normalized form; ignore duplicates.
        dest.executemany(
            """
            INSERT OR IGNORE INTO card_aliases(canonical_base_id, normalized_alias, alias_type)
            VALUES (?,?,?)
            """,
            alias_rows,
        )
        dest.execute(
            """
            INSERT INTO cards_fts(
              canonical_base_id, native_card_name, english_card_name, native_set_name,
              english_set_name, printed_collector_number, normalized_collector_number,
              set_code, aliases
            )
            SELECT
              canonical_base_id, native_card_name, english_card_name, native_set_name,
              english_set_name, printed_collector_number, normalized_collector_number,
              set_code, aliases
            FROM cards
            """
        )
        dest.executemany(
            "INSERT INTO meta(key,value) VALUES(?,?)",
            (
                ("schema_version", SCHEMA_VERSION),
                ("searchIndexSchemaVersion", SCHEMA_VERSION),
                ("source", "v1_production_search_en_jp"),
                ("packSourceLanguages", ",".join(sorted(wanted_pack_langs))),
                ("recordCount", str(len(card_inserts))),
                ("setCount", str(len(set_rows))),
            ),
        )
        dest.commit()
        return {
            "outputDb": str(output_db),
            "schemaVersion": SCHEMA_VERSION,
            "cardCount": len(card_inserts),
            "setCount": len(set_rows),
            "aliasCount": len(alias_rows),
            "languageCounts": pack_lang_counts,
            "languages": sorted(wanted_pack_langs),
        }
    finally:
        source.close()
        dest.close()
