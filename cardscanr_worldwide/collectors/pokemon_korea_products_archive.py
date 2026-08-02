"""Collect official Korean TCG product inventories and details from public archives."""

from __future__ import annotations

import hashlib
import json
import random
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

USER_AGENT = "CardScanR-catalogue-preservation/1.0"
CATEGORY_CDX_URL = (
    "https://web.archive.org/cdx/search/cdx?url=pokemoncard.co.kr/card/category/*"
    "&output=json&filter=statuscode:200&fl=timestamp,original,digest,statuscode"
    "&collapse=urlkey&sort=reverse&limit=10000"
)
DETAIL_CDX_URL = (
    "https://web.archive.org/cdx/search/cdx?url=pokemoncard.co.kr/card/*"
    "&output=json&filter=statuscode:200&fl=timestamp,original,digest,statuscode"
    "&collapse=urlkey&sort=reverse&limit=10000"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme or "https", parsed.netloc, parsed.path, "", ""))


def parse_cdx(payload: bytes, kind: str) -> list[dict[str, str]]:
    data = json.loads(payload)
    header = ["timestamp", "original", "digest", "statuscode"]
    if not data or data[0] != header:
        raise ValueError("Unexpected Korean product archive CDX response")
    latest: dict[str, dict[str, str]] = {}
    # Only these three official product categories are in scope.  The same URL
    # namespace also contains event/news categories and historical empty shells.
    pattern = r"/card/category/(info1|info2|info3)" if kind == "category" else r"/card/(\d+)"
    for values in data[1:]:
        row = dict(zip(header, values))
        path = urlsplit(row["original"]).path.rstrip("/")
        match = re.fullmatch(pattern, path)
        if not match:
            continue
        key = match.group(1)
        if key not in latest or row["timestamp"] > latest[key]["timestamp"]:
            row["provider_record_id"] = key
            row["replay_url"] = f"https://web.archive.org/web/{row['timestamp']}id_/{row['original']}"
            latest[key] = row
    return [latest[key] for key in sorted(latest, key=lambda value: (not value.isdigit(), int(value) if value.isdigit() else value))]


def _text(node) -> str | None:
    return " ".join(node.get_text(" ", strip=True).split()) if node else None


def parse_category(html: str, category_key: str) -> list[dict[str, object]]:
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for point in soup.select("article.white-panel .point[onclick]"):
        match = re.search(r"/card/(\d+)", str(point.get("onclick") or ""))
        title = _text(point.select_one("h4"))
        image = point.select_one("img[src]")
        if not match or not title or match.group(1) in seen:
            continue
        rows.append({
            "provider_record_id": match.group(1),
            "category_key": category_key,
            "local_name": title,
            "listing_image_url": canonical_url(str(image.get("src"))) if image else None,
            "listing_image_alt": image.get("alt") if image else None,
        })
        seen.add(match.group(1))
    if not rows:
        raise ValueError(f"Archived category {category_key} contains no product listings")
    return rows


def parse_product(html: str, source_url: str, provider_record_id: str) -> dict[str, object]:
    soup = BeautifulSoup(html, "html.parser")
    title_node = soup.select_one("h3.medium-title")
    if not title_node:
        raise ValueError("Archived official page does not contain a product title")
    fields: dict[str, str] = {}
    for item in title_node.find_next("ul").select("li") if title_node.find_next("ul") else []:
        label_node = item.select_one("b")
        label = _text(label_node)
        if not label:
            continue
        label_node.extract()
        fields[label] = "\n".join(
            part.strip() for part in item.get_text("\n", strip=True).splitlines() if part.strip()
        )
    release_date = next((value for key, value in fields.items() if key == "발매일"), None)
    price = next((value for key, value in fields.items() if key == "가격"), None)
    contents_text = next((value for key, value in fields.items() if key == "구성물"), None)
    if not any((release_date, price, contents_text)):
        raise ValueError("Archived official page is not a structured product detail")
    contents = [line.strip() for line in (contents_text or "").splitlines() if line.strip()]
    images: list[dict[str, object]] = []
    seen: set[str] = set()
    for image in soup.select("img[src]"):
        source = str(image.get("src"))
        if "data1.pokemonkorea.co.kr" not in source:
            continue
        url = canonical_url(source)
        if url in seen:
            continue
        images.append({"source_url": source, "canonical_url": url, "alt": image.get("alt")})
        seen.add(url)
    price_match = re.search(r"([\d,]+)\s*원", price or "")
    return {
        "provider_record_id": provider_record_id,
        "source_url": source_url,
        "local_name": _text(title_node),
        "release_date": release_date,
        "price_text": price,
        "price_krw": int(price_match.group(1).replace(",", "")) if price_match else None,
        "contents": contents,
        "notice": next((value for key, value in fields.items() if key == "주의"), None),
        "fields": fields,
        "images": images,
    }


SCHEMA = """
pragma journal_mode=wal;
create table if not exists fetch_log(url text primary key,status integer,sha256 text,byte_size integer,
 content_type text,storage_path text,fetched_at text,error text);
create table if not exists categories(category_key text primary key,source_url text not null,replay_url text not null,
 archive_timestamp text not null,archive_digest text not null,raw_sha256 text,status text not null,error text,updated_at text not null);
create table if not exists products(provider_record_id text primary key,categories_json text not null,
 listing_name text not null,listing_image_url text,listing_evidence_sha256 text not null,
 source_url text,replay_url text,archive_timestamp text,archive_digest text,parsed_json text,raw_sha256 text,
 status text not null,error text,updated_at text not null);
create table if not exists runs(id text primary key,mode text not null,status text not null,started_at text not null,
 completed_at text,counters_json text not null default '{}',error text);
"""


class Collector:
    def __init__(self, runtime_root: Path, delay_seconds: float = 0.5):
        self.root = runtime_root.resolve() / "pokemon-korea-products-archive"
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

    def _inventory(self, counters: dict[str, int]) -> dict[str, dict[str, str]]:
        categories = parse_cdx(self.fetch(CATEGORY_CDX_URL, ".json"), "category")
        detail_entries = parse_cdx(self.fetch(DETAIL_CDX_URL, ".json"), "detail")
        detail_map = {entry["provider_record_id"]: entry for entry in detail_entries}
        counters["category_pages"] = len(categories)
        counters["indexed_detail_pages"] = len(detail_entries)
        inventory: dict[str, dict[str, object]] = {}
        for entry in categories:
            try:
                content = self.fetch(entry["replay_url"], ".html")
                listings = parse_category(content.decode("utf-8"), entry["provider_record_id"])
                self.connection.execute(
                    "insert or replace into categories values (?, ?, ?, ?, ?, ?, 'parsed', null, ?)",
                    (entry["provider_record_id"], entry["original"], entry["replay_url"], entry["timestamp"],
                     entry["digest"], sha256(content), utc_now()),
                )
                for listing in listings:
                    item = inventory.setdefault(str(listing["provider_record_id"]), {
                        **listing, "categories": [], "listing_evidence_sha256": sha256(content),
                    })
                    item["categories"].append(entry["provider_record_id"])
                    if not item.get("listing_image_url") and listing.get("listing_image_url"):
                        item["listing_image_url"] = listing["listing_image_url"]
                counters["category_listings"] += len(listings)
            except Exception as error:
                self.connection.execute(
                    "insert or replace into categories values (?, ?, ?, ?, ?, null, 'error', ?, ?)",
                    (entry["provider_record_id"], entry["original"], entry["replay_url"], entry["timestamp"],
                     entry["digest"], f"{type(error).__name__}: {error}", utc_now()),
                )
                counters["category_errors"] += 1
            self.connection.commit()
        for record_id, listing in sorted(inventory.items(), key=lambda item: int(item[0])):
            detail = detail_map.get(record_id) or {}
            existing = self.connection.execute(
                "select status,parsed_json,raw_sha256,error from products where provider_record_id=?", (record_id,),
            ).fetchone()
            status = existing[0] if existing and existing[0] == "parsed" else "enumerated"
            self.connection.execute(
                """insert into products values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   on conflict(provider_record_id) do update set categories_json=excluded.categories_json,
                    listing_name=excluded.listing_name,listing_image_url=excluded.listing_image_url,
                    listing_evidence_sha256=excluded.listing_evidence_sha256,source_url=excluded.source_url,
                    replay_url=excluded.replay_url,archive_timestamp=excluded.archive_timestamp,
                    archive_digest=excluded.archive_digest,updated_at=excluded.updated_at""",
                (record_id, json.dumps(sorted(set(listing["categories"]))), listing["local_name"],
                 listing.get("listing_image_url"), listing["listing_evidence_sha256"], detail.get("original"),
                 detail.get("replay_url"), detail.get("timestamp"), detail.get("digest"),
                 existing[1] if existing else None, existing[2] if existing else None, status,
                 existing[3] if existing else None, utc_now()),
            )
        self.connection.commit()
        counters["unique_products"] = len(inventory)
        return detail_map

    def run(self, mode: str = "inventory") -> dict[str, int]:
        if mode not in {"inventory", "full"}:
            raise ValueError("mode must be inventory or full")
        run_id = sha256(utc_now().encode())[:24]
        self.connection.execute(
            "insert into runs(id,mode,status,started_at) values (?, ?, 'running', ?)", (run_id, mode, utc_now()),
        )
        self.connection.commit()
        counters = {"category_pages": 0, "category_listings": 0, "category_errors": 0,
                    "indexed_detail_pages": 0, "unique_products": 0, "parsed_products": 0,
                    "detail_errors": 0, "missing_detail_captures": 0}
        try:
            detail_map = self._inventory(counters)
            if mode == "full":
                rows = self.connection.execute(
                    "select provider_record_id from products where status!='parsed' order by cast(provider_record_id as integer)"
                ).fetchall()
                for (record_id,) in rows:
                    entry = detail_map.get(record_id)
                    if not entry:
                        self.connection.execute(
                            "update products set status='missing_capture',error=?,updated_at=? where provider_record_id=?",
                            ("No numeric official detail capture was indexed", utc_now(), record_id),
                        )
                        counters["missing_detail_captures"] += 1
                        continue
                    try:
                        content = self.fetch(entry["replay_url"], ".html")
                        parsed = parse_product(content.decode("utf-8"), entry["original"], record_id)
                        self.connection.execute(
                            """update products set parsed_json=?,raw_sha256=?,status='parsed',error=null,updated_at=?
                               where provider_record_id=?""",
                            (json.dumps(parsed, ensure_ascii=False, sort_keys=True), sha256(content), utc_now(), record_id),
                        )
                        counters["parsed_products"] += 1
                    except Exception as error:
                        self.connection.execute(
                            "update products set status='detail_error',error=?,updated_at=? where provider_record_id=?",
                            (f"{type(error).__name__}: {error}", utc_now(), record_id),
                        )
                        counters["detail_errors"] += 1
                    self.connection.commit()
            status = "completed" if not counters["category_errors"] and not counters["detail_errors"] else "completed_with_errors"
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
