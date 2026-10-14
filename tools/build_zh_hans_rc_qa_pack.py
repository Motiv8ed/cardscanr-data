#!/usr/bin/env python3
"""Build a local zh-cn catalogue pack from the isolated RC cards.jsonl for app QA.

- Metadata-only: placeholder image URLs only (never staging/archive paths).
- Storage language mapped to zh-cn (app pack convention).
- canonical_base_id = canonical_printing_id (same as global_builder) so variants coexist.
- Does not publish, upload, or touch live packs.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CARDS = ROOT / "data" / "global" / "catalogue" / "cards.jsonl"
OUT_DIR = ROOT / "reports" / "global_image_acquisition" / "pokecardex" / "release_candidate" / "qa_pack"
PLACEHOLDER = "https://assets.cardscanr.com/v2/catalog/pokemon/placeholders/card_missing.webp"
SCHEMA_VERSION = "2.1.0"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join("".join(ch if ch.isalnum() else " " for ch in text).split())


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / "zh-cn.sqlite"
    tmp = out.with_suffix(".sqlite.tmp")
    tmp.unlink(missing_ok=True)

    conn = sqlite3.connect(tmp)
    conn.executescript(
        """
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
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
        CREATE INDEX cards_language_number ON cards(language, normalized_collector_number);
        CREATE INDEX cards_set_number ON cards(set_id, normalized_collector_number);
        CREATE TABLE card_aliases(
          canonical_base_id TEXT NOT NULL,
          normalized_alias TEXT NOT NULL,
          alias_type TEXT NOT NULL,
          PRIMARY KEY(canonical_base_id, normalized_alias)
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
          PRIMARY KEY(product_variant_id, ordinal)
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

    counts: Counter[str] = Counter()
    seen_sets: set[str] = set()
    staging_leaks = 0
    attribution_ok = 0
    with CARDS.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            card = json.loads(line)
            if str(card.get("language")) != "zh-Hans":
                continue
            cid = str(card["canonicalPrintingId"])
            # Pack uniqueness: use printing id for both printing and base columns
            base_id = cid
            set_id = str(card["canonicalSetId"])
            provider_sets = card.get("providerSetIds") or {}
            provider_ids = card.get("providerCardIds") or {}
            set_code = str(
                provider_sets.get("pokecardex")
                or provider_sets.get("tcgdex")
                or next(iter(provider_sets.values()), "")
            )
            native_name = str(
                card.get("nativeCardName")
                or card.get("englishCardName")
                or card.get("canonicalCardName")
                or ""
            )
            english_name = card.get("englishCardName")
            native_set = str(card.get("nativeSetName") or set_code or "Chinese set")
            printed = str(card.get("printedCollectorNumber") or "")
            norm = str(card.get("normalizedCollectorNumber") or printed.split("/")[0])
            aliases = sorted({str(v) for v in (card.get("searchAliases") or []) if v})
            promo = int(
                "promo" in {str(v).casefold() for v in (card.get("designations") or [])}
                or "promo" in native_set.casefold()
            )
            # Force placeholders — never emit staging/archive URLs
            thumb = PLACEHOLDER
            display = PLACEHOLDER
            blob = json.dumps(card, ensure_ascii=False)
            if "image_staging" in blob or "CardScanR_Archive" in blob:
                # Private staging refs on row metadata are OK if not copied into URL fields
                pass
            for url in (thumb, display):
                if "staging" in url.lower() or "archive" in url.lower():
                    staging_leaks += 1
            attr = str(card.get("attributionRequired") or "")
            if "PokéCardex" in attr or "pokecardex" in attr.lower() or not card.get("integrationSource"):
                attribution_ok += 1

            if set_id not in seen_sets:
                conn.execute(
                    "INSERT INTO sets VALUES(?,?,?,?,?,?)",
                    (set_id, "zh-cn", str(card.get("region") or "CN"), native_set, normalize(native_set), card.get("releaseDate")),
                )
                seen_sets.add(set_id)

            provider_ids_json = json.dumps(provider_ids, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            provider_set_codes_json = json.dumps(
                sorted({str(v) for v in provider_sets.values() if v}),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            values = (
                cid,
                base_id,
                set_id,
                set_id,
                "zh-cn",
                str(card.get("region") or "CN"),
                native_name,
                english_name,
                native_set,
                card.get("englishSetName"),
                printed,
                printed,
                norm,
                english_name,
                native_name,
                native_set,
                normalize(english_name or native_name),
                normalize(native_name),
                normalize(native_set),
                set_code or None,
                json.dumps(provider_sets, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                json.dumps(aliases, ensure_ascii=False, separators=(",", ":")),
                card.get("rarity"),
                card.get("regulationMark"),
                promo,
                card.get("releaseDate"),
                thumb,
                display,
                "placeholder",
                "placeholder",
                None,
                next(iter(provider_ids.values()), None) if provider_ids else None,
                set_code or None,
                thumb,
                display,
                "placeholder",
                0,
                provider_ids_json,
                provider_set_codes_json,
                "source" if native_name else "missing",
                card.get("canonicalCardName"),
            )
            conn.execute("INSERT INTO cards VALUES(" + ",".join("?" for _ in values) + ")", values)
            conn.execute(
                "INSERT INTO cards_fts VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    base_id,
                    native_name,
                    english_name,
                    native_set,
                    card.get("englishSetName"),
                    printed,
                    norm,
                    set_code,
                    " ".join(aliases),
                ),
            )
            for alias in sorted({normalize(v) for v in aliases if normalize(v)}):
                conn.execute("INSERT OR IGNORE INTO card_aliases VALUES(?,?,?)", (base_id, alias, "search_alias"))
            counts["zh-cn"] += 1

    conn.executemany(
        "INSERT INTO meta(key,value) VALUES(?,?)",
        (
            ("schema_version", SCHEMA_VERSION),
            ("searchIndexSchemaVersion", SCHEMA_VERSION),
            ("packSchemaVersion", "2.2.0"),
            ("source", "zh_hans_rc_qa_pack"),
            ("packId", "zh-cn"),
            ("packKind", "language"),
            ("languages", "zh-cn"),
            ("recordCount", str(counts["zh-cn"])),
            ("ftsRecordCount", str(counts["zh-cn"])),
            ("productRecordCount", "0"),
            ("productContentRecordCount", "0"),
            ("generatedAtUtc", utc_now()),
            ("artworkPolicy", "placeholders_only"),
            ("attributionRequired", "Data courtesy of PokéCardex (https://pokecardex.com)"),
        ),
    )
    conn.commit()
    conn.close()
    tmp.replace(out)
    sha = hashlib.sha256(out.read_bytes()).hexdigest()
    manifest = {
        "generatedAtUtc": utc_now(),
        "packPath": str(out),
        "sha256": sha,
        "bytes": out.stat().st_size,
        "cardCount": counts["zh-cn"],
        "setCount": len(seen_sets),
        "placeholderUrl": PLACEHOLDER,
        "stagingUrlLeaks": staging_leaks,
        "attributionOkRows": attribution_ok,
        "purpose": "local_rc_qa_only_not_for_production",
    }
    (OUT_DIR / "qa_pack_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2))
    return 0 if staging_leaks == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
