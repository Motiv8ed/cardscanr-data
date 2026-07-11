from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "2.0.0"


def _normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join("".join(character if character.isalnum() else " " for character in text).split())


def _iter_jsonl(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_global_search_index(
    *,
    cards_path: Path,
    direct_images_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    images: dict[str, dict[str, Any]] = {}
    if direct_images_path.exists():
        for row in _iter_jsonl(direct_images_path):
            images[str(row["canonicalPrintingId"])] = row
    temporary = output_path.with_suffix(".sqlite.tmp")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.unlink(missing_ok=True)
    connection = sqlite3.connect(temporary)
    connection.executescript(
        """
        PRAGMA page_size=4096;
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        PRAGMA temp_store=MEMORY;
        PRAGMA auto_vacuum=NONE;
        PRAGMA application_id=1129534034;
        PRAGMA user_version=2;
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
          provider_set_id TEXT
          ,thumbnail_url TEXT
          ,large_image_url TEXT
          ,image_source TEXT
          ,image_cached INTEGER NOT NULL
          ,provider_ids_json TEXT NOT NULL
          ,provider_set_codes_json TEXT NOT NULL
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
        """
    )
    connection.executemany(
        "INSERT INTO meta(key,value) VALUES(?,?)",
        (
            ("schema_version", SCHEMA_VERSION),
            ("searchIndexSchemaVersion", SCHEMA_VERSION),
            ("source", "global_catalogue_canary"),
        ),
    )
    counts: Counter[str] = Counter()
    row_count = 0
    seen_sets: set[str] = set()
    for card in _iter_jsonl(cards_path):
        canonical_id = str(card["canonicalPrintingId"])
        image = images.get(canonical_id) or {}
        public_image = image.get("authenticationRequirement") == "not_required" and image.get("directUseTechnicalStatus") != "permanent_404"
        provider_ids = card.get("providerCardIds") or {}
        provider_sets = card.get("providerSetIds") or {}
        provider = str(image.get("provider") or "")
        set_code = str(provider_sets.get("tcgdex") or next(iter(provider_sets.values()), ""))
        aliases = sorted({str(value) for value in card.get("searchAliases") or [] if value})
        designations = {str(value).casefold() for value in card.get("designations") or []}
        promo = int("promo" in designations or "promo" in str(card.get("nativeSetName") or "").casefold())
        canonical_set_id = str(card["canonicalSetId"])
        native_name = str(card.get("nativeCardName") or "")
        english_name = card.get("englishCardName")
        native_set_name = str(card.get("nativeSetName") or "")
        english_set_name = card.get("englishSetName")
        if canonical_set_id not in seen_sets:
            connection.execute(
                "INSERT INTO sets VALUES(?,?,?,?,?,?)",
                (canonical_set_id, str(card["language"]), str(card["region"]), native_set_name, _normalize(native_set_name), card.get("releaseDate")),
            )
            seen_sets.add(canonical_set_id)
        thumbnail = image.get("normalizedThumbnailUrl") if public_image else None
        display = image.get("normalizedDisplayUrl") if public_image else None
        provider_ids_json = json.dumps(provider_ids, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        provider_set_codes_json = json.dumps(sorted({str(value) for value in provider_sets.values() if value}), ensure_ascii=False, separators=(",", ":"))
        values = (
            canonical_id, canonical_id, canonical_set_id, canonical_set_id, str(card["language"]), str(card["region"]),
            native_name, english_name, native_set_name, english_set_name,
            str(card["printedCollectorNumber"]), str(card["printedCollectorNumber"]), str(card["normalizedCollectorNumber"]),
            english_name, native_name, native_set_name, _normalize(english_name or native_name), _normalize(native_name), _normalize(native_set_name), set_code or None,
            json.dumps(provider_sets, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            json.dumps(aliases, ensure_ascii=False, separators=(",", ":")),
            card.get("rarity"), card.get("regulationMark"), promo, card.get("releaseDate"),
            thumbnail, display,
            provider or None, str(image.get("directUseTechnicalStatus") or "missing"),
            image.get("mirrorPermissionStatus"), provider_ids.get(provider) if provider else None,
            provider_sets.get(provider) if provider else None,
            thumbnail, display, provider or None, 0, provider_ids_json, provider_set_codes_json,
        )
        connection.execute("INSERT INTO cards VALUES(" + ",".join("?" for _ in values) + ")", values)
        connection.execute(
            "INSERT INTO cards_fts VALUES(?,?,?,?,?,?,?,?,?)",
            (canonical_id, native_name, english_name, native_set_name, english_set_name, str(card["printedCollectorNumber"]), str(card["normalizedCollectorNumber"]), set_code, " ".join(aliases)),
        )
        for alias in sorted({_normalize(value) for value in aliases if _normalize(value)}):
            connection.execute("INSERT OR IGNORE INTO card_aliases VALUES(?,?,?)", (canonical_id, alias, "search_alias"))
        counts[str(card["language"])] += 1
        row_count += 1
    connection.execute("INSERT INTO meta(key,value) VALUES(?,?)", ("recordCount", str(row_count)))
    connection.commit()
    connection.execute("VACUUM")
    connection.close()
    os.replace(temporary, output_path)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "path": output_path.as_posix(),
        "sizeBytes": output_path.stat().st_size,
        "sha256": _sha256(output_path),
        "records": row_count,
        "perLanguageCounts": dict(sorted(counts.items())),
    }


def verify_global_search_index(path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    records = connection.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    duplicate_ids = connection.execute(
        "SELECT COUNT(*) FROM (SELECT canonical_printing_id FROM cards GROUP BY canonical_printing_id HAVING COUNT(*)>1)"
    ).fetchone()[0]
    authenticated_urls = connection.execute(
        "SELECT COUNT(*) FROM cards WHERE image_thumbnail_url LIKE '%pokewallet.io%' OR image_thumbnail_url LIKE '%api_key%' OR image_thumbnail_url LIKE '%token=%'"
    ).fetchone()[0]
    fts_records = connection.execute("SELECT COUNT(*) FROM cards_fts").fetchone()[0]
    languages = dict(connection.execute("SELECT language,COUNT(*) FROM cards GROUP BY language ORDER BY language"))
    connection.close()
    issues = []
    if integrity != "ok": issues.append(f"integrity:{integrity}")
    if duplicate_ids: issues.append(f"duplicateCanonicalPrintingIds:{duplicate_ids}")
    if authenticated_urls: issues.append(f"authenticatedUrls:{authenticated_urls}")
    if fts_records != records: issues.append("ftsRecordCountMismatch")
    return {"classification": "PASS" if not issues else "FAIL", "records": records, "ftsRecords": fts_records, "duplicateCanonicalPrintingIds": duplicate_ids, "authenticatedUrls": authenticated_urls, "perLanguageCounts": languages, "issues": issues}
