#!/usr/bin/env python3
"""Pass 5: resumable multi-source discovery for remaining EN/JA unresolved images.

Auto-acquire/rehost only for hosts already permitted in CardScanR independence
ingestion (TCGdex, pokemontcg.io, Scrydex written auth), including Wayback
snapshots of those hosts. All other discoveries go to rights-review queues.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import image_independence_multisource_resolver as m

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "reports" / "image_independence"
STATE = ROOT / "data" / "images" / "independence" / "pass5"
REPORT_MIRROR = Path(r"D:\CardScanR\reports\image_independence")

UA = "CardScanR-ImageIndependence-Discovery/1.0"
HTTP_SEM = threading.Semaphore(10)
LOCK = threading.Lock()

PERMITTED_ACQUIRE_HOSTS = {
    "images.pokemontcg.io": "pokemon_tcg_api",
    "assets.tcgdex.net": "tcgdex",
    "images.scrydex.com": "scrydex",
}

RIGHTS_REVIEW_HOSTS = {
    "www.pokemon-card.com": "official_jp_metadata_only",
    "pokemon-card.com": "official_jp_metadata_only",
    "www.serebii.net": "fan_site_rights_review",
    "serebii.net": "fan_site_rights_review",
    "bulbapedia.bulbagarden.net": "metadata_only",
    "archives.bulbagarden.net": "fan_archive_rights_review",
    "www.pokellector.com": "pokellector_watermark_evidence_only",
    "den-media.pokellector.com": "pokellector_watermark_evidence_only",
    "limitlesstcg.com": "community_site_rights_review",
    "limitlesstcg.nyc3.cdn.digitaloceanspaces.com": "community_cdn_rights_review",
    "pkmncards.com": "fan_site_rights_review",
    "api.pokewallet.io": "pokewallet_authenticated_acquire_path",
}

SCRYDEX_PLACEHOLDER = m.SCRYDEX_MISSING_IMAGE_SHA256
WAYBACK_ORIG_RE = re.compile(r"web\.archive\.org/web/\d+(?:id_)?/(https?://.+)$")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def host_of(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ""


def http_get(url: str, *, timeout: int = 20, max_retries: int = 2) -> bytes:
    last: Exception | None = None
    for i in range(max_retries):
        try:
            with HTTP_SEM:
                req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = resp.read()
            if not data:
                raise RuntimeError("empty")
            return data
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(min(1.5**i, 4))
    assert last is not None
    raise last


def http_get_text(url: str, **kwargs: Any) -> str:
    return http_get(url, **kwargs).decode("utf-8", errors="replace")


@dataclass
class CardTarget:
    canonical: str
    language: str
    set_id: str
    set_name: str
    collector: str
    name: str
    failure_reason: str
    catalogue_urls: list[str] = field(default_factory=list)
    external: dict[str, Any] = field(default_factory=dict)


@dataclass
class Candidate:
    url: str
    source: str
    page_url: str | None
    rights_status: str
    match_evidence: str
    auto_acquire: bool
    query: str | None = None


def load_targets() -> list[CardTarget]:
    queue_path = REPORT / "en_jp_manual_acquisition_queue.csv"
    unresolved_path = REPORT / "en_jp_unresolved_images.csv"
    rows = list(csv.DictReader(queue_path.open(encoding="utf-8")))
    by_id: dict[str, dict] = {}
    for folder in ("en", "jp"):
        for path in (ROOT / "public/v1/catalog/pokemon" / folder / "cards").glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            for raw in (data.get("cards") if isinstance(data, dict) else data) or []:
                if not isinstance(raw, dict):
                    continue
                cid = raw.get("canonicalBaseId") or raw.get("id")
                if not cid:
                    continue
                urls: list[str] = []
                for key in ("imageUrl", "image"):
                    val = raw.get(key)
                    if isinstance(val, str) and val.startswith("http"):
                        urls.append(val)
                images = raw.get("images")
                if isinstance(images, dict):
                    urls.extend(str(v) for v in images.values() if isinstance(v, str) and v.startswith("http"))
                by_id[str(cid)] = {
                    "urls": list(dict.fromkeys(urls)),
                    "external": raw.get("externalIds") or raw.get("external") or {},
                    "name": raw.get("name") or "",
                    "setName": raw.get("setName") or "",
                }
    order: list[str] = []
    if unresolved_path.exists():
        order = [r["canonical_card_id"] for r in csv.DictReader(unresolved_path.open(encoding="utf-8"))]
    by_canon = {r["canonical_card_id"]: r for r in rows}
    ordered_ids = [c for c in order if c in by_canon] + [c for c in by_canon if c not in set(order)]
    out: list[CardTarget] = []
    for cid in ordered_ids:
        r = by_canon[cid]
        meta = by_id.get(cid, {})
        out.append(
            CardTarget(
                canonical=cid,
                language=r.get("language") or "",
                set_id=r.get("set_id") or "",
                set_name=r.get("set_name") or meta.get("setName") or "",
                collector=r.get("collector_number") or "",
                name=r.get("card_name") or meta.get("name") or "",
                failure_reason=r.get("failure_reason") or "",
                catalogue_urls=list(meta.get("urls") or []),
                external=dict(meta.get("external") or {}),
            )
        )
    return out


def build_queries(card: CardTarget) -> list[str]:
    name = (card.name or "").strip()
    set_name = (card.set_name or "").strip()
    collector = (card.collector or "").strip()
    lang = "Japanese" if card.language == "ja" else "English"
    num = collector.split("/", 1)[0].strip()
    queries = [
        f"{name} {set_name} {collector} pokemon card {lang}",
        f"{name} {set_name} {num} pokemon tcg",
        f'"{name}" "{set_name}" pokemon card',
    ]
    if card.language == "ja":
        queries.append(f"{name} {collector} {set_name} ポケモンカード")
    stamp = re.match(r"^([A-Za-z]{2,8})\s+(\d{1,4})", collector)
    if stamp:
        queries.append(f"{name} {stamp.group(1)} {stamp.group(2)} pokemon card")
    clean = re.sub(r"\s+\d+$", "", name)
    clean = re.sub(r"\s+(Winner|winner|League|Promo)$", "", clean).strip()
    if clean and clean != name:
        queries.append(f"{clean} {set_name} pokemon card")
    seen: set[str] = set()
    out: list[str] = []
    for q in queries:
        q = re.sub(r"\s+", " ", q).strip()
        key = q.lower()
        if q and key not in seen:
            seen.add(key)
            out.append(q)
    return out


def classify_url(url: str) -> tuple[str, bool]:
    h = host_of(url)
    if h in PERMITTED_ACQUIRE_HOSTS:
        return ("approved_independence_ingestion", True)
    if h == "web.archive.org":
        match = WAYBACK_ORIG_RE.search(url)
        if match:
            orig_host = host_of(match.group(1))
            if orig_host in PERMITTED_ACQUIRE_HOSTS:
                return ("wayback_of_permitted_host", True)
            if orig_host == "api.pokewallet.io":
                return ("wayback_of_pokewallet_prefer_live_auth_acquire", False)
        return ("wayback_unknown_original_rights_review", False)
    if h in RIGHTS_REVIEW_HOSTS:
        return (RIGHTS_REVIEW_HOSTS[h], False)
    return ("unknown_host_rights_review", False)


def wayback_available(url: str, limit: int = 3) -> list[str]:
    cdx = "https://web.archive.org/cdx/search/cdx?" + urllib.parse.urlencode(
        {
            "url": url,
            "output": "json",
            "fl": "timestamp,original,statuscode,mimetype",
            "filter": "statuscode:200",
            "limit": str(limit),
        }
    )
    try:
        rows = json.loads(http_get_text(cdx, timeout=40))
    except Exception:
        return []
    out: list[str] = []
    for row in rows[1:]:
        if len(row) < 2:
            continue
        out.append(f"https://web.archive.org/web/{row[0]}id_/{row[1]}")
    return out


def discover_catalogue_and_lightweight_permitted(card: CardTarget) -> list[Candidate]:
    """Catalogue + lightweight permitted CDN guesses.

    Full TCGdex set re-resolution was exhausted in pass4 for these leftovers;
    avoid re-paying that latency. Still try stamp/promo/external-id CDN forms
    and Wayback for non-PokéWallet catalogue URLs.
    """
    cands: list[Candidate] = []
    for url in card.catalogue_urls:
        if host_of(url) == "api.pokewallet.io":
            cands.append(
                Candidate(
                    url=url,
                    source="catalogue_pokewallet",
                    page_url=None,
                    rights_status="pokewallet_authenticated_acquire_path",
                    match_evidence="catalogue_url_use_authenticated_acquire_tool",
                    auto_acquire=False,
                    query=url,
                )
            )
            continue
        rights, auto = classify_url(url)
        cands.append(
            Candidate(
                url=url,
                source="catalogue_url",
                page_url=None,
                rights_status=rights,
                match_evidence="catalogue_display_url",
                auto_acquire=auto,
                query=url,
            )
        )
        for arch in wayback_available(url, limit=2):
            r2, a2 = classify_url(arch)
            cands.append(
                Candidate(
                    url=arch,
                    source="wayback_cdx",
                    page_url=f"https://web.archive.org/web/*/{url}",
                    rights_status=r2,
                    match_evidence="catalogue_url_wayback_snapshot",
                    auto_acquire=a2,
                    query=url,
                )
            )

    # Lightweight permitted CDN constructions (no TCGdex set list fetch).
    nums = m.collector_candidates(card.collector)
    stamp_prefix, stamp_num = m.parse_stamp_collector(card.collector)
    promo_set, promo_num = m.parse_promo_collector(card.collector)
    set_keys: list[tuple[str, str]] = []
    if stamp_prefix and stamp_num:
        set_keys.append((m.STAMP_PREFIX_TO_POKEMONTCG.get(stamp_prefix, stamp_prefix), "stamp_ptcg"))
        set_keys.append((m.STAMP_PREFIX_TO_TCGDEX.get(stamp_prefix, stamp_prefix), "stamp_tcgdex"))
    if promo_set and promo_num:
        set_keys.append((promo_set, "promo_prefix"))
    for ext_key in ("pokemonTcgApiId", "tcgdexCardId"):
        pid = card.external.get(ext_key)
        if pid:
            for c in m.scrydex_urls_for_ids([str(pid)]):
                rights, auto = classify_url(c.url)
                cands.append(
                    Candidate(
                        url=c.url,
                        source="external_id_scrydex",
                        page_url=None,
                        rights_status=rights,
                        match_evidence=f"external:{ext_key}",
                        auto_acquire=auto,
                    )
                )
    for set_key, basis in set_keys:
        for num in nums[:3]:
            if not re.fullmatch(r"\d+[a-zA-Z]?", num):
                continue
            for suffix in (f"{num}_hires.png", f"{num}.png"):
                url = f"https://images.pokemontcg.io/{set_key}/{suffix}"
                rights, auto = classify_url(url)
                cands.append(
                    Candidate(
                        url=url,
                        source="lightweight_pokemontcg",
                        page_url=None,
                        rights_status=rights,
                        match_evidence=basis,
                        auto_acquire=auto,
                    )
                )
            for c in m.scrydex_urls_for_ids([f"{set_key}-{num}", f"{set_key.upper()}-{num}"]):
                rights, auto = classify_url(c.url)
                cands.append(
                    Candidate(
                        url=c.url,
                        source="lightweight_scrydex",
                        page_url=None,
                        rights_status=rights,
                        match_evidence=basis,
                        auto_acquire=auto,
                    )
                )
            break
    return [c for c in cands if c.url]


def discover_limitless(card: CardTarget) -> list[Candidate]:
    # Limitless catalogue coverage is EN tournament cards; skip JA leftovers.
    if card.language != "en":
        return []
    q = f"{card.name} {card.set_name}".strip()
    if not q:
        return []
    url = "https://limitlesstcg.com/cards?" + urllib.parse.urlencode({"q": q})
    try:
        html = http_get_text(url, timeout=8, max_retries=1)
    except Exception:
        return []
    cands: list[Candidate] = []
    # Accept only concrete card detail paths like /cards/svp/175 or /cards/swsh/123
    for href in re.findall(r'href="(/cards/[a-z0-9-]+/\d+[a-z]?[^"]*)"', html, re.I)[:8]:
        if any(bad in href.lower() for bad in ("/advanced", "/syntax", "/search", "/sets")):
            continue
        page = urllib.parse.urljoin("https://limitlesstcg.com", href)
        cands.append(
            Candidate(
                url=page,
                source="limitless_search",
                page_url=page,
                rights_status="community_site_rights_review",
                match_evidence=f"search_query:{q}",
                auto_acquire=False,
                query=q,
            )
        )
    for img in re.findall(r"(https://limitlesstcg[^\s\"']+\.(?:png|jpg|webp))", html, re.I)[:5]:
        cands.append(
            Candidate(
                url=img,
                source="limitless_image",
                page_url=url,
                rights_status="community_cdn_rights_review",
                match_evidence=f"search_query:{q}",
                auto_acquire=False,
                query=q,
            )
        )
    return cands


def discover_serebii(card: CardTarget) -> list[Candidate]:
    if card.language != "en":
        return []
    slug = re.sub(r"[^a-z0-9]+", "", (card.set_name or "").lower())
    if not slug or len(slug) < 4:
        return []
    num = re.search(r"(\d+)", card.collector or "")
    if not num:
        return []
    n = int(num.group(1))
    page = f"https://www.serebii.net/card/{slug}/{n:03d}.shtml"
    try:
        html = http_get_text(page, timeout=10, max_retries=1)
    except Exception:
        return []
    cands: list[Candidate] = []
    for rel in re.findall(r'(/card/[^"\']+\.(?:png|jpg|jpeg|webp))', html, re.I)[:6]:
        img_url = urllib.parse.urljoin("https://www.serebii.net", rel)
        cands.append(
            Candidate(
                url=img_url,
                source="serebii",
                page_url=page,
                rights_status="fan_site_rights_review",
                match_evidence=f"serebii_page_setslug:{slug}:{n:03d}",
                auto_acquire=False,
            )
        )
    if not cands and "card" in html.lower():
        cands.append(
            Candidate(
                url=page,
                source="serebii_page",
                page_url=page,
                rights_status="fan_site_rights_review",
                match_evidence=f"serebii_page_exists:{slug}:{n:03d}",
                auto_acquire=False,
            )
        )
    return cands


def discover_official_jp(card: CardTarget) -> list[Candidate]:
    if card.language != "ja":
        return []
    keyword = (card.name or "").strip()
    if not keyword:
        return []
    url = "https://www.pokemon-card.com/card-search/index.php?" + urllib.parse.urlencode(
        {"keyword": keyword, "se_ta": "", "regulation_sidebar_form": "all"}
    )
    try:
        html = http_get_text(url, timeout=12, max_retries=1)
    except Exception:
        return []
    cands: list[Candidate] = []
    for card_id in re.findall(r"/card-search/details\.php/card/(\d+)", html)[:8]:
        page = f"https://www.pokemon-card.com/card-search/details.php/card/{card_id}"
        cands.append(
            Candidate(
                url=page,
                source="pokemon_card_com_search",
                page_url=page,
                rights_status="official_jp_metadata_only",
                match_evidence=f"keyword:{keyword}",
                auto_acquire=False,
                query=keyword,
            )
        )
    for rel in re.findall(r"(/assets/images/card_images/[^\"'\s]+\.(?:jpg|png|webp))", html, re.I)[:8]:
        img = urllib.parse.urljoin("https://www.pokemon-card.com", rel)
        cands.append(
            Candidate(
                url=img,
                source="pokemon_card_com_image",
                page_url=url,
                rights_status="official_jp_metadata_only",
                match_evidence=f"keyword:{keyword}",
                auto_acquire=False,
                query=keyword,
            )
        )
    return cands


def discover_duckduckgo_html(card: CardTarget) -> list[Candidate]:
    cands: list[Candidate] = []
    for q in build_queries(card)[:2]:
        url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode(
            {"q": q + " pokemon card"}
        )
        try:
            html = http_get_text(url, timeout=10, max_retries=1)
        except Exception:
            continue
        for enc in re.findall(r"uddg=([^&\"']+)", html)[:12]:
            target = urllib.parse.unquote(enc)
            if not target.startswith("http"):
                continue
            rights, _auto = classify_url(target)
            cands.append(
                Candidate(
                    url=target,
                    source="duckduckgo_html",
                    page_url=url,
                    rights_status=rights,
                    match_evidence=f"query:{q}",
                    auto_acquire=False,
                    query=q,
                )
            )
        time.sleep(0.35)
    return cands


def load_done() -> set[str]:
    path = STATE / "discovery_progress.jsonl"
    done: set[str] = set()
    if not path.exists():
        return done
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("canonical") and row.get("status") in {"searched", "acquired", "unresolved", "already_hosted"}:
            done.add(str(row["canonical"]))
    return done


def append_progress(row: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    path = STATE / "discovery_progress.jsonl"
    with LOCK:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def dedupe_candidates(cands: list[Candidate]) -> list[Candidate]:
    seen: set[str] = set()
    out: list[Candidate] = []
    for c in cands:
        key = c.url.split("?")[0].lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def try_acquire(card: CardTarget, cand: Candidate) -> dict | None:
    if not cand.auto_acquire:
        return None
    url = cand.url
    try:
        raw = http_get(url, timeout=60)
    except Exception as exc:  # noqa: BLE001
        return {"status": "fetch_fail", "error": f"{type(exc).__name__}:{exc}", "url": url}
    if sha256(raw) == SCRYDEX_PLACEHOLDER:
        return {"status": "placeholder", "url": url}
    if not m.magic_ok(raw):
        return {"status": "invalid_payload", "url": url}

    row = m.CardRow(
        canonical=card.canonical,
        language="ja" if card.language == "ja" else "en",
        folder="jp" if card.language == "ja" else "en",
        set_id=card.set_id,
        set_name=card.set_name,
        collector=card.collector,
        name=card.name,
        urls=[url],
        external=card.external,
    )
    card_id = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{card.set_id}-{card.collector}")[:80]
    dest = m.master_dir(row.language, card.set_id, card_id)
    if raw[:4] == b"\x89PNG":
        ext = ".png"
    elif len(raw) > 12 and raw[8:12] == b"WEBP":
        ext = ".webp"
    elif raw[:2] == b"\xff\xd8":
        ext = ".jpg"
    else:
        ext = ".bin"
    m.write_atomic(dest / f"original{ext}", raw)
    webp = m.to_webp(raw)
    display = dest / "display.webp"
    m.write_atomic(display, webp)
    digest = sha256(webp)
    object_key = f"cards/{row.language}/{card.set_id.lower()}/{card_id.lower()}/{digest[:12]}/display.webp"
    alias = f"cards/{row.language}/{card.set_id.lower()}/{card_id.lower()}/display.webp"
    uploaded = m.wrangler_put(object_key, display, "image/webp")
    public = None
    if uploaded:
        pointer = dest / "display.webp.current.txt"
        pointer.write_text(object_key, encoding="utf-8")
        m.wrangler_put(f"{alias}.current", pointer, "text/plain")
        m.wrangler_put(alias, display, "image/webp")
        public = f"{m.CDN}/{alias}"
    if "web.archive.org" in url:
        match = WAYBACK_ORIG_RE.search(url)
        orig_host = host_of(match.group(1)) if match else ""
        provider = "wayback_" + PERMITTED_ACQUIRE_HOSTS.get(orig_host, "permitted")
    else:
        provider = PERMITTED_ACQUIRE_HOSTS.get(host_of(url), "discovered")
    meta = {
        "canonicalBaseId": card.canonical,
        "cardId": card_id,
        "language": row.language,
        "setId": card.set_id,
        "setName": card.set_name,
        "collectorNumber": card.collector,
        "sha256": digest,
        "byteSize": len(webp),
        "mimeType": "image/webp",
        "sourceProvider": provider,
        "originalSourceUrl": url,
        "matchBasis": cand.match_evidence,
        "hostedObjectKey": object_key if uploaded else None,
        "publicUrl": public,
        "acquisition": "pass5_discovery",
        "acquiredAt": utc_now(),
        "provenanceConfidence": "CONFIRMED",
        "derivativeStatus": "display_webp",
        "uploaded": uploaded,
        "hostingStatus": "hosted" if uploaded else "local_only_pending_upload",
        "rightsBasis": cand.rights_status,
        "discoverySource": cand.source,
    }
    m.write_atomic(dest / "asset.json", json.dumps(meta, indent=2, ensure_ascii=False).encode("utf-8"))
    return {
        "status": "ok",
        "uploaded": uploaded,
        "provider": provider,
        "source": cand.source,
        "url": url,
        "bytes": len(webp),
        "publicUrl": public,
    }


_COVERED_CACHE: dict[str, dict] | None = None


def get_covered() -> dict[str, dict]:
    global _COVERED_CACHE
    if _COVERED_CACHE is None:
        _COVERED_CACHE = m.covered_canonicals()
    return _COVERED_CACHE


def process_card(card: CardTarget, upload: bool) -> dict:
    covered = get_covered()
    meta = covered.get(card.canonical)
    if meta and (meta.get("publicUrl") or meta.get("hostedObjectKey")):
        row = {"status": "already_hosted", "canonical": card.canonical, "at": utc_now()}
        append_progress(row)
        return row

    cands: list[Candidate] = []
    cands.extend(discover_catalogue_and_lightweight_permitted(card))
    # Fan/community discovery (rights-review only) — every remaining card.
    try:
        cands.extend(discover_limitless(card))
    except Exception:
        pass
    try:
        cands.extend(discover_serebii(card))
    except Exception:
        pass
    try:
        cands.extend(discover_official_jp(card))
    except Exception:
        pass
    reviewish = [c for c in cands if c.source not in {"catalogue_pokewallet", "resolver_error"}]
    if not reviewish:
        try:
            cands.extend(discover_duckduckgo_html(card))
        except Exception:
            pass
    cands = dedupe_candidates(cands)

    acquired = None
    if upload:
        for cand in cands:
            if not cand.auto_acquire:
                continue
            acquired = try_acquire(card, cand)
            if acquired and acquired.get("status") == "ok":
                break
            # If live permitted URL failed, try one Wayback snapshot of that URL.
            if acquired and acquired.get("status") in {"fetch_fail", "placeholder", "invalid_payload"}:
                if host_of(cand.url) in PERMITTED_ACQUIRE_HOSTS:
                    for arch in wayback_available(cand.url, limit=1):
                        rights, auto = classify_url(arch)
                        if not auto:
                            continue
                        acquired = try_acquire(
                            card,
                            Candidate(
                                url=arch,
                                source="wayback_of_failed_live",
                                page_url=None,
                                rights_status=rights,
                                match_evidence=f"wayback+{cand.match_evidence}",
                                auto_acquire=True,
                            ),
                        )
                        if acquired and acquired.get("status") == "ok":
                            break
                if acquired and acquired.get("status") == "ok":
                    break

    rights_review = [
        {
            "url": c.url,
            "source": c.source,
            "page_url": c.page_url,
            "rights_status": c.rights_status,
            "match_evidence": c.match_evidence,
            "query": c.query,
        }
        for c in cands
        if (not c.auto_acquire) and c.url
    ]
    permitted_hits = [c for c in cands if c.auto_acquire]

    if acquired and acquired.get("status") == "ok":
        status = "acquired"
        reason = "recovered_pass5"
    elif permitted_hits:
        status = "unresolved"
        reason = "permitted_candidates_failed_validation"
    elif rights_review:
        status = "unresolved"
        reason = "candidates_awaiting_rights_or_identity"
    else:
        status = "unresolved"
        reason = "no_discoverable_image_candidates"

    row = {
        "status": status,
        "canonical": card.canonical,
        "language": card.language,
        "set_id": card.set_id,
        "collector": card.collector,
        "name": card.name,
        "reason": reason,
        "queries": build_queries(card),
        "candidates_total": len(cands),
        "permitted_candidates": len(permitted_hits),
        "rights_review_candidates": len(rights_review),
        "acquired": acquired,
        "rights_review": rights_review[:20],
        "permitted_urls": [c.url for c in permitted_hits[:10]],
        "at": utc_now(),
    }
    append_progress(row)
    return row


def rebuild_outputs(targets: list[CardTarget]) -> dict:
    STATE.mkdir(parents=True, exist_ok=True)
    REPORT.mkdir(parents=True, exist_ok=True)
    latest: dict[str, dict] = {}
    path = STATE / "discovery_progress.jsonl"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("canonical"):
                latest[str(row["canonical"])] = row

    recovered_by_source: dict[str, int] = {}
    rights_rows: list[dict] = []
    classification_rows: list[dict] = []
    searched = acquired_n = 0
    for t in targets:
        row = latest.get(t.canonical)
        if not row:
            classification_rows.append(
                {
                    "canonical_card_id": t.canonical,
                    "language": t.language,
                    "set_id": t.set_id,
                    "set_name": t.set_name,
                    "collector_number": t.collector,
                    "card_name": t.name,
                    "classification": "not_yet_searched_pass5",
                    "notes": "",
                }
            )
            continue
        searched += 1
        if row.get("status") == "acquired":
            acquired_n += 1
            src = (row.get("acquired") or {}).get("source") or (row.get("acquired") or {}).get("provider") or "?"
            recovered_by_source[src] = recovered_by_source.get(src, 0) + 1
            continue
        for c in row.get("rights_review") or []:
            rights_rows.append(
                {
                    "canonical_card_id": t.canonical,
                    "language": t.language,
                    "set_id": t.set_id,
                    "set_name": t.set_name,
                    "collector_number": t.collector,
                    "card_name": t.name,
                    "candidate_url": c.get("url"),
                    "page_url": c.get("page_url"),
                    "source": c.get("source"),
                    "rights_status": c.get("rights_status"),
                    "match_evidence": c.get("match_evidence"),
                    "query": c.get("query"),
                    "action": "rights_review_or_collector_outreach",
                }
            )
        classification_rows.append(
            {
                "canonical_card_id": t.canonical,
                "language": t.language,
                "set_id": t.set_id,
                "set_name": t.set_name,
                "collector_number": t.collector,
                "card_name": t.name,
                "classification": row.get("reason") or "unresolved",
                "notes": f"rights={row.get('rights_review_candidates')}; permitted={row.get('permitted_candidates')}",
            }
        )

    rights_path = REPORT / "en_jp_pass5_rights_review_candidates.csv"
    class_path = REPORT / "en_jp_pass5_undiscoverable_or_blocked.csv"
    if rights_rows:
        with rights_path.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rights_rows[0].keys()))
            w.writeheader()
            w.writerows(rights_rows)
    else:
        rights_path.write_text("canonical_card_id,notes\n", encoding="utf-8")
    with class_path.open("w", encoding="utf-8", newline="") as fh:
        fields = [
            "canonical_card_id",
            "language",
            "set_id",
            "set_name",
            "collector_number",
            "card_name",
            "classification",
            "notes",
        ]
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(classification_rows)

    cards = m.load_cards()
    covered = m.covered_canonicals()
    progress = m.load_progress()
    summary = m.rebuild_reports(cards, covered, progress)
    awaiting_permission = sum(
        1 for r in classification_rows if r["classification"] == "candidates_awaiting_rights_or_identity"
    )
    genuinely_undiscoverable = sum(
        1 for r in classification_rows if r["classification"] == "no_discoverable_image_candidates"
    )
    out = {
        "generatedAtUtc": utc_now(),
        "verdict": "CARDSCANR_EN_JP_IMAGE_INDEPENDENCE_PASS5_DISCOVERY",
        "targets": len(targets),
        "searched": searched,
        "acquiredThisPass": acquired_n,
        "recoveredBySource": recovered_by_source,
        "rightsReviewCandidateRows": len(rights_rows),
        "cardsAwaitingPermission": awaiting_permission,
        "genuinelyUndiscoverable": genuinely_undiscoverable,
        "classificationRows": len(classification_rows),
        "summaryAfter": summary,
        "sourcesNotAccessibleWithoutPaymentOrPermission": [
            "Brave Image Search API (no API key; paid registration not approved)",
            "PokéWallet image API (auth-gated; no written rehost authorization)",
            "pokemon-card.com official images (registry: metadata_only)",
            "Pokellector (watermark; evidence-only)",
            "Serebii / Bulbagarden / Limitless / pkmncards (fan/community; rights review)",
            "Marketplace listing photos (link/evidence only)",
        ],
        "artifacts": {
            "rightsReviewCsv": str(rights_path),
            "classificationCsv": str(class_path),
            "progressJsonl": str(path),
        },
    }
    (REPORT / "pass5_discovery_summary.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    try:
        REPORT_MIRROR.mkdir(parents=True, exist_ok=True)
        for name in (
            "pass5_discovery_summary.json",
            "en_jp_pass5_rights_review_candidates.csv",
            "en_jp_pass5_undiscoverable_or_blocked.csv",
            "en_jp_unresolved_images.csv",
            "image_independence_summary.json",
        ):
            src = REPORT / name
            if src.exists():
                (REPORT_MIRROR / name).write_bytes(src.read_bytes())
    except OSError:
        pass
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--upload", action="store_true")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    STATE.mkdir(parents=True, exist_ok=True)
    m.load_tcgdex_sets()
    targets = load_targets()
    if args.report_only:
        print(json.dumps(rebuild_outputs(targets), indent=2), flush=True)
        return 0

    # Warm covered cache once (scanning master for every card was the bottleneck).
    covered = get_covered()
    print(json.dumps({"covered": len(covered)}), flush=True)

    done = set() if args.force else load_done()
    # Also treat currently hosted as done.
    for t in targets:
        meta = covered.get(t.canonical)
        if meta and (meta.get("publicUrl") or meta.get("hostedObjectKey")):
            done.add(t.canonical)
    todo = [t for t in targets if t.canonical not in done]
    if args.limit:
        todo = todo[: args.limit]
    print(json.dumps({"targets": len(targets), "todo": len(todo), "done": len(done)}), flush=True)

    stats = {"acquired": 0, "unresolved": 0, "already_hosted": 0, "rights_review_cards": 0}
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futs = {pool.submit(process_card, t, args.upload): t for t in todo}
        n = 0
        for fut in as_completed(futs):
            res = fut.result()
            n += 1
            st = res.get("status")
            if st == "acquired":
                stats["acquired"] += 1
            elif st == "already_hosted":
                stats["already_hosted"] += 1
            else:
                stats["unresolved"] += 1
                if int(res.get("rights_review_candidates") or 0) > 0:
                    stats["rights_review_cards"] += 1
            if n % 10 == 0 or n == len(todo):
                print(f"progress {n}/{len(todo)} {stats}", flush=True)

    if stats["acquired"]:
        subprocess.run(
            ["python", str(ROOT / "tools" / "apply_image_independence_to_catalogue.py")],
            cwd=str(ROOT),
            check=False,
        )

    summary = rebuild_outputs(targets)
    print("final_stats", json.dumps(stats), flush=True)
    print("summary_keys", list(summary.keys()), flush=True)
    print("acquiredThisPass", summary.get("acquiredThisPass"), "independent", (summary.get("summaryAfter") or {}).get("independentPct"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
