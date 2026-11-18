#!/usr/bin/env python3
"""Read-only public CardScanR catalogue pointer audit. Never fetches pack DBs/images."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

BASE = "https://assets.cardscanr.com/v2/catalog/pokemon"
URLS = {
    "search": BASE + "/search/catalogue.manifest.json",
    "packs": BASE + "/packs/active/catalogue.packs.manifest.json",
}
MAX_MANIFEST = 2 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise ValueError("redirect_denied")


def fetch(url: str) -> tuple[Any, bytes]:
    if url not in URLS.values():
        raise ValueError("unapproved_manifest_origin_or_path")
    opener = build_opener(NoRedirect())
    request = Request(url, headers={
        "Accept": "application/json",
        "Cache-Control": "no-cache",
        "User-Agent": "CardScanR-ReadOnly-PublicPointerAudit/1.0",
    })
    with opener.open(request, timeout=15) as response:
        if response.status != 200:
            raise ValueError("manifest_not_http_200")
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower().strip()
        if content_type not in {"application/json", "application/octet-stream", "text/plain"}:
            raise ValueError("unexpected_manifest_mime_type")
        raw = response.read(MAX_MANIFEST + 1)
        if len(raw) > MAX_MANIFEST:
            raise ValueError("manifest_exceeds_limit")
    return json.loads(raw), raw


def summarize(name: str, data: Any, raw: bytes) -> dict:
    if not isinstance(data, dict):
        raise ValueError("manifest_root_not_object")
    report = {
        "url": URLS[name],
        "http_read_success": True,
        "byte_size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "schema_version": data.get("catalogueSchemaVersion", data.get("schemaVersion")),
        "release_id": data.get("catalogueReleaseId", data.get("releaseId")),
        "generated_at": data.get("generatedAt", data.get("generatedAtUtc")),
    }
    if name == "packs":
        packs = data.get("packs")
        if not isinstance(packs, list):
            raise ValueError("packs_manifest_has_no_packs_list")
        report["total_packs"] = len(packs)
        language_packs = []
        for item in packs:
            if not isinstance(item, dict) or item.get("kind") != "language":
                continue
            languages = item.get("languages")
            lang = languages[0] if isinstance(languages, list) and len(languages) == 1 else None
            language_packs.append({
                "pack_id": item.get("packId"),
                "language": lang,
                "records": item.get("recordCount"),
                "byte_size": item.get("rawSqliteBytes"),
            })
        report["language_packs"] = sorted(language_packs, key=lambda p: str(p["language"]))
        report["optional_pack_ids"] = data.get("optionalPackIds") or []
    else:
        # Do not claim the app has installed or activated a manifest because HTTP 200 succeeded.
        report["database_bytes"] = data.get("databaseBytes")
        report["database_sha256"] = data.get("sha256", data.get("databaseSha256"))
    return report


def audit(get: Callable[[str], tuple[Any, bytes]] = fetch) -> dict:
    receipts, errors = {}, []
    for name, url in URLS.items():
        try:
            data, raw = get(url)
            receipts[name] = summarize(name, data, raw)
        except (ValueError, HTTPError, URLError, TimeoutError, OSError, UnicodeError,
                json.JSONDecodeError) as error:
            errors.append({"manifest": name, "error_type": type(error).__name__,
                           "reason": str(error)[:160]})
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "classification": "PASS" if len(receipts) == len(URLS) else "PARTIAL",
        "checks": receipts, "errors": errors,
        "database_packs_downloaded": 0, "image_bytes_downloaded": 0,
        "production_writes": 0, "app_install_verified": False,
        "notes": [
            "HTTP manifests reveal public catalogue pointers, not the current installed app version",
            "A record count is not verified completeness of per-card data or images",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.is_symlink() or (args.output.exists() and (not args.output.is_dir()
                                                             or any(args.output.iterdir()))):
        parser.error("output must be a new empty folder")
    report = audit()
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "SAFE-Public-Catalogue-Health.json").write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"classification": report["classification"],
                      "checks": list(report["checks"]), "errors": report["errors"]}))
    return 0 if report["classification"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
