#!/usr/bin/env python3
"""Rebuild missing/corrupt PokéCardex staging asset.json from display.webp + folder name."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

import pokecardex_zh_hans_acquire as p

DEFAULT_STAGING = Path(r"D:\CardScanR_Archive\image_staging\pokecardex_zh_hans_20261014")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def main() -> int:
    staging = DEFAULT_STAGING
    repaired = 0
    for display in staging.glob("zh-Hans/*/*/display.webp"):
        asset = display.parent / "asset.json"
        need = False
        if not asset.exists() or asset.stat().st_size == 0:
            need = True
        else:
            try:
                json.loads(asset.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                need = True
        if not need:
            continue
        set_code = display.parent.parent.name
        folder = display.parent.name
        num, _, ver = folder.partition("_v")
        data = display.read_bytes()
        try:
            with Image.open(display) as im:
                im.load()
                w, h = im.size
        except Exception as exc:
            qdir = staging / "_quarantine" / set_code / folder
            qdir.mkdir(parents=True, exist_ok=True)
            (qdir / "quarantine.json").write_text(
                json.dumps(
                    {
                        "reason": "undecodable_display",
                        "error": str(exc),
                        "path": str(display),
                        "at": utc_now(),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            # Remove bad bytes so a later acquire resume can refetch
            try:
                display.unlink()
            except OSError:
                pass
            if asset.exists():
                try:
                    asset.unlink()
                except OSError:
                    pass
            continue
        meta = {
            "schemaVersion": "1.0.0",
            "stagingKey": f"pokecardex|zh-Hans|{set_code}|{num}|v{ver or '1'}",
            "language": "zh-Hans",
            "region": "CN",
            "sourceProvider": "pokecardex",
            "providerSetId": set_code,
            "normalizedCollectorNumber": num[1:] if num.startswith("s") and num[1:].isdigit() else num,
            "providerVersionId": int(ver or 1) if str(ver or "1").isdigit() else 1,
            "rightsStatus": "staging_only_pokecardex_host_retrieve_allowed_pokemon_ip_pending",
            "licenceConfirmed": False,
            "productionPromotionAllowed": False,
            "rightsBasis": "pokecardex_written_permission_2026-10-13_images_host_only",
            "attributionRequired": "Data courtesy of PokéCardex (https://pokecardex.com)",
            "sha256": p.sha256_bytes(data),
            "width": w,
            "height": h,
            "bytes": len(data),
            "repairedAt": utc_now(),
            "validationStatus": "acquired",
        }
        asset.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        repaired += 1
    print(json.dumps({"repairedAssetJson": repaired}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
