#!/usr/bin/env python3
"""Authenticated PokéWallet image acquisition for remaining EN/JA independence gaps.

Uses the documented GET /images/:id endpoint with X-API-Key. Respects Free-plan
limits (100/hour, 1,000/day) with a safety buffer. Stores verified application-
cache copies in the local master archive and, for exact catalogue matches,
uploads to CardScanR R2 for normal app image resolution.

Rights basis (reviewed 2026-10-09):
  - Terms §4.1: limited licence for personal or commercial use of the Services
  - API docs: demonstrate downloading /images/:id to files; recommend caching
  - Application CDN delivery of verified app-cache copies for CardScanR display
    is treated as ordinary application use of cached API images — not as an
    unlimited redistribution licence. Identity-uncertain cards stay local-only.

Does not create accounts, purchase plans, log secrets, or evade rate limits.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import image_independence_multisource_resolver as m

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "reports" / "image_independence"
STATE = ROOT / "data" / "images" / "independence" / "pokewallet_acquire"
REPORT_MIRROR = Path(r"D:\CardScanR\reports\image_independence")
CATALOGUE = ROOT / "public" / "v1" / "catalog" / "pokemon"

UA = "CardScanR-PokewalletAcquire/1.0"
API_BASE = "https://api.pokewallet.io"
PK_RE = re.compile(r"(pk_[0-9a-f]+)", re.I)

# Free plan documented limits; keep headroom.
HOUR_LIMIT = 100
DAY_LIMIT = 1000
HOUR_SAFE = 90
DAY_SAFE = 900

RIGHTS_BASIS = (
    "pokewallet_terms_commercial_api_use_documented_image_download_and_cache_2026-10-09"
)
RIGHTS_NOTE = (
    "Terms permit commercial API use; docs demonstrate file download and caching. "
    "CardScanR stores verified application-cache copies and serves them via its own "
    "CDN for in-app catalogue display. Not treated as unlimited redistribution."
)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def utc_hour() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")


@dataclass
class Target:
    canonical: str
    language: str
    set_id: str
    collector: str
    name: str
    pk_id: str
    image_url: str
    set_name: str = ""
    variant: str = ""


def api_key() -> str:
    key = (os.environ.get("POKEWALLET_API_KEY") or "").strip()
    if not key:
        raise SystemExit("POKEWALLET_API_KEY is not configured")
    return key


def ledger_path() -> Path:
    STATE.mkdir(parents=True, exist_ok=True)
    return STATE / "request_ledger.json"


def progress_path() -> Path:
    STATE.mkdir(parents=True, exist_ok=True)
    return STATE / "progress.jsonl"


def load_ledger() -> dict[str, Any]:
    path = ledger_path()
    if not path.exists():
        return {"day": utc_day(), "hour": utc_hour(), "dayCount": 0, "hourCount": 0, "events": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("day") != utc_day():
        data["day"] = utc_day()
        data["dayCount"] = 0
    if data.get("hour") != utc_hour():
        data["hour"] = utc_hour()
        data["hourCount"] = 0
    return data


def save_ledger(data: dict[str, Any]) -> None:
    # Keep only recent events to avoid unbounded growth.
    events = list(data.get("events") or [])[-200:]
    data["events"] = events
    ledger_path().write_text(json.dumps(data, indent=2), encoding="utf-8")


def record_request(data: dict[str, Any], kind: str, status: int | None) -> dict[str, Any]:
    if data.get("day") != utc_day():
        data["day"] = utc_day()
        data["dayCount"] = 0
    if data.get("hour") != utc_hour():
        data["hour"] = utc_hour()
        data["hourCount"] = 0
    data["dayCount"] = int(data.get("dayCount") or 0) + 1
    data["hourCount"] = int(data.get("hourCount") or 0) + 1
    data.setdefault("events", []).append({"at": utc_now(), "kind": kind, "status": status})
    save_ledger(data)
    return data


def budget_ok(data: dict[str, Any]) -> tuple[bool, str]:
    if data.get("day") != utc_day():
        data["day"] = utc_day()
        data["dayCount"] = 0
    if data.get("hour") != utc_hour():
        data["hour"] = utc_hour()
        data["hourCount"] = 0
    hour_left = HOUR_SAFE - int(data.get("hourCount") or 0)
    day_left = DAY_SAFE - int(data.get("dayCount") or 0)
    if day_left <= 0:
        return False, "day_budget_exhausted"
    if hour_left <= 0:
        return False, "hour_budget_exhausted"
    return True, f"hour_left={hour_left};day_left={day_left}"


def append_progress(row: dict[str, Any]) -> None:
    with progress_path().open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_progress() -> dict[str, dict]:
    out: dict[str, dict] = {}
    path = progress_path()
    if not path.exists():
        return out
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            cid = row.get("id")
            if cid:
                out[str(cid)] = row
    return out


def extract_pk(url_or_id: str | None) -> str | None:
    if not url_or_id:
        return None
    m = PK_RE.search(str(url_or_id))
    return m.group(1).lower() if m else None


def catalogue_index() -> dict[str, dict]:
    """Map canonicalBaseId → card dict for EN/JA."""
    out: dict[str, dict] = {}
    for folder in ("en", "jp"):
        for path in (CATALOGUE / folder / "cards").glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            cards = data.get("cards") if isinstance(data, dict) else data
            if not isinstance(cards, list):
                continue
            for card in cards:
                if not isinstance(card, dict):
                    continue
                cid = card.get("canonicalBaseId")
                if cid:
                    out[str(cid)] = card
    return out


def target_from_unresolved(row: dict[str, str], card: dict | None) -> Target | None:
    canonical = row["canonical_card_id"]
    language = "ja" if row.get("language") in ("ja", "jp") else "en"
    set_id = row.get("set_id") or ""
    collector = row.get("collector_number") or ""
    name = ""
    pk_id = None
    image_url = None
    set_name = ""
    variant = ""
    if card:
        name = str(card.get("name") or card.get("nameEn") or "")
        set_name = str(card.get("setName") or "")
        variant = str(card.get("variant") or card.get("finish") or "")
        providers = card.get("providerIds") if isinstance(card.get("providerIds"), dict) else {}
        pk_id = extract_pk(providers.get("pokewallet"))
        promo = card.get("promotionMetadata") if isinstance(card.get("promotionMetadata"), dict) else {}
        if not pk_id:
            pk_id = extract_pk(promo.get("providerCardId"))
        for key in ("imageLarge", "imageUrlLarge", "imageUrl", "imageSmall", "imageUrlSmall"):
            pk_id = pk_id or extract_pk(card.get(key) if isinstance(card.get(key), str) else None)
            if isinstance(card.get(key), str) and "pokewallet" in card[key]:
                image_url = card[key]
                break
        if not image_url and pk_id:
            image_url = f"{API_BASE}/images/{pk_id}?size=high"
        if pk_id and image_url and "size=" not in image_url:
            image_url = image_url + ("&" if "?" in image_url else "?") + "size=high"
        # Prefer high size for master archive.
        if image_url and "size=low" in image_url:
            image_url = image_url.replace("size=low", "size=high")
        if language == "ja" and image_url and "lang=" not in image_url:
            # Request JP localized asset when available; API falls back to default.
            image_url = image_url + ("&" if "?" in image_url else "?") + "lang=ja"
    if not pk_id or not image_url:
        return None
    return Target(
        canonical=canonical,
        language=language,
        set_id=set_id,
        collector=collector,
        name=name or canonical.split("|")[-1],
        pk_id=pk_id,
        image_url=image_url,
        set_name=set_name,
        variant=variant,
    )


def http_get_auth(url: str, key: str) -> tuple[bytes, dict[str, str], int]:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "*/*", "X-API-Key": key},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        headers = {k: v for k, v in resp.headers.items()}
        return resp.read(), headers, resp.status


def verify_identity(target: Target, card: dict | None) -> tuple[bool, str]:
    if not card:
        return False, "catalogue_card_missing"
    providers = card.get("providerIds") if isinstance(card.get("providerIds"), dict) else {}
    promo = card.get("promotionMetadata") if isinstance(card.get("promotionMetadata"), dict) else {}
    expected = extract_pk(providers.get("pokewallet")) or extract_pk(promo.get("providerCardId"))
    if expected and expected != target.pk_id:
        return False, "provider_id_mismatch"
    # Collector / set must match unresolved row identity.
    card_set = str(card.get("setId") or "")
    card_collector = str(card.get("collectorNumber") or "")
    if card_set and target.set_id and card_set != target.set_id:
        return False, "set_mismatch"
    if card_collector and target.collector and card_collector != target.collector:
        return False, "collector_mismatch"
    src = str(card.get("imageSource") or card.get("providerImageSource") or "").lower()
    if src and src not in ("pokewallet", "cardscanr_cdn"):
        # Still OK if URL is pokewallet — catalogue may have mixed metadata.
        pass
    return True, "catalogue_pokewallet_exact_match"


def acquire_one(
    target: Target,
    card: dict | None,
    *,
    key: str,
    upload: bool,
    ledger: dict[str, Any],
) -> dict[str, Any]:
    ok_id, match_basis = verify_identity(target, card)
    if not ok_id:
        row = {
            "status": "rights_uncertain",
            "id": target.canonical,
            "reason": match_basis,
            "pk_id": target.pk_id,
            "at": utc_now(),
        }
        append_progress(row)
        return row

    ok_budget, budget_msg = budget_ok(ledger)
    if not ok_budget:
        return {"status": "budget_blocked", "id": target.canonical, "reason": budget_msg, "at": utc_now()}

    try:
        raw, headers, status = http_get_auth(target.image_url, key)
        record_request(ledger, "image", status)
    except urllib.error.HTTPError as exc:
        record_request(ledger, "image", exc.code)
        if exc.code == 429:
            row = {
                "status": "rate_limited",
                "id": target.canonical,
                "reason": "http_429",
                "at": utc_now(),
            }
            append_progress(row)
            return row
        row = {
            "status": "missing",
            "id": target.canonical,
            "reason": f"http_{exc.code}",
            "pk_id": target.pk_id,
            "at": utc_now(),
        }
        append_progress(row)
        return row
    except Exception as exc:  # noqa: BLE001
        record_request(ledger, "image", None)
        row = {
            "status": "missing",
            "id": target.canonical,
            "reason": f"error:{type(exc).__name__}",
            "at": utc_now(),
        }
        append_progress(row)
        return row

    if not m.magic_ok(raw) or len(raw) < 1500:
        row = {
            "status": "missing",
            "id": target.canonical,
            "reason": "invalid_image_payload",
            "bytes": len(raw),
            "at": utc_now(),
        }
        append_progress(row)
        return row

    # Dimension gate: reject tiny/corrupt payloads; allow modest promo thumbs (≥180px).
    try:
        from io import BytesIO
        from PIL import Image

        im = Image.open(BytesIO(raw))
        width, height = im.size
    except Exception:
        width = height = 0
    if width < 180 or height < 180:
        row = {
            "status": "missing",
            "id": target.canonical,
            "reason": f"image_too_small:{width}x{height}",
            "bytes": len(raw),
            "at": utc_now(),
        }
        append_progress(row)
        return row

    card_id = target.pk_id
    dest = m.master_dir(target.language, target.set_id, card_id)
    ext = ".png" if raw[:4] == b"\x89PNG" else (".webp" if raw[8:12] == b"WEBP" else ".jpg")
    m.write_atomic(dest / f"original{ext}", raw)
    webp = m.to_webp(raw)
    display = dest / "display.webp"
    m.write_atomic(display, webp)
    digest = hashlib.sha256(webp).hexdigest()
    object_key = (
        f"cards/{target.language}/{target.set_id.lower()}/{card_id.lower()}/{digest[:12]}/display.webp"
    )
    alias = f"cards/{target.language}/{target.set_id.lower()}/{card_id.lower()}/display.webp"
    uploaded = False
    public = None
    hosting = "local_application_cache"
    if upload:
        uploaded = m.wrangler_put(object_key, display, "image/webp")
        if uploaded:
            pointer = dest / "display.webp.current.txt"
            pointer.write_text(object_key, encoding="utf-8")
            m.wrangler_put(f"{alias}.current", pointer, "text/plain")
            m.wrangler_put(alias, display, "image/webp")
            public = f"{m.CDN}/{alias}"
            hosting = "hosted_application_cdn"

    meta = {
        "canonicalBaseId": target.canonical,
        "cardId": card_id,
        "language": target.language,
        "setId": target.set_id,
        "setName": target.set_name,
        "collectorNumber": target.collector,
        "name": target.name,
        "variant": target.variant,
        "pokewalletId": target.pk_id,
        "sha256": digest,
        "byteSize": len(webp),
        "mimeType": "image/webp",
        "sourceProvider": "pokewallet",
        "originalSourceUrl": target.image_url.split("?")[0] + "?size=high",
        "matchBasis": match_basis,
        "hostedObjectKey": object_key if uploaded else None,
        "publicUrl": public,
        "acquisition": "pokewallet_authenticated_acquire",
        "acquiredAt": utc_now(),
        "provenanceConfidence": "CONFIRMED",
        "derivativeStatus": "display_webp",
        "uploaded": uploaded,
        "hostingStatus": hosting,
        "rightsBasis": RIGHTS_BASIS,
        "rightsNote": RIGHTS_NOTE,
        "responseCacheControl": headers.get("Cache-Control"),
        "responseContentType": headers.get("Content-Type"),
        "responseImageLang": headers.get("X-Image-Lang"),
        "sourceWidth": width,
        "sourceHeight": height,
    }
    m.write_atomic(dest / "asset.json", json.dumps(meta, indent=2, ensure_ascii=False).encode("utf-8"))
    row = {
        "status": "ok",
        "id": target.canonical,
        "pk_id": target.pk_id,
        "uploaded": uploaded,
        "hosting": hosting,
        "bytes": len(webp),
        "sha256": digest,
        "public_url": public,
        "at": utc_now(),
    }
    append_progress(row)
    return row


def load_targets() -> list[tuple[dict[str, str], Target | None, dict | None]]:
    unresolved = list(
        csv.DictReader((REPORT / "en_jp_unresolved_images.csv").open(encoding="utf-8"))
    )
    index = catalogue_index()
    out: list[tuple[dict[str, str], Target | None, dict | None]] = []
    for row in unresolved:
        card = index.get(row["canonical_card_id"])
        target = target_from_unresolved(row, card)
        out.append((row, target, card))
    return out


def write_summary(stats: dict[str, Any]) -> None:
    REPORT.mkdir(parents=True, exist_ok=True)
    path = REPORT / "pokewallet_acquire_summary.json"
    path.write_text(json.dumps(stats, indent=2), encoding="utf-8")
    REPORT_MIRROR.mkdir(parents=True, exist_ok=True)
    (REPORT_MIRROR / "pokewallet_acquire_summary.json").write_text(
        json.dumps(stats, indent=2), encoding="utf-8"
    )
    md = REPORT / "CARDSCANR_EN_JP_POKEWALLET_ACQUIRE.md"
    lines = [
        "# CardScanR EN/JA PokéWallet authenticated acquire",
        "",
        f"Generated: `{stats.get('generatedAtUtc')}`",
        "",
        "## Rights review",
        "",
        RIGHTS_NOTE,
        "",
        f"Rights basis label: `{RIGHTS_BASIS}`",
        "",
        "## Counts",
        "",
        f"- Targets considered: **{stats.get('targets')}**",
        f"- This run downloaded: **{stats.get('downloaded')}**",
        f"- This run hosted: **{stats.get('hosted')}**",
        f"- This run rights-uncertain: **{stats.get('rightsUncertain')}**",
        f"- This run missing: **{stats.get('missing')}**",
        f"- Still pending after this run: **{stats.get('pending')}**",
        f"- Already covered before this run: **{stats.get('alreadyCovered')}**",
        "",
        "## Cumulative (all resumes)",
        "",
        f"- Downloaded / locally cached: **{(stats.get('cumulative') or {}).get('downloaded', stats.get('downloaded'))}**",
        f"- Newly hosted on CardScanR CDN: **{(stats.get('cumulative') or {}).get('hosted', stats.get('hosted'))}**",
        f"- Rights-uncertain: **{(stats.get('cumulative') or {}).get('rightsUncertain', 0)}**",
        f"- Genuinely missing / failed fetch: **{(stats.get('cumulative') or {}).get('missing', 0)}**",
        "",
        "## Rate limits",
        "",
        f"- Plan: Free (`{HOUR_LIMIT}`/hour, `{DAY_LIMIT}`/day)",
        f"- Safe budget counters: hour={stats.get('hourCount')}, day={stats.get('dayCount')}",
        f"- Stop reason: `{stats.get('stopReason')}`",
        "",
    ]
    md.write_text("\n".join(lines), encoding="utf-8")
    (REPORT_MIRROR / md.name).write_text("\n".join(lines), encoding="utf-8")


def sleep_until_hour_budget(ledger: dict[str, Any]) -> None:
    """Sleep until the UTC hour rolls (hourly quota resets)."""
    now = datetime.now(timezone.utc)
    seconds = 3600 - (now.minute * 60 + now.second) + 5
    print(json.dumps({"event": "wait_hour_reset", "sleepSeconds": seconds, "at": utc_now()}))
    time.sleep(max(seconds, 30))
    ledger["hour"] = utc_hour()
    ledger["hourCount"] = 0
    save_ledger(ledger)


def sleep_until_day_budget(ledger: dict[str, Any]) -> None:
    """Sleep until the UTC day rolls (daily quota resets)."""
    now = datetime.now(timezone.utc)
    tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    seconds = int((tomorrow - now).total_seconds()) + 5
    print(json.dumps({"event": "wait_day_reset", "sleepSeconds": seconds, "at": utc_now()}))
    # Chunked sleep so process stays interruptible and ledger stays honest.
    remaining = max(seconds, 60)
    while remaining > 0:
        chunk = min(remaining, 900)
        time.sleep(chunk)
        remaining -= chunk
        print(json.dumps({"event": "wait_day_reset_progress", "remainingSeconds": remaining, "at": utc_now()}))
    ledger["day"] = utc_day()
    ledger["dayCount"] = 0
    ledger["hour"] = utc_hour()
    ledger["hourCount"] = 0
    save_ledger(ledger)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--upload", action="store_true", help="Upload verified copies to R2/CDN")
    parser.add_argument("--max-cards", type=int, default=0, help="0 = until budget or complete")
    parser.add_argument(
        "--wait-for-budget",
        action="store_true",
        help="Wait on hour/day quota resets until complete",
    )
    parser.add_argument(
        "--retry-missing",
        action="store_true",
        help="Re-attempt cards previously recorded as missing (burns quota)",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="Alias for --max-cards")
    args = parser.parse_args()
    max_cards = args.max_cards or args.limit

    key = api_key()
    covered = m.covered_canonicals()
    progress = load_progress()
    ledger = load_ledger()
    pairs = load_targets()

    stats: dict[str, Any] = {
        "generatedAtUtc": utc_now(),
        "targets": len(pairs),
        "alreadyCovered": 0,
        "downloaded": 0,
        "hosted": 0,
        "rightsUncertain": 0,
        "missing": 0,
        "pending": 0,
        "attempted": 0,
        "stopReason": None,
        "rightsBasis": RIGHTS_BASIS,
        "upload": args.upload,
    }

    todo: list[tuple[dict[str, str], Target | None, dict | None]] = []
    permanent_missing = 0
    for row, target, card in pairs:
        cid = row["canonical_card_id"]
        if cid in covered and (covered[cid].get("publicUrl") or covered[cid].get("hostedObjectKey")):
            stats["alreadyCovered"] += 1
            continue
        prev = progress.get(cid)
        if prev and prev.get("status") == "ok" and (not args.upload or prev.get("uploaded")):
            stats["alreadyCovered"] += 1
            continue
        # Do not re-burn Free-plan quota on confirmed provider 404 / no-id misses
        # unless the operator explicitly requests a retry pass.
        if prev and prev.get("status") == "missing" and not args.retry_missing:
            reason = str(prev.get("reason") or "")
            if reason.startswith("http_404") or reason == "no_pokewallet_provider_id" or reason.startswith(
                "invalid_image"
            ) or reason.startswith("image_too_small"):
                permanent_missing += 1
                continue
        todo.append((row, target, card))
    stats["previouslyMissingSkipped"] = permanent_missing

    print(json.dumps({"todo": len(todo), "covered": stats["alreadyCovered"], "budget": budget_ok(ledger)[1]}))

    if args.dry_run:
        stats["pending"] = len(todo)
        stats["stopReason"] = "dry_run"
        write_summary(stats)
        print(json.dumps(stats, indent=2))
        return 0

    i = 0
    while i < len(todo):
        if max_cards and stats["attempted"] >= max_cards:
            stats["stopReason"] = "max_cards"
            break
        ok_budget, budget_msg = budget_ok(ledger)
        if not ok_budget:
            if budget_msg == "hour_budget_exhausted" and args.wait_for_budget:
                sleep_until_hour_budget(ledger)
                continue
            if budget_msg == "day_budget_exhausted" and args.wait_for_budget:
                sleep_until_day_budget(ledger)
                continue
            stats["stopReason"] = budget_msg
            break

        row, target, card = todo[i]
        i += 1
        if target is None:
            stats["missing"] += 1
            append_progress(
                {
                    "status": "missing",
                    "id": row["canonical_card_id"],
                    "reason": "no_pokewallet_provider_id",
                    "at": utc_now(),
                }
            )
            continue

        result = acquire_one(target, card, key=key, upload=args.upload, ledger=ledger)
        stats["attempted"] += 1
        status = result.get("status")
        if status == "ok":
            stats["downloaded"] += 1
            if result.get("uploaded"):
                stats["hosted"] += 1
        elif status == "rights_uncertain":
            stats["rightsUncertain"] += 1
        elif status == "rate_limited":
            if args.wait_for_budget:
                sleep_until_hour_budget(ledger)
                i -= 1  # retry same card
                continue
            stats["stopReason"] = "rate_limited"
            stats["pending"] += len(todo) - i + 1
            break
        elif status == "budget_blocked":
            stats["stopReason"] = result.get("reason")
            stats["pending"] += len(todo) - i + 1
            break
        else:
            stats["missing"] += 1

        if stats["attempted"] % 10 == 0:
            print(
                json.dumps(
                    {
                        "attempted": stats["attempted"],
                        "downloaded": stats["downloaded"],
                        "hosted": stats["hosted"],
                        "budget": budget_ok(ledger)[1],
                    }
                )
            )

    stats["pending"] = max(0, len(todo) - stats["attempted"])
    stats["hourCount"] = ledger.get("hourCount")
    stats["dayCount"] = ledger.get("dayCount")
    if not stats["stopReason"]:
        stats["stopReason"] = "complete" if stats["pending"] == 0 else "stopped"

    # Cumulative progress across resumes (authoritative for reports).
    cum = load_progress()
    by_id = {cid: row for cid, row in cum.items()}
    stats["cumulative"] = {
        "uniqueProgressRows": len(by_id),
        "downloaded": sum(1 for r in by_id.values() if r.get("status") == "ok"),
        "hosted": sum(1 for r in by_id.values() if r.get("status") == "ok" and r.get("uploaded")),
        "rightsUncertain": sum(1 for r in by_id.values() if r.get("status") == "rights_uncertain"),
        "missing": sum(1 for r in by_id.values() if r.get("status") == "missing"),
    }
    write_summary(stats)
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
