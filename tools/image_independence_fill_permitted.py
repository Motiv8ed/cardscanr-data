#!/usr/bin/env python3
"""Acquire permitted public-host EN/JA image gaps into local master (+ optional R2 upload)."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
CATALOGUE = ROOT / "public" / "v1" / "catalog" / "pokemon"
MASTER = ROOT / "data" / "images" / "master"
STATE = ROOT / "data" / "images" / "independence"
REPORT = Path(r"D:\CardScanR\reports\image_independence")
CDN = "https://cardscanr-images.andygore149.workers.dev"
BUCKET = "cardscanr-card-images"
WORKER = Path(r"D:\CardScanR\card_scanner_app\scripts\card_image_cloud\worker-cardscanr-images")
UA = "CardScanR-ImageIndependence/1.0"
PERMITTED = {"images.pokemontcg.io": "pokemon_tcg_api", "assets.tcgdex.net": "tcgdex"}
PROHIBITED = {
    "images.scrydex.com": "scrydex_rehost_prohibited",
    "api.pokewallet.io": "pokewallet_authenticated_acquire_path",
}
LOCK = threading.Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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


def http_get(url: str) -> bytes:
    last = None
    for i in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
            if not data:
                raise RuntimeError("empty")
            return data
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(min(2**i, 8))
    raise last  # type: ignore[misc]


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


def safe(seg: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", seg)


def master_dir(lang: str, set_id: str, card_id: str) -> Path:
    return MASTER / lang / safe(set_id) / safe(card_id)


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def covered_canonicals() -> set[str]:
    out: set[str] = set()
    if not MASTER.exists():
        return out
    for sidecar in MASTER.rglob("asset.json"):
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        cid = meta.get("canonicalBaseId")
        if cid and any(sidecar.parent.glob("display.*")):
            out.add(str(cid))
    return out


def load_cards() -> list[dict]:
    cards: list[dict] = []
    for folder, lang in (("en", "en"), ("jp", "ja")):
        for path in sorted((CATALOGUE / folder / "cards").glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            raw_cards = data.get("cards") if isinstance(data, dict) else data
            if not isinstance(raw_cards, list):
                continue
            for raw in raw_cards:
                if not isinstance(raw, dict):
                    continue
                set_id = str(raw.get("setId") or path.stem)
                collector = str(raw.get("collectorNumber") or "")
                external = raw.get("externalIds") if isinstance(raw.get("externalIds"), dict) else {}
                providers = raw.get("providerIds") if isinstance(raw.get("providerIds"), dict) else {}
                cands = []
                for v in (
                    external.get("pokemonTcgApiId"),
                    external.get("tcgdexCardId"),
                    providers.get("pokemonTcgApi"),
                    providers.get("tcgdex"),
                    f"{set_id}-{collector}",
                ):
                    if v and str(v) not in cands:
                        cands.append(str(v))
                urls = []
                for k in ("imageLarge", "imageUrlLarge", "imageUrl", "imageSmall", "imageUrlSmall"):
                    v = raw.get(k)
                    if isinstance(v, str) and v.strip() and v.strip() not in urls:
                        urls.append(v.strip())
                cards.append(
                    {
                        "canonical": str(raw.get("canonicalBaseId") or f"pokemon|{folder}|{set_id}|{collector}"),
                        "language": lang,
                        "set_id": set_id,
                        "collector": collector,
                        "candidates": cands,
                        "urls": urls,
                        "imageSource": str(raw.get("imageSource") or ""),
                    }
                )
    return cards


def choose_url(card: dict) -> tuple[str | None, str | None, str | None]:
    for url in card["urls"]:
        h = host_of(url)
        if h in PROHIBITED:
            continue
        if h in PERMITTED:
            return url, PERMITTED[h], None
    if not card["urls"]:
        return None, None, "source_absent"
    h = host_of(card["urls"][0])
    if h in PROHIBITED:
        return None, None, PROHIBITED[h]
    return None, None, f"no_permitted_source_host:{h or 'unknown'}"


def wrangler_put(key: str, path: Path, content_type: str) -> bool:
    cmd = [
        "npx",
        "wrangler",
        "r2",
        "object",
        "put",
        f"{BUCKET}/{key}",
        "--file",
        str(path),
        "--content-type",
        content_type,
        "--cache-control",
        "public, max-age=31536000, immutable",
        "--remote",
    ]
    r = subprocess.run(
        cmd,
        cwd=str(WORKER),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        shell=True,
    )
    return r.returncode == 0


def process(card: dict, upload: bool) -> dict:
    url, provider, reason = choose_url(card)
    if not url:
        return {"status": "skip", "reason": reason, "id": card["canonical"]}
    card_id = card["candidates"][0] if card["candidates"] else f"{card['set_id']}-{card['collector']}"
    dest = master_dir(card["language"], card["set_id"], card_id)
    try:
        raw = http_get(url)
        if not magic_ok(raw):
            return {"status": "fail", "reason": "invalid_payload", "id": card["canonical"], "url": url}
        # original
        ext = ".png" if raw[:4] == b"\x89PNG" else (".webp" if raw[8:12] == b"WEBP" else ".bin")
        write_atomic(dest / f"original{ext}", raw)
        webp = to_webp(raw)
        display = dest / "display.webp"
        write_atomic(display, webp)
        digest = sha256(webp)
        object_key = f"cards/{card['language']}/{card['set_id'].lower()}/{card_id.lower()}/{digest[:12]}/display.webp"
        alias = f"cards/{card['language']}/{card['set_id'].lower()}/{card_id.lower()}/display.webp"
        uploaded = False
        public = None
        if upload:
            uploaded = wrangler_put(object_key, display, "image/webp")
            if uploaded:
                pointer = dest / "display.webp.current.txt"
                pointer.write_text(object_key, encoding="utf-8")
                wrangler_put(f"{alias}.current", pointer, "text/plain")
                wrangler_put(alias, display, "image/webp")
                public = f"{CDN}/{alias}"
        meta = {
            "canonicalBaseId": card["canonical"],
            "cardId": card_id,
            "language": card["language"],
            "setId": card["set_id"],
            "collectorNumber": card["collector"],
            "sha256": digest,
            "byteSize": len(webp),
            "mimeType": "image/webp",
            "sourceProvider": provider,
            "originalSourceUrl": url,
            "hostedObjectKey": object_key if uploaded else None,
            "publicUrl": public,
            "acquisition": "permitted_public_acquire",
            "acquiredAt": utc_now(),
            "provenanceConfidence": "CONFIRMED",
            "derivativeStatus": "display_webp",
            "uploaded": uploaded,
            "hostingStatus": "hosted" if uploaded else "local_only_pending_upload",
        }
        write_atomic(dest / "asset.json", json.dumps(meta, indent=2).encode("utf-8"))
        return {"status": "ok", "id": card["canonical"], "uploaded": uploaded, "bytes": len(webp)}
    except urllib.error.HTTPError as exc:
        return {"status": "fail", "reason": f"http_{exc.code}", "id": card["canonical"], "url": url}
    except Exception as exc:  # noqa: BLE001
        return {"status": "fail", "reason": str(exc), "id": card["canonical"], "url": url}


def rebuild_reports(cards: list[dict], covered: set[str]) -> None:
    REPORT.mkdir(parents=True, exist_ok=True)
    master_rows = []
    unresolved = []
    for sidecar in MASTER.rglob("asset.json"):
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        display = sidecar.parent / "display.webp"
        master_rows.append(
            {
                "canonical_card_id": meta.get("canonicalBaseId", ""),
                "language": meta.get("language", ""),
                "set_id": meta.get("setId", ""),
                "collector_number": meta.get("collectorNumber", ""),
                "card_id": meta.get("cardId", ""),
                "local_path": str(display) if display.exists() else "",
                "sha256": meta.get("sha256", ""),
                "byte_size": meta.get("byteSize", ""),
                "mime_type": meta.get("mimeType", ""),
                "source_provider": meta.get("sourceProvider", ""),
                "original_source_url": meta.get("originalSourceUrl", ""),
                "hosted_object_key": meta.get("hostedObjectKey", ""),
                "public_url": meta.get("publicUrl", ""),
                "provenance_confidence": meta.get("provenanceConfidence", ""),
                "derivative_status": meta.get("derivativeStatus", ""),
                "acquisition": meta.get("acquisition", ""),
                "acquired_at": meta.get("acquiredAt", ""),
            }
        )
    by_canon = {r["canonical_card_id"]: r for r in master_rows if r["canonical_card_id"]}
    for card in cards:
        if card["canonical"] in by_canon and (by_canon[card["canonical"]].get("local_path")):
            continue
        url, provider, reason = choose_url(card)
        unresolved.append(
            {
                "canonical_card_id": card["canonical"],
                "language": card["language"],
                "set_id": card["set_id"],
                "collector_number": card["collector"],
                "attempted_sources": ",".join(sorted({host_of(u) for u in card["urls"]})),
                "failure_reason": reason or "acquire_failed",
                "category": reason or "acquire_failed",
                "next_recovery_path": (
                    "run_pokewallet_authenticated_acquire"
                    if reason and "pokewallet" in reason
                    else (
                        "do_not_mirror_without_written_authorization;_seek_alternate_lawful_source"
                        if reason and "scrydex" in reason
                        else "retry_or_manual_import"
                    )
                ),
            }
        )
    fields = list(master_rows[0].keys()) if master_rows else [
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
    ]
    with (REPORT / "en_jp_image_master_manifest.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(master_rows)
    ufields = [
        "canonical_card_id",
        "language",
        "set_id",
        "collector_number",
        "attempted_sources",
        "failure_reason",
        "category",
        "next_recovery_path",
    ]
    with (REPORT / "en_jp_unresolved_images.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=ufields)
        w.writeheader()
        w.writerows(unresolved)

    def count_lang(lang: str) -> dict:
        total = sum(1 for c in cards if c["language"] == lang)
        local = sum(1 for c in cards if c["language"] == lang and c["canonical"] in by_canon and by_canon[c["canonical"]].get("local_path"))
        hosted = sum(
            1
            for c in cards
            if c["language"] == lang and c["canonical"] in by_canon and by_canon[c["canonical"]].get("public_url")
        )
        unres = sum(1 for u in unresolved if u["language"] == lang)
        return {"total": total, "localMaster": local, "hosted": hosted, "unresolved": unres}

    cats: dict[str, int] = {}
    for u in unresolved:
        cats[u["category"]] = cats.get(u["category"], 0) + 1
    summary = {
        "generatedAtUtc": utc_now(),
        "en": count_lang("en"),
        "ja": count_lang("ja"),
        "masterRoot": str(MASTER.resolve()),
        "cdnBase": CDN,
        "unresolvedCategoryCounts": cats,
        "masterRows": len(master_rows),
    }
    (REPORT / "image_independence_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--upload", action="store_true")
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--report-only", action="store_true")
    args = parser.parse_args()
    STATE.mkdir(parents=True, exist_ok=True)
    cards = load_cards()
    covered = covered_canonicals()
    if args.report_only:
        rebuild_reports(cards, covered)
        return 0
    todo = []
    for card in cards:
        if card["canonical"] in covered and not args.upload:
            continue
        url, _, _ = choose_url(card)
        if url and card["canonical"] not in covered:
            todo.append(card)
        elif args.upload and card["canonical"] in covered:
            # upload local-only
            # find sidecar
            todo.append(card)
    # De-dupe upload queue: only local-only when upload and already covered
    if args.upload:
        refined = []
        for card in cards:
            if card["canonical"] not in covered:
                url, _, _ = choose_url(card)
                if url:
                    refined.append(card)
                continue
            # covered: upload if publicUrl missing
            # cheap scan by path candidates
            for cand in card["candidates"]:
                side = master_dir(card["language"], card["set_id"], cand) / "asset.json"
                if side.exists():
                    meta = json.loads(side.read_text(encoding="utf-8"))
                    if not meta.get("publicUrl"):
                        refined.append(card)
                    break
        todo = refined
    if args.limit:
        todo = todo[: args.limit]
    print("todo", len(todo), "covered_already", len(covered), flush=True)
    stats = {"ok": 0, "fail": 0, "uploaded": 0, "bytes": 0}
    failures = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futs = [pool.submit(process, c, args.upload) for c in todo]
        done = 0
        for fut in as_completed(futs):
            res = fut.result()
            done += 1
            if res["status"] == "ok":
                stats["ok"] += 1
                stats["bytes"] += int(res.get("bytes") or 0)
                if res.get("uploaded"):
                    stats["uploaded"] += 1
            elif res["status"] == "fail":
                stats["fail"] += 1
                failures.append(res)
            if done % 50 == 0 or done == len(todo):
                print(f"progress {done}/{len(todo)} {stats}", flush=True)
    (STATE / "fill_permitted_stats.json").write_text(
        json.dumps({"stats": stats, "failures": failures[:1000]}, indent=2), encoding="utf-8"
    )
    rebuild_reports(cards, covered_canonicals())
    print("final_stats", stats, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
