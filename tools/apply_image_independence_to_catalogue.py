#!/usr/bin/env python3
"""Point EN/JA catalogue image fields at CardScanR CDN when master has publicUrl."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOGUE = ROOT / "public" / "v1" / "catalog" / "pokemon"
MASTER = ROOT / "data" / "images" / "master"


def index_master() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for sidecar in MASTER.rglob("asset.json"):
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        public = (meta.get("publicUrl") or "").strip()
        # CDN-reconciled assets always have publicUrl from manifests.
        if not public and meta.get("acquisition") == "cdn_reconcile":
            # Reconstruct alias URL from identity.
            lang = meta.get("language")
            set_id = meta.get("setId")
            card_id = meta.get("cardId")
            if lang and set_id and card_id:
                public = (
                    f"https://cardscanr-images.andygore149.workers.dev/"
                    f"cards/{lang}/{str(set_id).lower()}/{str(card_id).lower()}/display.webp"
                )
                meta = {**meta, "publicUrl": public}
        if not public:
            continue
        if meta.get("canonicalBaseId"):
            out[str(meta["canonicalBaseId"])] = meta
        lang = str(meta.get("language") or "")
        card_id = str(meta.get("cardId") or "")
        if lang and card_id:
            out[f"{lang}|{card_id}"] = meta
            out[f"{lang}|{card_id.lower()}"] = meta
    return out


def keys_for(card: dict, set_id: str, folder: str) -> list[str]:
    keys = []
    if card.get("canonicalBaseId"):
        keys.append(str(card["canonicalBaseId"]))
    lang = "ja" if folder == "jp" else folder
    external = card.get("externalIds") if isinstance(card.get("externalIds"), dict) else {}
    providers = card.get("providerIds") if isinstance(card.get("providerIds"), dict) else {}
    for value in (
        external.get("pokemonTcgApiId"),
        external.get("tcgdexCardId"),
        providers.get("pokemonTcgApi"),
        providers.get("tcgdex"),
    ):
        if value:
            keys.append(f"{lang}|{value}")
            keys.append(f"{lang}|{str(value).lower()}")
    collector = str(card.get("collectorNumber") or "")
    keys.append(f"{lang}|{set_id}-{collector}")
    return keys


def apply_card(card: dict, meta: dict) -> bool:
    public = str(meta.get("publicUrl") or "").strip()
    if not public:
        return False
    thumb = public
    if public.endswith("/display.webp"):
        thumb = public[: -len("display.webp")] + "thumb.webp"
    original_small = card.get("imageSmall") or card.get("imageUrlSmall") or card.get("imageUrl")
    original_large = card.get("imageLarge") or card.get("imageUrlLarge") or card.get("imageUrl")
    card["imageProvenance"] = {
        "provider": meta.get("sourceProvider") or card.get("imageSource"),
        "originalUrl": meta.get("originalSourceUrl") or original_large or original_small,
        "originalSmallUrl": original_small,
        "originalLargeUrl": original_large,
        "retrievedAt": meta.get("acquiredAt"),
        "cdnPath": meta.get("hostedObjectKey"),
        "publicUrl": public,
        "derivativeStatus": meta.get("derivativeStatus"),
        "rightsStatus": "approved_for_mirror",
        "provenanceConfidence": meta.get("provenanceConfidence") or "CONFIRMED",
        "sha256": meta.get("sha256"),
    }
    card["imageSourceOriginal"] = card.get("imageSource")
    card["imageUrl"] = thumb
    card["imageUrlSmall"] = thumb
    card["imageSmall"] = thumb
    card["imageUrlLarge"] = public
    card["imageLarge"] = public
    card["imageSource"] = "cardscanr_cdn"
    card["imageCached"] = True
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    master = index_master()
    print("hosted master entries", len(master))
    changed_cards = 0
    changed_files = 0
    for folder in ("en", "jp"):
        for path in sorted((CATALOGUE / folder / "cards").glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            cards = data.get("cards") if isinstance(data, dict) else data
            if not isinstance(cards, list):
                continue
            file_changed = False
            for card in cards:
                if not isinstance(card, dict):
                    continue
                set_id = str(card.get("setId") or path.stem)
                hit = None
                for key in keys_for(card, set_id, folder):
                    hit = master.get(key)
                    if hit:
                        break
                if not hit:
                    continue
                if apply_card(card, hit):
                    changed_cards += 1
                    file_changed = True
            if file_changed and not args.dry_run:
                if isinstance(data, dict):
                    data["cards"] = cards
                    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                else:
                    path.write_text(json.dumps(cards, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                changed_files += 1
    print(json.dumps({"changedCards": changed_cards, "changedFiles": changed_files, "dryRun": args.dry_run}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
