#!/usr/bin/env python3
"""Upload local master display.webp assets missing publicUrl to R2 via Cloudflare API."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MASTER = ROOT / "data" / "images" / "master"
STATE = ROOT / "data" / "images" / "independence"
CDN = "https://cardscanr-images.andygore149.workers.dev"
BUCKET = "cardscanr-card-images"
ACCOUNT = "bf8ce806dea1ac650343311ac77c35ee"
WRANGLER_TOML = Path.home() / "AppData/Roaming/xdg.config/.wrangler/config/default.toml"
WORKER = Path(r"D:\CardScanR\card_scanner_app\scripts\card_image_cloud\worker-cardscanr-images")
PROGRESS = STATE / "upload_progress.jsonl"
LOCK = threading.Lock()
TOKEN_LOCK = threading.Lock()
_oauth_token: str | None = None
_token_loaded_at = 0.0


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def refresh_wrangler_token() -> str:
    global _oauth_token, _token_loaded_at
    try:
        subprocess.run(
            ["npx", "wrangler", "whoami"],
            cwd=str(WORKER),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            shell=True,
        )
    except Exception:
        pass
    text = WRANGLER_TOML.read_text(encoding="utf-8")
    m = re.search(r'oauth_token\s*=\s*"([^"]+)"', text)
    if not m:
        raise RuntimeError("oauth_token missing")
    with TOKEN_LOCK:
        _oauth_token = m.group(1)
        _token_loaded_at = time.time()
        return _oauth_token


def get_token() -> str:
    global _oauth_token, _token_loaded_at
    with TOKEN_LOCK:
        if _oauth_token and (time.time() - _token_loaded_at) < 1500:
            return _oauth_token
    return refresh_wrangler_token()


def r2_put(key: str, data: bytes, content_type: str) -> None:
    last: Exception | None = None
    for i in range(8):
        token = get_token()
        url = (
            f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/r2/buckets/"
            f"{BUCKET}/objects/{urllib.parse.quote(key, safe='')}"
        )
        try:
            req = urllib.request.Request(
                url,
                data=data,
                method="PUT",
                headers={"Authorization": f"Bearer {token}", "Content-Type": content_type},
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            if not body.get("success"):
                raise RuntimeError(str(body.get("errors") or body)[:300])
            return
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code in (401, 403):
                refresh_wrangler_token()
                time.sleep(2)
            elif exc.code == 429:
                time.sleep(min(4 * (2**i), 60))
            else:
                time.sleep(min(2**i, 15))
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(min(2**i, 15))
    assert last is not None
    raise last


def append(row: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    with LOCK:
        with PROGRESS.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_done_ok() -> set[str]:
    done: set[str] = set()
    if not PROGRESS.exists():
        return done
    with PROGRESS.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("status") == "ok" and row.get("id"):
                done.add(str(row["id"]))
    return done


def collect_todo(only_acquisition: str, done: set[str]) -> list[Path]:
    todo: list[Path] = []
    for sidecar in MASTER.rglob("asset.json"):
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except Exception:
            continue
        cid = str(meta.get("canonicalBaseId") or "")
        if not cid or cid in done:
            continue
        if meta.get("publicUrl"):
            continue
        if only_acquisition and meta.get("acquisition") != only_acquisition:
            continue
        if not (sidecar.parent / "display.webp").exists():
            continue
        todo.append(sidecar)
    return todo


def upload_one(sidecar: Path) -> dict:
    meta = json.loads(sidecar.read_text(encoding="utf-8"))
    cid = str(meta.get("canonicalBaseId") or sidecar.parent.name)
    display = sidecar.parent / "display.webp"
    lang = str(meta.get("language") or "")
    set_id = str(meta.get("setId") or "")
    card_id = str(meta.get("cardId") or sidecar.parent.name)
    digest = str(meta.get("sha256") or "")
    if not (lang and set_id and card_id and digest and display.exists()):
        row = {"status": "fail", "id": cid, "reason": "incomplete_meta", "at": utc_now()}
        append(row)
        return row
    alias = f"cards/{lang}/{set_id.lower()}/{card_id.lower()}/display.webp"
    object_key = f"cards/{lang}/{set_id.lower()}/{card_id.lower()}/{digest[:12]}/display.webp"
    try:
        raw = display.read_bytes()
        r2_put(alias, raw, "image/webp")
        public = f"{CDN}/{alias}"
        meta["hostedObjectKey"] = object_key
        meta["hostedAliasKey"] = alias
        meta["publicUrl"] = public
        meta["uploaded"] = True
        meta["hostingStatus"] = "hosted"
        meta["uploadedAt"] = utc_now()
        sidecar.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        (sidecar.parent / "display.webp.current.txt").write_text(alias, encoding="utf-8")
        row = {"status": "ok", "id": cid, "key": alias, "public": public, "at": utc_now()}
        append(row)
        return row
    except Exception as exc:  # noqa: BLE001
        row = {"status": "fail", "id": cid, "reason": str(exc)[:300], "at": utc_now()}
        append(row)
        return row


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--only-acquisition", default="multisource_resolver")
    args = parser.parse_args()

    refresh_wrangler_token()
    done = load_done_ok()
    print(f"scanning master under {MASTER} done_ok={len(done)}", flush=True)
    todo = collect_todo(args.only_acquisition, done)
    if args.limit:
        todo = todo[: args.limit]
    print(json.dumps({"todo": len(todo), "already_done_progress": len(done), "concurrency": args.concurrency}), flush=True)

    stats = {"ok": 0, "fail": 0}
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futs = [pool.submit(upload_one, p) for p in todo]
        n = 0
        for fut in as_completed(futs):
            res = fut.result()
            n += 1
            stats[res.get("status", "fail")] = stats.get(res.get("status", "fail"), 0) + 1
            if n % 50 == 0 or n == len(todo):
                print(f"progress {n}/{len(todo)} {stats}", flush=True)
                sys.stdout.flush()
            if n % 500 == 0:
                refresh_wrangler_token()
    (STATE / "upload_stats.json").write_text(json.dumps(stats, indent=2), encoding="utf-8")
    print("final", stats, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
