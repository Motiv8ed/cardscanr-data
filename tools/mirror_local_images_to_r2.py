#!/usr/bin/env python3
"""Mirror locally acquired catalogue image bytes to CardScanR R2.

Uploads immutable SHA-addressed WebP derivatives:
  v2/catalog/pokemon/images/by-sha/<sha256>/display.webp
  v2/catalog/pokemon/images/by-sha/<sha256>/thumb.webp

Original source URLs remain provenance only. App-facing URLs must use R2.
"""

from __future__ import annotations

import argparse
import io
import json
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = Path(r"D:\CardScanR_worldwide_runtime_20260802")
DEFAULT_CHECKPOINT = RUNTIME / "image_mirror_r2" / "mirror_checkpoint.sqlite"
DEFAULT_REPORT = ROOT / "reports" / "cloudflare_migration" / "IMAGE_MIRROR_PROGRESS.json"
CACHE_CONTROL = "public, max-age=31536000, immutable"
CARDSCANR_HOST_MARKERS = ("r2.dev", "cardscanr", "pages.dev", "andygore149.workers.dev")


@dataclass(frozen=True)
class Asset:
    source_url: str
    sha256: str
    cache_path: Path
    byte_size: int
    content_type: str | None
    entity_kind: str


def utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_cloudflare_config(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def is_cardscanr_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(marker in host for marker in CARDSCANR_HOST_MARKERS)


def iter_checkpoint_assets(checkpoint: Path, entity_kind: str) -> Iterator[Asset]:
    connection = sqlite3.connect(f"file:{checkpoint.resolve().as_posix()}?mode=ro", uri=True)
    try:
        # Avoid full-table scans on huge pending-only checkpoints.
        has_pass = connection.execute(
            "select 1 from assets where status = 'pass' limit 1"
        ).fetchone()
        if has_pass is None:
            return
        rows = connection.execute(
            """
            select source_url, sha256, cache_path, coalesce(byte_size, 0), content_type
            from assets
            where status = 'pass'
              and sha256 is not null
              and length(sha256) = 64
              and cache_path is not null
              and cache_path != ''
            """
        )
        for source_url, sha256, cache_path, byte_size, content_type in rows:
            path = Path(cache_path)
            if not path.is_file():
                continue
            yield Asset(
                source_url=str(source_url),
                sha256=str(sha256).casefold(),
                cache_path=path,
                byte_size=int(byte_size or 0),
                content_type=content_type,
                entity_kind=entity_kind,
            )
    finally:
        connection.close()


def discover_assets() -> list[Asset]:
    by_sha: dict[str, Asset] = {}
    # Prefer regional pass checkpoints; skip accidental nested runtimes.
    checkpoints = sorted(
        path
        for path in RUNTIME.glob("card_image_validation*/checkpoint.sqlite")
        if "accidental_nested" not in path.as_posix()
        # Root EN probe DB is pending-only (~411k rows) and has no pass assets.
        and path.parent.name != "card_image_validation"
    )
    for checkpoint in checkpoints:
        print(f"discover {checkpoint.parent.name}", flush=True)
        for asset in iter_checkpoint_assets(checkpoint, "card"):
            by_sha.setdefault(asset.sha256, asset)
    product_checkpoint = RUNTIME / "product_image_validation" / "checkpoint.sqlite"
    if product_checkpoint.is_file():
        print("discover product_image_validation", flush=True)
        for asset in iter_checkpoint_assets(product_checkpoint, "product"):
            by_sha.setdefault(asset.sha256, asset)
    print(f"discover complete unique={len(by_sha)}", flush=True)
    return list(by_sha.values())


def ensure_checkpoint(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    connection.execute(
        """
        create table if not exists mirrored (
          sha256 text primary key,
          source_url text not null,
          entity_kind text not null,
          display_key text not null,
          thumb_key text not null,
          display_url text not null,
          thumb_url text not null,
          source_bytes integer not null,
          display_bytes integer not null,
          thumb_bytes integer not null,
          technical_status text not null,
          rights_status text not null default 'unknown',
          mirrored_at text not null
        )
        """
    )
    connection.commit()
    return connection


def make_derivatives(source: Path, *, display_max: int, thumb_max: int) -> tuple[bytes, bytes, int, int]:
    # Some official PNGs embed oversized iTXt/zTXt chunks that trip Pillow defaults.
    Image.MAX_IMAGE_PIXELS = None
    try:
        from PIL import PngImagePlugin

        PngImagePlugin.MAX_TEXT_CHUNK = 100 * 1024 * 1024
        PngImagePlugin.MAX_TEXT_MEMORY = 200 * 1024 * 1024
    except Exception:  # noqa: BLE001 - best-effort Pillow tuning
        pass
    with Image.open(source) as image:
        image.load()
        image = image.convert("RGBA") if image.mode in {"P", "RGBA", "LA"} else image.convert("RGB")
        display = image.copy()
        display.thumbnail((display_max, display_max), Image.Resampling.LANCZOS)
        thumb = image.copy()
        thumb.thumbnail((thumb_max, thumb_max), Image.Resampling.LANCZOS)
        display_buf = io.BytesIO()
        thumb_buf = io.BytesIO()
        if display.mode != "RGBA":
            display = display.convert("RGB")
        if thumb.mode != "RGBA":
            thumb = thumb.convert("RGB")
        display.save(display_buf, format="WEBP", quality=80, method=0)
        thumb.save(thumb_buf, format="WEBP", quality=78, method=0)
        display_bytes = display_buf.getvalue()
        thumb_bytes = thumb_buf.getvalue()
        return display_bytes, thumb_bytes, max(display.size), max(thumb.size)


def object_exists(client: Any, bucket: str, key: str, expected_size: int | None = None) -> bool:
    try:
        head = client.head_object(Bucket=bucket, Key=key)
    except ClientError:
        return False
    if expected_size is not None and int(head.get("ContentLength") or 0) != expected_size:
        return False
    return True


def put_bytes(client: Any, *, bucket: str, key: str, data: bytes, content_type: str) -> str:
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=data,
        ContentType=content_type,
        CacheControl=CACHE_CONTROL,
    )
    return "uploaded"


def public_url(base: str, key: str) -> str:
    return f"{base.rstrip('/')}/{key.lstrip('/')}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=ROOT / "cloudflare_env.local.json")
    parser.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--display-max", type=int, default=800)
    parser.add_argument("--thumb-max", type=int, default=300)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = load_cloudflare_config(args.config)
    bucket = cfg["r2Bucket"]
    public_base = cfg.get("r2PublicBaseUrl") or cfg.get("r2PublicDevUrl")
    if not public_base:
        print("missing r2PublicBaseUrl", file=sys.stderr)
        return 2
    thread_local = threading.local()

    def client_for_thread() -> Any:
        client = getattr(thread_local, "client", None)
        if client is None:
            client = boto3.client(
                "s3",
                endpoint_url=cfg["r2S3Endpoint"],
                aws_access_key_id=cfg["r2AccessKeyId"],
                aws_secret_access_key=cfg["r2SecretAccessKey"],
                region_name="auto",
                config=Config(signature_version="s3v4", max_pool_connections=max(16, args.workers * 2)),
            )
            thread_local.client = client
        return client

    assets = discover_assets()
    assets.sort(key=lambda item: (item.entity_kind, item.sha256))
    if args.limit:
        assets = assets[: args.limit]

    checkpoint = ensure_checkpoint(args.checkpoint)
    checkpoint.execute("PRAGMA journal_mode=WAL")
    already = {
        row[0]
        for row in checkpoint.execute("select sha256 from mirrored")
    }
    pending = [asset for asset in assets if asset.sha256 not in already]
    uploaded = 0
    existed = 0
    skipped = len(assets) - len(pending)
    failed = 0
    source_bytes = 0
    display_bytes_total = 0
    thumb_bytes_total = 0
    failures: list[dict[str, str]] = []
    lock = threading.Lock()
    done = 0

    def process_one(asset: Asset) -> dict[str, Any]:
        display_key = f"v2/catalog/pokemon/images/by-sha/{asset.sha256}/display.webp"
        thumb_key = f"v2/catalog/pokemon/images/by-sha/{asset.sha256}/thumb.webp"
        display_data, thumb_data, _, _ = make_derivatives(
            asset.cache_path, display_max=args.display_max, thumb_max=args.thumb_max
        )
        source_size = asset.byte_size or asset.cache_path.stat().st_size
        if args.dry_run:
            return {
                "status": "dry_run",
                "asset": asset,
                "display_key": display_key,
                "thumb_key": thumb_key,
                "display_data": display_data,
                "thumb_data": thumb_data,
                "source_size": source_size,
            }
        client = client_for_thread()
        put_bytes(client, bucket=bucket, key=display_key, data=display_data, content_type="image/webp")
        put_bytes(client, bucket=bucket, key=thumb_key, data=thumb_data, content_type="image/webp")
        return {
            "status": "uploaded",
            "asset": asset,
            "display_key": display_key,
            "thumb_key": thumb_key,
            "display_data": display_data,
            "thumb_data": thumb_data,
            "source_size": source_size,
        }

    batch_size = max(args.workers * 8, 64)
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for batch_start in range(0, len(pending), batch_size):
            batch = pending[batch_start : batch_start + batch_size]
            futures = {pool.submit(process_one, asset): asset for asset in batch}
            for future in as_completed(futures):
                asset = futures[future]
                try:
                    result = future.result()
                    with lock:
                        done += 1
                        source_bytes += int(result["source_size"])
                        display_bytes_total += len(result["display_data"])
                        thumb_bytes_total += len(result["thumb_data"])
                        if result["status"] == "uploaded":
                            uploaded += 1
                            display_url = public_url(public_base, result["display_key"])
                            thumb_url = public_url(public_base, result["thumb_key"])
                            checkpoint.execute(
                                """
                                insert or replace into mirrored(
                                  sha256, source_url, entity_kind, display_key, thumb_key, display_url, thumb_url,
                                  source_bytes, display_bytes, thumb_bytes, technical_status, rights_status, mirrored_at
                                ) values (?,?,?,?,?,?,?,?,?,?,?,?,?)
                                """,
                                (
                                    asset.sha256,
                                    asset.source_url,
                                    asset.entity_kind,
                                    result["display_key"],
                                    result["thumb_key"],
                                    display_url,
                                    thumb_url,
                                    int(result["source_size"]),
                                    len(result["display_data"]),
                                    len(result["thumb_data"]),
                                    "mirrored_to_r2",
                                    "unknown",
                                    utc_iso(),
                                ),
                            )
                            if uploaded % 20 == 0:
                                checkpoint.commit()
                        elif result["status"] == "dry_run":
                            skipped += 1
                        if done % 50 == 0 or done == len(pending):
                            checkpoint.commit()
                            print(
                                json.dumps(
                                    {
                                        "progress": done,
                                        "pending": len(pending),
                                        "total": len(assets),
                                        "uploaded": uploaded,
                                        "skipped": skipped,
                                        "failed": failed,
                                    }
                                ),
                                flush=True,
                            )
                except Exception as exc:  # noqa: BLE001 - continue batch
                    with lock:
                        failed += 1
                        done += 1
                        failures.append(
                            {"sha256": asset.sha256, "error": repr(exc), "path": str(asset.cache_path)}
                        )
                        if done % 50 == 0 or done == len(pending):
                            print(
                                json.dumps(
                                    {
                                        "progress": done,
                                        "pending": len(pending),
                                        "total": len(assets),
                                        "uploaded": uploaded,
                                        "skipped": skipped,
                                        "failed": failed,
                                    }
                                ),
                                flush=True,
                            )
            checkpoint.commit()
    checkpoint.commit()

    report = {
        "generatedAt": utc_iso(),
        "bucket": bucket,
        "publicBaseUrl": public_base,
        "discoveredUniqueSha": len(assets),
        "uploaded": uploaded,
        "existed": existed,
        "skippedAlreadyMirrored": skipped,
        "failed": failed,
        "sourceBytesProcessed": source_bytes,
        "displayBytesUploadedApprox": display_bytes_total,
        "thumbBytesUploadedApprox": thumb_bytes_total,
        "checkpoint": str(args.checkpoint),
        "failuresSample": failures[:50],
        "dryRun": bool(args.dry_run),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    map_path = args.checkpoint.parent / "mirror_map.jsonl"
    with map_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in checkpoint.execute(
            "select sha256, source_url, entity_kind, display_url, thumb_url, technical_status from mirrored order by sha256"
        ):
            handle.write(
                json.dumps(
                    {
                        "sha256": row[0],
                        "sourceUrl": row[1],
                        "entityKind": row[2],
                        "displayUrl": row[3],
                        "thumbnailUrl": row[4],
                        "technicalStatus": row[5],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
    checkpoint.close()
    print(json.dumps({"ok": failed == 0, "report": str(args.report), "map": str(map_path)}, indent=2))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
