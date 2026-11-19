#!/usr/bin/env python3
"""HEAD-only availability preflight for exact verified TCGdex image URLs.

Validates response metadata only. No image bytes, redirects, caching, mirroring,
credentials, or production data writes. Source permissions are NEVER inferred.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener
from tools.tcgdex_metadata_batch import NoRedirect

# These nine extensionless source image references were verified in the
# review-only PR #73 artifact generated on 19-Nov-2026.
IMAGE_BASES = tuple(
    "https://assets.tcgdex.net/" + lang + "/" + series + "/" + sid + "/" + number
    for lang, series, sid in (
        ("zh-tw", "S", "SI"),
        ("id", "SV", "SV4a"),
        ("th", "SV", "SV7s"),
    )
    for number in ("001", "002", "003")
)
MAX_HEAD_REQUESTS = 9


def low_webp_url(base: str) -> str:
    parts = urlsplit(base)
    if parts.scheme != "https" or parts.netloc != "assets.tcgdex.net" or parts.query or parts.fragment:
        raise ValueError("unexpected_asset_host")
    pieces = parts.path.strip("/").split("/")
    if len(pieces) != 4:
        raise ValueError("unexpected_asset_path")
    lang, series, set_id, number = pieces
    if (lang, series, set_id) not in (("zh-tw","S","SI"),("id","SV","SV4a"),("th","SV","SV7s")):
        raise ValueError("asset_identity_not_allowlisted")
    if number not in ("001","002","003"):
        raise ValueError("asset_collector_not_allowlisted")
    return base + "/low.webp"


def head_check(url: str) -> dict:
    # Explicitly exclude redirects so an expected image cannot lead to an
    # unrelated host, tracking endpoint or arbitrary binary payload.
    opener = build_opener(NoRedirect())
    request = Request(url, method="HEAD", headers={
        "Accept": "image/webp", "User-Agent": "CardScanR-image-source-head-metadata-canary/1.0",
    })
    try:
        with opener.open(request, timeout=8) as response:
            code = int(response.status)
            ctype = response.headers.get("Content-Type","").split(";",1)[0].strip().lower()
            clen = response.headers.get("Content-Length")
            return {
                "http_status": code, "content_type": ctype,
                "declared_bytes": int(clen) if clen and clen.isdigit() else None,
                "classification": "confirmed_image_metadata"
                if code == 200 and ctype == "image/webp" else "unverified_response",
            }
    except HTTPError as exc:
        code = int(exc.code)
        category = {404:"not_found",405:"head_unsupported",429:"rate_limited",403:"forbidden"}.get(code,"http_error")
        return {"http_status": code, "content_type": None, "declared_bytes": None,
                "classification": category}
    except (URLError, TimeoutError, OSError) as exc:
        return {"http_status": None, "content_type": None, "declared_bytes": None,
                "classification": type(exc).__name__}


def sample_image_references(*, pace_seconds: float = 1.25, checker=head_check) -> dict:
    if not 0 <= pace_seconds <= 3:
        raise ValueError("invalid_head_pacing")
    checks = []
    for base in IMAGE_BASES:
        url = low_webp_url(base)
        if checks and pace_seconds:
            time.sleep(pace_seconds)
        row = checker(url)
        if not isinstance(row, dict) or "classification" not in row:
            raise ValueError("invalid_head_probe_result")
        checks.append({"source_image_base": base, "probe_url": url, **row})
        # Stop on rate limit, never issue retries or run into quota exhaustion.
        if row["classification"] == "rate_limited":
            break
    results = {
        "schema_version": 1,
        "classification": "PRIVATE_SOURCE_ASSET_HEAD_METADATA_ONLY",
        "asset_host": "assets.tcgdex.net",
        "documented_asset_suffix": "/low.webp",
        "image_source_samples": len(IMAGE_BASES),
        "head_requests": len(checks),
        "max_head_requests": MAX_HEAD_REQUESTS,
        "counts": {
            status: sum(item["classification"] == status for item in checks)
            for status in sorted({item["classification"] for item in checks})
        },
        "observations": checks,
        "image_bodies_downloaded": 0, "binary_files_saved": 0,
        "production_writes": 0, "canonical_identity_assigned": False,
        "artwork_rights_granted": False, "safe_to_publish": False,
    }
    if results["head_requests"] > MAX_HEAD_REQUESTS:
        raise AssertionError("too_many_requests")
    return results


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args=parser.parse_args()
    if args.output.is_symlink() or args.output.exists():
        raise FileExistsError("output_file_must_not_exist")
    result=sample_image_references()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    print(json.dumps({"head_requests":result["head_requests"], "counts":result["counts"],
                      "image_bodies_downloaded":0,"safe_to_publish":False},sort_keys=True))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
