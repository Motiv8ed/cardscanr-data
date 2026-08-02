"""Resumable acquisition and technical validation for sealed-product images."""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import numpy as np
from PIL import Image

from .schema import connect
from .tcgdex import canonical_json, stable_id

VALIDATOR = "cardscanr-product-image-technical"
VALIDATOR_VERSION = "1.0.0"
USER_AGENT = "CardScanR-catalogue-preservation/1.0"
MAX_BYTES = 30 * 1024 * 1024
CHECKPOINT_SCHEMA = """
pragma journal_mode=wal;
create table if not exists assets(
 source_url text primary key,status text not null default 'pending',attempts integer not null default 0,
 attempted_at text,http_status integer,content_type text,byte_size integer,sha256 text,cache_path text,
 result_json text not null default '{}',error text
);
create table if not exists candidates(
 candidate_id text primary key,variant_id text not null,provider_id text not null,source_url text not null,
 foreign key(source_url) references assets(source_url)
);
"""
_thread_local = threading.local()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _client() -> httpx.Client:
    client = getattr(_thread_local, "client", None)
    if client is None:
        client = httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=45, http2=False)
        _thread_local.client = client
    return client


def _bits_hex(bits: np.ndarray) -> str:
    value = 0
    for bit in bits.flatten():
        value = (value << 1) | int(bool(bit))
    return f"{value:0{(bits.size + 3) // 4}x}"


def _phash(grayscale: Image.Image) -> str:
    values = np.asarray(grayscale.resize((32, 32), Image.Resampling.LANCZOS), dtype=np.float64)
    size = 32
    positions = np.arange(size)
    transform = np.cos(np.pi * (2 * positions[:, None] + 1) * positions[None, :] / (2 * size))
    transform[:, 0] *= 1 / np.sqrt(2)
    dct = (2 / size) * transform.T @ values @ transform
    low = dct[:8, :8]
    median = np.median(low.flatten()[1:])
    return _bits_hex(low > median)


def inspect_image(content: bytes) -> dict[str, Any]:
    if not content:
        raise ValueError("empty response")
    prefix = content[:512].lstrip().lower()
    if prefix.startswith((b"<!doctype html", b"<html", b"<?xml")):
        raise ValueError("response is an HTML/XML document, not an image")
    with Image.open(io.BytesIO(content)) as image:
        image.load()
        width, height = image.size
        image_format = image.format
        if not image_format or width < 32 or height < 32:
            raise ValueError(f"implausible decoded image: {image_format} {width}x{height}")
        grayscale = image.convert("L")
        ahash_values = np.asarray(grayscale.resize((8, 8), Image.Resampling.LANCZOS))
        ahash = _bits_hex(ahash_values > ahash_values.mean())
        dhash_values = np.asarray(grayscale.resize((9, 8), Image.Resampling.LANCZOS))
        dhash = _bits_hex(dhash_values[:, 1:] > dhash_values[:, :-1])
        return {
            "sha256": hashlib.sha256(content).hexdigest(), "byte_size": len(content),
            "width": width, "height": height, "format": image_format.upper(),
            "mode": image.mode, "average_hash": ahash, "difference_hash": dhash,
            "perceptual_hash": _phash(grayscale),
        }


def _extension(image_format: str) -> str:
    return {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif", "BMP": ".bmp"}.get(
        image_format.upper(), ".img"
    )


def _fetch(source_url: str, cache_root: Path, delay_seconds: float) -> dict[str, Any]:
    time.sleep(max(0.0, delay_seconds))
    attempted_at = utc_now()
    status = None
    content_type = None
    try:
        with _client().stream("GET", source_url) as response:
            status = response.status_code
            content_type = response.headers.get("content-type")
            if status in (404, 410):
                return {"status": "not_found", "attempted_at": attempted_at, "http_status": status,
                        "content_type": content_type, "error": f"HTTP {status}"}
            if status in (401, 403, 429) or status >= 500:
                return {"status": "retryable_error", "attempted_at": attempted_at, "http_status": status,
                        "content_type": content_type, "error": f"HTTP {status}"}
            response.raise_for_status()
            chunks = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > MAX_BYTES:
                    raise ValueError(f"image exceeds {MAX_BYTES} bytes")
                chunks.append(chunk)
        content = b"".join(chunks)
        result = inspect_image(content)
        cache_path = cache_root / result["sha256"][:2] / f"{result['sha256']}{_extension(result['format'])}"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        if not cache_path.exists():
            cache_path.write_bytes(content)
        return {"status": "pass", "attempted_at": attempted_at, "http_status": status,
                "content_type": content_type, "cache_path": str(cache_path), "result": result}
    except (httpx.HTTPError, OSError) as error:
        return {"status": "retryable_error", "attempted_at": attempted_at,
                "http_status": status, "content_type": content_type,
                "error": f"{type(error).__name__}: {error}"}
    except Exception as error:
        return {"status": "fail", "attempted_at": attempted_at,
                "http_status": status, "content_type": content_type,
                "error": f"{type(error).__name__}: {error}"}


def register_candidates(database: Path, checkpoint: Path) -> dict[str, int]:
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    staging = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    progress = sqlite3.connect(checkpoint)
    try:
        progress.executescript(CHECKPOINT_SCHEMA)
        rows = staging.execute(
            "select id,sealed_product_variant_id,provider_id,source_url from product_image_candidate order by id"
        ).fetchall()
        with progress:
            for candidate_id, variant_id, provider_id, source_url in rows:
                progress.execute("insert or ignore into assets(source_url) values (?)", (source_url,))
                progress.execute(
                    "insert or replace into candidates values (?,?,?,?)",
                    (candidate_id, variant_id, provider_id, source_url),
                )
        return {"candidates": len(rows), "distinct_urls": progress.execute("select count(*) from assets").fetchone()[0]}
    finally:
        staging.close()
        progress.close()


def acquire(checkpoint: Path, cache_root: Path, workers: int = 4, limit: int | None = None,
            delay_seconds: float = 0.05, providers: list[str] | None = None) -> dict[str, int]:
    progress = sqlite3.connect(checkpoint)
    try:
        query = "select a.source_url from assets a where a.status in ('pending','retryable_error') and a.attempts<3"
        parameters: list[str] = []
        if providers:
            placeholders = ",".join("?" for _ in providers)
            query += f" and exists (select 1 from candidates c where c.source_url=a.source_url and c.provider_id in ({placeholders}))"
            parameters.extend(providers)
        query += " order by a.source_url"
        urls = [row[0] for row in progress.execute(query, parameters).fetchall()]
        if limit is not None:
            urls = urls[:limit]
        counters: dict[str, int] = {"selected": len(urls)}
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = {executor.submit(_fetch, url, cache_root, delay_seconds): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                result = future.result()
                details = result.get("result") or {}
                with progress:
                    progress.execute(
                        """update assets set status=?,attempts=attempts+1,attempted_at=?,http_status=?,
                           content_type=?,byte_size=?,sha256=?,cache_path=?,result_json=?,error=? where source_url=?""",
                        (result["status"], result["attempted_at"], result.get("http_status"),
                         result.get("content_type"), details.get("byte_size"), details.get("sha256"),
                         result.get("cache_path"), canonical_json(details), result.get("error"), url),
                    )
                counters[result["status"]] = counters.get(result["status"], 0) + 1
        return counters
    finally:
        progress.close()


def apply_results(database: Path, checkpoint: Path) -> dict[str, int]:
    progress = sqlite3.connect(f"file:{checkpoint.resolve()}?mode=ro", uri=True)
    progress.row_factory = sqlite3.Row
    staging = connect(str(database))
    counters: Counter[str] = Counter()
    try:
        rows = progress.execute(
            """select c.*,a.status,a.attempted_at,a.http_status,a.content_type,a.byte_size,a.sha256,
                      a.cache_path,a.result_json,a.error
                 from candidates c join assets a on a.source_url=c.source_url
                where a.status!='pending' order by c.candidate_id"""
        ).fetchall()
        with staging:
            for row in rows:
                status = row["status"]
                outcome = {"pass": "acquired", "not_found": "not_found", "fail": "invalid",
                           "retryable_error": "retryable_error"}[status]
                evidence = {
                    "http_status": row["http_status"], "content_type": row["content_type"],
                    "cache_path": row["cache_path"], "technical": json.loads(row["result_json"] or "{}"),
                    "error": row["error"], "rights_decision": "preserved_from_candidate",
                }
                attempt_id = stable_id("product-image-attempt", row["candidate_id"], VALIDATOR)
                staging.execute(
                    """insert into image_acquisition_attempt values (?,?,?,?,?,?,?,?,?)
                       on conflict(id) do update set attempted_at=excluded.attempted_at,http_status=excluded.http_status,
                        outcome=excluded.outcome,evidence_json=excluded.evidence_json""",
                    (attempt_id, "sealed_product_variant", row["variant_id"], row["provider_id"], row["source_url"],
                     row["attempted_at"], row["http_status"], outcome, canonical_json(evidence)),
                )
                validation_status = "pass" if status == "pass" else ("warning" if status == "retryable_error" else "fail")
                validation_id = stable_id("product-image-validation", row["candidate_id"], VALIDATOR)
                staging.execute(
                    """insert into image_validation_result values (?,null,?,?,?,?,?,?)
                       on conflict(id) do update set status=excluded.status,checks_json=excluded.checks_json,
                        checked_at=excluded.checked_at""",
                    (validation_id, row["candidate_id"], VALIDATOR, VALIDATOR_VERSION, validation_status,
                     canonical_json({
                         "http_availability": {"status": "pass" if status == "pass" else validation_status,
                                               "http_status": row["http_status"]},
                         "decode_and_dimensions": {"status": validation_status, **evidence},
                         "watermark_or_seller_background": {"status": "not_evaluated"},
                         "identity_match": {"status": "not_evaluated"},
                     }), row["attempted_at"]),
                )
                if status == "pass":
                    staging.execute("update product_image_candidate set validation_status='verified' where id=?",
                                    (row["candidate_id"],))
                elif status in ("not_found", "fail"):
                    staging.execute("update product_image_candidate set validation_status='invalid' where id=?",
                                    (row["candidate_id"],))
                counters[status] += 1
        counters["applied"] = len(rows)
        return dict(counters)
    finally:
        staging.close()
        progress.close()


def checkpoint_counts(checkpoint: Path) -> dict[str, int]:
    connection = sqlite3.connect(checkpoint)
    try:
        return {status: count for status, count in connection.execute(
            "select status,count(*) from assets group by status order by status"
        )}
    finally:
        connection.close()
