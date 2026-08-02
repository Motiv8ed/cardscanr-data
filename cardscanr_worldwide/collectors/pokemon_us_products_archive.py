"""Collect the official US Pokémon TCG product gallery from public archive captures."""

from __future__ import annotations

import hashlib
import json
import random
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

USER_AGENT = "CardScanR-catalogue-preservation/1.0"
PREFIX = "/us/pokemon-tcg/product-gallery/"
NON_PRODUCT_SLUGS = {"undefinedregions"}
CDX_URL = (
    "https://web.archive.org/cdx/search/cdx?url=www.pokemon.com/us/pokemon-tcg/product-gallery/*"
    "&from=2015&to=2026&output=json&filter=statuscode:200&fl=timestamp,original,digest,statuscode"
    "&collapse=urlkey&sort=reverse&limit=5000"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_url(url: str) -> str:
    absolute = urljoin("https://www.pokemon.com", url)
    parsed = urlsplit(absolute)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def parse_cdx(payload: bytes) -> list[dict[str, str]]:
    data = json.loads(payload)
    header = ["timestamp", "original", "digest", "statuscode"]
    if not data or data[0] != header:
        raise ValueError("Unexpected US product gallery CDX response")
    latest: dict[str, dict[str, str]] = {}
    for values in data[1:]:
        row = dict(zip(header, values))
        path = urlsplit(row["original"]).path.rstrip("/")
        if not path.startswith(PREFIX):
            continue
        slug = path[len(PREFIX):]
        if not slug or "/" in slug or not re.fullmatch(r"[a-z0-9-]+", slug) or slug in NON_PRODUCT_SLUGS:
            continue
        if slug not in latest or row["timestamp"] > latest[slug]["timestamp"]:
            row["provider_record_id"] = slug
            row["replay_url"] = f"https://web.archive.org/web/{row['timestamp']}id_/{row['original']}"
            latest[slug] = row
    return [latest[key] for key in sorted(latest)]


def _text(node) -> str | None:
    return " ".join(node.get_text(" ", strip=True).split()) if node else None


def parse_product(html: str, source_url: str, provider_record_id: str) -> dict[str, object]:
    soup = BeautifulSoup(html, "html.parser")
    title_node = soup.select_one("h1.us-title") or soup.select_one("h1")
    body = soup.select_one(".full-article-body")
    if not title_node or not body:
        raise ValueError("Archived official page does not contain a product detail")
    date_text = _text(soup.select_one(".date.generic-date")) or ""
    date_match = re.search(r"Launch:\s*([A-Za-z]+\s+\d{1,2},\s+\d{4})", date_text)
    contents = [_text(node) for node in body.select("li") if _text(node)]
    images = []
    seen = set()
    for image in soup.find_all("img"):
        src = next(
            (str(image.get(attribute)) for attribute in ("src", "data-src", "data-preload-src")
             if image.get(attribute)),
            "",
        )
        if not re.search(r"(?:assets\.pokemon\.com|/static-assets/).*/trading-card-game/", src):
            continue
        url = canonical_url(src)
        if url not in seen:
            images.append({"source_url": src, "canonical_url": url, "alt": image.get("alt")})
            seen.add(url)
    paragraphs = [_text(node) for node in body.find_all("p", recursive=False) if _text(node)]
    return {
        "provider_record_id": provider_record_id, "source_url": source_url,
        "local_name": _text(title_node), "release_date_text": date_match.group(1) if date_match else None,
        "description": "\n".join(paragraphs) if paragraphs else _text(body),
        "contents": contents, "images": images,
    }


SCHEMA = """
pragma journal_mode=wal;
create table if not exists fetch_log(url text primary key,status integer,sha256 text,byte_size integer,
 content_type text,storage_path text,fetched_at text,error text);
create table if not exists products(provider_record_id text primary key,source_url text not null,
 replay_url text not null,archive_timestamp text not null,archive_digest text not null,
 local_name text,parsed_json text,raw_sha256 text,status text not null,error text,updated_at text not null);
create table if not exists runs(id text primary key,status text not null,started_at text not null,
 completed_at text,counters_json text not null default '{}',error text);
"""


class Collector:
    def __init__(self, runtime_root: Path, delay_seconds: float = 0.5):
        self.root = runtime_root.resolve() / "pokemon-us-products-archive"
        self.raw = self.root / "raw"
        self.raw.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.root / "checkpoint.sqlite")
        self.connection.executescript(SCHEMA)
        self.client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=60, follow_redirects=True, http2=False)
        self.delay_seconds = delay_seconds

    def close(self) -> None:
        self.connection.close()
        self.client.close()

    def fetch(self, url: str, suffix: str) -> bytes:
        cached = self.connection.execute(
            "select storage_path from fetch_log where url=? and status=200", (url,),
        ).fetchone()
        if cached and cached[0] and Path(cached[0]).exists():
            return Path(cached[0]).read_bytes()
        last_error: Exception | None = None
        for attempt in range(5):
            time.sleep(self.delay_seconds + random.uniform(0, self.delay_seconds * 0.2))
            try:
                response = self.client.get(url)
                response.raise_for_status()
                content = response.content
                checksum = sha256(content)
                path = self.raw / f"{checksum}{suffix}"
                if not path.exists():
                    path.write_bytes(content)
                self.connection.execute(
                    "insert or replace into fetch_log values (?, ?, ?, ?, ?, ?, ?, null)",
                    (url, response.status_code, checksum, len(content), response.headers.get("content-type"),
                     str(path), utc_now()),
                )
                self.connection.commit()
                return content
            except Exception as error:
                last_error = error
                if attempt < 4:
                    time.sleep(min(2 ** attempt, 8))
        self.connection.execute(
            "insert or replace into fetch_log(url,fetched_at,error) values (?, ?, ?)",
            (url, utc_now(), f"{type(last_error).__name__}: {last_error}"),
        )
        self.connection.commit()
        raise last_error or RuntimeError("Unknown archive fetch error")

    def fallback_captures(self, slug: str) -> list[dict[str, str]]:
        header = ["timestamp", "original", "digest", "statuscode"]
        expected_path = f"{PREFIX}{slug}"
        rows: dict[tuple[str, str, str], dict[str, str]] = {}
        for host in ("www.pokemon.com", "pokemon.com"):
            query = urlencode({
                "url": f"{host}{expected_path}*",
                "from": "2015", "to": "2026", "output": "json", "filter": "statuscode:200",
                "fl": "timestamp,original,digest,statuscode", "collapse": "digest",
                "sort": "reverse", "limit": "100",
            })
            try:
                data = json.loads(self.fetch(f"https://web.archive.org/cdx/search/cdx?{query}", ".json"))
            except (json.JSONDecodeError, OSError, httpx.HTTPError):
                continue
            if not data or data[0] != header:
                continue
            for values in data[1:]:
                row = dict(zip(header, values))
                if urlsplit(row["original"]).path.rstrip("/") != expected_path:
                    continue
                row["replay_url"] = f"https://web.archive.org/web/{row['timestamp']}id_/{row['original']}"
                rows[(row["timestamp"], row["original"], row["digest"])] = row
        return sorted(rows.values(), key=lambda row: row["timestamp"], reverse=True)

    def run(self) -> dict[str, int]:
        run_id = sha256(utc_now().encode())[:24]
        self.connection.execute("insert into runs(id,status,started_at) values (?, 'running', ?)", (run_id, utc_now()))
        self.connection.commit()
        counters = {"indexed_products": 0, "parsed_products": 0, "errors": 0}
        try:
            entries = parse_cdx(self.fetch(CDX_URL, ".json"))
            counters["indexed_products"] = len(entries)
            for entry in entries:
                existing = self.connection.execute(
                    "select status from products where provider_record_id=?", (entry["provider_record_id"],),
                ).fetchone()
                if existing and existing[0] == "parsed":
                    counters["parsed_products"] += 1
                    continue
                try:
                    content = self.fetch(entry["replay_url"], ".html")
                    parsed = parse_product(content.decode("utf-8"), entry["original"], entry["provider_record_id"])
                except Exception as initial_error:
                    parsed = None
                    try:
                        fallbacks = self.fallback_captures(entry["provider_record_id"])
                    except Exception:
                        fallbacks = []
                    for fallback in fallbacks:
                        if fallback["timestamp"] == entry["timestamp"] and fallback["original"] == entry["original"]:
                            continue
                        try:
                            content = self.fetch(fallback["replay_url"], ".html")
                            parsed = parse_product(
                                content.decode("utf-8"), fallback["original"], entry["provider_record_id"],
                            )
                            entry = {**entry, **fallback}
                            break
                        except Exception:
                            continue
                    if parsed is None:
                        error = initial_error
                        self.connection.execute(
                            "insert or replace into products values (?, ?, ?, ?, ?, null, null, null, 'error', ?, ?)",
                            (entry["provider_record_id"], entry["original"], entry["replay_url"], entry["timestamp"],
                             entry["digest"], f"{type(error).__name__}: {error}", utc_now()),
                        )
                        counters["errors"] += 1
                        self.connection.commit()
                        continue
                try:
                    self.connection.execute(
                        "insert or replace into products values (?, ?, ?, ?, ?, ?, ?, ?, 'parsed', null, ?)",
                        (entry["provider_record_id"], entry["original"], entry["replay_url"], entry["timestamp"],
                         entry["digest"], parsed["local_name"], json.dumps(parsed, ensure_ascii=False, sort_keys=True),
                         sha256(content), utc_now()),
                    )
                    counters["parsed_products"] += 1
                except Exception as error:
                    self.connection.execute(
                        "insert or replace into products values (?, ?, ?, ?, ?, null, null, null, 'error', ?, ?)",
                        (entry["provider_record_id"], entry["original"], entry["replay_url"], entry["timestamp"],
                         entry["digest"], f"{type(error).__name__}: {error}", utc_now()),
                    )
                    counters["errors"] += 1
                self.connection.commit()
            status = "completed" if not counters["errors"] else "completed_with_errors"
            self.connection.execute(
                "update runs set status=?,completed_at=?,counters_json=? where id=?",
                (status, utc_now(), json.dumps(counters, sort_keys=True), run_id),
            )
            self.connection.commit()
            return counters
        except Exception as error:
            self.connection.execute(
                "update runs set status='failed',completed_at=?,error=? where id=?",
                (utc_now(), f"{type(error).__name__}: {error}", run_id),
            )
            self.connection.commit()
            raise
