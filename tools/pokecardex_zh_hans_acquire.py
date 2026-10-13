#!/usr/bin/env python3
"""Acquire PokéCardex Simplified Chinese catalogue metadata + stage images.

Boundaries
----------
- Written permission 2026-10-13: metadata commercial OK; images may be retrieved
  to own storage; Pokémon artwork rights NOT granted; no hotlinking; attribution.
- Writes metadata/reports under this worktree reports/ and image bytes ONLY under
  CARDSCANR_POKECARDEX_STAGING_ROOT (default Archive path) — never the TCGdex
  10k milestone staging root, never production master/CDN.
- Rate-limited, resumable, single-process lock.
- Client AES key is extracted from public JS each run (not a CardScanR secret).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html as html_lib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "reports" / "global_image_acquisition" / "pokecardex"
DEFAULT_STAGING = Path(
    os.environ.get(
        "CARDSCANR_POKECARDEX_STAGING_ROOT",
        r"D:\CardScanR_Archive\image_staging\pokecardex_zh_hans_20261014",
    )
)
DEFAULT_LIVE = Path(os.environ.get("CARDSCANR_DATA_LIVE_ROOT", r"D:\CardScanR_Data\cardscanr-data"))
BASE = "https://www.pokecardex.com"
SCANS_CDN = "https://pokecardex-scans.b-cdn.net"
UA = "CardScanR-CatalogueSync/1.0 (+pokecardex-written-permission-2026-10-13; rate-limited)"
DEFAULT_MIN_INTERVAL = 0.45
DEFAULT_CONCURRENCY = 2
DEFAULT_MAX_IN_FLIGHT = 6
LOCK = threading.Lock()
RATE_LIMIT_PAUSE_THRESHOLD = 5
RATE_LIMIT_PAUSE_SECONDS = 180.0


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_seg(value: Any) -> str:
    text = str(value or "unknown")
    return re.sub(r"[^\w.\-]+", "_", text)[:120]


class _ScriptExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_script = False
        self.scripts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._in_script = tag.lower() == "script"

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script":
            self._in_script = False

    def handle_data(self, data: str) -> None:
        if self._in_script and data.strip():
            self.scripts.append(data)


def http_get(url: str, *, min_interval: float, last_request: list[float], headers: dict | None = None) -> bytes:
    with LOCK:
        now = time.monotonic()
        wait_for = min_interval - (now - last_request[0])
        if wait_for > 0:
            time.sleep(wait_for)
        last_request[0] = time.monotonic()
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "*/*",
            "Referer": f"{BASE}/en/series?zone=chn",
            **(headers or {}),
        },
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        return resp.read()


def extract_aes_key(js: str) -> str:
    m = re.search(r"""(?:const|let|var)\s+_ut\s*=\s*["']([^"']+)["']""", js)
    if not m:
        raise RuntimeError("could not extract public client AES key from PokéCardex JS")
    return m.group(1)


def decrypt_initial_data(payload: dict[str, str], key: str) -> Any:
    iv = base64.b64decode(payload["iv"])
    data = base64.b64decode(payload["data"])
    raw_key = key.encode("utf-8")
    if len(raw_key) not in {16, 24, 32}:
        raise RuntimeError(f"unexpected AES key length {len(raw_key)}")
    decryptor = Cipher(algorithms.AES(raw_key), modes.CBC(iv)).decryptor()
    plain = decryptor.update(data) + decryptor.finalize()
    # PKCS7
    pad = plain[-1]
    if 1 <= pad <= 16:
        plain = plain[:-pad]
    return json.loads(plain.decode("utf-8"))


def extract_encrypted_blob(html: str) -> dict[str, str]:
    """Extract window.__INITIAL_DATA_*_ENCRYPTED__ JSON blob from page HTML."""
    patterns = [
        r"window\.__INITIAL_DATA_ENCRYPTED__\s*=\s*(\{.*?\})\s*;",
        r"window\.__INITIAL_DATA_ALL_ENCRYPTED__\s*=\s*(\{.*?\})\s*;",
        r"__INITIAL_DATA_ENCRYPTED__\s*=\s*(\{.*?\})\s*;",
        r"__INITIAL_DATA_ALL_ENCRYPTED__\s*=\s*(\{.*?\})\s*;",
    ]
    for pat in patterns:
        m = re.search(pat, html, re.S)
        if not m:
            continue
        blob = m.group(1)
        try:
            return json.loads(blob)
        except json.JSONDecodeError:
            # JS object with unquoted keys
            blob_json = re.sub(r"([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:", r'\1"\2":', blob)
            blob_json = blob_json.replace("'", '"')
            return json.loads(blob_json)
    raise RuntimeError("encrypted initial data blob not found in HTML")


def fetch_and_decrypt_page(url: str, *, key: str, min_interval: float, last_request: list[float]) -> dict[str, Any]:
    html = html_lib.unescape(
        http_get(url, min_interval=min_interval, last_request=last_request).decode("utf-8", "replace")
    )
    enc = extract_encrypted_blob(html)
    data = decrypt_initial_data(enc, key)
    if not isinstance(data, dict):
        raise RuntimeError(f"unexpected decrypted payload type {type(data)}")
    return data


def list_chn_set_codes(series_menu: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract CHN sets from decrypted seriesMenu blocks."""
    sets: list[dict[str, Any]] = []
    blocks = (series_menu or {}).get("blocksByRegion") or {}
    # Prefer CHN / chinese region keys
    for region_key, region_blocks in blocks.items():
        region_cf = str(region_key).casefold()
        if region_cf not in {"chn", "cn", "chinese", "zh", "zh-hans"}:
            # Some payloads nest all regions; keep only links containing /chn/
            pass
        if not isinstance(region_blocks, list):
            continue
        for block in region_blocks:
            for serie in (block or {}).get("series") or []:
                link = str(serie.get("link") or "")
                short = str(serie.get("shortName") or "")
                if "/chn/" in link.casefold() or region_cf in {"chn", "cn", "chinese"}:
                    sets.append(
                        {
                            "id": serie.get("id"),
                            "shortName": short,
                            "fullName": serie.get("fullName") or serie.get("commercialName"),
                            "link": link,
                            "block": (block or {}).get("nameUS") or (block or {}).get("name"),
                            "regionKey": region_key,
                        }
                    )
    # Deduplicate by shortName
    by_code: dict[str, dict[str, Any]] = {}
    for row in sets:
        code = str(row.get("shortName") or "")
        if code:
            by_code[code] = row
    return [by_code[k] for k in sorted(by_code)]


def normalize_collector(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    left = text.split("/", 1)[0].strip()
    digits = re.sub(r"^\D+", "", left)
    digits = re.sub(r"\D+$", "", digits) or left
    try:
        return str(int(digits))
    except ValueError:
        return left.casefold()


def image_url(set_code: str, sort_or_num: int, *, version_id: int | None = None) -> str:
    # BunnyCDN optimizer requires ?class=md (returns webp); bare .jpg is 403.
    if version_id and version_id not in {0, 1}:
        return f"{SCANS_CDN}/sets_chn/{set_code}/{version_id}/{sort_or_num}.jpg?class=md"
    return f"{SCANS_CDN}/sets_chn/{set_code}/{sort_or_num}.jpg?class=md"


def staging_card_dir(staging_root: Path, set_code: str, card: dict[str, Any], version: dict[str, Any] | None) -> Path:
    ver = str((version or {}).get("id_version") or "1")
    num = normalize_collector(card.get("num_card") or card.get("sort") or card.get("ordre_affichage"))
    return staging_root / "zh-Hans" / safe_seg(set_code) / f"{safe_seg(num)}_v{safe_seg(ver)}"


def acquire_run_lock(staging_root: Path) -> Path:
    staging_root.mkdir(parents=True, exist_ok=True)
    lock_path = staging_root / "acquire.pid"
    if lock_path.exists():
        try:
            old = int(lock_path.read_text(encoding="utf-8").strip() or "0")
        except ValueError:
            old = 0
        if old and old != os.getpid():
            try:
                os.kill(old, 0)
                raise SystemExit(f"another pokecardex acquire holds {lock_path} (pid={old})")
            except OSError:
                pass
    lock_path.write_text(str(os.getpid()), encoding="utf-8")
    return lock_path


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
            key = str(row.get("stagingKey") or "")
            status = str(row.get("status") or "")
            if not key or status not in {"ok", "skip_existing", "permanent_404", "quarantine"}:
                continue
            if status in {"ok", "skip_existing"}:
                p = row.get("path")
                if not p or not Path(str(p)).exists():
                    continue
            done[key] = status
    return done


def append_progress(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with LOCK:
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def magic_ok(data: bytes) -> bool:
    return (
        data.startswith(b"\xff\xd8\xff")
        or data.startswith(b"\x89PNG")
        or data.startswith(b"RIFF")
        or data.startswith(b"\x52\x49\x46\x46")  # WEBP via RIFF
    )


def process_image(
    *,
    staging_root: Path,
    set_code: str,
    card: dict[str, Any],
    version: dict[str, Any] | None,
    min_interval: float,
    last_request: list[float],
    rate_limit_state: dict[str, Any],
) -> dict[str, Any]:
    ver = version or {"id_version": 1, "nom_version_us": "Standard"}
    sort_num = int(card.get("sort") or card.get("ordre_affichage") or normalize_collector(card.get("num_card")) or 0)
    if sort_num <= 0:
        return {"status": "skip", "reason": "no_sort_num"}
    url = image_url(set_code, sort_num, version_id=int(ver.get("id_version") or 1))
    # Prefer base image path for standard; versioned path may 404 — try base then versioned.
    candidates = [image_url(set_code, sort_num)]
    if int(ver.get("id_version") or 1) not in {0, 1}:
        candidates.insert(0, url)
    out_dir = staging_card_dir(staging_root, set_code, card, ver)
    display = out_dir / "display.webp"
    asset = out_dir / "asset.json"
    staging_key = f"pokecardex|zh-Hans|{set_code}|{normalize_collector(card.get('num_card'))}|v{ver.get('id_version')}"
    if display.exists() and asset.exists():
        return {"status": "skip_existing", "stagingKey": staging_key, "path": str(display)}
    if rate_limit_state.get("paused"):
        return {"status": "paused_rate_limit", "stagingKey": staging_key}

    data = None
    used_url = None
    last_err = None
    for candidate in candidates:
        try:
            data = http_get(candidate, min_interval=min_interval, last_request=last_request)
            used_url = candidate
            rate_limit_state["consecutive429"] = 0
            break
        except urllib.error.HTTPError as exc:
            last_err = exc
            if exc.code == 404:
                continue
            if exc.code == 429:
                rate_limit_state["consecutive429"] = int(rate_limit_state.get("consecutive429") or 0) + 1
                if rate_limit_state["consecutive429"] >= RATE_LIMIT_PAUSE_THRESHOLD:
                    rate_limit_state["paused"] = True
                    rate_limit_state["pausedAt"] = utc_now()
                    return {"status": "paused_rate_limit", "stagingKey": staging_key, "http": 429}
            return {"status": "error", "stagingKey": staging_key, "http": exc.code, "reason": str(exc)}
        except Exception as exc:
            return {"status": "error", "stagingKey": staging_key, "reason": str(exc)}

    if data is None:
        return {
            "status": "permanent_404",
            "stagingKey": staging_key,
            "reason": f"404:{last_err}",
        }
    if not magic_ok(data):
        return {"status": "quarantine", "stagingKey": staging_key, "reason": "invalid_magic"}

    with Image.open(BytesIO(data)) as im:
        im.load()
        w, h = im.size
        # Convert/store as JPEG original bytes for fidelity; also write webp display optional later
    out_dir.mkdir(parents=True, exist_ok=True)
    display.write_bytes(data)
    meta = {
        "schemaVersion": "1.0.0",
        "stagingKey": staging_key,
        "language": "zh-Hans",
        "region": "CN",
        "sourceProvider": "pokecardex",
        "providerSetId": set_code,
        "providerCardId": str(card.get("id_card")),
        "providerVersionId": ver.get("id_version"),
        "versionLabel": ver.get("nom_version_us") or ver.get("nom_version"),
        "englishCardName": card.get("name_card_en"),
        "printedCollectorNumber": str(card.get("num_card") or ""),
        "normalizedCollectorNumber": normalize_collector(card.get("num_card")),
        "illustrator": card.get("nom_illustrateur"),
        "rarityId": card.get("id_rarete"),
        "originalSourceUrl": used_url,
        "rightsStatus": "staging_only_pokecardex_host_retrieve_allowed_pokemon_ip_pending",
        "licenceConfirmed": False,
        "productionPromotionAllowed": False,
        "rightsBasis": "pokecardex_written_permission_2026-10-13_images_host_only",
        "attributionRequired": "Data courtesy of PokéCardex (https://pokecardex.com)",
        "sha256": sha256_bytes(data),
        "width": w,
        "height": h,
        "bytes": len(data),
        "acquiredAt": utc_now(),
        "validationStatus": "acquired",
    }
    asset.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"status": "ok", "stagingKey": staging_key, "path": str(display), "set": set_code}


def load_catalogue_zh_hans(live: Path) -> list[dict[str, Any]]:
    path = live / "data" / "global" / "catalogue" / "cards.jsonl"
    rows = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if str(row.get("language") or "") == "zh-Hans":
                rows.append(row)
    return rows


def reconcile(
    metadata_cards: list[dict[str, Any]],
    catalogue: list[dict[str, Any]],
) -> dict[str, Any]:
    cat_by_set_num: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in catalogue:
        providers = row.get("providerSetIds") or {}
        codes = set()
        if isinstance(providers, dict):
            for v in providers.values():
                if v:
                    codes.add(str(v))
        # also parse tcgdex from canonicalSetId
        csid = str(row.get("canonicalSetId") or "")
        if "tcgdex:" in csid:
            codes.add(csid.split("tcgdex:", 1)[1])
        num = normalize_collector(row.get("printedCollectorNumber") or row.get("normalizedCollectorNumber"))
        for code in codes:
            cat_by_set_num.setdefault((code.casefold(), num), []).append(row)

    matched = 0
    unmatched = 0
    set_codes_pc = Counter()
    for card in metadata_cards:
        set_code = str(card.get("providerSetId") or card.get("nom_court") or "").casefold()
        num = normalize_collector(card.get("printedCollectorNumber") or card.get("num_card"))
        set_codes_pc[set_code] += 1
        if cat_by_set_num.get((set_code, num)):
            matched += 1
        else:
            unmatched += 1

    cat_codes = Counter()
    for row in catalogue:
        providers = row.get("providerSetIds") or {}
        if isinstance(providers, dict):
            for v in providers.values():
                if v:
                    cat_codes[str(v)] += 1

    return {
        "catalogueZhHansPrintings": len(catalogue),
        "pokecardexMetadataPrintings": len(metadata_cards),
        "matchedBySetCodeAndCollector": matched,
        "unmatchedPokecardexRows": unmatched,
        "pokecardexSetCodes": len(set_codes_pc),
        "catalogueProviderSetCodes": len(cat_codes),
        "note": (
            "Match uses provider set code + normalized collector number only. "
            "PokéCardex set codes often differ from TCGdex codes, so unmatched "
            "does not always mean a physically new printing."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="Download images (default: metadata-only)")
    parser.add_argument("--staging-root", type=Path, default=DEFAULT_STAGING)
    parser.add_argument("--live-root", type=Path, default=DEFAULT_LIVE)
    parser.add_argument("--min-interval", type=float, default=DEFAULT_MIN_INTERVAL)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--max-sets", type=int, default=0, help="0=all CHN sets")
    parser.add_argument("--image-limit", type=int, default=0, help="0=no limit on new image downloads")
    parser.add_argument("--standard-only", action="store_true", default=True)
    parser.add_argument("--all-versions", action="store_true", help="Also try non-standard version image paths")
    args = parser.parse_args()
    if args.all_versions:
        args.standard_only = False

    REPORT.mkdir(parents=True, exist_ok=True)
    staging_root = args.staging_root
    last_request = [0.0]
    # Discover JS URL from series page
    series_html_raw = http_get(
        f"{BASE}/en/series?zone=chn", min_interval=args.min_interval, last_request=last_request
    ).decode("utf-8", "replace")
    series_html = html_lib.unescape(series_html_raw)
    js_path = re.search(r"/react/sets/assets/index-[^\"']+\.js", series_html)
    if not js_path:
        raise SystemExit("could not locate PokéCardex sets JS bundle")
    js_url = urllib.parse.urljoin(BASE, js_path.group(0))
    js = http_get(js_url, min_interval=args.min_interval, last_request=last_request).decode("utf-8", "replace")
    key = extract_aes_key(js)

    series_data = decrypt_initial_data(extract_encrypted_blob(series_html), key)
    set_list = list_chn_set_codes(series_data.get("seriesMenu") or {})
    if not set_list:
        # Fallback: scrape /en/series/chn/CODE links from HTML
        codes = sorted(set(re.findall(r"/en/series/chn/([A-Za-z0-9]+)", series_html)))
        set_list = [{"shortName": c, "link": f"{BASE}/en/series/chn/{c}", "fullName": c} for c in codes]
    if args.max_sets:
        set_list = set_list[: args.max_sets]

    (REPORT / "chn_sets_discovered.json").write_text(
        json.dumps({"generatedAtUtc": utc_now(), "sets": set_list}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    metadata_path = REPORT / "zh_hans_cards.jsonl"
    # Resume metadata by set code
    existing_meta_sets: set[str] = set()
    if metadata_path.exists():
        with metadata_path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                existing_meta_sets.add(str(row.get("providerSetId") or ""))

    all_meta_rows: list[dict[str, Any]] = []
    if metadata_path.exists():
        with metadata_path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    try:
                        all_meta_rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass

    meta_status: Counter[str] = Counter()
    for set_row in set_list:
        code = str(set_row.get("shortName") or "")
        if not code:
            continue
        if code in existing_meta_sets:
            meta_status["skip_existing_set"] += 1
            continue
        url = str(set_row.get("link") or f"{BASE}/en/series/chn/{code}")
        if not url.startswith("http"):
            url = urllib.parse.urljoin(BASE, url)
        try:
            data = fetch_and_decrypt_page(
                url, key=key, min_interval=args.min_interval, last_request=last_request
            )
        except Exception as exc:
            meta_status["set_error"] += 1
            append_progress(
                REPORT / "metadata_progress.jsonl",
                {"at": utc_now(), "set": code, "status": "error", "reason": str(exc)},
            )
            continue
        current = data.get("currentSeries") or {}
        cartes = data.get("cartes") or []
        rarete_map = {r.get("id_rarete"): r.get("nom_rarete") for r in (data.get("raretes") or [])}
        with metadata_path.open("a", encoding="utf-8", newline="\n") as handle:
            for card in cartes:
                versions = card.get("versions") or [{"id_version": 1, "nom_version_us": "Standard"}]
                for ver in versions:
                    if args.standard_only and int(ver.get("id_version") or 1) not in {0, 1}:
                        # Still record non-standard as metadata-only rows without forcing image path
                        pass
                    row = {
                        "language": "zh-Hans",
                        "region": "CN",
                        "sourceProvider": "pokecardex",
                        "providerSetId": code,
                        "providerSetNumericId": current.get("id") or card.get("id_serie"),
                        "setName": current.get("fullName") or set_row.get("fullName"),
                        "setTotalCards": current.get("totalCards"),
                        "providerCardId": card.get("id_card"),
                        "providerVersionId": ver.get("id_version"),
                        "versionLabel": ver.get("nom_version_us") or ver.get("nom_version"),
                        "englishCardName": card.get("name_card_en"),
                        "frenchCardName": card.get("name_card_fr"),
                        "germanCardName": card.get("name_card_de"),
                        "printedCollectorNumber": card.get("num_card"),
                        "normalizedCollectorNumber": normalize_collector(card.get("num_card")),
                        "sort": card.get("sort") or card.get("ordre_affichage"),
                        "illustrator": card.get("nom_illustrateur"),
                        "rarityId": card.get("id_rarete"),
                        "rarityName": rarete_map.get(card.get("id_rarete")),
                        "pokedexIds": card.get("id_pokedex_list") or [card.get("id_pokedex")],
                        "rightsStatus": "approved_written_permission_commercial_metadata_2026-10-13",
                        "attributionRequired": "Data courtesy of PokéCardex (https://pokecardex.com)",
                        "sourcePageUrl": url,
                        "collectedAt": utc_now(),
                    }
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                    all_meta_rows.append(row)
        existing_meta_sets.add(code)
        meta_status["sets_ok"] += 1
        append_progress(
            REPORT / "metadata_progress.jsonl",
            {"at": utc_now(), "set": code, "status": "ok", "cards": len(cartes)},
        )

    # Unique printings: card id + version
    unique_keys = {
        f"{r.get('providerCardId')}|v{r.get('providerVersionId')}"
        for r in all_meta_rows
    }
    standard_rows = [
        r for r in all_meta_rows if int(r.get("providerVersionId") or 1) in {0, 1}
    ]

    catalogue = load_catalogue_zh_hans(args.live_root)
    recon = reconcile(standard_rows, catalogue)

    image_summary: dict[str, Any] = {"executed": False}
    if args.execute:
        lock_path = acquire_run_lock(staging_root)
        progress_path = staging_root / "progress.jsonl"
        done = load_progress(progress_path)
        rate_limit_state: dict[str, Any] = {"consecutive429": 0, "paused": False}
        status_counts: Counter[str] = Counter()
        newly = 0
        try:
            # Build work items for standard versions first
            work = []
            for r in standard_rows:
                set_code = str(r.get("providerSetId") or "")
                card = {
                    "id_card": r.get("providerCardId"),
                    "num_card": r.get("printedCollectorNumber"),
                    "sort": r.get("sort"),
                    "ordre_affichage": r.get("sort"),
                    "name_card_en": r.get("englishCardName"),
                    "nom_illustrateur": r.get("illustrator"),
                    "id_rarete": r.get("rarityId"),
                }
                ver = {
                    "id_version": r.get("providerVersionId") or 1,
                    "nom_version_us": r.get("versionLabel") or "Standard",
                }
                staging_key = (
                    f"pokecardex|zh-Hans|{set_code}|"
                    f"{normalize_collector(r.get('printedCollectorNumber'))}|v{ver['id_version']}"
                )
                if staging_key in done:
                    status_counts[done[staging_key]] += 1
                    continue
                work.append((set_code, card, ver, staging_key))
            if args.image_limit:
                work = work[: args.image_limit]

            with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
                in_flight = set()
                idx = 0
                while idx < len(work) or in_flight:
                    if rate_limit_state.get("paused"):
                        print("paused: sustained 429", flush=True)
                        time.sleep(RATE_LIMIT_PAUSE_SECONDS)
                        rate_limit_state["paused"] = False
                        rate_limit_state["consecutive429"] = 0
                    while idx < len(work) and len(in_flight) < max(1, min(args.concurrency, DEFAULT_MAX_IN_FLIGHT)):
                        set_code, card, ver, staging_key = work[idx]
                        idx += 1
                        fut = pool.submit(
                            process_image,
                            staging_root=staging_root,
                            set_code=set_code,
                            card=card,
                            version=ver,
                            min_interval=args.min_interval,
                            last_request=last_request,
                            rate_limit_state=rate_limit_state,
                        )
                        in_flight.add(fut)
                    done_futs, in_flight = wait(in_flight, return_when=FIRST_COMPLETED)
                    for fut in done_futs:
                        result = fut.result()
                        status = str(result.get("status") or "error")
                        status_counts[status] += 1
                        if status == "ok":
                            newly += 1
                        if status in {"ok", "skip_existing", "permanent_404", "quarantine", "error", "paused_rate_limit"}:
                            append_progress(
                                progress_path,
                                {**result, "at": utc_now()},
                            )
                        if (sum(status_counts.values()) % 50) == 0:
                            print("progress", dict(status_counts), flush=True)
        finally:
            if lock_path.exists() and lock_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                lock_path.unlink()

        # Count staged displays
        staged = 0
        if staging_root.exists():
            staged = sum(1 for _ in staging_root.rglob("display.webp"))
            staged += sum(1 for _ in staging_root.rglob("display.jpg"))
        image_summary = {
            "executed": True,
            "statusCounts": dict(status_counts),
            "newlyDownloaded": newly,
            "stagedDisplayFiles": staged,
            "stagingRoot": str(staging_root),
            "productionPromotion": "forbidden_pending_pokemon_ip_review",
        }

    summary = {
        "generatedAtUtc": utc_now(),
        "source": "pokecardex",
        "permission": "pokecardex_written_permission_2026-10-13",
        "attributionRequired": "Data courtesy of PokéCardex (https://pokecardex.com)",
        "setsDiscovered": len(set_list),
        "metadataStatus": dict(meta_status),
        "metadataRowsTotal": len(all_meta_rows),
        "uniqueCardVersionKeys": len(unique_keys),
        "standardVersionRows": len(standard_rows),
        "reconciliation": recon,
        "images": image_summary,
        "preservedTcgdexStagingCountExpected": 10049,
        "denominators": {
            "globalPrintings": 117665,
            "urlCandidatesTcgdex": 92817,
            "enJaProduction": 68011,
            "tcgdexStagingValidatedNonEnJa": 10049,
            "catalogueZhHansPrintings": recon["catalogueZhHansPrintings"],
            "pokecardexZhHansMetadataStandard": len(standard_rows),
            "note": (
                "Do not subtract these denominators from each other as one coverage %: "
                "EN/JA production IDs, global catalogue IDs, TCGdex URL candidates, "
                "TCGdex staging, and PokéCardex zh-Hans metadata are different universes."
            ),
        },
    }
    (REPORT / "acquire_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
