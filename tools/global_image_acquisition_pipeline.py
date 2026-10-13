#!/usr/bin/env python3
"""Worldwide Pokémon TCG image acquisition into isolated staging storage.

Reuses CardScanR independence conventions (asset.json provenance, WebP display,
fail-closed identity) without writing production master/CDN unless explicitly
approved later.

Rights posture (this tool):
- assets.tcgdex.net / images.pokemontcg.io: public hosts used by the EN/JA
  independence programme; staged here with rightsStatus documenting
  production-mirror confirmation still required for non-EN/JA formal ledger.
- images.scrydex.com: written authorization 2026-10-07 (CardScanR CDN use).
- Official regional catalogues / uncertain hosts: discovery only (no download).

Never overwrites an existing good staging original (sha256 match skip; mismatch
quarantine). Never mutates data/images/master.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
# Shared live data root (worktree code + shared binaries on D: data volume).
DATA_ROOT = Path(r"D:\CardScanR_Data\cardscanr-data")
DIRECT_IMAGES = DATA_ROOT / "data" / "global" / "catalogue" / "direct_images.jsonl"
MASTER = DATA_ROOT / "data" / "images" / "master"
STAGING_DEFAULT = Path(r"D:\CardScanR_Archive\image_staging\global_acq_20261013")
REPORT = ROOT / "reports" / "global_image_acquisition"
QUARANTINE = "quarantine"
UA = "CardScanR-GlobalImageAcquisition/1.0 (+https://cardscanr.com; respectful bulk; contact ops)"
LOCK = threading.Lock()
CANDIDATES_PATH: Path = DIRECT_IMAGES

# Operational hosts already used for EN/JA independence local master + CDN.
PERMITTED_DOWNLOAD_HOSTS = {
    "assets.tcgdex.net": {
        "provider": "tcgdex",
        "rightsStatus": "public_host_operational_staging",
        "productionMirrorStatus": "pending_formal_confirmation_non_en_ja",
        "licenceNote": "TCGdex metadata MIT; artwork redistribution not expressly licensed; EN/JA independence already mirrored for CardScanR product use.",
    },
    "images.pokemontcg.io": {
        "provider": "pokemon_tcg_api",
        "rightsStatus": "public_host_operational_staging",
        "productionMirrorStatus": "pending_formal_confirmation_non_en_ja",
        "licenceNote": "API terms allow use; complete artwork archive grant not expressly documented.",
    },
    "images.scrydex.com": {
        "provider": "scrydex",
        "rightsStatus": "scrydex_written_authorization_2026-10-07",
        "productionMirrorStatus": "approved_written_authorization_cardscanr_cdn",
        "licenceNote": "Written Scrydex Support authorization for CardScanR CDN/cache/rehost (own product use; no Pokémon IP sublicense).",
    },
}

DISCOVERY_ONLY_HOSTS = {
    "asia.pokemon-card.com": "official_asia_terms_prohibit_external_reuse",
    "www.pokemon.com": "official_site_no_bulk_commercial_grant",
    "api.pokewallet.io": "authenticated_path_bulk_rehost_unclear",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe(seg: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", (seg or "").strip())[:180] or "unknown"


def host_of(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ""


def magic_ok(data: bytes) -> bool:
    if len(data) < 12:
        return False
    if data[:3] == b"\xff\xd8\xff":
        return True
    if data[:4] == b"\x89PNG":
        return True
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    return False


def http_get(url: str, *, timeout: int = 60) -> bytes:
    last: Exception | None = None
    for i in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "image/*,*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
            if not data:
                raise RuntimeError("empty_body")
            return data
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(min(2**i, 10))
    assert last is not None
    raise last


def to_webp(data: bytes) -> bytes:
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return data
    im = Image.open(BytesIO(data))
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGBA")
    buf = BytesIO()
    im.save(buf, format="WEBP", quality=82, method=4)
    out = buf.getvalue()
    if not magic_ok(out):
        raise RuntimeError("webp_failed")
    return out


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def iter_direct_images(path: Path | None = None) -> Any:
    src = path or CANDIDATES_PATH
    with src.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def staging_dir(staging_root: Path, row: dict[str, Any]) -> Path:
    lang = safe(str(row.get("language") or "unknown"))
    set_id = safe(str(row.get("providerSetId") or row.get("canonicalSetId") or "set"))
    card_id = safe(str(row.get("providerCardId") or row.get("canonicalPrintingId") or "card"))
    return staging_root / lang / set_id / card_id


def choose_url(row: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None, str | None]:
    for key in ("normalizedDisplayUrl", "normalizedThumbnailUrl", "originalDocumentedImageUrl"):
        url = row.get(key)
        if not isinstance(url, str) or not url.startswith("http"):
            continue
        host = host_of(url)
        if host in DISCOVERY_ONLY_HOSTS:
            return None, None, f"discovery_only:{DISCOVERY_ONLY_HOSTS[host]}"
        if host in PERMITTED_DOWNLOAD_HOSTS:
            return url, PERMITTED_DOWNLOAD_HOSTS[host], None
        return None, None, f"unknown_host:{host or 'empty'}"
    return None, None, "no_image_url"


def load_progress(path: Path) -> dict[str, Any]:
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"completed": {}, "failed": {}, "quarantined": {}}


def save_progress(path: Path, progress: dict[str, Any]) -> None:
    write_json(path, progress)


def census(*, staging_root: Path) -> dict[str, Any]:
    by_lang_candidates: Counter[str] = Counter()
    by_lang_host: dict[str, Counter[str]] = defaultdict(Counter)
    by_lang_staging_ok: Counter[str] = Counter()
    by_lang_staging_quarantine: Counter[str] = Counter()
    master_langs = sorted(p.name for p in MASTER.iterdir() if p.is_dir()) if MASTER.exists() else []

    for row in iter_direct_images():
        lang = str(row.get("language") or "?")
        by_lang_candidates[lang] += 1
        url, policy, reason = choose_url(row)
        host = host_of(url) if url else (reason or "none")
        by_lang_host[lang][host] += 1

    if staging_root.exists():
        for sidecar in staging_root.rglob("asset.json"):
            try:
                rel = sidecar.relative_to(staging_root)
            except Exception:
                continue
            if not rel.parts:
                continue
            if rel.parts[0] == QUARANTINE:
                lang = rel.parts[1] if len(rel.parts) > 1 else "unknown"
                by_lang_staging_quarantine[lang] += 1
                continue
            lang = rel.parts[0]
            if any(sidecar.parent.glob("display.*")):
                by_lang_staging_ok[lang] += 1

    # EN/JA verified master from independence summary when present.
    indep_path = DATA_ROOT / "reports" / "image_independence" / "image_independence_summary.json"
    independence = json.loads(indep_path.read_text(encoding="utf-8")) if indep_path.is_file() else {}

    payload = {
        "schemaVersion": "1.0.0",
        "generatedAtUtc": utc_now(),
        "masterLanguagesPresent": master_langs,
        "enJaIndependence": independence.get("en"),
        "jaIndependence": independence.get("ja"),
        "independenceMasterRows": independence.get("masterRows"),
        "independencePct": independence.get("independentPct"),
        "globalDirectImageCandidatesByLanguage": dict(sorted(by_lang_candidates.items())),
        "globalDirectImageCandidateTotal": int(sum(by_lang_candidates.values())),
        "candidateHostsByLanguage": {k: dict(v) for k, v in sorted(by_lang_host.items())},
        "stagingVerifiedByLanguage": dict(sorted(by_lang_staging_ok.items())),
        "stagingQuarantineByLanguage": dict(sorted(by_lang_staging_quarantine.items())),
        "stagingRoot": str(staging_root),
        "rightsNote": (
            "Non-EN/JA candidates are overwhelmingly assets.tcgdex.net with "
            "formal productionMirrorStatus pending; this census counts verified "
            "local files only (master for EN/JA, staging for this run)."
        ),
    }
    REPORT.mkdir(parents=True, exist_ok=True)
    write_json(REPORT / "coverage_census.json", payload)
    return payload


def acquire_one(row: dict[str, Any], staging_root: Path) -> dict[str, Any]:
    cid = str(row.get("canonicalPrintingId") or "")
    url, policy, reason = choose_url(row)
    if not url or not policy:
        return {"status": "skip", "id": cid, "reason": reason}

    dest = staging_dir(staging_root, row)
    asset_path = dest / "asset.json"
    display_path = dest / "display.webp"

    if asset_path.is_file() and display_path.is_file():
        try:
            existing = json.loads(asset_path.read_text(encoding="utf-8"))
            if existing.get("originalSourceUrl") == url and existing.get("sha256"):
                return {"status": "already", "id": cid, "sha256": existing.get("sha256")}
        except Exception:
            pass

    try:
        raw = http_get(url)
        if not magic_ok(raw):
            qdir = staging_root / QUARANTINE / safe(str(row.get("language"))) / safe(cid)
            write_atomic(qdir / "payload.bin", raw)
            write_json(
                qdir / "quarantine.json",
                {"id": cid, "url": url, "reason": "invalid_magic", "at": utc_now()},
            )
            return {"status": "quarantine", "id": cid, "reason": "invalid_magic"}

        ext = ".png" if raw[:4] == b"\x89PNG" else (".webp" if raw[8:12] == b"WEBP" else ".jpg")
        original_path = dest / f"original{ext}"
        if original_path.is_file():
            prev = sha256(original_path.read_bytes())
            cur = sha256(raw)
            if prev != cur:
                qdir = staging_root / QUARANTINE / safe(str(row.get("language"))) / safe(cid)
                write_atomic(qdir / f"conflict_new{ext}", raw)
                write_json(
                    qdir / "quarantine.json",
                    {
                        "id": cid,
                        "url": url,
                        "reason": "sha_mismatch_existing_original",
                        "existingSha256": prev,
                        "newSha256": cur,
                        "at": utc_now(),
                    },
                )
                return {"status": "quarantine", "id": cid, "reason": "sha_mismatch"}
        else:
            write_atomic(original_path, raw)

        webp = to_webp(raw)
        write_atomic(display_path, webp)
        digest = sha256(webp)
        # Thumbnail
        im = Image.open(BytesIO(webp))
        im.thumbnail((256, 256))
        tbuf = BytesIO()
        im.save(tbuf, format="WEBP", quality=80, method=4)
        write_atomic(dest / "thumb.webp", tbuf.getvalue())

        meta = {
            "schemaVersion": "1.0.0",
            "canonicalPrintingId": cid,
            "canonicalBaseId": cid,
            "cardId": row.get("providerCardId"),
            "language": row.get("language"),
            "region": row.get("region"),
            "setId": row.get("providerSetId") or row.get("canonicalSetId"),
            "collectorNumber": row.get("printedCollectorNumber")
            or row.get("normalizedCollectorNumber"),
            "sha256": digest,
            "byteSize": len(webp),
            "mimeType": "image/webp",
            "sourceProvider": policy["provider"],
            "originalSourceUrl": url,
            "rightsStatus": policy["rightsStatus"],
            "productionMirrorStatus": policy["productionMirrorStatus"],
            "licenceNote": policy["licenceNote"],
            "matchBasis": "direct_images_catalogue_identity",
            "provenanceConfidence": "CONFIRMED",
            "acquisition": "global_staging_permitted_public_acquire",
            "acquiredAt": utc_now(),
            "storageClass": "isolated_staging_not_production_master",
            "hostedObjectKey": None,
            "publicUrl": None,
        }
        write_atomic(asset_path, json.dumps(meta, indent=2, ensure_ascii=False).encode("utf-8"))
        return {
            "status": "ok",
            "id": cid,
            "language": row.get("language"),
            "bytes": len(webp),
            "sha256": digest,
            "provider": policy["provider"],
        }
    except urllib.error.HTTPError as exc:
        return {"status": "fail", "id": cid, "reason": f"http_{exc.code}", "url": url}
    except Exception as exc:  # noqa: BLE001
        return {"status": "fail", "id": cid, "reason": str(exc)[:200], "url": url}


def acquire(
    *,
    staging_root: Path,
    languages: set[str] | None,
    skip_en_ja: bool,
    limit: int | None,
    concurrency: int,
    resume: bool,
) -> dict[str, Any]:
    staging_root.mkdir(parents=True, exist_ok=True)
    progress_path = staging_root / "progress.json"
    progress = load_progress(progress_path) if resume else {"completed": {}, "failed": {}, "quarantined": {}}
    completed = progress.setdefault("completed", {})
    failed = progress.setdefault("failed", {})
    quarantined = progress.setdefault("quarantined", {})

    work: list[dict[str, Any]] = []
    for row in iter_direct_images():
        lang = str(row.get("language") or "")
        if skip_en_ja and lang in {"en", "ja"}:
            continue
        if languages and lang not in languages:
            continue
        cid = str(row.get("canonicalPrintingId") or "")
        if not cid:
            continue
        if resume and cid in completed:
            continue
        url, policy, reason = choose_url(row)
        if not url or not policy:
            continue
        work.append(row)
        if limit is not None and len(work) >= limit:
            break

    stats: Counter[str] = Counter()
    by_lang_ok: Counter[str] = Counter()
    started = utc_now()

    def _run(row: dict[str, Any]) -> dict[str, Any]:
        result = acquire_one(row, staging_root)
        with LOCK:
            stats[result["status"]] += 1
            cid = result.get("id") or ""
            if result["status"] in {"ok", "already"}:
                completed[cid] = {
                    "at": utc_now(),
                    "sha256": result.get("sha256"),
                    "language": result.get("language") or row.get("language"),
                    "status": result["status"],
                }
                by_lang_ok[str(row.get("language") or "?")] += 1
                failed.pop(cid, None)
            elif result["status"] == "quarantine":
                quarantined[cid] = result
            elif result["status"] == "fail":
                failed[cid] = result
            if (stats["ok"] + stats["already"] + stats["fail"] + stats["quarantine"]) % 25 == 0:
                save_progress(progress_path, progress)
        return result

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = [pool.submit(_run, row) for row in work]
        for fut in as_completed(futures):
            fut.result()

    save_progress(progress_path, progress)
    summary = {
        "schemaVersion": "1.0.0",
        "generatedAtUtc": utc_now(),
        "startedAtUtc": started,
        "stagingRoot": str(staging_root),
        "planned": len(work),
        "stats": dict(stats),
        "acquiredOkByLanguage": dict(sorted(by_lang_ok.items())),
        "completedTotal": len(completed),
        "failedTotal": len(failed),
        "quarantinedTotal": len(quarantined),
        "skipEnJa": skip_en_ja,
        "languagesFilter": sorted(languages) if languages else None,
        "limit": limit,
        "concurrency": concurrency,
        "productionMasterUntouched": True,
        "cdnUploads": 0,
    }
    write_json(REPORT / "acquire_summary.json", summary)
    write_json(staging_root / "acquire_summary.json", summary)
    return summary


def discover_tcgdex_extra_languages() -> dict[str, Any]:
    """Probe TCGdex set counts for languages sparse/missing in direct_images."""
    langs = [
        "en",
        "ja",
        "fr",
        "de",
        "it",
        "es",
        "pt",
        "zh-cn",
        "zh-tw",
        "ko",
        "th",
        "id",
        "nl",
        "pl",
        "ru",
    ]
    out: dict[str, Any] = {"generatedAtUtc": utc_now(), "languages": {}}
    for lang in langs:
        url = f"https://api.tcgdex.net/v2/{lang}/sets"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            out["languages"][lang] = {
                "http": getattr(resp, "status", 200),
                "sets": len(data) if isinstance(data, list) else None,
            }
        except Exception as exc:  # noqa: BLE001
            out["languages"][lang] = {"error": str(exc)[:160]}
        time.sleep(0.3)
    write_json(REPORT / "tcgdex_language_sets_probe.json", out)
    return out


def build_rights_matrix() -> dict[str, Any]:
    matrix = {
        "schemaVersion": "1.0.0",
        "generatedAtUtc": utc_now(),
        "sources": [
            {
                "source": "tcgdex_assets",
                "api": "https://api.tcgdex.net/v2/{lang}/...",
                "bulk": "list endpoints + set details; cache recommended",
                "languages": "en,ja,fr,de,it,es,pt,zh-cn,zh-tw,ko,th,id,nl,pl,ru (+variants)",
                "technicalAvailability": "public CDN assets.tcgdex.net",
                "metadataRights": "MIT cards-database",
                "imageRights": "not expressly licensed for redistribution",
                "cardscanrActionThisRun": "download_to_isolated_staging",
                "productionLibrary": "blocked_pending_formal_confirmation_for_non_en_ja",
                "notes": "EN/JA already in production master via independence programme.",
            },
            {
                "source": "pokemon_tcg_api",
                "api": "https://api.pokemontcg.io/v2",
                "bulk": "paginated; rate limits",
                "languages": "en",
                "technicalAvailability": "images.pokemontcg.io",
                "metadataRights": "API terms",
                "imageRights": "archive rehost not expressly documented",
                "cardscanrActionThisRun": "staging_if_present_in_candidates",
                "productionLibrary": "en_already_covered_via_independence",
            },
            {
                "source": "scrydex",
                "api": "paid credits",
                "bulk": "paginated credit-metered",
                "languages": "en,ja",
                "technicalAvailability": "images.scrydex.com",
                "metadataRights": "paid API",
                "imageRights": "written authorization 2026-10-07 for CardScanR CDN/cache/rehost",
                "cardscanrActionThisRun": "no_paid_api_spend; URLs eligible if already known",
                "productionLibrary": "approved_for_cardscanr_cdn_en_ja",
            },
            {
                "source": "pokewallet",
                "api": "authenticated",
                "bulk": "set-by-set; plan limits",
                "languages": "en,ja,+EU/CJK subsets",
                "technicalAvailability": "auth-gated binaries",
                "imageRights": "bulk rehost unclear",
                "cardscanrActionThisRun": "discovery_only_no_new_download",
                "productionLibrary": "existing_en_ja_independence_used_prior_acquire_path",
            },
            {
                "source": "official_regional_catalogues",
                "api": "none",
                "bulk": "none",
                "languages": "id,th,zh-Hant,ko,zh-Hans,jp,en",
                "technicalAvailability": "consumer sites",
                "imageRights": "terms prohibit external reuse (Asia trainers policy reviewed)",
                "cardscanrActionThisRun": "discovery_only",
                "productionLibrary": "prohibited_without_written_permission",
            },
            {
                "source": "PokemonTCG/pokemon-tcg-data",
                "api": "GitHub dataset",
                "bulk": "JSON metadata",
                "languages": "en",
                "technicalAvailability": "metadata only",
                "imageRights": "dataset does not grant artwork",
                "cardscanrActionThisRun": "discovery_only",
                "productionLibrary": "n/a",
            },
            {
                "source": "tcgdex/cards-database",
                "api": "GitHub MIT",
                "bulk": "card metadata",
                "languages": "multilingual",
                "technicalAvailability": "metadata",
                "imageRights": "MIT does not cover artwork CDN",
                "cardscanrActionThisRun": "metadata_reference_only",
                "productionLibrary": "n/a",
            },
            {
                "source": "community_collector_submissions",
                "api": "n/a",
                "bulk": "manual",
                "languages": "all gaps",
                "technicalAvailability": "requires intake workflow",
                "imageRights": "contributor licence required",
                "cardscanrActionThisRun": "ranked_opportunity_only",
                "productionLibrary": "requires_contributor_terms",
            },
        ],
    }
    write_json(REPORT / "rights_access_matrix.json", matrix)
    lines = [
        "# Source rights / access matrix",
        "",
        f"Generated: `{matrix['generatedAtUtc']}`",
        "",
        "| Source | Languages | Technical | Image rights | This run | Production library |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for s in matrix["sources"]:
        lines.append(
            f"| {s['source']} | {s.get('languages','')} | {s.get('technicalAvailability','')} | "
            f"{s.get('imageRights','')} | {s.get('cardscanrActionThisRun','')} | "
            f"{s.get('productionLibrary','')} |"
        )
    (REPORT / "rights_access_matrix.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return matrix


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("census", "acquire", "discover-langs", "rights-matrix", "all-preflight"),
    )
    parser.add_argument("--staging-root", type=Path, default=STAGING_DEFAULT)
    parser.add_argument("--languages", default="", help="Comma-separated language filter")
    parser.add_argument("--include-en-ja", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument(
        "--candidates",
        type=Path,
        default=None,
        help="Optional candidates JSONL (default: production direct_images.jsonl)",
    )
    args = parser.parse_args()

    global CANDIDATES_PATH
    if args.candidates is not None:
        CANDIDATES_PATH = args.candidates

    REPORT.mkdir(parents=True, exist_ok=True)
    langs = {x.strip() for x in args.languages.split(",") if x.strip()} or None

    if args.command == "census":
        print(json.dumps(census(staging_root=args.staging_root), indent=2))
        return 0
    if args.command == "discover-langs":
        print(json.dumps(discover_tcgdex_extra_languages(), indent=2))
        return 0
    if args.command == "rights-matrix":
        print(json.dumps(build_rights_matrix(), indent=2))
        return 0
    if args.command == "all-preflight":
        build_rights_matrix()
        discover_tcgdex_extra_languages()
        print(json.dumps(census(staging_root=args.staging_root), indent=2))
        return 0
    if args.command == "acquire":
        summary = acquire(
            staging_root=args.staging_root,
            languages=langs,
            skip_en_ja=not args.include_en_ja,
            limit=args.limit,
            concurrency=args.concurrency,
            resume=not args.no_resume,
        )
        print(json.dumps(summary, indent=2))
        # Failures are expected for missing CDN assets; non-zero only if nothing acquired
        # when work was planned.
        if summary.get("planned", 0) and not summary.get("completedTotal", 0):
            return 2
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
