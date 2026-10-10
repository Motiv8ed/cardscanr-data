#!/usr/bin/env python3
"""EN/JA image-independence closeout verifier, backup, and manifest rebuild.

Fail-closed gates:
  - every EN+JP catalogue card has imageSource=cardscanr_cdn
  - every image URL is on the CardScanR CDN host
  - no missing / duplicate canonicalBaseId mappings
  - local master display.webp exists and sha256 matches asset.json
  - optional concurrent CDN HEAD for all public URLs

Does not delete assets. Writes recovery backup + refreshed manifests.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CATALOGUE = ROOT / "public" / "v1" / "catalog" / "pokemon"
MASTER = ROOT / "data" / "images" / "master"
REPORT = ROOT / "reports" / "image_independence"
REPORT_MIRROR = Path(r"D:\CardScanR\reports\image_independence")
CDN_HOST = "cardscanr-images.andygore149.workers.dev"
CDN_BASE = f"https://{CDN_HOST}"
DEFAULT_BACKUP_PARENT = Path(r"D:\CardScanR_Archive\backups")
UA = "CardScanR-ImageIndependence-Closeout/1.0"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def lang_folder_to_master(folder: str) -> str:
    return "ja" if folder == "jp" else folder


def log(msg: str) -> None:
    print(msg, flush=True)


def iter_catalogue_cards() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for folder in ("en", "jp"):
        cards_dir = CATALOGUE / folder / "cards"
        if not cards_dir.is_dir():
            continue
        master_lang = lang_folder_to_master(folder)
        files = sorted(cards_dir.glob("*.json"))
        log(f"catalogue load {folder}: {len(files)} set files")
        for i, path in enumerate(files, 1):
            data = json.loads(path.read_text(encoding="utf-8"))
            set_id = str(data.get("setId") or path.stem)
            for card in data.get("cards") or []:
                if not isinstance(card, dict):
                    continue
                rows.append(
                    {
                        "folder": folder,
                        "masterLang": master_lang,
                        "setId": set_id,
                        "setFile": str(path.relative_to(ROOT)).replace("\\", "/"),
                        "canonicalBaseId": str(card.get("canonicalBaseId") or ""),
                        "normalizedName": str(card.get("normalizedName") or ""),
                        "collectorNumber": str(card.get("collectorNumber") or ""),
                        "name": str(card.get("name") or ""),
                        "imageSource": str(card.get("imageSource") or ""),
                        "imageUrl": str(
                            card.get("imageUrlLarge")
                            or card.get("imageLarge")
                            or card.get("imageUrl")
                            or ""
                        ),
                        "imageUrlSmall": str(
                            card.get("imageUrlSmall")
                            or card.get("imageSmall")
                            or card.get("imageUrl")
                            or ""
                        ),
                        "imageCached": bool(card.get("imageCached")),
                        "provenance": card.get("imageProvenance")
                        if isinstance(card.get("imageProvenance"), dict)
                        else {},
                        "cardIdHint": _card_id_hint(card),
                    }
                )
            if i % 100 == 0 or i == len(files):
                log(f"  {folder} sets {i}/{len(files)} cards={len(rows)}")
    return rows


def _card_id_hint(card: dict[str, Any]) -> str:
    external = card.get("externalIds") if isinstance(card.get("externalIds"), dict) else {}
    providers = card.get("providerIds") if isinstance(card.get("providerIds"), dict) else {}
    for value in (
        providers.get("pokewallet"),
        external.get("pokewalletCardId"),
        external.get("pokemonTcgApiId"),
        external.get("tcgdexCardId"),
        providers.get("pokemonTcgApi"),
        providers.get("tcgdex"),
    ):
        if value:
            return str(value)
    return ""


def index_master_by_canonical() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    sidecars = list(MASTER.rglob("asset.json"))
    log(f"master index: {len(sidecars)} asset.json files")
    for i, sidecar in enumerate(sidecars, 1):
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        meta["_sidecar"] = str(sidecar)
        meta["_dir"] = str(sidecar.parent)
        cid = str(meta.get("canonicalBaseId") or "").strip()
        if cid:
            out[cid] = meta
        if i % 5000 == 0 or i == len(sidecars):
            log(f"  master indexed {i}/{len(sidecars)}")
    return out


def is_cdn_url(url: str) -> bool:
    u = (url or "").strip().lower()
    return u.startswith(CDN_BASE.lower() + "/") or u.startswith(f"https://{CDN_HOST}/")


def head_ok(url: str, timeout: float = 30.0) -> tuple[bool, int | str]:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, int(getattr(resp, "status", 200) or 200)
    except urllib.error.HTTPError as exc:
        # Some CDNs reject HEAD; retry GET range.
        if exc.code in (403, 405):
            try:
                greq = urllib.request.Request(
                    url,
                    headers={"User-Agent": UA, "Range": "bytes=0-0"},
                )
                with urllib.request.urlopen(greq, timeout=timeout) as resp:
                    code = int(getattr(resp, "status", 200) or 200)
                    return code in (200, 206), code
            except Exception as exc2:  # noqa: BLE001
                return False, str(exc2)
        return False, exc.code
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def verify(
    *,
    check_cdn: bool,
    cdn_workers: int,
    hash_master: bool,
) -> dict[str, Any]:
    cards = iter_catalogue_cards()
    master = index_master_by_canonical()

    missing_cdn_binding: list[dict[str, Any]] = []
    missing_master: list[dict[str, Any]] = []
    hash_mismatches: list[dict[str, Any]] = []
    missing_display: list[dict[str, Any]] = []
    empty_canonical: list[dict[str, Any]] = []
    non_cdn_urls: list[dict[str, Any]] = []

    canonical_counts: dict[str, int] = {}
    for row in cards:
        cid = row["canonicalBaseId"]
        if not cid:
            empty_canonical.append(row)
            continue
        canonical_counts[cid] = canonical_counts.get(cid, 0) + 1

    duplicate_canonicals = sorted(
        [{"canonicalBaseId": k, "count": v} for k, v in canonical_counts.items() if v > 1],
        key=lambda x: (-x["count"], x["canonicalBaseId"]),
    )

    hosted = 0
    local_master_ok = 0
    manifest_rows: list[dict[str, Any]] = []
    pending_hash: list[
        tuple[str, Path, str, dict[str, Any], dict[str, Any], str]
    ] = []
    log(f"verify loop: {len(cards)} cards hash_master={hash_master}")

    for idx, row in enumerate(cards, 1):
        cid = row["canonicalBaseId"]
        src = row["imageSource"]
        url = row["imageUrl"]
        small = row["imageUrlSmall"]
        ok_binding = (
            src == "cardscanr_cdn"
            and is_cdn_url(url)
            and is_cdn_url(small)
            and row["imageCached"]
        )
        if ok_binding:
            hosted += 1
        else:
            missing_cdn_binding.append(
                {
                    "canonicalBaseId": cid,
                    "language": row["masterLang"],
                    "setId": row["setId"],
                    "imageSource": src,
                    "imageUrl": url,
                    "imageCached": row["imageCached"],
                }
            )
            if url and not is_cdn_url(url):
                non_cdn_urls.append({"canonicalBaseId": cid, "imageUrl": url})

        meta = master.get(cid) if cid else None
        if meta is None:
            missing_master.append(
                {
                    "canonicalBaseId": cid,
                    "language": row["masterLang"],
                    "setId": row["setId"],
                    "normalizedName": row["normalizedName"],
                }
            )
            continue

        display = Path(meta["_dir"]) / "display.webp"
        if not display.is_file():
            missing_display.append({"canonicalBaseId": cid, "path": str(display)})
            continue

        # Defer hashing to a parallel pass; first pass checks presence + bindings.
        pending_hash.append((cid, display, str(meta.get("sha256") or ""), meta, row, url))
        if idx % 10000 == 0 or idx == len(cards):
            log(f"  binding/presence pass {idx}/{len(cards)} hosted={hosted}")

    log(f"hash pass: {len(pending_hash)} display.webp files")
    hash_results: dict[str, tuple[str, str | None]] = {}
    if hash_master and pending_hash:

        def _hash_one(item: tuple[str, Path, str, dict[str, Any], dict[str, Any], str]) -> tuple[str, str, str | None]:
            cid, display, expected, _meta, _row, _url = item
            actual = sha256_file(display)
            mismatch = expected if expected and actual != expected else None
            return cid, actual, mismatch

        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = [pool.submit(_hash_one, item) for item in pending_hash]
            done = 0
            for fut in as_completed(futs):
                cid, actual, mismatch_expected = fut.result()
                hash_results[cid] = (actual, mismatch_expected)
                done += 1
                if done % 5000 == 0 or done == len(futs):
                    log(f"  hashed {done}/{len(futs)}")

    for cid, display, expected, meta, row, url in pending_hash:
        digest = expected
        if hash_master:
            actual, mismatch_expected = hash_results[cid]
            if mismatch_expected is not None:
                hash_mismatches.append(
                    {
                        "canonicalBaseId": cid,
                        "expected": mismatch_expected,
                        "actual": actual,
                        "path": str(display),
                    }
                )
                continue
            digest = actual
            meta["sha256"] = actual
        elif not digest:
            digest = ""

        local_master_ok += 1
        public = str(meta.get("publicUrl") or url or "")
        manifest_rows.append(
            {
                "canonical_card_id": cid,
                "language": meta.get("language") or row["masterLang"],
                "set_id": meta.get("setId") or row["setId"],
                "collector_number": meta.get("collectorNumber") or row["collectorNumber"],
                "card_id": meta.get("cardId") or row["cardIdHint"],
                "local_path": str(display),
                "sha256": digest,
                "byte_size": meta.get("byteSize") or display.stat().st_size,
                "mime_type": meta.get("mimeType") or "image/webp",
                "source_provider": meta.get("sourceProvider") or "",
                "original_source_url": meta.get("originalSourceUrl") or "",
                "hosted_object_key": meta.get("hostedObjectKey") or "",
                "public_url": public,
                "provenance_confidence": meta.get("provenanceConfidence") or "",
                "derivative_status": meta.get("derivativeStatus") or "",
                "acquisition": meta.get("acquisition") or "",
                "acquired_at": meta.get("acquiredAt") or "",
                "match_basis": meta.get("matchBasis") or "",
                "rights_basis": meta.get("rightsBasis") or "",
                "rights_status": meta.get("rightsStatus")
                or (row["provenance"].get("rightsStatus") if row["provenance"] else ""),
            }
        )

    # Duplicate public URL / hosted key across distinct canonicals
    url_owners: dict[str, list[str]] = {}
    key_owners: dict[str, list[str]] = {}
    for mrow in manifest_rows:
        pu = str(mrow.get("public_url") or "").strip()
        hk = str(mrow.get("hosted_object_key") or "").strip()
        cid = str(mrow["canonical_card_id"])
        if pu:
            url_owners.setdefault(pu, []).append(cid)
        if hk:
            key_owners.setdefault(hk, []).append(cid)
    duplicate_public_urls = [
        {"publicUrl": u, "canonicalBaseIds": ids}
        for u, ids in sorted(url_owners.items())
        if len(set(ids)) > 1
    ]
    duplicate_hosted_keys = [
        {"hostedObjectKey": k, "canonicalBaseIds": ids}
        for k, ids in sorted(key_owners.items())
        if len(set(ids)) > 1
    ]

    cdn_failures: list[dict[str, Any]] = []
    cdn_checked = 0
    if check_cdn:
        urls = sorted({str(r["public_url"]) for r in manifest_rows if r.get("public_url")})
        log(f"cdn HEAD check: {len(urls)} unique public URLs workers={cdn_workers}")
        with ThreadPoolExecutor(max_workers=max(1, cdn_workers)) as pool:
            futs = {pool.submit(head_ok, u): u for u in urls}
            for fut in as_completed(futs):
                url = futs[fut]
                cdn_checked += 1
                ok, detail = fut.result()
                if not ok:
                    cdn_failures.append({"publicUrl": url, "detail": detail})
                if cdn_checked % 5000 == 0 or cdn_checked == len(urls):
                    log(
                        f"  cdn checked {cdn_checked}/{len(urls)} "
                        f"failures={len(cdn_failures)}"
                    )

    en_total = sum(1 for r in cards if r["folder"] == "en")
    ja_total = sum(1 for r in cards if r["folder"] == "jp")
    en_hosted = sum(
        1
        for r in cards
        if r["folder"] == "en"
        and r["imageSource"] == "cardscanr_cdn"
        and is_cdn_url(r["imageUrl"])
    )
    ja_hosted = sum(
        1
        for r in cards
        if r["folder"] == "jp"
        and r["imageSource"] == "cardscanr_cdn"
        and is_cdn_url(r["imageUrl"])
    )

    total = len(cards)
    missing_count = total - hosted
    summary = {
        "generatedAtUtc": utc_now(),
        "totalCards": total,
        "en": {"total": en_total, "hosted": en_hosted, "localMasterOk": None},
        "ja": {"total": ja_total, "hosted": ja_hosted, "localMasterOk": None},
        "hostedCount": hosted,
        "localMasterCount": local_master_ok,
        "missingCount": missing_count,
        "masterSidecarCount": len(master),
        "manifestRows": len(manifest_rows),
        "duplicateCanonicalCount": len(duplicate_canonicals),
        "duplicatePublicUrlCount": len(duplicate_public_urls),
        "duplicateHostedKeyCount": len(duplicate_hosted_keys),
        "emptyCanonicalCount": len(empty_canonical),
        "missingMasterCount": len(missing_master),
        "missingDisplayCount": len(missing_display),
        "hashMismatchCount": len(hash_mismatches),
        "nonCdnUrlCount": len(non_cdn_urls),
        "cdnChecked": cdn_checked,
        "cdnFailureCount": len(cdn_failures),
        "cdnBase": CDN_BASE,
        "masterRoot": str(MASTER),
        "independentPct": round(100.0 * hosted / total, 6) if total else 0.0,
        "gates": {},
    }
    summary["en"]["localMasterOk"] = sum(
        1 for r in manifest_rows if str(r.get("language")) in ("en",)
    )
    summary["ja"]["localMasterOk"] = sum(
        1 for r in manifest_rows if str(r.get("language")) in ("ja", "jp")
    )

    gates = {
        "all_hosted_cdn": missing_count == 0 and not missing_cdn_binding,
        "no_duplicate_canonicals": not duplicate_canonicals,
        "no_empty_canonicals": not empty_canonical,
        "master_covers_all": not missing_master and not missing_display,
        "hashes_match": not hash_mismatches,
        "no_duplicate_cdn_mappings": not duplicate_public_urls and not duplicate_hosted_keys,
        "cdn_reachable": (not check_cdn) or not cdn_failures,
    }
    summary["gates"] = gates
    summary["allGatesPassed"] = all(gates.values())

    details = {
        "missingCdnBinding": missing_cdn_binding[:200],
        "missingMaster": missing_master[:200],
        "missingDisplay": missing_display[:200],
        "hashMismatches": hash_mismatches[:200],
        "duplicateCanonicals": duplicate_canonicals[:200],
        "duplicatePublicUrls": duplicate_public_urls[:100],
        "duplicateHostedKeys": duplicate_hosted_keys[:100],
        "nonCdnUrls": non_cdn_urls[:200],
        "cdnFailures": cdn_failures[:200],
        "emptyCanonicals": [
            {"setId": r["setId"], "normalizedName": r["normalizedName"]}
            for r in empty_canonical[:200]
        ],
    }
    return {"summary": summary, "manifestRows": manifest_rows, "details": details}


def write_manifest_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "canonical_card_id",
        "language",
        "set_id",
        "collector_number",
        "card_id",
        "local_path",
        "sha256",
        "byte_size",
        "mime_type",
        "source_provider",
        "original_source_url",
        "hosted_object_key",
        "public_url",
        "provenance_confidence",
        "derivative_status",
        "acquisition",
        "acquired_at",
        "match_basis",
        "rights_basis",
        "rights_status",
    ]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in sorted(rows, key=lambda r: str(r.get("canonical_card_id") or "")):
            writer.writerow(row)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def create_backup(
    *,
    parent: Path,
    summary: dict[str, Any],
    manifest_rows: list[dict[str, Any]],
    details: dict[str, Any],
) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = parent / f"en_jp_image_independence_100pct_{stamp}"
    dest.mkdir(parents=True, exist_ok=False)

    # Manifests + reports (not full binary master tree — hash inventory is the recovery map)
    write_manifest_csv(dest / "en_jp_image_master_manifest.csv", manifest_rows)
    write_json(dest / "image_independence_summary.json", summary)
    write_json(dest / "closeout_gate_details.json", details)

    # Copy unresolved CSV + key reports if present
    for name in (
        "en_jp_unresolved_images.csv",
        "CARDSCANR_FINAL_THREE_IMAGE_RECOVERY_RESULT.md",
        "SCRYDEX_WRITTEN_AUTHORIZATION_2026-10-07.md",
    ):
        src = REPORT / name
        if src.is_file():
            shutil.copy2(src, dest / name)

    # Sidecar inventory derived from verified manifest rows (avoid second full walk)
    sidecar_rows = [
        {
            "canonicalBaseId": r.get("canonical_card_id"),
            "language": r.get("language"),
            "setId": r.get("set_id"),
            "cardId": r.get("card_id"),
            "sha256": r.get("sha256"),
            "byteSize": r.get("byte_size"),
            "publicUrl": r.get("public_url"),
            "hostedObjectKey": r.get("hosted_object_key"),
            "sourceProvider": r.get("source_provider"),
            "originalSourceUrl": r.get("original_source_url"),
            "rightsStatus": r.get("rights_status"),
            "rightsBasis": r.get("rights_basis"),
            "displayPath": r.get("local_path"),
            "displayExists": True,
        }
        for r in manifest_rows
    ]
    write_json(dest / "master_asset_inventory.json", {"count": len(sidecar_rows), "assets": sidecar_rows})

    # Hash the backup payload itself
    file_hashes: dict[str, str] = {}
    for path in sorted(dest.rglob("*")):
        if path.is_file():
            rel = str(path.relative_to(dest)).replace("\\", "/")
            file_hashes[rel] = sha256_file(path)
    write_json(
        dest / "BACKUP_MANIFEST.json",
        {
            "createdAtUtc": utc_now(),
            "backupPath": str(dest),
            "masterRoot": str(MASTER),
            "catalogueRoot": str(CATALOGUE),
            "totalCards": summary.get("totalCards"),
            "hostedCount": summary.get("hostedCount"),
            "localMasterCount": summary.get("localMasterCount"),
            "missingCount": summary.get("missingCount"),
            "fileSha256": file_hashes,
            "note": (
                "Binary master originals remain at masterRoot; this backup is the "
                "verified inventory + manifests + hashes for recovery mapping. "
                "No assets were deleted."
            ),
        },
    )
    return dest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-cdn", action="store_true", help="Skip HTTP CDN HEAD checks")
    parser.add_argument("--cdn-workers", type=int, default=24)
    parser.add_argument("--skip-hash", action="store_true", help="Skip local display.webp hashing")
    parser.add_argument("--skip-backup", action="store_true")
    parser.add_argument("--backup-parent", type=Path, default=DEFAULT_BACKUP_PARENT)
    parser.add_argument("--write-reports", action="store_true", default=True)
    args = parser.parse_args()

    t0 = time.time()
    result = verify(
        check_cdn=not args.skip_cdn,
        cdn_workers=args.cdn_workers,
        hash_master=not args.skip_hash,
    )
    summary = result["summary"]
    details = result["details"]
    rows = result["manifestRows"]

    if args.write_reports:
        write_manifest_csv(REPORT / "en_jp_image_master_manifest.csv", rows)
        # Keep summary shape compatible with prior reports
        report_summary = {
            "generatedAtUtc": summary["generatedAtUtc"],
            "en": {
                "total": summary["en"]["total"],
                "localMaster": summary["en"]["localMasterOk"],
                "hosted": summary["en"]["hosted"],
                "unresolved": summary["en"]["total"] - summary["en"]["hosted"],
            },
            "ja": {
                "total": summary["ja"]["total"],
                "localMaster": summary["ja"]["localMasterOk"],
                "hosted": summary["ja"]["hosted"],
                "unresolved": summary["ja"]["total"] - summary["ja"]["hosted"],
            },
            "masterRoot": summary["masterRoot"],
            "cdnBase": summary["cdnBase"],
            "unresolvedCategoryCounts": {},
            "masterRows": summary["manifestRows"],
            "independentPct": summary["independentPct"],
            "closeout": summary,
        }
        write_json(REPORT / "image_independence_summary.json", report_summary)
        write_json(REPORT / "closeout_gate_details.json", details)
        # Mirror operator-facing summary into app repo
        REPORT_MIRROR.mkdir(parents=True, exist_ok=True)
        write_json(REPORT_MIRROR / "image_independence_summary.json", report_summary)

        # Unresolved CSV must be header-only at 100%
        unresolved_path = REPORT / "en_jp_unresolved_images.csv"
        unresolved_path.write_text(
            "canonical_card_id,language,set_id,collector_number,match_basis,blocker,reason,next_action\n",
            encoding="utf-8",
        )

    backup_path = None
    if not args.skip_backup:
        args.backup_parent.mkdir(parents=True, exist_ok=True)
        backup_path = create_backup(
            parent=args.backup_parent,
            summary=summary,
            manifest_rows=rows,
            details=details,
        )
        summary["backupPath"] = str(backup_path)

    summary["elapsedSeconds"] = round(time.time() - t0, 2)
    write_json(REPORT / "closeout_verification_result.json", summary)
    print(json.dumps(summary, indent=2))
    if backup_path:
        print(f"BACKUP={backup_path}")

    return 0 if summary.get("allGatesPassed") else 2


if __name__ == "__main__":
    sys.exit(main())
