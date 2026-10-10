#!/usr/bin/env python3
"""Pass 7: rigorous final hunt for the remaining EN/JA unresolved cards.

Rejects false-positive fan-site search scrapes. Auto-acquires only exact-identity
matches from permitted hosts (pokemontcg / tcgdex / scrydex / PokéWallet auth).
Produces per-card final accounting + collector-scan request list.
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
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import image_independence_multisource_resolver as m
import image_independence_pokewallet_acquire as pw

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "reports" / "image_independence"
STATE = ROOT / "data" / "images" / "independence" / "pass7"
MIRROR = Path(r"D:\CardScanR\reports\image_independence")
CATALOGUE = ROOT / "public" / "v1" / "catalog" / "pokemon"
UA = "CardScanR-Pass7-Final67/1.0"

PERMITTED_HOSTS = {
    "images.pokemontcg.io": "pokemon_tcg_api",
    "assets.tcgdex.net": "tcgdex",
    "images.scrydex.com": "scrydex",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def http_get(url: str, *, headers: dict[str, str] | None = None, timeout: int = 25) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(4000)


def host_of(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ""


@dataclass
class Card:
    canonical: str
    language: str
    set_id: str
    collector: str
    name: str
    set_name: str
    variant: str
    providers: dict[str, Any]
    card: dict[str, Any]
    ja_name: str = ""
    en_name: str = ""


@dataclass
class Hit:
    url: str
    source: str
    evidence: str
    auto: bool
    identity_ok: bool
    identity_notes: str
    page: str = ""
    rights: str = ""


def load_cards() -> list[Card]:
    rows = list(csv.DictReader((REPORT / "en_jp_unresolved_images.csv").open(encoding="utf-8")))
    index: dict[str, dict] = {}
    for folder in ("en", "jp"):
        for path in (CATALOGUE / folder / "cards").glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            for c in data.get("cards") or []:
                if isinstance(c, dict) and c.get("canonicalBaseId"):
                    index[str(c["canonicalBaseId"])] = c
    out: list[Card] = []
    for r in rows:
        c = index.get(r["canonical_card_id"], {})
        providers = c.get("providerIds") if isinstance(c.get("providerIds"), dict) else {}
        out.append(
            Card(
                canonical=r["canonical_card_id"],
                language="ja" if r.get("language") in ("ja", "jp") else "en",
                set_id=r.get("set_id") or "",
                collector=r.get("collector_number") or "",
                name=str(c.get("name") or r.get("card_name") or ""),
                set_name=str(c.get("setName") or r.get("set_name") or ""),
                variant=str(c.get("variant") or c.get("finish") or c.get("rarity") or ""),
                providers=providers,
                card=c,
                ja_name=str(c.get("nameJa") or c.get("japaneseName") or ""),
                en_name=str(c.get("nameEn") or c.get("englishName") or ""),
            )
        )
    return out


def core_name(name: str) -> str:
    text = re.sub(r"\s+", " ", (name or "").strip())
    # Strip stamp/staff/foil noise for search, but keep Pokémon name.
    text = re.sub(
        r"\b(staff|winner|promo|pixel|cosmo|cosmos|holo|foil|gold signature|"
        r"tord reklev|fernando cifuentes|delta species|"
        r"national championships?|regional championships?|"
        r"world championship|constructed|starter|deck)\b",
        " ",
        text,
        flags=re.I,
    )
    text = re.sub(r"\b(20\d{2})\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def collector_core(collector: str) -> str:
    c = (collector or "").strip()
    # Keep promo codes like SWSH029 / S-P 163
    if re.search(r"[A-Za-z]", c) and "/" not in c:
        return c
    if "/" in c:
        return c.split("/", 1)[0].lstrip("0") or "0"
    return c.lstrip("0") or c


def is_stamped_or_special(card: Card) -> bool:
    blob = " ".join(
        [
            card.name,
            card.variant,
            card.set_name,
            card.canonical,
        ]
    ).lower()
    keys = (
        "staff",
        "winner",
        "signature",
        "world championship",
        "tord reklev",
        "fernando",
        "pixel",
        "stamp",
        "league",
        "professor program",
    )
    return any(k in blob for k in keys)


def verify_pokemontcg_item(card: Card, item: dict) -> tuple[bool, str]:
    iname = str(item.get("name") or "")
    inum = str(item.get("number") or "")
    iset = str((item.get("set") or {}).get("id") or "")
    want = core_name(card.name) or core_name(card.en_name)
    if not want:
        return False, "no_search_name"
    # First token of core name must appear
    token = want.split()[0].lower()
    if token not in iname.lower():
        return False, f"name_mismatch:{iname}"
    num = collector_core(card.collector)
    if re.search(r"[A-Za-z]", num):
        # promo codes must match closely
        if num.replace(" ", "").lower() not in inum.replace(" ", "").lower() and inum.replace(" ", "").lower() not in num.replace(" ", "").lower():
            return False, f"promo_number_mismatch:{inum}"
    else:
        if inum.lstrip("0") != num.lstrip("0"):
            return False, f"number_mismatch:{inum}"
    if is_stamped_or_special(card):
        # Base set printings are NOT acceptable substitutes for stamped/WCS exclusives.
        return False, f"stamped_exclusive_blocks_base_api:{iset}/{inum}"
    return True, f"pokemontcg:{item.get('id')}"


def search_pokemontcg(card: Card) -> list[Hit]:
    hits: list[Hit] = []
    want = core_name(card.name) or core_name(card.en_name)
    if not want:
        return hits
    num = collector_core(card.collector)
    q = f'name:"{want.split()[0]}"'
    if num and not re.search(r"[A-Za-z]", num) and num.isdigit():
        q += f" number:{num}"
    headers = {"Accept": "application/json"}
    key = (os.environ.get("POKEMON_TCG_API_KEY") or "").strip()
    if key:
        headers["X-Api-Key"] = key
    st, body = http_get(
        "https://api.pokemontcg.io/v2/cards?" + urllib.parse.urlencode({"q": q, "pageSize": "25"}),
        headers=headers,
    )
    if st != 200:
        return hits
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return hits
    for item in data.get("data") or []:
        ok, notes = verify_pokemontcg_item(card, item)
        images = item.get("images") if isinstance(item.get("images"), dict) else {}
        url = images.get("large") or images.get("small")
        if not url:
            continue
        hits.append(
            Hit(
                url=str(url),
                source="pokemontcg_api",
                evidence=notes,
                auto=ok,
                identity_ok=ok,
                identity_notes=notes,
                rights="approved_independence_ingestion" if ok else "identity_rejected",
            )
        )
        if ok:
            break
    return hits


def search_tcgdex(card: Card) -> list[Hit]:
    hits: list[Hit] = []
    lang = "ja" if card.language == "ja" else "en"
    want = core_name(card.ja_name if lang == "ja" and card.ja_name else card.name) or core_name(card.en_name)
    if not want:
        return hits
    # JP constructed decks often use English names in catalogue — try both.
    names = [want.split()[0]]
    if card.en_name:
        names.append(core_name(card.en_name).split()[0])
    if card.ja_name:
        names.append(card.ja_name[:20])
    num = collector_core(card.collector)
    seen = set()
    for n in names:
        if not n or n in seen:
            continue
        seen.add(n)
        st, body = http_get(
            f"https://api.tcgdex.net/v2/{lang}/cards?" + urllib.parse.urlencode({"name": n})
        )
        if st != 200:
            continue
        try:
            arr = json.loads(body.decode("utf-8", "replace"))
        except Exception:
            continue
        if not isinstance(arr, list):
            continue
        for item in arr[:30]:
            if not isinstance(item, dict):
                continue
            local_id = str(item.get("localId") or "")
            name = str(item.get("name") or "")
            cid = item.get("id")
            if not cid:
                continue
            # number gate
            if num and not re.search(r"[A-Za-z]", num):
                if local_id.split("/")[0].lstrip("0") != num.lstrip("0"):
                    continue
            elif num and re.search(r"[A-Za-z]", num):
                if num.replace(" ", "").lower() not in local_id.replace(" ", "").lower():
                    continue
            if is_stamped_or_special(card):
                hits.append(
                    Hit(
                        url="",
                        source="tcgdex_api",
                        evidence=f"rejected_stamped_exclusive:{cid}",
                        auto=False,
                        identity_ok=False,
                        identity_notes="stamped_or_special_printing_not_substitutable",
                    )
                )
                continue
            st2, detail = http_get(f"https://api.tcgdex.net/v2/{lang}/cards/{cid}")
            if st2 != 200:
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
                    evidence=f"tcgdex:{cid};name={name};localId={local_id}",
                    auto=True,
                    identity_ok=True,
                    identity_notes=f"tcgdex_exact_number:{local_id}",
                    rights="approved_independence_ingestion",
                )
            )
            return hits
        time.sleep(0.05)
    return hits


def search_pokewallet_reid(card: Card, key: str) -> list[Hit]:
    hits: list[Hit] = []
    want = core_name(card.name) or core_name(card.en_name) or card.ja_name
    num = collector_core(card.collector)
    if not want:
        return hits
    queries = [f"{want} {num}".strip(), want]
    if card.ja_name:
        queries.insert(0, f"{card.ja_name} {num}".strip())
    for q in queries[:3]:
        st, body = http_get(
            f"https://api.pokewallet.io/search?q={urllib.parse.quote(q)}",
            headers={"X-API-Key": key},
        )
        if st != 200:
            continue
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except Exception:
            continue
        for item in (data.get("results") or [])[:15]:
            if not isinstance(item, dict):
                continue
            rid = str(item.get("id") or "")
            info = item.get("card_info") if isinstance(item.get("card_info"), dict) else {}
            if not rid.startswith("pk_"):
                continue
            name = str(info.get("name") or "")
            number = str(info.get("number") or info.get("card_number") or "")
            lang = str(info.get("language") or "").lower()
            token = (want.split()[0] if want else "").lower()
            if token and token not in name.lower() and (not card.ja_name or card.ja_name[:2] not in name):
                continue
            if num and not re.search(r"[A-Za-z]", num):
                if number.split("/")[0].lstrip("0") != num.lstrip("0"):
                    continue
            if card.language == "ja" and lang and lang not in ("ja", "jap", "japanese", "jp"):
                continue
            if card.language == "en" and lang and lang not in ("en", "eng", "english"):
                continue
            if is_stamped_or_special(card):
                # Only accept if search result name itself indicates stamp/staff/wcs.
                blob = (name + " " + str(info)).lower()
                if not any(k in blob for k in ("staff", "winner", "championship", "signature", "stamp")):
                    hits.append(
                        Hit(
                            url=f"https://api.pokewallet.io/images/{rid}?size=high",
                            source="pokewallet_search_reid",
                            evidence=f"rejected_base_for_stamped:{name}/{number}",
                            auto=False,
                            identity_ok=False,
                            identity_notes="base_result_for_stamped_exclusive",
                        )
                    )
                    continue
            url = f"https://api.pokewallet.io/images/{rid}?size=high"
            st2, raw = http_get(url, headers={"X-API-Key": key})
            if st2 != 200 or not m.magic_ok(raw) or len(raw) < 1500:
                continue
            hits.append(
                Hit(
                    url=url,
                    source="pokewallet_search_reid",
                    evidence=f"live_image:{rid};name={name};number={number};lang={lang}",
                    auto=True,
                    identity_ok=True,
                    identity_notes="pokewallet_live_reid",
                    rights=pw.RIGHTS_BASIS,
                )
            )
            return hits
        time.sleep(0.12)
    return hits


def audit_existing_pkmncards_candidates() -> dict[str, list[dict]]:
    path = REPORT / "en_jp_pass6_rights_review_candidates.csv"
    if not path.exists():
        return {}
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    by: dict[str, list[dict]] = defaultdict(list)
    # Detect false positives: same URL used for many unrelated cards.
    url_counts = Counter(r.get("candidate_url") or "" for r in rows)
    for r in rows:
        url = r.get("candidate_url") or ""
        note = "candidate"
        if url_counts[url] >= 3:
            note = "false_positive_shared_search_thumbnail"
        # Filename set codes that clearly don't match JA deck kits / WCS stamps
        fname = url.rsplit("/", 1)[-1].lower()
        if "sv1_en_" in fname and r.get("language") == "ja":
            note = "false_positive_en_scarlet_violet_image_for_ja_card"
        if "sv1_en_" in fname and "2282" in (r.get("set_id") or ""):
            note = "false_positive_base_sv_image_for_wcs_stamp"
        by[r["canonical_card_id"]].append({**r, "audit_note": note})
    return by


def acquire_hit(card: Card, hit: Hit, *, upload: bool, key: str) -> dict[str, Any]:
    if not hit.auto or not hit.identity_ok or not hit.url:
        return {"status": "skipped"}
    if host_of(hit.url) == "api.pokewallet.io":
        pk = pw.extract_pk(hit.url) or ""
        if not pk:
            return {"status": "missing", "reason": "no_pk"}
        st, raw = http_get(hit.url, headers={"X-API-Key": key})
        if st != 200 or not m.magic_ok(raw) or len(raw) < 1500:
            return {"status": "missing", "reason": f"http_{st}"}
        provider = "pokewallet"
        card_id = pk
        rights = pw.RIGHTS_BASIS
        acquisition = "pass7_pokewallet_reid"
    else:
        st, raw = http_get(hit.url)
        if st != 200 or not m.magic_ok(raw) or len(raw) < 1500:
            return {"status": "missing", "reason": f"http_{st}"}
        if hashlib.sha256(raw).hexdigest() == m.SCRYDEX_MISSING_IMAGE_SHA256:
            return {"status": "missing", "reason": "placeholder"}
        provider = PERMITTED_HOSTS.get(host_of(hit.url), "discovered")
        card_id = f"{card.set_id}-{card.collector}".replace("/", "-").replace(" ", "")
        rights = hit.rights or "approved_independence_ingestion"
        acquisition = "pass7_permitted_source"

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
        "originalSourceUrl": hit.url.split("?")[0] + ("?size=high" if "pokewallet" in hit.url else ""),
        "matchBasis": hit.evidence,
        "hostedObjectKey": object_key if uploaded else None,
        "publicUrl": public,
        "acquisition": acquisition,
        "acquiredAt": utc_now(),
        "provenanceConfidence": "CONFIRMED",
        "derivativeStatus": "display_webp",
        "uploaded": uploaded,
        "hostingStatus": "hosted_application_cdn" if uploaded else "local_application_cache",
        "rightsBasis": rights,
    }
    m.write_atomic(dest / "asset.json", json.dumps(meta, indent=2, ensure_ascii=False).encode("utf-8"))
    return {"status": "ok", "uploaded": uploaded, "public": public, "source": hit.source, "sha256": digest}


def classify_blocker(card: Card, hits: list[Hit], pkmn_audit: list[dict]) -> str:
    if any(h.auto and h.identity_ok for h in hits):
        return "recoverable_permitted"
    if is_stamped_or_special(card):
        return "stamped_or_exclusive_printing_no_permitted_public_image"
    if any(a.get("audit_note", "").startswith("false_positive") for a in pkmn_audit):
        if card.language == "ja":
            return "ja_constructed_deck_kit_no_permitted_image"
        return "no_permitted_exact_match_after_false_positive_reject"
    if card.language == "ja":
        return "ja_constructed_deck_kit_no_permitted_image"
    return "no_permitted_exact_match_found"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--upload", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    STATE.mkdir(parents=True, exist_ok=True)

    cards = load_cards()
    if args.limit:
        cards = cards[: args.limit]
    pkmn_by = audit_existing_pkmncards_candidates()
    try:
        key = pw.load_api_keys()[0]
    except SystemExit:
        key = ""

    ledger: list[dict[str, Any]] = []
    scan_rows: list[dict[str, str]] = []
    permission_rows: list[dict[str, str]] = []
    stats = Counter()

    print(json.dumps({"targets": len(cards), "upload": args.upload, "pw": bool(key)}))

    for i, card in enumerate(cards, 1):
        hits: list[Hit] = []
        if key:
            hits.extend(search_pokewallet_reid(card, key))
        if not any(h.auto and h.identity_ok for h in hits):
            hits.extend(search_pokemontcg(card))
        if not any(h.auto and h.identity_ok for h in hits):
            hits.extend(search_tcgdex(card))

        acquired = None
        for hit in hits:
            if hit.auto and hit.identity_ok:
                acquired = acquire_hit(card, hit, upload=args.upload, key=key)
                if acquired.get("status") == "ok":
                    stats["recovered"] += 1
                    if acquired.get("uploaded"):
                        stats["hosted"] += 1
                    stats[f"source:{hit.source}"] += 1
                    break
                acquired = None

        pkmn_audit = pkmn_by.get(card.canonical, [])
        blocker = classify_blocker(card, hits, pkmn_audit) if not acquired else "recovered"

        # Permission candidates: only keep if audit says not false positive AND URL unique-ish.
        for a in pkmn_audit:
            if str(a.get("audit_note", "")).startswith("false_positive"):
                stats["false_positive_candidates_rejected"] += 1
                continue
            permission_rows.append(
                {
                    "canonical_card_id": card.canonical,
                    "language": card.language,
                    "set_id": card.set_id,
                    "collector_number": card.collector,
                    "card_name": card.name,
                    "candidate_url": a.get("candidate_url") or "",
                    "page_url": a.get("page_url") or "",
                    "source": a.get("source") or "pkmncards",
                    "rights_status": "fan_site_permission_required",
                    "identity_status": "unverified_or_weak",
                    "audit_note": a.get("audit_note") or "",
                    "action": "do_not_rehost_without_written_permission_and_exact_identity_proof",
                }
            )

        row = {
            "canonical_card_id": card.canonical,
            "language": card.language,
            "set_id": card.set_id,
            "set_name": card.set_name,
            "collector_number": card.collector,
            "card_name": card.name,
            "variant": card.variant,
            "stamped_or_special": is_stamped_or_special(card),
            "blocker": blocker,
            "recovered": bool(acquired),
            "source": (acquired or {}).get("source"),
            "public_url": (acquired or {}).get("public"),
            "hit_count": len(hits),
            "identity_notes": "; ".join(h.identity_notes for h in hits[:3]),
            "pkmncards_audit": "; ".join(sorted({a.get("audit_note") or "" for a in pkmn_audit})) or "none",
            "reference_urls": " | ".join(
                u
                for u in [
                    str(card.card.get("imageLarge") or ""),
                    str(card.card.get("imageUrl") or ""),
                    *(a.get("page_url") or "" for a in pkmn_audit[:1]),
                ]
                if u
            ),
        }
        ledger.append(row)

        if not acquired:
            stats[f"blocker:{blocker}"] += 1
            scan_rows.append(
                {
                    "canonical_card_id": card.canonical,
                    "language": card.language,
                    "set_id": card.set_id,
                    "set_name": card.set_name,
                    "collector_number": card.collector,
                    "card_name": card.name,
                    "variant_or_stamp_notes": card.variant
                    or ("stamped/exclusive" if is_stamped_or_special(card) else "standard_printing_in_set"),
                    "exact_requirements": (
                        "Scan the exact printing: matching language, set, collector number, "
                        "artwork, and any staff/winner/WCS/signature stamp. "
                        "Do not submit a different set/edition even if the Pokémon is the same."
                    ),
                    "example_reference_url": (row["reference_urls"].split(" | ")[0] if row["reference_urls"] else ""),
                    "blocker": blocker,
                    "suggested_filename": f"{card.language}_{card.set_id}_{card.collector.replace('/', '-')}_{card.canonical.split('|')[-1]}.jpg",
                }
            )

        if i % 10 == 0 or i == len(cards):
            print(json.dumps({"progress": f"{i}/{len(cards)}", **dict(stats)}))

    # Write outputs
    REPORT.mkdir(parents=True, exist_ok=True)
    ledger_csv = REPORT / "en_jp_pass7_final67_ledger.csv"
    with ledger_csv.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(ledger[0].keys()))
        w.writeheader()
        w.writerows(ledger)

    scan_csv = REPORT / "en_jp_collector_scan_request_final67.csv"
    if scan_rows:
        with scan_csv.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(scan_rows[0].keys()))
            w.writeheader()
            w.writerows(scan_rows)

    perm_csv = REPORT / "en_jp_pass7_permission_candidates.csv"
    if permission_rows:
        with perm_csv.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(permission_rows[0].keys()))
            w.writeheader()
            w.writerows(permission_rows)
    else:
        # Explicit empty permission file stating false positives rejected
        with perm_csv.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(
                fh,
                fieldnames=[
                    "canonical_card_id",
                    "note",
                ],
            )
            w.writeheader()
            w.writerow(
                {
                    "canonical_card_id": "",
                    "note": "No verified permission candidates. Pass6 pkmncards rows were audited as false-positive shared search thumbnails.",
                }
            )

    # Markdown report
    by_blocker = Counter(r["blocker"] for r in ledger)
    recovered = [r for r in ledger if r["recovered"]]
    md = [
        "# CardScanR EN/JA Pass 7 — Final 67 accounting",
        "",
        f"Generated: `{utc_now()}`",
        "",
        "## Outcome",
        "",
        f"- Targets: **{len(cards)}**",
        f"- Recovered & hosted this pass: **{stats.get('hosted', 0)}**",
        f"- Still missing after pass: **{len(scan_rows)}**",
        "",
        "## Recoveries by source",
        "",
    ]
    for k, v in sorted(stats.items()):
        if k.startswith("source:"):
            md.append(f"- `{k.split(':',1)[1]}`: **{v}**")
    md += ["", "## Remaining blockers", ""]
    for k, v in by_blocker.most_common():
        if k == "recovered":
            continue
        md.append(f"- `{k}`: **{v}**")
    md += [
        "",
        "## Permission candidates",
        "",
        "Pass6 pkmncards candidates were re-audited. Shared `sv1_en_208.jpg` (and similar) "
        "search-page thumbnails are **false positives** and are not exact-identity matches. "
        "No fan-site URL is approved for rehost in this pass.",
        "",
        f"Permission CSV: `{perm_csv.name}`",
        "",
        "## Collector scan request",
        "",
        f"Cards requiring new exact scans: **{len(scan_rows)}**",
        f"CSV: `{scan_csv.name}`",
        "",
        "## Per-card ledger",
        "",
        f"CSV: `{ledger_csv.name}`",
        "",
    ]
    if recovered:
        md += ["### Recovered", ""]
        for r in recovered:
            md.append(
                f"- `{r['canonical_card_id']}` ← `{r['source']}` → `{r.get('public_url')}`"
            )
        md.append("")
    md += ["### Still missing (individual)", ""]
    for r in ledger:
        if r["recovered"]:
            continue
        md.append(
            f"- `{r['canonical_card_id']}` — {r['card_name']} [{r['language']} {r['set_id']} #{r['collector_number']}] — blocker=`{r['blocker']}`"
        )

    report_md = REPORT / "CARDSCANR_EN_JP_IMAGE_INDEPENDENCE_PASS7_FINAL67.md"
    report_md.write_text("\n".join(md) + "\n", encoding="utf-8")

    summary = {
        "generatedAtUtc": utc_now(),
        "targets": len(cards),
        "recovered": stats.get("recovered", 0),
        "hosted": stats.get("hosted", 0),
        "stillMissing": len(scan_rows),
        "blockerCounts": dict(by_blocker),
        "sourceCounts": {k: v for k, v in stats.items() if k.startswith("source:")},
        "falsePositiveCandidatesRejected": stats.get("false_positive_candidates_rejected", 0),
        "artifacts": {
            "ledger": str(ledger_csv),
            "scanRequest": str(scan_csv),
            "permissionCandidates": str(perm_csv),
            "report": str(report_md),
        },
    }
    (REPORT / "pass7_final67_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    MIRROR.mkdir(parents=True, exist_ok=True)
    for p in (ledger_csv, scan_csv, perm_csv, report_md, REPORT / "pass7_final67_summary.json"):
        if p.exists():
            (MIRROR / p.name).write_bytes(p.read_bytes())

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
