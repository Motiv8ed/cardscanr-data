#!/usr/bin/env python3
"""Pass 10: import identity-confirmed seller photos for the final 3 cards."""

from __future__ import annotations

import hashlib
import json
import urllib.request
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps, ImageStat
import image_independence_multisource_resolver as m

ROOT = Path(__file__).resolve().parents[1]
CAND = ROOT / "data" / "images" / "independence" / "pass10_candidates"
PROCESSED = CAND / "processed"
PROCESSED.mkdir(parents=True, exist_ok=True)
RIGHTS_STATUS = "seller_photo_attribution_retained_licence_not_inferred"
RIGHTS = (
    "user_confirmed_acquisition_permission_pass8_2026-10-10; "
    "seller_licence_not_inferred_from_public_availability"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def crop_card(path: Path, out: Path, *, pad: float = 0.01) -> Path:
    im = Image.open(path).convert("RGB")
    # Auto-contrast + find content via simple border detection
    gray = ImageOps.grayscale(im)
    w, h = gray.size
    # Sample border median to detect background
    border = []
    for x in range(w):
        border.append(gray.getpixel((x, 0)))
        border.append(gray.getpixel((x, h - 1)))
    for y in range(h):
        border.append(gray.getpixel((0, y)))
        border.append(gray.getpixel((w - 1, y)))
    border.sort()
    bg = border[len(border) // 2]
    # Threshold: pixels far from background are content
    px = gray.load()
    ys = []
    xs = []
    thr = 28
    step = max(1, min(w, h) // 400)
    for y in range(0, h, step):
        for x in range(0, w, step):
            if abs(px[x, y] - bg) > thr:
                xs.append(x)
                ys.append(y)
    if not xs:
        im.save(out, format="JPEG", quality=95)
        return out
    left, right = min(xs), max(xs)
    top, bottom = min(ys), max(ys)
    # Expand slightly then clamp
    dx = int((right - left) * pad)
    dy = int((bottom - top) * pad)
    box = (
        max(0, left - dx),
        max(0, top - dy),
        min(w, right + dx),
        min(h, bottom + dy),
    )
    cropped = im.crop(box)
    # Prefer portrait card aspect ~0.716
    cw, ch = cropped.size
    aspect = cw / ch if ch else 1
    if aspect > 0.85:  # too wide — keep as-is after light trim
        pass
    cropped = ImageOps.exif_transpose(cropped)
    cropped.save(out, format="JPEG", quality=95)
    print(f"cropped {path.name} -> {out.name} {cropped.size} from {im.size}")
    return out


def acquire_local(
    *,
    image_path: Path,
    canonical: str,
    language: str,
    set_id: str,
    collector: str,
    name: str,
    set_name: str,
    card_id: str,
    source: str,
    evidence: str,
    original_url: str,
) -> dict:
    raw = image_path.read_bytes()
    dest = m.master_dir(language, set_id, card_id)
    ext = ".jpg"
    m.write_atomic(dest / f"original{ext}", raw)
    # also keep source sidecar evidence
    (dest / "source_evidence.json").write_text(
        json.dumps(
            {
                "canonicalBaseId": canonical,
                "source": source,
                "evidence": evidence,
                "originalUrl": original_url,
                "localSourcePath": str(image_path),
                "acquiredAt": utc_now(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    webp = m.to_webp(raw)
    display = dest / "display.webp"
    m.write_atomic(display, webp)
    # thumb
    im = Image.open(BytesIO(webp)).convert("RGB")
    im.thumbnail((300, 420))
    tbuf = BytesIO()
    im.save(tbuf, format="WEBP", quality=80, method=4)
    thumb = dest / "thumb.webp"
    m.write_atomic(thumb, tbuf.getvalue())
    digest = hashlib.sha256(webp).hexdigest()
    object_key = (
        f"cards/{language}/{set_id.lower()}/{card_id.lower()}/{digest[:12]}/display.webp"
    )
    thumb_key = (
        f"cards/{language}/{set_id.lower()}/{card_id.lower()}/{digest[:12]}/thumb.webp"
    )
    alias = f"cards/{language}/{set_id.lower()}/{card_id.lower()}/display.webp"
    thumb_alias = f"cards/{language}/{set_id.lower()}/{card_id.lower()}/thumb.webp"
    uploaded = m.wrangler_put(object_key, display, "image/webp")
    public = None
    if uploaded:
        m.wrangler_put(thumb_key, thumb, "image/webp")
        pointer = dest / "display.webp.current.txt"
        pointer.write_text(object_key, encoding="utf-8")
        m.wrangler_put(f"{alias}.current", pointer, "text/plain")
        m.wrangler_put(alias, display, "image/webp")
        m.wrangler_put(thumb_alias, thumb, "image/webp")
        public = f"{m.CDN}/{alias}"
        code = urllib.request.urlopen(
            urllib.request.Request(public, headers={"User-Agent": "CardScanR-pass10"}),
            timeout=40,
        ).status
        if code != 200:
            return {"status": "cdn_verify_failed", "public": public, "http": code}
    meta = {
        "canonicalBaseId": canonical,
        "cardId": card_id,
        "language": language,
        "setId": set_id,
        "setName": set_name,
        "collectorNumber": collector,
        "name": name,
        "sha256": digest,
        "byteSize": len(webp),
        "mimeType": "image/webp",
        "sourceProvider": source,
        "originalSourceUrl": original_url,
        "matchBasis": evidence,
        "hostedObjectKey": object_key if uploaded else None,
        "publicUrl": public,
        "acquisition": "pass10_seller_photo_recovery",
        "acquiredAt": utc_now(),
        "provenanceConfidence": "CONFIRMED",
        "derivativeStatus": "display_webp",
        "uploaded": uploaded,
        "hostingStatus": "hosted_application_cdn" if uploaded else "local_only",
        "rightsStatus": RIGHTS_STATUS,
        "rightsBasis": RIGHTS,
    }
    m.write_atomic(
        dest / "asset.json",
        json.dumps(meta, indent=2, ensure_ascii=False).encode("utf-8"),
    )
    return {"status": "ok", "public": public, "sha256": digest, "cardId": card_id}


def main() -> None:
    jobs = []
    # Kirlia — best flat front from RL Trading LV listing
    kirlia = crop_card(
        CAND / "kirlia_rltrading_front.jpg",
        PROCESSED / "kirlia_068_195_tord_reklev_front.jpg",
    )
    jobs.append(
        dict(
            image_path=kirlia,
            canonical="pokemon|en|2282|068/195|kirlia_2023_tord_reklev",
            language="en",
            set_id="2282",
            collector="068/195",
            name="Kirlia 2023 Tord Reklev",
            set_name="World Championship Decks",
            card_id="pk_d902316dd1f790984a0ac09946ee39738ef72cf59135fff3dec78a62e6b202fd52ac9dbea7fecf2a8b0a6fcec160",
            source="ebay_seller_photo",
            evidence=(
                "ebay.co.uk/itm/407219812615 RL Trading LV seller photo: "
                "Tord Reklev signature on art-box bottom-right; 068/195 Refinement; "
                "companion WCS 2023 Yokohama back photo on same listing. "
                "Corroborated by ebay.co.uk/itm/227343408134 RoseCitySupply."
            ),
            original_url="https://i.ebayimg.com/images/g/uD8AAeSw-OhqqerX/s-l1600.jpg",
        )
    )
    # Iron Thorns gold — full front + gold close-up from same listing
    iron = crop_card(
        CAND / "iron_03_s-l1600.jpg",
        PROCESSED / "iron_thorns_ex_gold_cifuentes_front.jpg",
    )
    jobs.append(
        dict(
            image_path=iron,
            canonical=(
                "pokemon|en|2282|077/167|"
                "iron_thorns_ex_077_167_2024_fernando_cifuentes_gold_signature"
            ),
            language="en",
            set_id="2282",
            collector="077/167",
            name="Iron Thorns ex 077 167 2024 Fernando Cifuentes Gold Signature",
            set_name="World Championship Decks",
            card_id="pk_dc448145a1ebaf8938a15a5c8bedac31d0dfa7b80e57bdc9fc84ed69dbdd00bb6740b3b1e6c434c0d32b260bb737",
            source="ebay_seller_photo",
            evidence=(
                "ebay.co.uk/itm/407167955445 teamrocketrips_shop: photo 03 full front "
                "shows WCS legality text + Fernando Cifuentes signature; photo 02 "
                "macro proves metallic GOLD ink; photo 04 is 2024 WCS Honolulu back. "
                "eBay item-specifics may disagree with physical footer (no TWM code); "
                "physical WCS treatment confirmed."
            ),
            original_url="https://i.ebayimg.com/images/g/03cAAeSw7RNqjPja/s-l1600.jpg",
        )
    )
    # Rayquaza Pixel Cosmos — seller photo; foil grain checked (dense pixels, no large galaxy swirls)
    ray = crop_card(
        CAND / "ray_01_s-l1600.jpg",
        PROCESSED / "rayquaza_swsh029_pixel_cosmos_front.jpg",
    )
    jobs.append(
        dict(
            image_path=ray,
            canonical="pokemon|en|2374|SWSH029|rayquaza_swsh029_pixel_cosmos_holo",
            language="en",
            set_id="2374",
            collector="SWSH029",
            name="Rayquaza SWSH029 Pixel Cosmos Holo",
            set_name="Miscellaneous Cards & Products",
            card_id="pk_bca4b8b16f72c213f356287c852825d71a268c9b50a45083331e5df7211a59baf2e92333ecc108db52dac238f0f1d9",
            source="ebay_seller_photo",
            evidence=(
                "ebay.com/itm/297646741631 (via ebay.co.uk): seller photo shows SWSH029 "
                "so-taro front; art-box foil is dense pixel/speckle cosmos without large "
                "circular galaxy swirls — consistent with Pixel Cosmos (Sea & Sky). "
                "Not title-only; foil inspected from photograph."
            ),
            original_url="https://i.ebayimg.com/images/g/Ad0AAeSwdCho2uIp/s-l1600.jpg",
        )
    )

    results = []
    for job in jobs:
        print("acquiring", job["canonical"])
        res = acquire_local(**job)
        print(res)
        results.append({"canonical": job["canonical"], **res})
    (CAND / "pass10_import_results.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
