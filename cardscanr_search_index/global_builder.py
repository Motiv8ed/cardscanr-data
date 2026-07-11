from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "2.0.0"


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
        CREATE TABLE cards(
          canonical_printing_id TEXT PRIMARY KEY,
          canonical_set_id TEXT NOT NULL,
          language TEXT NOT NULL,
          region TEXT NOT NULL,
          native_card_name TEXT NOT NULL,
          english_card_name TEXT,
          native_set_name TEXT NOT NULL,
          english_set_name TEXT,
          printed_collector_number TEXT NOT NULL,
          normalized_collector_number TEXT NOT NULL,
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
        ) WITHOUT ROWID;
        CREATE INDEX cards_language_region ON cards(language,region);
        CREATE INDEX cards_set_number ON cards(canonical_set_id,normalized_collector_number);
        CREATE INDEX cards_language_number ON cards(language,normalized_collector_number);
        CREATE INDEX cards_rarity ON cards(rarity);
        CREATE INDEX cards_regulation ON cards(regulation_mark);
        CREATE INDEX cards_promo ON cards(promo_status);
        CREATE VIRTUAL TABLE cards_fts USING fts5(
          canonical_printing_id UNINDEXED,
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
        (("schemaVersion", SCHEMA_VERSION), ("source", "global_catalogue_canary")),
    )
    counts: Counter[str] = Counter()
    row_count = 0
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
        values = (
            canonical_id, str(card["canonicalSetId"]), str(card["language"]), str(card["region"]),
            str(card.get("nativeCardName") or ""), card.get("englishCardName"),
            str(card.get("nativeSetName") or ""), card.get("englishSetName"),
            str(card["printedCollectorNumber"]), str(card["normalizedCollectorNumber"]), set_code or None,
            json.dumps(provider_sets, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            json.dumps(aliases, ensure_ascii=False, separators=(",", ":")),
            card.get("rarity"), card.get("regulationMark"), promo, card.get("releaseDate"),
            image.get("normalizedThumbnailUrl") if public_image else None,
            image.get("normalizedDisplayUrl") if public_image else None,
            provider or None, str(image.get("directUseTechnicalStatus") or "missing"),
            image.get("mirrorPermissionStatus"), provider_ids.get(provider) if provider else None,
            provider_sets.get(provider) if provider else None,
        )
        connection.execute("INSERT INTO cards VALUES(" + ",".join("?" for _ in values) + ")", values)
        connection.execute(
            "INSERT INTO cards_fts VALUES(?,?,?,?,?,?,?,?,?)",
            (canonical_id, values[4], values[5], values[6], values[7], values[8], values[9], values[10], " ".join(aliases)),
        )
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
