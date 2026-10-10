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
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import image_independence_multisource_resolver as m

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "reports" / "image_independence"
STATE = ROOT / "data" / "images" / "independence" / "pokewallet_acquire"
REPORT_MIRROR = Path(r"D:\CardScanR\reports\image_independence")
CATALOGUE = ROOT / "public" / "v1" / "catalog" / "pokemon"
LOCAL_ENV = ROOT / "pokewallet_env.local.json"
LOCAL_CREDS = ROOT / "config" / "provider_credentials.local.json"

UA = "CardScanR-PokewalletAcquire/1.0"
API_BASE = "https://api.pokewallet.io"
PK_RE = re.compile(r"(pk_[0-9a-f]+)", re.I)

# Free plan documented limits; keep headroom. Pro limits are detected live per key.
HOUR_LIMIT = 100
DAY_LIMIT = 1000
HOUR_SAFE = 90
DAY_SAFE = 900
PROGRESS_LOCK = threading.Lock()
LEDGER_LOCK = threading.Lock()
SLEEP_LOCK = threading.Lock()

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


@dataclass
class KeyAccount:
    """One PokéWallet API key with its own safe quota ledger."""

    key: str
    fingerprint: str
    hour_limit: int = HOUR_LIMIT
    day_limit: int = DAY_LIMIT
    hour_safe: int = HOUR_SAFE
    day_safe: int = DAY_SAFE
    ledger: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"key_{self.fingerprint}"


def key_fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def load_api_keys() -> list[str]:
    """Load API keys from env + gitignored local files. Never logs values."""
    keys: list[str] = []

    def add(value: str | None) -> None:
        text = (value or "").strip()
        if text and text not in keys:
            keys.append(text)

    add(os.environ.get("POKEWALLET_API_KEY"))
    multi = (os.environ.get("POKEWALLET_API_KEYS") or "").strip()
    if multi:
        for part in re.split(r"[\s,;]+", multi):
            add(part)

    for path in (LOCAL_ENV, LOCAL_CREDS):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict):
            if isinstance(data.get("apiKeys"), list):
                for item in data["apiKeys"]:
                    if isinstance(item, str):
                        add(item)
            pw = data.get("pokewallet")
            if isinstance(pw, dict):
                if isinstance(pw.get("apiKeys"), list):
                    for item in pw["apiKeys"]:
                        if isinstance(item, str):
                            add(item)
                add(pw.get("apiKey") if isinstance(pw.get("apiKey"), str) else None)
                add(pw.get("api_key") if isinstance(pw.get("api_key"), str) else None)

    if not keys:
        raise SystemExit(
            "No PokéWallet API keys configured "
            "(POKEWALLET_API_KEY / POKEWALLET_API_KEYS / pokewallet_env.local.json)"
        )
    return keys


def api_key() -> str:
    """Back-compat: first configured key."""
    return load_api_keys()[0]


def probe_key_limits(key: str) -> tuple[int, int, int, int]:
    """Return (hour_limit, hour_remaining, day_limit, day_remaining)."""
    req = urllib.request.Request(
        f"{API_BASE}/sets",
        headers={"User-Agent": UA, "Accept": "application/json", "X-API-Key": key},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        h_lim = int(resp.headers.get("X-RateLimit-Limit-Hour") or HOUR_LIMIT)
        h_rem = int(resp.headers.get("X-RateLimit-Remaining-Hour") or h_lim)
        d_lim = int(resp.headers.get("X-RateLimit-Limit-Day") or DAY_LIMIT)
        d_rem = int(resp.headers.get("X-RateLimit-Remaining-Day") or d_lim)
        return h_lim, h_rem, d_lim, d_rem


def build_accounts(keys: list[str]) -> list[KeyAccount]:
    accounts: list[KeyAccount] = []
    for key in keys:
        fp = key_fingerprint(key)
        try:
            h_lim, h_rem, d_lim, d_rem = probe_key_limits(key)
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"event": "key_probe_failed", "fp": fp, "error": type(exc).__name__}))
            continue
        # Keep ~10% headroom under published limits.
        hour_safe = max(1, int(h_lim * 0.9))
        day_safe = max(1, int(d_lim * 0.9))
        # Align local counters with live remaining when possible.
        hour_used = max(0, h_lim - h_rem)
        day_used = max(0, d_lim - d_rem)
        acct = KeyAccount(
            key=key,
            fingerprint=fp,
            hour_limit=h_lim,
            day_limit=d_lim,
            hour_safe=hour_safe,
            day_safe=day_safe,
        )
        ledger = load_ledger(fp)
        # Prefer the higher of local count vs live-implied usage for safety.
        ledger["hourCount"] = max(int(ledger.get("hourCount") or 0), hour_used)
        ledger["dayCount"] = max(int(ledger.get("dayCount") or 0), day_used)
        ledger["hourLimit"] = h_lim
        ledger["dayLimit"] = d_lim
        ledger["hourSafe"] = hour_safe
        ledger["daySafe"] = day_safe
        save_ledger(fp, ledger)
        acct.ledger = ledger
        accounts.append(acct)
        print(
            json.dumps(
                {
                    "event": "key_ready",
                    "fp": fp,
                    "hourLimit": h_lim,
                    "hourRemaining": h_rem,
                    "dayLimit": d_lim,
                    "dayRemaining": d_rem,
                    "hourSafe": hour_safe,
                    "daySafe": day_safe,
                }
            )
        )
    if not accounts:
        raise SystemExit("No usable PokéWallet API keys after probe")
    return accounts


def ledger_path(fingerprint: str | None = None) -> Path:
    STATE.mkdir(parents=True, exist_ok=True)
    if fingerprint:
        return STATE / f"request_ledger_{fingerprint}.json"
    return STATE / "request_ledger.json"


def progress_path() -> Path:
    STATE.mkdir(parents=True, exist_ok=True)
    return STATE / "progress.jsonl"


def load_ledger(fingerprint: str | None = None) -> dict[str, Any]:
    path = ledger_path(fingerprint)
    # Migrate legacy single-ledger onto the first fingerprint if present.
    if fingerprint and not path.exists() and ledger_path(None).exists() and fingerprint == "legacy":
        path = ledger_path(None)
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


def save_ledger(fingerprint: str | None, data: dict[str, Any]) -> None:
    events = list(data.get("events") or [])[-200:]
    data["events"] = events
    ledger_path(fingerprint).write_text(json.dumps(data, indent=2), encoding="utf-8")


def record_request(
    data: dict[str, Any],
    kind: str,
    status: int | None,
    *,
    fingerprint: str | None = None,
    hour_safe: int | None = None,
    day_safe: int | None = None,
) -> dict[str, Any]:
    with LEDGER_LOCK:
        if data.get("day") != utc_day():
            data["day"] = utc_day()
            data["dayCount"] = 0
        if data.get("hour") != utc_hour():
            data["hour"] = utc_hour()
            data["hourCount"] = 0
        data["dayCount"] = int(data.get("dayCount") or 0) + 1
        data["hourCount"] = int(data.get("hourCount") or 0) + 1
        if hour_safe is not None:
            data["hourSafe"] = hour_safe
        if day_safe is not None:
            data["daySafe"] = day_safe
        data.setdefault("events", []).append({"at": utc_now(), "kind": kind, "status": status})
        save_ledger(fingerprint, data)
    return data


def budget_ok(
    data: dict[str, Any],
    *,
    hour_safe: int | None = None,
    day_safe: int | None = None,
) -> tuple[bool, str]:
    if data.get("day") != utc_day():
        data["day"] = utc_day()
        data["dayCount"] = 0
    if data.get("hour") != utc_hour():
        data["hour"] = utc_hour()
        data["hourCount"] = 0
    hs = int(hour_safe if hour_safe is not None else data.get("hourSafe") or HOUR_SAFE)
    ds = int(day_safe if day_safe is not None else data.get("daySafe") or DAY_SAFE)
    hour_left = hs - int(data.get("hourCount") or 0)
    day_left = ds - int(data.get("dayCount") or 0)
    if day_left <= 0:
        return False, "day_budget_exhausted"
    if hour_left <= 0:
        return False, "hour_budget_exhausted"
    return True, f"hour_left={hour_left};day_left={day_left}"


def append_progress(row: dict[str, Any]) -> None:
    with PROGRESS_LOCK:
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
    fingerprint: str | None = None,
    hour_safe: int | None = None,
    day_safe: int | None = None,
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

    ok_budget, budget_msg = budget_ok(ledger, hour_safe=hour_safe, day_safe=day_safe)
    if not ok_budget:
        return {"status": "budget_blocked", "id": target.canonical, "reason": budget_msg, "at": utc_now()}

    try:
        raw, headers, status = http_get_auth(target.image_url, key)
        record_request(
            ledger,
            "image",
            status,
            fingerprint=fingerprint,
            hour_safe=hour_safe,
            day_safe=day_safe,
        )
    except urllib.error.HTTPError as exc:
        record_request(
            ledger,
            "image",
            exc.code,
            fingerprint=fingerprint,
            hour_safe=hour_safe,
            day_safe=day_safe,
        )
        if exc.code == 429:
            row = {
                "status": "rate_limited",
                "id": target.canonical,
                "reason": "http_429",
                "key_fp": fingerprint,
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
        record_request(
            ledger,
            "image",
            None,
            fingerprint=fingerprint,
            hour_safe=hour_safe,
            day_safe=day_safe,
        )
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


def sleep_until_hour_budget(accounts: list[KeyAccount]) -> None:
    """Sleep until the UTC hour rolls (hourly quota resets)."""
    with SLEEP_LOCK:
        # Re-check after lock — another worker may have already waited.
        acct, reason = pick_account(accounts)
        if acct is not None or reason != "hour_budget_exhausted":
            return
        now = datetime.now(timezone.utc)
        seconds = 3600 - (now.minute * 60 + now.second) + 5
        print(json.dumps({"event": "wait_hour_reset", "sleepSeconds": seconds, "at": utc_now()}))
        time.sleep(max(seconds, 30))
        with LEDGER_LOCK:
            for acct in accounts:
                acct.ledger["hour"] = utc_hour()
                acct.ledger["hourCount"] = 0
                save_ledger(acct.fingerprint, acct.ledger)


def sleep_until_day_budget(accounts: list[KeyAccount]) -> None:
    """Sleep until the UTC day rolls (daily quota resets)."""
    with SLEEP_LOCK:
        acct, reason = pick_account(accounts)
        if acct is not None or reason != "day_budget_exhausted":
            return
        now = datetime.now(timezone.utc)
        tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        seconds = int((tomorrow - now).total_seconds()) + 5
        print(json.dumps({"event": "wait_day_reset", "sleepSeconds": seconds, "at": utc_now()}))
        remaining = max(seconds, 60)
        while remaining > 0:
            chunk = min(remaining, 900)
            time.sleep(chunk)
            remaining -= chunk
            print(
                json.dumps(
                    {"event": "wait_day_reset_progress", "remainingSeconds": remaining, "at": utc_now()}
                )
            )
        with LEDGER_LOCK:
            for acct in accounts:
                acct.ledger["day"] = utc_day()
                acct.ledger["dayCount"] = 0
                acct.ledger["hour"] = utc_hour()
                acct.ledger["hourCount"] = 0
                save_ledger(acct.fingerprint, acct.ledger)


def pick_account(accounts: list[KeyAccount]) -> tuple[KeyAccount | None, str]:
    """Choose the account with the most remaining safe day budget."""
    best: KeyAccount | None = None
    best_day_left = -1
    any_hour_blocked = False
    any_day_blocked = False
    for acct in accounts:
        ok, reason = budget_ok(acct.ledger, hour_safe=acct.hour_safe, day_safe=acct.day_safe)
        if not ok:
            if reason == "hour_budget_exhausted":
                any_hour_blocked = True
            if reason == "day_budget_exhausted":
                any_day_blocked = True
            continue
        day_left = acct.day_safe - int(acct.ledger.get("dayCount") or 0)
        if day_left > best_day_left:
            best = acct
            best_day_left = day_left
    if best:
        return best, budget_ok(best.ledger, hour_safe=best.hour_safe, day_safe=best.day_safe)[1]
    if any_hour_blocked and not all(
        budget_ok(a.ledger, hour_safe=a.hour_safe, day_safe=a.day_safe)[1] == "day_budget_exhausted"
        for a in accounts
    ):
        return None, "hour_budget_exhausted"
    if any_day_blocked:
        return None, "day_budget_exhausted"
    return None, "no_budget"


def aggregate_budget(accounts: list[KeyAccount]) -> dict[str, Any]:
    hour_left = 0
    day_left = 0
    for acct in accounts:
        ok, _ = budget_ok(acct.ledger, hour_safe=acct.hour_safe, day_safe=acct.day_safe)
        hl = max(0, acct.hour_safe - int(acct.ledger.get("hourCount") or 0))
        dl = max(0, acct.day_safe - int(acct.ledger.get("dayCount") or 0))
        if ok:
            hour_left += hl
            day_left += dl
    return {
        "keys": len(accounts),
        "hour_left_total": hour_left,
        "day_left_total": day_left,
        "per_key": [
            {
                "fp": a.fingerprint,
                "hourLimit": a.hour_limit,
                "dayLimit": a.day_limit,
                "budget": budget_ok(a.ledger, hour_safe=a.hour_safe, day_safe=a.day_safe)[1],
            }
            for a in accounts
        ],
    }


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
    parser.add_argument(
        "--concurrency",
        type=int,
        default=0,
        help="Parallel workers (default = number of API keys)",
    )
    args = parser.parse_args()
    max_cards = args.max_cards or args.limit

    keys = load_api_keys()
    accounts = build_accounts(keys)
    workers = args.concurrency or len(accounts)
    covered = m.covered_canonicals()
    progress = load_progress()
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
        "keyCount": len(accounts),
    }
    stats_lock = threading.Lock()

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
        if prev and prev.get("status") == "missing" and not args.retry_missing:
            reason = str(prev.get("reason") or "")
            if reason.startswith("http_404") or reason == "no_pokewallet_provider_id" or reason.startswith(
                "invalid_image"
            ) or reason.startswith("image_too_small"):
                permanent_missing += 1
                continue
        todo.append((row, target, card))
    stats["previouslyMissingSkipped"] = permanent_missing

    print(
        json.dumps(
            {
                "todo": len(todo),
                "covered": stats["alreadyCovered"],
                "budget": aggregate_budget(accounts),
                "workers": workers,
            }
        )
    )

    if args.dry_run:
        stats["pending"] = len(todo)
        stats["stopReason"] = "dry_run"
        write_summary(stats)
        print(json.dumps(stats, indent=2))
        return 0

    work: queue.Queue[tuple[dict[str, str], Target | None, dict | None]] = queue.Queue()
    for item in todo:
        work.put(item)

    stop_flag = threading.Event()

    def worker() -> None:
        while not stop_flag.is_set():
            if max_cards:
                with stats_lock:
                    if stats["attempted"] >= max_cards:
                        return
            try:
                item = work.get(timeout=1.0)
            except queue.Empty:
                return
            row, target, card = item

            # Wait until at least one key has budget (or stop).
            while not stop_flag.is_set():
                acct, budget_msg = pick_account(accounts)
                if acct is not None:
                    break
                if budget_msg == "hour_budget_exhausted" and args.wait_for_budget:
                    sleep_until_hour_budget(accounts)
                    continue
                if budget_msg == "day_budget_exhausted" and args.wait_for_budget:
                    sleep_until_day_budget(accounts)
                    continue
                with stats_lock:
                    stats["stopReason"] = budget_msg
                stop_flag.set()
                work.put(item)
                return
            else:
                work.put(item)
                return

            if target is None:
                with stats_lock:
                    stats["missing"] += 1
                    stats["attempted"] += 1
                append_progress(
                    {
                        "status": "missing",
                        "id": row["canonical_card_id"],
                        "reason": "no_pokewallet_provider_id",
                        "at": utc_now(),
                    }
                )
                continue

            result = acquire_one(
                target,
                card,
                key=acct.key,
                upload=args.upload,
                ledger=acct.ledger,
                fingerprint=acct.fingerprint,
                hour_safe=acct.hour_safe,
                day_safe=acct.day_safe,
            )
            status = result.get("status")
            if status == "budget_blocked" or status == "rate_limited":
                work.put(item)
                if status == "rate_limited" and args.wait_for_budget:
                    sleep_until_hour_budget(accounts)
                elif status == "rate_limited":
                    with stats_lock:
                        stats["stopReason"] = "rate_limited"
                    stop_flag.set()
                    return
                continue

            with stats_lock:
                stats["attempted"] += 1
                if status == "ok":
                    stats["downloaded"] += 1
                    if result.get("uploaded"):
                        stats["hosted"] += 1
                elif status == "rights_uncertain":
                    stats["rightsUncertain"] += 1
                else:
                    stats["missing"] += 1
                if max_cards and stats["attempted"] >= max_cards:
                    stats["stopReason"] = "max_cards"
                    stop_flag.set()
                if stats["attempted"] % 10 == 0:
                    print(
                        json.dumps(
                            {
                                "attempted": stats["attempted"],
                                "downloaded": stats["downloaded"],
                                "hosted": stats["hosted"],
                                "budget": aggregate_budget(accounts),
                            }
                        )
                    )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(worker) for _ in range(workers)]
        for fut in as_completed(futs):
            fut.result()

    stats["pending"] = work.qsize()
    stats["hourCount"] = sum(int(a.ledger.get("hourCount") or 0) for a in accounts)
    stats["dayCount"] = sum(int(a.ledger.get("dayCount") or 0) for a in accounts)
    if not stats["stopReason"]:
        stats["stopReason"] = "complete" if stats["pending"] == 0 else "stopped"

    cum = load_progress()
    by_id = {cid: row for cid, row in cum.items()}
    stats["cumulative"] = {
        "uniqueProgressRows": len(by_id),
        "downloaded": sum(1 for r in by_id.values() if r.get("status") == "ok"),
        "hosted": sum(1 for r in by_id.values() if r.get("status") == "ok" and r.get("uploaded")),
        "rightsUncertain": sum(1 for r in by_id.values() if r.get("status") == "rights_uncertain"),
        "missing": sum(1 for r in by_id.values() if r.get("status") == "missing"),
    }
    stats["budget"] = aggregate_budget(accounts)
    write_summary(stats)
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
