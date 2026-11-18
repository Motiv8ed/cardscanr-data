#!/usr/bin/env python3
"""Inspect fixed historical public R2 catalogue releases without downloading pack DBs."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.parse import urlsplit

BASE = "https://assets.cardscanr.com/v2/catalog/pokemon/packs"
RELEASES = {
    "august_production": BASE + "/production-packs-20260803/catalogue.packs.manifest.json",
    "august_canary": BASE + "/canary4-packs-20260803/catalogue.packs.manifest.json",
}
ALLOWED_HOSTS = {"assets.cardscanr.com",
                 "pub-258b8de1c4964f538a8cb08022761430.r2.dev"}
MAX_SIZE = 2 * 1024 * 1024
MAX_HEADS = 12


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise ValueError("redirect_denied")


def fetch(url: str) -> tuple[Any, bytes]:
    if url not in RELEASES.values():
        raise ValueError("unapproved_manifest_url")
    with build_opener(NoRedirect()).open(
        Request(url, headers={"User-Agent": "CardScanR-Immutable-Pack-ReadOnlyAudit/1.0",
                              "Accept": "application/json"}),
        timeout=15
    ) as resp:
        if resp.status != 200:
            raise ValueError("not_http_200")
        body = resp.read(MAX_SIZE + 1)
        if len(body) > MAX_SIZE:
            raise ValueError("oversized_manifest")
    return json.loads(body), body


def head(url: str) -> dict:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS or parsed.username or parsed.password or parsed.port not in (None, 443):
        return {"state": "rejected_url_outside_allowlist"}
    try:
        with build_opener(NoRedirect()).open(
            Request(url, method="HEAD", headers={"User-Agent": "CardScanR-Immutable-Pack-ReadOnlyAudit/1.0"}),
            timeout=12
        ) as resp:
            return {"state": "http_response", "status": resp.status,
                    "content_length": resp.headers.get("Content-Length")}
    except (ValueError, OSError, HTTPError, URLError, TimeoutError) as exc:
        return {"state": "failed", "error_type": type(exc).__name__,
                "http_status": getattr(exc, "code", None)}


def inspect(label: str, manifest: Any, raw: bytes,
            check_head: Callable[[str], dict] = head) -> dict:
    if not isinstance(manifest, dict) or not isinstance(manifest.get("packs"), list):
        raise ValueError("invalid_pack_manifest")
    all_packs = manifest["packs"]
    language_packs = []
    heads = 0
    for pack in all_packs:
        if not isinstance(pack, dict) or pack.get("kind") != "language":
            continue
        languages = pack.get("languages")
        lang = languages[0] if isinstance(languages, list) and len(languages) == 1 else None
        rec = {"pack_id": pack.get("packId"), "language": lang,
               "records": pack.get("recordCount"),
               "raw_bytes": pack.get("rawSqliteBytes")}
        url = pack.get("compressedDatabaseUrl")
        if isinstance(url, str) and heads < MAX_HEADS:
            heads += 1
            rec["compressed_pack_head"] = check_head(url)
        language_packs.append(rec)
    return {
        "name": label,
        "release_id": manifest.get("catalogueReleaseId"),
        "generated_at": manifest.get("generatedAt"),
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "manifest_bytes": len(raw),
        "total_packs": len(all_packs),
        "language_packs": language_packs,
        "pack_head_requests": heads,
        "immutable_release_only": True,
        "active_pointer_untouched": True,
    }


def audit(get: Callable[[str], tuple[Any, bytes]] = fetch,
          check_head: Callable[[str], dict] = head) -> dict:
    releases, failures = {}, []
    for label, url in RELEASES.items():
        try:
            payload, raw = get(url)
            releases[label] = inspect(label, payload, raw, check_head=check_head)
        except (ValueError, OSError, TimeoutError, HTTPError, URLError, json.JSONDecodeError) as exc:
            failures.append({"name": label, "error_type": type(exc).__name__,
                             "http_status": getattr(exc, "code", None)})
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "classification": "PASS" if len(releases) == len(RELEASES) else "PARTIAL",
        "releases": releases, "failures": failures,
        "databases_downloaded": 0, "artwork_files_downloaded": 0,
        "production_writes": 0, "active_pointer_modified": False,
        "release_eligibility_verified": False,
        "note": "Presence of archived packs and HEAD 200 are not rights/quality/compatibility approval.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.is_symlink() or (args.output.exists() and
                                   (not args.output.is_dir() or any(args.output.iterdir()))):
        parser.error("output must be a new empty folder")
    report = audit()
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "SAFE-Archived-Language-Pack-Inventory.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"classification": report["classification"],
                      "release_ids": [v["release_id"] for v in report["releases"].values()],
                      "failures": report["failures"]}))
    return 0 if report["classification"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
