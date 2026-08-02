"""Resumable collector for the official Japanese Pokémon TCG product feed."""

from __future__ import annotations

import hashlib
import json
import random
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx

BASE = "https://www.pokemon-card.com"
API = f"{BASE}/products/resultAPI.php"
USER_AGENT = "CardScanR-catalogue-research/1.0"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def parse_page(payload: bytes) -> dict[str, object]:
    data = json.loads(payload)
    if data.get("result") != 1 or not isinstance(data.get("products"), list):
        raise ValueError(f"Official Japanese product API returned an error: {data.get('errMsg')}")
    return data


def product_identity(product: dict[str, object]) -> str:
    identity = json.dumps({
        key: product.get(key) for key in (
            "productTitle", "productType", "tumbsImg", "releaseDate", "link_cardList", "link_detailPage",
        )
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(identity.encode()).hexdigest()[:24]


SCHEMA = """
pragma journal_mode=wal;
create table if not exists fetch_log(url text primary key,status integer,sha256 text,byte_size integer,
 content_type text,storage_path text,fetched_at text,error text);
create table if not exists products(provider_record_id text primary key,local_name text not null,
 product_type text,release_date_text text,price_text text,image_url text,parsed_json text not null,
 raw_sha256 text not null,updated_at text not null);
create table if not exists runs(id text primary key,status text not null,started_at text not null,
 completed_at text,counters_json text not null default '{}',error text);
"""


class Collector:
    def __init__(self, runtime_root: Path, delay_seconds: float = 0.35):
        self.root = runtime_root.resolve() / "pokemon-japan-products"
        self.raw = self.root / "raw"
        self.raw.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.root / "checkpoint.sqlite")
        self.connection.executescript(SCHEMA)
        self.client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Referer": f"{BASE}/products/index.html"},
            timeout=45, follow_redirects=True, http2=False,
        )
        self.delay_seconds = delay_seconds

    def close(self) -> None:
        self.connection.close()
        self.client.close()

    def fetch(self, url: str) -> bytes:
        cached = self.connection.execute(
            "select storage_path from fetch_log where url=? and status=200", (url,),
        ).fetchone()
        if cached and cached[0] and Path(cached[0]).exists():
            return Path(cached[0]).read_bytes()
        time.sleep(self.delay_seconds + random.uniform(0, self.delay_seconds * 0.2))
        try:
            response = self.client.get(url)
            response.raise_for_status()
            content = response.content
            checksum = sha256(content)
            path = self.raw / f"{checksum}.json"
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
            self.connection.execute(
                "insert or replace into fetch_log(url,fetched_at,error) values (?, ?, ?)",
                (url, utc_now(), f"{type(error).__name__}: {error}"),
            )
            self.connection.commit()
            raise

    def run(self) -> dict[str, int]:
        run_id = sha256(utc_now().encode())[:24]
        self.connection.execute("insert into runs(id,status,started_at) values (?, 'running', ?)", (run_id, utc_now()))
        self.connection.commit()
        try:
            first_url = f"{API}?{urlencode({'page': 1})}"
            first = parse_page(self.fetch(first_url))
            max_page = int(first["maxPage"])
            declared = int(first["hitCnt"])
            seen: set[str] = set()
            for page in range(1, max_page + 1):
                data = first if page == 1 else parse_page(self.fetch(f"{API}?{urlencode({'page': page})}"))
                for product in data["products"]:
                    record_id = product_identity(product)
                    seen.add(record_id)
                    raw = json.dumps(product, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    image_url = f"{BASE}{product['tumbsImg']}" if str(product.get("tumbsImg") or "").startswith("/") else product.get("tumbsImg")
                    self.connection.execute(
                        "insert or replace into products values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (record_id, product["productTitle"], product.get("productType"), product.get("releaseDate"),
                         product.get("priceTxt"), image_url, raw, sha256(raw.encode()), utc_now()),
                    )
                self.connection.commit()
            if len(seen) != declared:
                raise RuntimeError(f"Japanese product API declared {declared} records but enumerated {len(seen)}")
            counters = {"declared_products": declared, "products": len(seen), "pages": max_page}
            self.connection.execute(
                "update runs set status='completed',completed_at=?,counters_json=? where id=?",
                (utc_now(), json.dumps(counters, sort_keys=True), run_id),
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

