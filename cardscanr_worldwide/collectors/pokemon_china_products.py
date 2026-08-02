"""Checkpointed collector for official mainland-China Pokémon TCG products."""

from __future__ import annotations

import hashlib
import json
import random
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

BASE = "https://www.pokemon.cn"
USER_AGENT = "CardScanR-catalogue-research/1.0"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def unsigned_image_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def parse_index(html: str, *, product_category: bool = False) -> tuple[list[str], list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    product_links: set[str] = set()
    article_links: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"])
        if re.match(r"^https://www\.pokemon\.cn/tcg/(?:product|other|event|campaign)/\d+\.html$", href):
            article_links.add(href)
        if re.match(r"^https://www\.pokemon\.cn/tcg/product/\d+\.html$", href):
            product_links.add(href)
        if product_category and re.match(r"^https://www\.pokemon\.cn/tcg/\d+\.html$", href):
            product_links.add(href)
            article_links.add(href)
    return sorted(product_links), sorted(article_links)


def parse_product(html: str, url: str) -> dict[str, object]:
    soup = BeautifulSoup(html, "html.parser")
    article = soup.find("article") or soup.find("main")
    text = " ".join(article.get_text(" ", strip=True).split()) if article else ""
    title = soup.title.get_text(" ", strip=True).split(" | ", 1)[0] if soup.title else url
    product_name_match = re.search(r"商品名[:：]\s*(.+?)\s+发售日", text)
    release_match = re.search(r"发售日\s*(\d{4})年(\d{1,2})月(\d{1,2})日", text)
    msrp_match = re.search(r"建议零售价\s*([^。]+?)(?:\s+商品内容|\s+购买渠道|$)", text)
    contents_match = re.search(r"商品内容\s*(.+?)(?:\s+购买渠道|\s+※|$)", text)
    images = []
    if article:
        for image in article.find_all("img", src=True):
            source = str(image["src"])
            if "image.pokemon.com.cn" in source and "/uploads/" in source:
                images.append({"source_url": source, "canonical_url": unsigned_image_url(source), "alt": image.get("alt")})
    post_id_match = re.search(r"/(\d+)\.html$", url)
    return {
        "provider_record_id": post_id_match.group(1) if post_id_match else digest_url(url)[:16],
        "source_url": url,
        "article_title": title,
        "local_name": product_name_match.group(1).strip() if product_name_match else title,
        "release_date": (f"{release_match.group(1)}-{int(release_match.group(2)):02d}-{int(release_match.group(3)):02d}"
                         if release_match else None),
        "msrp_text": msrp_match.group(1).strip() if msrp_match else None,
        "contents_text": contents_match.group(1).strip() if contents_match else None,
        "images": images,
        "article_text": text,
    }


def digest_url(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


SCHEMA = """
pragma journal_mode=wal;
create table if not exists fetch_log(url text primary key,status integer,sha256 text,byte_size integer,
 content_type text,storage_path text,fetched_at text,error text);
create table if not exists products(provider_record_id text primary key,source_url text not null,
 local_name text not null,release_date text,msrp_text text,contents_text text,parsed_json text not null,
 raw_sha256 text not null,updated_at text not null);
create table if not exists runs(id text primary key,status text not null,started_at text not null,
 completed_at text,counters_json text not null default '{}',error text);
"""


class Collector:
    def __init__(self, runtime_root: Path, delay_seconds: float = 0.35):
        self.root = runtime_root.resolve() / "pokemon-cn-products"
        self.raw = self.root / "raw"
        self.raw.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.root / "checkpoint.sqlite")
        self.connection.executescript(SCHEMA)
        self.client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=45, follow_redirects=True,
                                   http2=False)
        self.delay_seconds = delay_seconds
        robots_response = self.client.get(f"{BASE}/robots.txt")
        robots_response.raise_for_status()
        self.robot = RobotFileParser()
        self.robot.set_url(f"{BASE}/robots.txt")
        self.robot.parse(robots_response.text.splitlines())
        robots_path = self.raw / f"robots-{sha256(robots_response.content)}.txt"
        if not robots_path.exists():
            robots_path.write_bytes(robots_response.content)

    def close(self) -> None:
        self.connection.close()
        self.client.close()

    def fetch(self, url: str) -> bytes:
        cached = self.connection.execute(
            "select storage_path from fetch_log where url=? and status=200", (url,),
        ).fetchone()
        if cached and Path(cached[0]).exists():
            return Path(cached[0]).read_bytes()
        if not self.robot.can_fetch(USER_AGENT, url):
            raise PermissionError(f"robots.txt disallows collection: {url}")
        time.sleep(self.delay_seconds + random.uniform(0, self.delay_seconds * 0.2))
        try:
            response = self.client.get(url)
            response.raise_for_status()
            content = response.content
            checksum = sha256(content)
            path = self.raw / f"{checksum}.html"
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

    def enumerate_products(self, max_pages: int = 200) -> list[str]:
        products: set[str] = set()
        seen_articles: set[str] = set()
        for page in range(1, max_pages + 1):
            url = f"{BASE}/tcg" if page == 1 else f"{BASE}/tcg/p/{page}"
            try:
                content = self.fetch(url)
            except httpx.HTTPStatusError as error:
                if error.response.status_code == 404:
                    break
                raise
            product_links, article_links = parse_index(content.decode("utf-8"))
            products.update(product_links)
            new_articles = set(article_links) - seen_articles
            seen_articles.update(article_links)
            if not new_articles:
                break
        category = self.fetch(f"{BASE}/products_category/products")
        category_products, _ = parse_index(category.decode("utf-8"), product_category=True)
        products.update(category_products)
        return sorted(products)

    def run(self) -> dict[str, int]:
        run_id = digest_url(utc_now())[:24]
        self.connection.execute("insert into runs(id,status,started_at) values (?, 'running', ?)", (run_id, utc_now()))
        self.connection.commit()
        try:
            links = self.enumerate_products()
            for url in links:
                content = self.fetch(url)
                parsed = parse_product(content.decode("utf-8"), url)
                self.connection.execute(
                    "insert or replace into products values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (parsed["provider_record_id"], url, parsed["local_name"], parsed["release_date"],
                     parsed["msrp_text"], parsed["contents_text"],
                     json.dumps(parsed, ensure_ascii=False, sort_keys=True), sha256(content), utc_now()),
                )
                self.connection.commit()
            counters = {"products": len(links), "parsed_products": self.connection.execute("select count(*) from products").fetchone()[0]}
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
