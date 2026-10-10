#!/usr/bin/env python3
"""Re-upload local master display/thumb for CDN aliases that 404 or contain spaces."""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

import image_independence_multisource_resolver as m

ROOT = Path(__file__).resolve().parent.parent
CATALOGUE = ROOT / "public" / "v1" / "catalog" / "pokemon" / "en" / "cards"

# Canonical IDs that failed closeout CDN HEAD/GET.
TARGETS = [
    "pokemon|en|pkmtch|SV-P 162|giratina_vstar",
    "pokemon|en|sm7|141|rainbow_brush",
    "pokemon|en|sv6|71|heliolisk",
    "pokemon|en|xy10|88|cinccino",
    "pokemon|en|xy10|90|alakazam_spirit_link",
    "pokemon|en|xyp|XY39|kingdra",
    "pokemon|en|xyp|XY46|altaria",
    "pokemon|en|xyp|XY68|chesnaught",
]


def safe_seg(seg: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", seg).lower()


_ASSET_INDEX: dict[str, Path] | None = None


def asset_index() -> dict[str, Path]:
    global _ASSET_INDEX
    if _ASSET_INDEX is None:
        idx: dict[str, Path] = {}
        for sidecar in (ROOT / "data" / "images" / "master").rglob("asset.json"):
            try:
                meta = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            cid = str(meta.get("canonicalBaseId") or "").strip()
            if cid:
                idx[cid] = sidecar
        _ASSET_INDEX = idx
    return _ASSET_INDEX


def find_asset(canonical: str) -> Path | None:
    return asset_index().get(canonical)


def verify_url(url: str) -> int:
    req = urllib.request.Request(url, headers={"User-Agent": "CardScanR-cdn-repair"})
    with urllib.request.urlopen(req, timeout=40) as resp:
        return int(getattr(resp, "status", 200) or 200)


def rebind_catalogue(canonical: str, public: str, object_key: str, meta: dict) -> bool:
    set_id = str(meta.get("setId") or "")
    path = CATALOGUE / f"{set_id}.json"
    if not path.is_file():
        # set files are sometimes lowercased differently
        matches = list(CATALOGUE.glob("*.json"))
        path = next((p for p in matches if p.stem.lower() == set_id.lower()), path)
    data = json.loads(path.read_text(encoding="utf-8"))
    thumb = public[: -len("display.webp")] + "thumb.webp" if public.endswith("display.webp") else public
    changed = False
    for card in data.get("cards") or []:
        if str(card.get("canonicalBaseId") or "") != canonical:
            continue
        prov = card.get("imageProvenance") if isinstance(card.get("imageProvenance"), dict) else {}
        card["imageUrl"] = thumb
        card["imageUrlSmall"] = thumb
        card["imageSmall"] = thumb
        card["imageUrlLarge"] = public
        card["imageLarge"] = public
        card["imageSource"] = "cardscanr_cdn"
        card["imageCached"] = True
        prov.update(
            {
                "cdnPath": object_key,
                "publicUrl": public,
                "sha256": meta.get("sha256"),
                "provider": meta.get("sourceProvider") or prov.get("provider"),
            }
        )
        card["imageProvenance"] = prov
        changed = True
    if changed:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return changed


def repair_one(canonical: str) -> dict:
    sidecar = find_asset(canonical)
    if sidecar is None:
        return {"canonical": canonical, "status": "asset_missing"}
    meta = json.loads(sidecar.read_text(encoding="utf-8"))
    dest = sidecar.parent
    display = dest / "display.webp"
    thumb = dest / "thumb.webp"
    if not display.is_file():
        return {"canonical": canonical, "status": "display_missing"}
    if not thumb.is_file():
        # regenerate small thumb if absent
        from io import BytesIO

        from PIL import Image

        im = Image.open(display).convert("RGB")
        im.thumbnail((300, 420))
        buf = BytesIO()
        im.save(buf, format="WEBP", quality=80, method=4)
        m.write_atomic(thumb, buf.getvalue())

    language = str(meta.get("language") or "en")
    set_id = safe_seg(str(meta.get("setId") or ""))
    card_id = safe_seg(str(meta.get("cardId") or dest.name))
    digest = str(meta.get("sha256") or "")
    if not digest:
        import hashlib

        digest = hashlib.sha256(display.read_bytes()).hexdigest()
        meta["sha256"] = digest

    object_key = f"cards/{language}/{set_id}/{card_id}/{digest[:12]}/display.webp"
    thumb_key = f"cards/{language}/{set_id}/{card_id}/{digest[:12]}/thumb.webp"
    alias = f"cards/{language}/{set_id}/{card_id}/display.webp"
    thumb_alias = f"cards/{language}/{set_id}/{card_id}/thumb.webp"

    ok_content = m.wrangler_put(object_key, display, "image/webp")
    ok_thumb = m.wrangler_put(thumb_key, thumb, "image/webp")
    pointer = dest / "display.webp.current.txt"
    pointer.write_text(object_key, encoding="utf-8")
    ok_ptr = m.wrangler_put(f"{alias}.current", pointer, "text/plain")
    ok_alias = m.wrangler_put(alias, display, "image/webp")
    ok_talias = m.wrangler_put(thumb_alias, thumb, "image/webp")
    public = f"{m.CDN}/{alias}"

    http = None
    try:
        http = verify_url(public)
    except Exception as exc:  # noqa: BLE001
        http = str(exc)

    meta["hostedObjectKey"] = object_key
    meta["publicUrl"] = public
    meta["uploaded"] = bool(ok_content and ok_alias)
    meta["hostingStatus"] = (
        "hosted_application_cdn" if meta["uploaded"] else meta.get("hostingStatus")
    )
    meta["cardIdSafe"] = card_id
    sidecar.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rebound = rebind_catalogue(canonical, public, object_key, meta)
    return {
        "canonical": canonical,
        "status": "ok" if http == 200 and meta["uploaded"] else "partial",
        "public": public,
        "http": http,
        "uploads": {
            "content": ok_content,
            "thumb": ok_thumb,
            "pointer": ok_ptr,
            "alias": ok_alias,
            "thumbAlias": ok_talias,
        },
        "catalogueRebound": rebound,
    }


def main() -> int:
    results = []
    for cid in TARGETS:
        print("repair", cid, flush=True)
        results.append(repair_one(cid))
        print(json.dumps(results[-1], indent=2), flush=True)
    out = ROOT / "reports" / "image_independence" / "cdn_alias_repair_result.json"
    out.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    ok = all(r.get("status") == "ok" for r in results)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
