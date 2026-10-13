#!/usr/bin/env python3
"""Acquire public TCGdex image candidates into isolated staging storage.

Boundaries
----------
- Writes ONLY to CARDSCANR_IMAGE_STAGING_ROOT (default under CardScanR_Archive);
  never production master/CDN.
- Does not overwrite existing good staging displays or change canonical IDs.
- Tags every asset with rightsStatus reflecting mirror-permission posture.
- Skips hosts that are discovery-only / prohibited for download.
- Resumable via progress JSONL; rate-limited with retries.
- Progress entries whose files no longer exist are not treated as done.

LIVE catalogue/master binaries may live outside the worktree. Pass
--live-root (default: D:\\CardScanR_Data\\cardscanr-data) for data paths.
Reports are written under this repository's reports/global_image_acquisition/.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LIVE = Path(os.environ.get("CARDSCANR_DATA_LIVE_ROOT", r"D:\CardScanR_Data\cardscanr-data"))
# Keep binaries outside any git worktree (shared archive volume).
DEFAULT_STAGING = Path(
    os.environ.get(
        "CARDSCANR_IMAGE_STAGING_ROOT",
        r"D:\CardScanR_Archive\image_staging\global_acq_20261013",
    )
)
UA = "CardScanR-GlobalImageStaging/1.0"
REPORT = ROOT / "reports" / "global_image_acquisition"
# Conservative defaults: ~2–3 HTTP starts/sec shared across workers; never unrestricted.
DEFAULT_CONCURRENCY = 3
DEFAULT_MIN_INTERVAL = 0.40
DEFAULT_MAX_IN_FLIGHT = 12
# Pause source after this many consecutive 429s.
RATE_LIMIT_PAUSE_THRESHOLD = 5
RATE_LIMIT_PAUSE_SECONDS = 120.0

# Hosts allowed for staging download of image bodies (not production promotion).
STAGING_DOWNLOAD_HOSTS = {
    "assets.tcgdex.net": {
        "provider": "tcgdex",
        "rightsStatus": "staging_only_mirror_permission_pending",
        "licenceConfirmed": False,
        "productionPromotionAllowed": False,
        "basis": "public_direct_url_canary_approved;_rehost_pending_written_permission",
    },
    "images.pokemontcg.io": {
        "provider": "pokemon_tcg_api",
        "rightsStatus": "staging_only_mirror_permission_pending",
        "licenceConfirmed": False,
        "productionPromotionAllowed": False,
        "basis": "public_host_used_in_en_ja_independence;_global_rehost_pending_review",
    },
    "images.scrydex.com": {
        "provider": "scrydex",
        "rightsStatus": "approved_written_authorization_cardscanr_cdn",
        "licenceConfirmed": True,
        "productionPromotionAllowed": True,
        "basis": "scrydex_written_authorization_2026-10-07",
    },
}

# Do not download bodies from these hosts in this pipeline.
DISCOVERY_ONLY_HOSTS = {
    "api.pokewallet.io": "pokewallet_authenticated_rehost_unclear",
    "asia.pokemon-card.com": "official_asia_terms_prohibit_reuse",
    "www.pokemon.com": "official_site_no_bulk_grant",
    "pokemoncard.co.kr": "official_korea_terms_require_prior_agreement",
}

LOCK = threading.Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_seg(seg: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", str(seg))[:180]


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


def http_get(url: str, *, min_interval: float, last_request: list[float]) -> bytes:
    # Pace starts without holding the lock during sleep/download so workers overlap.
    while True:
        with LOCK:
            now = time.monotonic()
            wait = min_interval - (now - last_request[0])
            if wait <= 0:
                last_request[0] = now
                break
        time.sleep(min(wait, 0.05))
    last: Exception | None = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "image/webp,image/*;q=0.8"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
            if not data:
                raise RuntimeError("empty_body")
            return data
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code in {404, 410}:
                raise
            if exc.code == 429:
                time.sleep(min(2 ** attempt * 2, 30))
            else:
                time.sleep(min(2 ** attempt, 8))
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(min(2 ** attempt, 8))
    raise last  # type: ignore[misc]


def staging_dir(staging_root: Path, row: dict[str, Any]) -> Path:
    lang = safe_seg(row.get("language") or "unknown")
    set_id = safe_seg(row.get("providerSetId") or row.get("canonicalSetId") or "unknown")
    card_id = safe_seg(row.get("providerCardId") or row.get("canonicalPrintingId") or "unknown")
    return staging_root / lang / set_id / card_id


def load_progress(path: Path) -> dict[str, str]:
    done: dict[str, str] = {}
    if not path.exists():
        return done
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            cid = str(row.get("canonicalPrintingId") or "")
            status = str(row.get("status") or "")
            if not cid or status not in {"ok", "skip_existing", "permanent_404", "quarantine"}:
                continue
            # Do not treat wiped/missing paths as done — allow re-acquire.
            if status in {"ok", "skip_existing"}:
                path = row.get("path")
                if not path or not Path(str(path)).exists():
                    continue
            done[cid] = status
    return done


def append_progress(path: Path, row: dict[str, Any], *, mirror: Path | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
    with LOCK:
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
        if mirror is not None:
            mirror.parent.mkdir(parents=True, exist_ok=True)
            with mirror.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()


def acquire_run_lock(staging_root: Path) -> Path:
    lock_path = staging_root / "acquire.pid"
    staging_root.mkdir(parents=True, exist_ok=True)
    if lock_path.exists():
        try:
            old_pid = int(lock_path.read_text(encoding="utf-8").strip() or "0")
        except ValueError:
            old_pid = 0
        if old_pid:
            try:
                os.kill(old_pid, 0)
            except OSError:
                pass
            else:
                raise SystemExit(f"another acquire process holds {lock_path} (pid={old_pid})")
    lock_path.write_text(str(os.getpid()), encoding="utf-8")
    return lock_path


def iter_direct_images(path: Path):
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            yield json.loads(line)


def choose_url(row: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None, str | None]:
    for key in ("normalizedDisplayUrl", "normalizedThumbnailUrl", "originalDocumentedImageUrl"):
        url = row.get(key)
        if not isinstance(url, str) or not url.startswith("http"):
            continue
        host = host_of(url)
        if host in DISCOVERY_ONLY_HOSTS:
            return None, None, DISCOVERY_ONLY_HOSTS[host]
        policy = STAGING_DOWNLOAD_HOSTS.get(host)
        if policy:
            return url, policy, None
        return None, None, f"host_not_in_staging_allowlist:{host or 'unknown'}"
    return None, None, "no_image_url"


def parse_tcgdex_asset_url(url: str) -> dict[str, str] | None:
    """Parse assets.tcgdex.net/{lang}/{serie}/{set}/{localId}/{quality}.{ext}."""
    try:
        parts = [p for p in urllib.parse.urlparse(url).path.split("/") if p]
    except Exception:
        return None
    # Expect: lang, serie, set, localId, filename
    if len(parts) < 5:
        return None
    lang, _serie, set_id, local_id = parts[0], parts[1], parts[2], parts[3]
    return {
        "urlLanguage": lang.casefold(),
        "urlSetId": set_id,
        "urlLocalId": local_id,
    }


def normalize_collector(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    # Strip common padded / slash forms: 001/165 -> 1, 020 -> 20
    left = text.split("/", 1)[0].strip()
    digits = re.sub(r"^\D+", "", left)
    digits = re.sub(r"\D+$", "", digits) or left
    try:
        return str(int(digits))
    except ValueError:
        return left.casefold()


def validate_url_identity(row: dict[str, Any], url: str) -> list[str]:
    """Fail-closed identity checks using URL path vs catalogue row (no cross-lang sub)."""
    issues: list[str] = []
    host = host_of(url)
    if host != "assets.tcgdex.net":
        return issues
    parsed = parse_tcgdex_asset_url(url)
    if not parsed:
        issues.append("unparseable_tcgdex_url")
        return issues
    row_lang = str(row.get("language") or "").casefold()
    # Catalogue uses es-419 / pt-BR; TCGdex path often es / pt
    lang_aliases = {
        "es-419": {"es", "es-419", "es-mx", "es-la"},
        "es": {"es", "es-es"},
        "pt-br": {"pt", "pt-br"},
        "pt-pt": {"pt", "pt-pt"},
        "zh-hant": {"zh-tw", "zh-hant", "zh-hk"},
        "zh-hans": {"zh-cn", "zh-hans"},
    }
    allowed_langs = lang_aliases.get(row_lang, {row_lang}) | {row_lang}
    if parsed["urlLanguage"] not in allowed_langs:
        issues.append(
            f"language_mismatch:row={row_lang}:url={parsed['urlLanguage']}"
        )
    provider_set = str(row.get("providerSetId") or "").strip()
    if provider_set and provider_set.casefold() != parsed["urlSetId"].casefold():
        issues.append(f"set_mismatch:row={provider_set}:url={parsed['urlSetId']}")
    row_num = normalize_collector(
        row.get("printedCollectorNumber") or row.get("normalizedCollectorNumber")
    )
    url_num = normalize_collector(parsed["urlLocalId"])
    if row_num and url_num and row_num != url_num:
        issues.append(f"collector_mismatch:row={row_num}:url={url_num}")
    provider_card = str(row.get("providerCardId") or "")
    url_set = parsed["urlSetId"]
    if provider_card and url_set:
        # Prefer prefix match so hyphenated set ids (e.g. P-A-010) are not
        # mis-split as set="P" / local="A-010".
        prefix = url_set + "-"
        if provider_card.casefold().startswith(prefix.casefold()):
            plocal = provider_card[len(prefix) :]
            if plocal and normalize_collector(plocal) != url_num:
                issues.append(f"provider_card_num_mismatch:{provider_card}")
        elif "-" in provider_card:
            # Legacy single-hyphen form: bw10-100
            pset, _, plocal = provider_card.partition("-")
            if pset and pset.casefold() != url_set.casefold():
                issues.append(f"provider_card_set_mismatch:{provider_card}")
            if plocal and normalize_collector(plocal) != url_num:
                issues.append(f"provider_card_num_mismatch:{provider_card}")
    return issues


def process_row(
    row: dict[str, Any],
    *,
    staging_root: Path,
    quarantine_root: Path,
    min_interval: float,
    last_request: list[float],
    rate_limit_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cid = str(row.get("canonicalPrintingId") or "")
    url, policy, reason = choose_url(row)
    if not url or not policy:
        return {
            "canonicalPrintingId": cid,
            "status": "skip",
            "reason": reason,
            "language": row.get("language"),
            "at": utc_now(),
        }
    identity_issues = validate_url_identity(row, url)
    if identity_issues:
        qdir = quarantine_root / safe_seg(cid)
        write_atomic(
            qdir / "quarantine.json",
            json.dumps(
                {
                    "canonicalPrintingId": cid,
                    "url": url,
                    "reason": "identity_mismatch",
                    "issues": identity_issues,
                    "at": utc_now(),
                },
                indent=2,
            ).encode("utf-8"),
        )
        return {
            "canonicalPrintingId": cid,
            "status": "quarantine",
            "reason": "identity_mismatch",
            "issues": identity_issues,
            "language": row.get("language"),
            "at": utc_now(),
        }
    dest = staging_dir(staging_root, row)
    display = dest / "display.webp"
    asset_path = dest / "asset.json"
    if display.exists() and asset_path.exists():
        return {
            "canonicalPrintingId": cid,
            "status": "skip_existing",
            "language": row.get("language"),
            "path": str(display),
            "at": utc_now(),
        }
    if rate_limit_state and rate_limit_state.get("paused"):
        return {
            "canonicalPrintingId": cid,
            "status": "paused_rate_limit",
            "reason": "source_paused_after_repeated_429",
            "language": row.get("language"),
            "at": utc_now(),
        }
    try:
        raw = http_get(url, min_interval=min_interval, last_request=last_request)
        if rate_limit_state is not None:
            rate_limit_state["consecutive429"] = 0
        if not magic_ok(raw):
            qdir = quarantine_root / safe_seg(cid)
            write_atomic(qdir / "payload.bin", raw)
            write_atomic(
                qdir / "quarantine.json",
                json.dumps(
                    {"canonicalPrintingId": cid, "url": url, "reason": "invalid_magic", "at": utc_now()},
                    indent=2,
                ).encode("utf-8"),
            )
            return {
                "canonicalPrintingId": cid,
                "status": "quarantine",
                "reason": "invalid_magic",
                "language": row.get("language"),
                "at": utc_now(),
            }
        with Image.open(BytesIO(raw)) as im:
            im.load()
            width, height = im.size
        ratio = (width / height) if height else 0.0
        plausible = 0.55 <= ratio <= 0.85 and width >= 200 and height >= 280
        webp = to_webp(raw)
        digest = sha256_bytes(webp)
        ext = ".png" if raw[:4] == b"\x89PNG" else (".webp" if raw[8:12] == b"WEBP" else ".bin")
        write_atomic(dest / f"original{ext}", raw)
        write_atomic(display, webp)
        meta = {
            "schemaVersion": "1.0.0",
            "canonicalPrintingId": cid,
            "language": row.get("language"),
            "region": row.get("region"),
            "canonicalSetId": row.get("canonicalSetId"),
            "printedCollectorNumber": row.get("printedCollectorNumber"),
            "normalizedCollectorNumber": row.get("normalizedCollectorNumber"),
            "provider": policy["provider"],
            "providerCardId": row.get("providerCardId"),
            "providerSetId": row.get("providerSetId"),
            "originalSourceUrl": url,
            "sha256": digest,
            "byteSize": len(webp),
            "mimeType": "image/webp",
            "width": width,
            "height": height,
            "aspectRatio": round(ratio, 4),
            "plausibleAspect": plausible,
            "identityValidated": True,
            "identityChecks": "url_path_vs_catalogue_row",
            "variantAssumption": "unspecified_single_face_from_provider_url",
            "rightsStatus": policy["rightsStatus"],
            "licenceConfirmed": policy["licenceConfirmed"],
            "productionPromotionAllowed": policy["productionPromotionAllowed"],
            "rightsBasis": policy["basis"],
            "artworkRightsNote": "MIT metadata licence does not grant Pokémon artwork redistribution rights",
            "storageClass": "isolated_staging",
            "acquiredAt": utc_now(),
            "acquisitionTool": "global_image_staging_acquire.py",
            "validationStatus": "validated" if plausible else "quarantine_aspect",
        }
        write_atomic(asset_path, json.dumps(meta, indent=2, ensure_ascii=False).encode("utf-8"))
        if not plausible:
            qdir = quarantine_root / safe_seg(cid)
            write_atomic(
                qdir / "quarantine.json",
                json.dumps(
                    {
                        "canonicalPrintingId": cid,
                        "url": url,
                        "reason": "implausible_dimensions_or_aspect",
                        "width": width,
                        "height": height,
                        "stagingPath": str(dest),
                        "at": utc_now(),
                    },
                    indent=2,
                ).encode("utf-8"),
            )
            return {
                "canonicalPrintingId": cid,
                "status": "quarantine",
                "reason": "implausible_dimensions_or_aspect",
                "language": row.get("language"),
                "sha256": digest,
                "at": utc_now(),
            }
        return {
            "canonicalPrintingId": cid,
            "status": "ok",
            "language": row.get("language"),
            "provider": policy["provider"],
            "sha256": digest,
            "bytes": len(webp),
            "width": width,
            "height": height,
            "rightsStatus": policy["rightsStatus"],
            "validationStatus": "validated",
            "path": str(display),
            "at": utc_now(),
        }
    except urllib.error.HTTPError as exc:
        if exc.code == 429 and rate_limit_state is not None:
            rate_limit_state["consecutive429"] = int(rate_limit_state.get("consecutive429") or 0) + 1
            if rate_limit_state["consecutive429"] >= RATE_LIMIT_PAUSE_THRESHOLD:
                rate_limit_state["paused"] = True
                rate_limit_state["pausedAt"] = utc_now()
                rate_limit_state["pauseSeconds"] = RATE_LIMIT_PAUSE_SECONDS
                time.sleep(RATE_LIMIT_PAUSE_SECONDS)
                rate_limit_state["paused"] = False
                rate_limit_state["consecutive429"] = 0
        status = "permanent_404" if exc.code in {404, 410} else "fail"
        return {
            "canonicalPrintingId": cid,
            "status": status,
            "reason": f"http_{exc.code}",
            "language": row.get("language"),
            "url": url,
            "at": utc_now(),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "canonicalPrintingId": cid,
            "status": "fail",
            "reason": f"{type(exc).__name__}:{exc}",
            "language": row.get("language"),
            "url": url,
            "at": utc_now(),
        }


def build_before_census(live_root: Path, staging_root: Path) -> dict[str, Any]:
    cards_path = live_root / "data" / "global" / "catalogue" / "cards.jsonl"
    direct_path = live_root / "data" / "global" / "catalogue" / "direct_images.jsonl"
    master_root = live_root / "data" / "images" / "master"
    by_lang = Counter()
    with_candidate = Counter()
    no_candidate = Counter()
    if cards_path.exists():
        for line in cards_path.open(encoding="utf-8"):
            if not line.strip():
                continue
            row = json.loads(line)
            lang = str(row.get("language") or "?")
            by_lang[lang] += 1
            if row.get("imageProvenance"):
                with_candidate[lang] += 1
            else:
                no_candidate[lang] += 1
    direct_by_lang = Counter()
    direct_hosts = Counter()
    if direct_path.exists():
        for line in direct_path.open(encoding="utf-8"):
            if not line.strip():
                continue
            row = json.loads(line)
            direct_by_lang[str(row.get("language") or "?")] += 1
            url = row.get("normalizedDisplayUrl") or ""
            direct_hosts[host_of(str(url)) or "?"] += 1
    master_verified = {"en": 0, "ja": 0}
    # Prefer independence summary over walking 68k assets.
    summary_path = live_root / "reports" / "image_independence" / "image_independence_summary.json"
    if summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        master_verified["en"] = int((summary.get("en") or {}).get("localMaster") or 0)
        master_verified["ja"] = int((summary.get("ja") or {}).get("localMaster") or 0)
    elif master_root.exists():
        for lang in ("en", "ja"):
            root = master_root / lang
            if root.exists():
                master_verified[lang] = sum(1 for _ in root.rglob("display.webp"))
    staging_verified: dict[str, int] = defaultdict(int)
    if staging_root.exists():
        for asset in staging_root.rglob("asset.json"):
            if not (asset.parent / "display.webp").exists():
                continue
            meta = json.loads(asset.read_text(encoding="utf-8"))
            staging_verified[str(meta.get("language") or "?")] += 1
    return {
        "generatedAtUtc": utc_now(),
        "globalCataloguePrintingsByLanguage": dict(sorted(by_lang.items())),
        "globalCatalogueWithImageCandidate": dict(sorted(with_candidate.items())),
        "globalCatalogueWithoutImageCandidate": dict(sorted(no_candidate.items())),
        "directImageCandidatesByLanguage": dict(sorted(direct_by_lang.items())),
        "directImageHosts": dict(direct_hosts),
        "productionMasterVerifiedFiles": master_verified,
        "stagingVerifiedFilesBefore": dict(sorted(staging_verified.items())),
        "notes": [
            "Production master EN/JA uses the EN/JP product catalogue identity system (68,011).",
            "Global catalogue uses layered canonicalPrintingId identity; staging binds to those IDs.",
            "URL/metadata presence is not counted as verified coverage.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-root", type=Path, default=DEFAULT_LIVE)
    parser.add_argument("--languages", default="", help="Comma-separated language filter; empty=all")
    parser.add_argument("--exclude-languages", default="en,ja", help="Skip langs already covered in production master by default")
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Max NEW download attempts this run (0=until queue empty; still rate-limited)",
    )
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument(
        "--min-interval",
        type=float,
        default=DEFAULT_MIN_INTERVAL,
        help="Minimum seconds between HTTP starts (global shared gate)",
    )
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--execute", action="store_true", help="Perform staging downloads")
    parser.add_argument(
        "--candidates",
        type=Path,
        default=None,
        help="Optional candidates JSONL (default: live direct_images.jsonl)",
    )
    args = parser.parse_args()

    live_root: Path = args.live_root
    staging_root = Path(
        os.environ.get("CARDSCANR_IMAGE_STAGING_ROOT", str(DEFAULT_STAGING))
    )
    quarantine_root = staging_root / "_quarantine"
    progress_path = staging_root / "progress.jsonl"
    progress_mirror = REPORT / "progress.jsonl"
    direct_path = (
        args.candidates
        if args.candidates is not None
        else live_root / "data" / "global" / "catalogue" / "direct_images.jsonl"
    )
    REPORT.mkdir(parents=True, exist_ok=True)

    before = build_before_census(live_root, staging_root)
    (REPORT / "before_coverage.json").write_text(json.dumps(before, indent=2), encoding="utf-8")

    if args.report_only or not args.execute:
        print(json.dumps({"mode": "report_only", "before": before}, indent=2))
        if not args.execute:
            return 0

    if not direct_path.exists():
        raise SystemExit(f"missing direct images catalogue: {direct_path}")

    lock_path = acquire_run_lock(staging_root)
    include = {x.strip() for x in args.languages.split(",") if x.strip()}
    exclude = {x.strip() for x in args.exclude_languages.split(",") if x.strip()}
    done = load_progress(progress_path)
    # Also treat on-disk validated staging assets as done (progress may lag after moves).
    if staging_root.exists():
        for asset in staging_root.rglob("asset.json"):
            if "_quarantine" in asset.parts:
                continue
            if not (asset.parent / "display.webp").exists():
                continue
            try:
                meta = json.loads(asset.read_text(encoding="utf-8"))
            except Exception:
                continue
            cid_disk = str(meta.get("canonicalPrintingId") or "")
            if cid_disk:
                done.setdefault(cid_disk, "skip_existing")
    last_request = [0.0]
    rate_limit_state: dict[str, Any] = {"consecutive429": 0, "paused": False}
    by_lang_queue: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in iter_direct_images(direct_path):
        lang = str(row.get("language") or "")
        if include and lang not in include:
            continue
        if lang in exclude:
            continue
        if str(row.get("directUseTechnicalStatus") or "") == "permanent_404":
            continue
        cid = str(row.get("canonicalPrintingId") or "")
        if not cid or cid in done:
            continue
        by_lang_queue[lang].append(row)
    # Round-robin languages so a single run covers FR/DE/ES/... not only the first JSONL block.
    queued = []
    while by_lang_queue:
        for lang in list(by_lang_queue.keys()):
            bucket = by_lang_queue[lang]
            if not bucket:
                del by_lang_queue[lang]
                continue
            queued.append(bucket.pop(0))
            if not bucket:
                del by_lang_queue[lang]
            if args.limit and len(queued) >= args.limit:
                by_lang_queue.clear()
                break

    print(
        f"queued={len(queued)} staging_root={staging_root} "
        f"concurrency={args.concurrency} min_interval={args.min_interval}",
        flush=True,
    )
    counts: Counter[str] = Counter()
    by_lang_ok: Counter[str] = Counter()
    # Bound in-flight futures — submitting 60k+ at once stalls/OOMs.
    max_in_flight = min(DEFAULT_MAX_IN_FLIGHT, max(args.concurrency * 3, args.concurrency))
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        idx = 0
        in_flight: set[Any] = set()
        while idx < len(queued) or in_flight:
            while idx < len(queued) and len(in_flight) < max_in_flight:
                row = queued[idx]
                idx += 1
                in_flight.add(
                    pool.submit(
                        process_row,
                        row,
                        staging_root=staging_root,
                        quarantine_root=quarantine_root,
                        min_interval=args.min_interval,
                        last_request=last_request,
                        rate_limit_state=rate_limit_state,
                    )
                )
            done, in_flight = wait(in_flight, return_when=FIRST_COMPLETED)
            for fut in done:
                result = fut.result()
                append_progress(progress_path, result, mirror=progress_mirror)
                status = str(result.get("status") or "unknown")
                counts[status] += 1
                if status == "ok":
                    by_lang_ok[str(result.get("language") or "?")] += 1
                if sum(counts.values()) % 100 == 0:
                    print(
                        f"progress {sum(counts.values())}/{len(queued)} {dict(counts)}",
                        flush=True,
                    )

    after_staging: dict[str, int] = defaultdict(int)
    for asset in staging_root.rglob("asset.json"):
        if asset.parent.name.startswith("_"):
            continue
        if not (asset.parent / "display.webp").exists():
            continue
        meta = json.loads(asset.read_text(encoding="utf-8"))
        after_staging[str(meta.get("language") or "?")] += 1

    summary = {
        "generatedAtUtc": utc_now(),
        "stagingRoot": str(staging_root),
        "queued": len(queued),
        "statusCounts": dict(counts),
        "newlyAcquiredOkByLanguage": dict(sorted(by_lang_ok.items())),
        "stagingVerifiedFilesAfter": dict(sorted(after_staging.items())),
        "productionPromotion": "forbidden_in_this_run",
        "beforeCoveragePath": str(REPORT / "before_coverage.json"),
    }
    (REPORT / "staging_acquire_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    try:
        if lock_path.exists() and lock_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
            lock_path.unlink()
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
