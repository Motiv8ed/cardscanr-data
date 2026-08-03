#!/usr/bin/env python3
"""Rewrite search-index image URLs to CardScanR R2 mirrors/placeholders."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlparse

RUNTIME = Path(r"D:\CardScanR_worldwide_runtime_20260802")
DEFAULT_MIRROR_DB = RUNTIME / "image_mirror_r2" / "mirror_checkpoint.sqlite"
PLACEHOLDER = (
    "https://pub-258b8de1c4964f538a8cb08022761430.r2.dev/"
    "v2/catalog/pokemon/placeholders/card_missing.webp"
)
CARDSCANR_MARKERS = ("r2.dev", "cardscanr", "pages.dev", "andygore149.workers.dev")


def is_cardscanr(url: str | None) -> bool:
    if not url:
        return True
    host = urlparse(url).netloc.lower()
    return any(marker in host for marker in CARDSCANR_MARKERS)


def load_url_map(mirror_db: Path) -> dict[str, tuple[str, str]]:
    """Map source_url -> (display_url, thumb_url)."""
    mapping: dict[str, tuple[str, str]] = {}
    if not mirror_db.is_file():
        return mapping
    connection = sqlite3.connect(f"file:{mirror_db.resolve().as_posix()}?mode=ro", uri=True)
    try:
        for source_url, display_url, thumb_url in connection.execute(
            "select source_url, display_url, thumb_url from mirrored"
        ):
            mapping[str(source_url)] = (str(display_url), str(thumb_url))
    finally:
        connection.close()
    # Also join validation checkpoints for alternate source_url spellings sharing sha.
    for checkpoint in list(RUNTIME.glob("card_image_validation*/checkpoint.sqlite")) + [
        RUNTIME / "product_image_validation" / "checkpoint.sqlite"
    ]:
        if not checkpoint.is_file():
            continue
        connection = sqlite3.connect(f"file:{checkpoint.resolve().as_posix()}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                """
                select a.source_url, m.display_url, m.thumb_url
                from assets a
                join mirrored m on m.sha256 = lower(a.sha256)
                where a.status = 'pass' and a.sha256 is not null
                """
            )
        except sqlite3.OperationalError:
            # mirrored table lives only in mirror_db; attach it.
            connection.close()
            connection = sqlite3.connect(f"file:{checkpoint.resolve().as_posix()}?mode=ro", uri=True)
            connection.execute(f"attach database '{mirror_db.resolve().as_posix()}' as mirror")
            rows = connection.execute(
                """
                select a.source_url, m.display_url, m.thumb_url
                from assets a
                join mirror.mirrored m on m.sha256 = lower(a.sha256)
                where a.status = 'pass' and a.sha256 is not null
                """
            )
        for source_url, display_url, thumb_url in rows:
            mapping.setdefault(str(source_url), (str(display_url), str(thumb_url)))
        connection.close()
    return mapping


def rewrite(database: Path, output: Path, mirror_db: Path, placeholder: str) -> dict:
    if output.exists():
        output.unlink()
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(database, output)
    url_map = load_url_map(mirror_db)
    connection = sqlite3.connect(str(output))
    card_rewritten = 0
    card_placeholder = 0
    product_rewritten = 0
    product_placeholder = 0
    for printing_id, display, thumb in connection.execute(
        "select canonical_printing_id, image_display_url, image_thumbnail_url from cards"
    ):
        new_display = display
        new_thumb = thumb
        changed = False
        image_source = "cardscanr_r2"
        if not display and not thumb:
            new_display = placeholder
            new_thumb = placeholder
            card_placeholder += 1
            changed = True
            image_source = "cardscanr_placeholder"
        elif display and not is_cardscanr(display):
            mapped = url_map.get(display)
            if mapped:
                new_display, new_thumb = mapped
                card_rewritten += 1
            else:
                new_display = placeholder
                new_thumb = placeholder
                card_placeholder += 1
                image_source = "cardscanr_placeholder"
            changed = True
        elif thumb and not is_cardscanr(thumb):
            mapped = url_map.get(thumb)
            if mapped:
                new_display = mapped[0]
                new_thumb = mapped[1]
                card_rewritten += 1
            else:
                new_display = display or placeholder
                new_thumb = placeholder
                card_placeholder += 1
                image_source = "cardscanr_placeholder"
            changed = True
        if changed:
            connection.execute(
                "update cards set image_display_url=?, image_thumbnail_url=?, thumbnail_url=?, large_image_url=?, image_source=? where canonical_printing_id=?",
                (new_display, new_thumb, new_thumb, new_display, image_source, printing_id),
            )
    for variant_id, image_url in connection.execute(
        "select product_variant_id, image_url from sealed_products"
    ):
        if not image_url:
            connection.execute(
                "update sealed_products set image_url=?, image_provider=? where product_variant_id=?",
                (placeholder, "cardscanr_placeholder", variant_id),
            )
            product_placeholder += 1
        elif not is_cardscanr(image_url):
            mapped = url_map.get(image_url)
            if mapped:
                connection.execute(
                    "update sealed_products set image_url=?, image_provider=? where product_variant_id=?",
                    (mapped[0], "cardscanr_r2", variant_id),
                )
                product_rewritten += 1
            else:
                connection.execute(
                    "update sealed_products set image_url=?, image_provider=? where product_variant_id=?",
                    (placeholder, "cardscanr_placeholder", variant_id),
                )
                product_placeholder += 1
    connection.commit()
    third_cards = connection.execute(
        """
        select count(*) from cards
        where (coalesce(image_display_url,'') != '' and image_display_url not like '%r2.dev%'
               and image_display_url not like '%cardscanr%' and image_display_url not like '%pages.dev%'
               and image_display_url not like '%andygore149.workers.dev%')
           or (coalesce(image_thumbnail_url,'') != '' and image_thumbnail_url not like '%r2.dev%'
               and image_thumbnail_url not like '%cardscanr%' and image_thumbnail_url not like '%pages.dev%'
               and image_thumbnail_url not like '%andygore149.workers.dev%')
        """
    ).fetchone()[0]
    third_products = connection.execute(
        """
        select count(*) from sealed_products
        where coalesce(image_url,'') != ''
          and image_url not like '%r2.dev%'
          and image_url not like '%cardscanr%'
          and image_url not like '%pages.dev%'
          and image_url not like '%andygore149.workers.dev%'
        """
    ).fetchone()[0]
    connection.close()
    return {
        "output": str(output),
        "urlMapSize": len(url_map),
        "cardRewritten": card_rewritten,
        "cardPlaceholder": card_placeholder,
        "productRewritten": product_rewritten,
        "productPlaceholder": product_placeholder,
        "thirdPartyCardUrlsRemaining": third_cards,
        "thirdPartyProductUrlsRemaining": third_products,
        "thirdPartyRuntimeImageUrls": third_cards + third_products,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mirror-db", type=Path, default=DEFAULT_MIRROR_DB)
    parser.add_argument("--placeholder-url", default=PLACEHOLDER)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    result = rewrite(args.database, args.output, args.mirror_db, args.placeholder_url)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0 if result["thirdPartyRuntimeImageUrls"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
