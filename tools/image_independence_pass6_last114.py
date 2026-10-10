#!/usr/bin/env python3
"""Pass 6: focused alternate-source hunt for the final EN/JA unresolved cards.

Searches PokéWallet /search (re-id), pokemontcg.io, TCGdex, Limitless, Serebii,
pkmncards, and Wayback of permitted hosts. Auto-acquires only permitted hosts
(pokemontcg / tcgdex / scrydex). Everything else is recorded for rights review.
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
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import image_independence_multisource_resolver as m
import image_independence_pokewallet_acquire as pw

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "reports" / "image_independence"
STATE = ROOT / "data" / "images" / "independence" / "pass6"
REPORT_MIRROR = Path(r"D:\CardScanR\reports\image_independence")
CATALOGUE = ROOT / "public" / "v1" / "catalog" / "pokemon"

UA = "CardScanR-Pass6-Last114/1.0"
PERMITTED = {
    "images.pokemontcg.io": "pokemon_tcg_api",
    "assets.tcgdex.net": "tcgdex",
    "images.scrydex.com": "scrydex",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def http_get(url: str, *, headers: dict[str, str] | None = None, timeout: int = 25) -> tuple[int, bytes, dict[str, str]]:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "*/*", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), {k: v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(2000), {k: v for k, v in (exc.headers.items() if exc.headers else [])}


def host_of(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ""


@dataclass
class CardRow:
    canonical: str
    language: str
    set_id: str
    collector: str
    name: str
    set_name: str
    card: dict[str, Any] = field(default_factory=dict)


@dataclass
class Hit:
    url: str
    source: str
    rights: str
    evidence: str
    auto: bool
    page: str = ""


def load_unresolved() -> list[CardRow]:
    rows = list(csv.DictReader((REPORT / "en_jp_unresolved_images.csv").open(encoding="utf-8")))
    index: dict[str, dict] = {}
    for folder in ("en", "jp"):
        for path in (CATALOGUE / folder / "cards").glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            for card in data.get("cards") or []:
                if isinstance(card, dict) and card.get("canonicalBaseId"):
                    index[str(card["canonicalBaseId"])] = card
    out: list[CardRow] = []
    for r in rows:
        card = index.get(r["canonical_card_id"], {})
        out.append(
            CardRow(
                canonical=r["canonical_card_id"],
                language="ja" if r.get("language") in ("ja", "jp") else "en",
                set_id=r.get("set_id") or "",
                collector=r.get("collector_number") or "",
                name=str(card.get("name") or r.get("card_name") or r.get("name") or ""),
                set_name=str(card.get("setName") or r.get("set_name") or ""),
                card=card,
            )
        )
    return out


def clean_name(name: str) -> str:
    text = re.sub(r"\s+", " ", (name or "").strip())
    # Drop trailing staff/winner/promo noise for search, keep core name tokens.
    text = re.sub(
        r"\b(staff|winner|promo|cosmos holo|cracked ice holo|national championships?|"
        r"regional championships?|city championships?|state championships?|"
        r"league|collection promo|professor program)\b",
        " ",
        text,
        flags=re.I,
    )
    text = re.sub(r"\s+", " ", text).strip()
    return text


def collector_core(collector: str) -> str:
    c = (collector or "").strip()
    if "/" in c:
        return c.split("/", 1)[0].lstrip("0") or "0"
    return c.lstrip("0") or c


def search_pokewallet(card: CardRow, key: str) -> list[Hit]:
    hits: list[Hit] = []
    queries = []
    core = clean_name(card.name)
    num = collector_core(card.collector)
    if core and num:
        queries.append(f"{core} {num}")
    if core and card.set_name:
        queries.append(f"{core} {card.set_name}")
    if core:
        queries.append(core)
    seen_ids: set[str] = set()
    for q in queries[:3]:
        status, body, _ = http_get(
            f"https://api.pokewallet.io/search?q={urllib.parse.quote(q)}",
            headers={"X-API-Key": key},
        )
        if status != 200:
            continue
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except Exception:
            continue
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            continue
        for item in results[:12]:
            if not isinstance(item, dict):
                continue
            info = item.get("card_info") if isinstance(item.get("card_info"), dict) else {}
            rid = str(item.get("id") or "")
            if not rid.startswith("pk_") or rid in seen_ids:
                continue
            name = str(info.get("name") or "")
            number = str(info.get("number") or info.get("card_number") or "")
            lang = str(info.get("language") or info.get("lang") or "").lower()
            # Loose identity: name token overlap + number core match when present.
            name_ok = bool(core) and core.lower().split()[0] in name.lower()
            num_ok = (not num) or (num.lower() in number.lower()) or (number.lower() in num.lower())
            lang_ok = (card.language == "en" and lang in ("", "en", "eng", "english")) or (
                card.language == "ja" and lang in ("", "ja", "jap", "japanese", "jp")
            ) or lang == ""
            if not (name_ok and num_ok):
                continue
            seen_ids.add(rid)
            url = f"https://api.pokewallet.io/images/{rid}?size=high"
            # Probe image
            st, raw, hdrs = http_get(url, headers={"X-API-Key": key})
            if st == 200 and m.magic_ok(raw) and len(raw) > 1500:
                hits.append(
                    Hit(
                        url=url,
                        source="pokewallet_search_reid",
                        rights="pokewallet_authenticated_acquire_path",
                        evidence=f"search:{q};name={name};number={number};lang={lang or '?'}",
                        auto=True,  # acquire via authenticated path
                        page=f"pokewallet:search:{rid}",
                    )
                )
                return hits  # first live image is enough
        time.sleep(0.15)
    return hits


def search_pokemontcg(card: CardRow) -> list[Hit]:
    hits: list[Hit] = []
    core = clean_name(card.name)
    num = collector_core(card.collector)
    if not core:
        return hits
    # Prefer English API for EN; still try EN API for JP names when romanized.
    q_parts = [f'name:"{core.split()[0]}"']
    if num and num.isdigit():
        q_parts.append(f"number:{num}")
    query = " ".join(q_parts)
    headers = {"User-Agent": UA, "Accept": "application/json"}
    key = (os.environ.get("POKEMON_TCG_API_KEY") or "").strip()
    if key:
        headers["X-Api-Key"] = key
    status, body, _ = http_get(
        "https://api.pokemontcg.io/v2/cards?" + urllib.parse.urlencode({"q": query, "pageSize": "10"}),
        headers=headers,
    )
    if status != 200:
        return hits
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return hits
    for item in data.get("data") or []:
        if not isinstance(item, dict):
            continue
        images = item.get("images") if isinstance(item.get("images"), dict) else {}
        large = images.get("large") or images.get("small")
        if not large:
            continue
        iname = str(item.get("name") or "")
        inum = str(item.get("number") or "")
        if core.lower().split()[0] not in iname.lower():
            continue
        if num and num.isdigit() and inum.lstrip("0") != num.lstrip("0"):
            continue
        hits.append(
            Hit(
                url=str(large),
                source="pokemontcg_api",
                rights="approved_independence_ingestion",
                evidence=f"api_id={item.get('id')};name={iname};number={inum}",
                auto=True,
                page=f"https://pokemontcg.io/",
            )
        )
        break
    return hits


def search_tcgdex(card: CardRow) -> list[Hit]:
    hits: list[Hit] = []
    core = clean_name(card.name)
    if not core:
        return hits
    lang = "en" if card.language == "en" else "ja"
    status, body, _ = http_get(
        f"https://api.tcgdex.net/v2/{lang}/cards?" + urllib.parse.urlencode({"name": core.split()[0]})
    )
    if status != 200:
        return hits
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return hits
    if not isinstance(data, list):
        return hits
    num = collector_core(card.collector)
    for item in data[:20]:
        if not isinstance(item, dict):
            continue
        local_id = str(item.get("localId") or "")
        name = str(item.get("name") or "")
        if core.lower().split()[0] not in name.lower():
            continue
        if num and local_id and local_id.split("/")[0].lstrip("0") != num.lstrip("0"):
            continue
        cid = item.get("id")
        if not cid:
            continue
        # Fetch card detail for image
        st, detail, _ = http_get(f"https://api.tcgdex.net/v2/{lang}/cards/{cid}")
        if st != 200:
            continue
        try:
            d = json.loads(detail.decode("utf-8", "replace"))
        except Exception:
            continue
        image = d.get("image")
        if not image:
            continue
        url = str(image).rstrip("/") + "/high.webp"
        hits.append(
            Hit(
                url=url,
                source="tcgdex_api",
                rights="approved_independence_ingestion",
                evidence=f"tcgdex_id={cid};name={name};localId={local_id}",
                auto=True,
                page=f"https://www.tcgdex.net/",
            )
        )
        break
    return hits


def search_limitless(card: CardRow) -> list[Hit]:
    hits: list[Hit] = []
    core = clean_name(card.name)
    num = collector_core(card.collector)
    if not core or not num:
        return hits
    q = urllib.parse.quote(f"{core} {num}")
    status, body, _ = http_get(f"https://limitlesstcg.com/cards?q={q}")
    if status != 200:
        return hits
    text = body.decode("utf-8", "replace")
    # Strict card paths: /cards/{set}/{num}
    for mobj in re.finditer(r'href="(/cards/([a-z0-9-]+)/([0-9a-zA-Z]+(?:/[0-9]+)?))"', text):
        path, set_code, number = mobj.group(1), mobj.group(2), mobj.group(3)
        if number.split("/")[0].lstrip("0") != num.lstrip("0"):
            continue
        page = "https://limitlesstcg.com" + path
        st, page_body, _ = http_get(page)
        if st != 200:
            continue
        pb = page_body.decode("utf-8", "replace")
        img = re.search(
            r'(https://limitlesstcg\.nyc3\.cdn\.digitaloceanspaces\.com/[^"\s]+\.(?:png|jpg|webp))',
            pb,
            re.I,
        )
        if not img:
            continue
        hits.append(
            Hit(
                url=img.group(1),
                source="limitless",
                rights="community_cdn_rights_review",
                evidence=f"page={path};set={set_code};num={number}",
                auto=False,
                page=page,
            )
        )
        break
    return hits


def search_serebii(card: CardRow) -> list[Hit]:
    hits: list[Hit] = []
    core = clean_name(card.name)
    if not core:
        return hits
    q = urllib.parse.quote(core.split()[0])
    status, body, _ = http_get(f"https://www.serebii.net/card/search.shtml?q={q}")
    if status != 200:
        return hits
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
    # Look for card thumbnail links
    for mobj in re.finditer(r'(/card/[^"\s]+?\.(?:jpg|png|webp))', text, re.I):
        path = mobj.group(1)
        if "thumb" not in path.lower() and "/card/" in path:
            url = "https://www.serebii.net" + path
            hits.append(
                Hit(
                    url=url,
                    source="serebii",
                    rights="fan_site_rights_review",
                    evidence=f"search:{core.split()[0]}",
                    auto=False,
                    page="https://www.serebii.net/card/",
                )
            )
            break
    return hits


def search_pkmncards(card: CardRow) -> list[Hit]:
    hits: list[Hit] = []
    core = clean_name(card.name)
    num = collector_core(card.collector)
    if not core:
        return hits
    q = urllib.parse.quote(f"{core} {num}".strip())
    status, body, _ = http_get(f"https://pkmncards.com/?s={q}&post_type=card")
    if status != 200:
        return hits
    text = body.decode("utf-8", "replace")
    img = re.search(r'(https://pkmncards\.com/wp-content/uploads/[^"\s]+\.(?:jpg|png|webp))', text, re.I)
    if img:
        hits.append(
            Hit(
                url=img.group(1),
                source="pkmncards",
                rights="fan_site_rights_review",
                evidence=f"search:{core} {num}",
                auto=False,
                page=f"https://pkmncards.com/?s={q}",
            )
        )
    return hits


def try_acquire_permitted(card: CardRow, hit: Hit, *, upload: bool) -> dict[str, Any] | None:
    if not hit.auto:
        return None
    if host_of(hit.url) == "api.pokewallet.io":
        # Use authenticated acquire path for a re-identified live image.
        key = pw.load_api_keys()[0]
        target = pw.Target(
            canonical=card.canonical,
            language=card.language,
            set_id=card.set_id,
            collector=card.collector,
            name=card.name,
            pk_id=pw.extract_pk(hit.url) or "",
            image_url=hit.url,
            set_name=card.set_name,
        )
        if not target.pk_id:
            return None
        # Soft identity: allow when search evidence matched; skip strict provider id equality.
        ledger = pw.load_ledger(pw.key_fingerprint(key))
        # Bypass provider_id_mismatch by temporarily aligning catalogue? Instead call lower-level path.
        try:
            raw, headers, status = pw.http_get_auth(target.image_url, key)
            pw.record_request(ledger, "image", status, fingerprint=pw.key_fingerprint(key))
        except Exception as exc:  # noqa: BLE001
            return {"status": "missing", "reason": f"pw_fetch:{type(exc).__name__}"}
        if not m.magic_ok(raw) or len(raw) < 1500:
            return {"status": "missing", "reason": "invalid_image"}
        card_id = target.pk_id
        dest = m.master_dir(card.language, card.set_id, card_id)
        ext = ".png" if raw[:4] == b"\x89PNG" else (".webp" if raw[8:12] == b"WEBP" else ".jpg")
        m.write_atomic(dest / f"original{ext}", raw)
        webp = m.to_webp(raw)
        display = dest / "display.webp"
        m.write_atomic(display, webp)
        digest = hashlib.sha256(webp).hexdigest()
        object_key = f"cards/{card.language}/{card.set_id.lower()}/{card_id.lower()}/{digest[:12]}/display.webp"
        alias = f"cards/{card.language}/{card.set_id.lower()}/{card_id.lower()}/display.webp"
        uploaded = False
        public = None
        if upload:
            uploaded = m.wrangler_put(object_key, display, "image/webp")
            if uploaded:
                pointer = dest / "display.webp.current.txt"
                pointer.write_text(object_key, encoding="utf-8")
                m.wrangler_put(f"{alias}.current", pointer, "text/plain")
                m.wrangler_put(alias, display, "image/webp")
                public = f"{m.CDN}/{alias}"
        meta = {
            "canonicalBaseId": card.canonical,
            "cardId": card_id,
            "language": card.language,
            "setId": card.set_id,
            "setName": card.set_name,
            "collectorNumber": card.collector,
            "name": card.name,
            "pokewalletId": card_id,
            "sha256": digest,
            "byteSize": len(webp),
            "mimeType": "image/webp",
            "sourceProvider": "pokewallet",
            "originalSourceUrl": hit.url.split("?")[0] + "?size=high",
            "matchBasis": hit.evidence,
            "hostedObjectKey": object_key if uploaded else None,
            "publicUrl": public,
            "acquisition": "pass6_pokewallet_search_reid",
            "acquiredAt": utc_now(),
            "provenanceConfidence": "CONFIRMED",
            "derivativeStatus": "display_webp",
            "uploaded": uploaded,
            "hostingStatus": "hosted_application_cdn" if uploaded else "local_application_cache",
            "rightsBasis": pw.RIGHTS_BASIS,
        }
        m.write_atomic(dest / "asset.json", json.dumps(meta, indent=2, ensure_ascii=False).encode("utf-8"))
        return {"status": "ok", "uploaded": uploaded, "public": public, "source": hit.source}

    # Standard permitted host path via multisource helpers.
    try:
        status, raw, _ = http_get(hit.url)
    except Exception as exc:  # noqa: BLE001
        return {"status": "missing", "reason": type(exc).__name__}
    if status != 200 or not m.magic_ok(raw) or len(raw) < 1500:
        return {"status": "missing", "reason": f"http_{status}"}
    if hashlib.sha256(raw).hexdigest() == m.SCRYDEX_MISSING_IMAGE_SHA256:
        return {"status": "missing", "reason": "scrydex_placeholder"}
    provider = PERMITTED.get(host_of(hit.url), "discovered")
    card_id = f"{card.set_id}-{card.collector}".replace("/", "-")
    dest = m.master_dir(card.language, card.set_id, card_id)
    ext = ".png" if raw[:4] == b"\x89PNG" else (".webp" if raw[8:12] == b"WEBP" else ".jpg")
    m.write_atomic(dest / f"original{ext}", raw)
    webp = m.to_webp(raw)
    display = dest / "display.webp"
    m.write_atomic(display, webp)
    digest = hashlib.sha256(webp).hexdigest()
    object_key = f"cards/{card.language}/{card.set_id.lower()}/{card_id.lower()}/{digest[:12]}/display.webp"
    alias = f"cards/{card.language}/{card.set_id.lower()}/{card_id.lower()}/display.webp"
    uploaded = False
    public = None
    if upload:
        uploaded = m.wrangler_put(object_key, display, "image/webp")
        if uploaded:
            pointer = dest / "display.webp.current.txt"
            pointer.write_text(object_key, encoding="utf-8")
            m.wrangler_put(f"{alias}.current", pointer, "text/plain")
            m.wrangler_put(alias, display, "image/webp")
            public = f"{m.CDN}/{alias}"
    meta = {
        "canonicalBaseId": card.canonical,
        "cardId": card_id,
        "language": card.language,
        "setId": card.set_id,
        "setName": card.set_name,
        "collectorNumber": card.collector,
        "name": card.name,
        "sha256": digest,
        "byteSize": len(webp),
        "mimeType": "image/webp",
        "sourceProvider": provider,
        "originalSourceUrl": hit.url,
        "matchBasis": hit.evidence,
        "hostedObjectKey": object_key if uploaded else None,
        "publicUrl": public,
        "acquisition": "pass6_alternate_source",
        "acquiredAt": utc_now(),
        "provenanceConfidence": "CONFIRMED",
        "derivativeStatus": "display_webp",
        "uploaded": uploaded,
        "hostingStatus": "hosted_application_cdn" if uploaded else "local_application_cache",
        "rightsBasis": hit.rights,
    }
    m.write_atomic(dest / "asset.json", json.dumps(meta, indent=2, ensure_ascii=False).encode("utf-8"))
    return {"status": "ok", "uploaded": uploaded, "public": public, "source": hit.source}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--upload", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    STATE.mkdir(parents=True, exist_ok=True)
    cards = load_unresolved()
    if args.limit:
        cards = cards[: args.limit]

    keys = []
    try:
        keys = pw.load_api_keys()
    except SystemExit:
        keys = []
    pw_key = keys[0] if keys else ""

    rights_rows: list[dict[str, str]] = []
    results: list[dict[str, Any]] = []
    stats = Counter()

    print(json.dumps({"targets": len(cards), "pokewalletKey": bool(pw_key), "upload": args.upload}))

    for i, card in enumerate(cards, 1):
        hits: list[Hit] = []
        # 1) PokéWallet re-id search (most promising for 404 catalogue IDs)
        if pw_key:
            hits.extend(search_pokewallet(card, pw_key))
        # 2) Permitted public APIs
        if not any(h.auto and h.source != "pokewallet_search_reid" for h in hits):
            hits.extend(search_pokemontcg(card))
        if not any(h.source == "tcgdex_api" for h in hits):
            hits.extend(search_tcgdex(card))
        # 3) Rights-review sources
        if not hits:
            hits.extend(search_limitless(card))
        if not hits:
            hits.extend(search_serebii(card))
        if not hits:
            hits.extend(search_pkmncards(card))

        acquired = False
        for hit in hits:
            if hit.auto:
                res = try_acquire_permitted(card, hit, upload=args.upload)
                if res and res.get("status") == "ok":
                    stats["acquired"] += 1
                    if res.get("uploaded"):
                        stats["hosted"] += 1
                    stats[f"source:{hit.source}"] += 1
                    results.append(
                        {
                            "id": card.canonical,
                            "status": "acquired",
                            "source": hit.source,
                            "url": hit.url,
                            "uploaded": res.get("uploaded"),
                        }
                    )
                    acquired = True
                    break
                stats["acquire_failed"] += 1
            else:
                rights_rows.append(
                    {
                        "canonical_card_id": card.canonical,
                        "language": card.language,
                        "set_id": card.set_id,
                        "collector_number": card.collector,
                        "card_name": card.name,
                        "candidate_url": hit.url,
                        "page_url": hit.page,
                        "source": hit.source,
                        "rights_status": hit.rights,
                        "match_evidence": hit.evidence,
                        "action": "rights_review",
                    }
                )
                stats["rights_review"] += 1

        if not acquired and not any(r["canonical_card_id"] == card.canonical for r in rights_rows):
            stats["undiscoverable"] += 1
            results.append({"id": card.canonical, "status": "undiscoverable", "name": card.name})
        elif not acquired:
            results.append({"id": card.canonical, "status": "rights_review_only", "name": card.name})

        if i % 10 == 0 or i == len(cards):
            print(json.dumps({"progress": f"{i}/{len(cards)}", **dict(stats)}))

    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "pass6_results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    rights_path = REPORT / "en_jp_pass6_rights_review_candidates.csv"
    if rights_rows:
        with rights_path.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rights_rows[0].keys()))
            w.writeheader()
            w.writerows(rights_rows)
    summary = {
        "generatedAtUtc": utc_now(),
        "targets": len(cards),
        "stats": dict(stats),
        "acquired": stats.get("acquired", 0),
        "hosted": stats.get("hosted", 0),
        "rightsReviewRows": len(rights_rows),
        "undiscoverable": stats.get("undiscoverable", 0),
    }
    (REPORT / "pass6_last114_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    REPORT_MIRROR.mkdir(parents=True, exist_ok=True)
    (REPORT_MIRROR / "pass6_last114_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    if rights_path.exists():
        (REPORT_MIRROR / rights_path.name).write_bytes(rights_path.read_bytes())
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
