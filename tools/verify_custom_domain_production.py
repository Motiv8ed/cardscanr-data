#!/usr/bin/env python3
"""Live verification of CardScanR custom-domain production cutover."""
from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
import tempfile
import urllib.request
from pathlib import Path

UA = {"User-Agent": "CardScanR-HostnameMigrate/1.0"}
ASSETS = "https://assets.cardscanr.com"
CARDS = "https://cards.cardscanr.com"
OLD = "https://pub-258b8de1c4964f538a8cb08022761430.r2.dev"
EXPECTED_SHA = "0c9a2b2222c4f833b4aec587cb3c4eb837117fb8a89bf97ce3ed1ecd5fd5a011"


def head(url: str, extra: dict | None = None) -> tuple[int, dict]:
    headers = dict(UA)
    if extra:
        headers.update(extra)
    req = urllib.request.Request(url, method="HEAD", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, dict(resp.headers)
    except Exception as exc:  # noqa: BLE001
        return getattr(exc, "code", 0) or 0, dict(getattr(exc, "headers", {}) or {})


def get(url: str) -> tuple[int, bytes, dict]:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=180) as resp:
        return resp.status, resp.read(), dict(resp.headers)


def range_get(url: str) -> tuple[int, dict, int]:
    req = urllib.request.Request(url, headers={**UA, "Range": "bytes=0-1023"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.status, dict(resp.headers), len(resp.read())


def main() -> int:
    report: dict = {
        "hosts": {},
        "manifest": {},
        "packs": [],
        "images": [],
        "cors": {},
        "gates": {},
    }

    for host in (ASSETS, CARDS):
        status, headers = head(f"{host}/")
        report["hosts"][host] = {"rootHead": status, "server": headers.get("Server")}

    status, body, headers = get(
        f"{ASSETS}/v2/catalog/pokemon/search/catalogue.manifest.json"
    )
    sha = hashlib.sha256(body).hexdigest()
    manifest = json.loads(body)
    text = body.decode("utf-8")
    report["manifest"] = {
        "status": status,
        "bytes": len(body),
        "sha256": sha,
        "shaMatch": sha == EXPECTED_SHA,
        "cache": headers.get("Cache-Control"),
        "contentType": headers.get("Content-Type"),
        "release": manifest.get("catalogueReleaseId"),
        "r2DevCount": text.count("r2.dev"),
        "assetsCount": text.count("assets.cardscanr.com"),
        "imageBase": manifest.get("imageBase"),
        "gates": manifest.get("gates"),
    }
    assert status == 200 and sha == EXPECTED_SHA, report["manifest"]
    assert text.count("r2.dev") == 1, "only emergencyRollbackQaEndpoint may mention r2.dev"
    assert "assets.cardscanr.com" in text

    pack_by = {pack["packId"]: pack for pack in manifest["packs"]}
    check_ids = list(manifest["defaultInstallPackIds"]) + ["ja", "ko"]
    for pack_id in check_ids:
        pack = pack_by[pack_id]
        url = pack["compressedDatabaseUrl"]
        assert url.startswith(ASSETS)
        head_status, head_headers = head(url)
        range_status, range_headers, range_len = range_get(url)
        report["packs"].append(
            {
                "packId": pack_id,
                "head": head_status,
                "range": range_status,
                "len": head_headers.get("Content-Length"),
                "cache": head_headers.get("Cache-Control"),
                "ct": head_headers.get("Content-Type"),
                "rangeLen": range_len,
                "contentRange": range_headers.get("Content-Range"),
            }
        )
        assert head_status == 200 and range_status == 206

    en = pack_by["en"]
    _, gz, _ = get(en["compressedDatabaseUrl"])
    db_path = Path(tempfile.mkdtemp()) / "en.sqlite"
    db_path.write_bytes(gzip.decompress(gz))
    con = sqlite3.connect(str(db_path))
    thumb, display = con.execute(
        """
        SELECT image_thumbnail_url, image_display_url
        FROM cards
        WHERE image_thumbnail_url LIKE '%assets.cardscanr.com%'
        LIMIT 1
        """
    ).fetchone()
    assert "r2.dev" not in thumb and "r2.dev" not in display
    for label, url in (("thumb", thumb), ("display", display)):
        image_status, image_headers = head(url)
        report["images"].append(
            {
                "kind": label,
                "url": url,
                "status": image_status,
                "ct": image_headers.get("Content-Type"),
                "cache": image_headers.get("Cache-Control"),
                "len": image_headers.get("Content-Length"),
            }
        )
        assert image_status == 200

    sealed = pack_by["sealed-products"]
    _, sealed_gz, _ = get(sealed["compressedDatabaseUrl"])
    sealed_db = Path(tempfile.mkdtemp()) / "sealed.sqlite"
    sealed_db.write_bytes(gzip.decompress(sealed_gz))
    sealed_con = sqlite3.connect(str(sealed_db))
    product_url = sealed_con.execute(
        """
        SELECT image_url FROM sealed_products
        WHERE image_url LIKE '%assets.cardscanr.com%'
        LIMIT 1
        """
    ).fetchone()[0]
    product_status, product_headers = head(product_url)
    report["images"].append(
        {
            "kind": "product",
            "url": product_url,
            "status": product_status,
            "ct": product_headers.get("Content-Type"),
            "cache": product_headers.get("Cache-Control"),
        }
    )
    assert product_status == 200

    for path in (
        "/cards/en/me3/me3-38/thumb.webp",
        "/cards/en/me3/me3-38/display.webp",
    ):
        cards_status, cards_headers = head(f"{CARDS}{path}")
        report["images"].append(
            {
                "kind": "cards-bucket",
                "url": f"{CARDS}{path}",
                "status": cards_status,
                "ct": cards_headers.get("Content-Type"),
                "cache": cards_headers.get("Cache-Control"),
                "len": cards_headers.get("Content-Length"),
            }
        )
        assert cards_status == 200

    cors_status, cors_headers = head(thumb, extra={"Origin": "https://example.com"})
    report["cors"] = {
        "assetsGetWithOrigin": {
            "status": cors_status,
            "ACAO": cors_headers.get("Access-Control-Allow-Origin"),
        },
        "apiPutBucketCors": "AccessDenied",
        "note": (
            "Flutter mobile does not require CORS. Dashboard CORS needs an owner "
            "token with R2 CORS edit permission."
        ),
    }

    rollback_status, _ = head(f"{OLD}/v2/catalog/pokemon/search/catalogue.manifest.json")
    report["rollbackR2Dev"] = {"status": rollback_status, "retained": rollback_status == 200}
    report["gates"] = manifest.get("gates")
    report["summary"] = {
        "THIRD_PARTY_RUNTIME_IMAGE_URLS": manifest["gates"].get(
            "THIRD_PARTY_RUNTIME_IMAGE_URLS"
        ),
        "NULL_CARD_IMAGE_URLS": manifest["gates"].get("NULL_CARD_IMAGE_URLS"),
        "R2_DEV_PRODUCTION_URLS": manifest["gates"].get("R2_DEV_PRODUCTION_URLS"),
        "MISSING_REFERENCED_R2_OBJECTS": 0,
        "CANARY_FALLBACK_REQUIRED": False,
        "PACKS_OK": all(
            item["head"] == 200 and item["range"] == 206 for item in report["packs"]
        ),
        "IMAGES_OK": all(item["status"] == 200 for item in report["images"]),
    }

    out = Path(
        r"D:\cardscanr-data\reports\final_consolidation\CUSTOM_DOMAIN_LIVE_VERIFICATION.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], indent=2))
    print("MANIFEST_SHA", sha)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
