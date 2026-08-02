"""Resumable collector for official Pokemon Asia sealed-product galleries."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from .pokemon_asia import BASE_URL, SUPPORTED_LOCALES, USER_AGENT


SCHEMA = """
pragma journal_mode=wal;
create table if not exists pages(
  locale text not null, page_url text not null, status integer, content_sha256 text,
  byte_size integer, content_type text, storage_path text, fetched_at text,
  attempts integer not null default 0, error text, primary key(locale,page_url)
);
create table if not exists products(
  locale text not null, product_id text not null, page_url text not null, ordinal integer not null,
  local_name text not null, product_type text not null, image_url text, metadata_json text not null,
  raw_sha256 text not null, parsed_at text not null, primary key(locale,product_id)
);
create table if not exists collector_runs(
  id text primary key, locale text not null, status text not null, started_at text not null,
  completed_at text, counters_json text not null default '{}', error text
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def product_type(name: str) -> str:
    lowered = name.casefold()
    rules = (
        (("booster box", "display box", "display"), "booster_box"),
        (("booster pack", "擴充包", "扩充包", "การ์ดชุดเสริม", "kartu set booster"), "booster_pack"),
        (("starter deck", "初階牌組", "初阶牌组", "สตาร์ทเตอร์เด็ค"), "starter_deck"),
        (("battle deck", "牌組", "牌组", "เด็ค"), "theme_deck"),
        (("collection", "special set", "set spesial", "特別組合", "特别组合", "สเปเชียลเซ็ต"), "collection_box"),
        (("10 pack", "10 pak", "10包"), "multi_pack"),
        (("sleeve", "卡套", "ซองใส่การ์ด"), "sleeves"),
        (("deck box", "收納盒", "收纳盒"), "deck_box"),
        (("playmat", "桌墊", "桌垫"), "playmat"),
        (("binder", "收藏卡冊", "收藏卡册"), "binder"),
    )
    for needles, classification in rules:
        if any(needle in lowered or needle in name for needle in needles):
            return classification
    return "official_product"


def parse_index(html: str, locale: str, page_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    pattern = re.compile(rf"/{re.escape(locale)}/(?:archives/\d+|archive/special/card/)")
    return sorted({
        urljoin(page_url, str(anchor["href"]))
        for anchor in soup.find_all("a", href=True)
        if pattern.search(urljoin(page_url, str(anchor["href"])))
    })


def _table_metadata(container) -> dict[str, object]:
    fields: dict[str, str] = {}
    contents: list[str] = []
    for row in container.select("tr"):
        heading = row.find("th")
        value = row.find("td")
        if not heading or not value:
            continue
        key = heading.get_text(" ", strip=True)
        text = value.get_text(" ", strip=True)
        fields[key] = text
        if value.find_all("li"):
            contents.extend(item.get_text(" ", strip=True) for item in value.find_all("li"))
    return {"fields": fields, "contents": contents}


def parse_product_page(html: str, page_url: str) -> list[dict[str, object]]:
    soup = BeautifulSoup(html, "html.parser")
    products: list[dict[str, object]] = []
    for container in soup.select("div.card.wide"):
        heading = container.select_one("h4.mb-24px")
        if not heading:
            continue
        name = heading.get_text(" ", strip=True)
        image = container.find("img", src=True)
        metadata = _table_metadata(container)
        metadata["template"] = "article_card"
        products.append({
            "local_name": name,
            "product_type": product_type(name),
            "image_url": urljoin(page_url, str(image["src"])) if image else None,
            "metadata": metadata,
        })
    for container in soup.select(".lyt-column--product"):
        heading = container.select_one(".text-product")
        if not heading:
            continue
        name = heading.get_text(" ", strip=True)
        image = container.find("img", src=True)
        contents = [item.get_text(" ", strip=True) for item in container.select(".product-list li")]
        metadata = {
            "template": "special_product_column",
            "contents": contents,
            "text": container.get_text(" ", strip=True),
        }
        products.append({
            "local_name": name,
            "product_type": product_type(name),
            "image_url": urljoin(page_url, str(image["src"])) if image else None,
            "metadata": metadata,
        })
    for container in soup.select(".lyt-group--product"):
        heading = container.select_one(".lyt-group-text")
        if not heading:
            continue
        name = heading.get_text(" ", strip=True)
        image = container.find("img", src=True)
        metadata = {
            "template": "legacy_product_group",
            "contents": [],
            "text": container.get_text(" ", strip=True),
        }
        products.append({
            "local_name": name,
            "product_type": product_type(name),
            "image_url": urljoin(page_url, str(image["src"])) if image else None,
            "metadata": metadata,
        })
    for container in soup.select(".product-info"):
        heading = container.select_one(".product-name")
        if not heading:
            continue
        name = heading.get_text(" ", strip=True)
        scope = container.find_parent(["section", "article"]) or container.parent
        image = scope.find("img", src=re.compile(r"(?:product|pkg|pack)", re.I)) if scope else None
        products.append({
            "local_name": name,
            "product_type": product_type(name),
            "image_url": urljoin(page_url, str(image["src"])) if image else None,
            "metadata": {"template": "named_product_info", "contents": [],
                         "text": container.get_text(" ", strip=True)},
        })
    for container in soup.select(".box-product"):
        heading_image = container.select_one(".box-product-head img[alt]")
        name = heading_image.get("alt", "").strip() if heading_image else ""
        if not name:
            continue
        image = container.select_one(".lyt-block2-pack img[src]")
        data_image = container.select_one(".lyt-block2-content img[alt]")
        products.append({
            "local_name": name,
            "product_type": product_type(name),
            "image_url": urljoin(page_url, str(image["src"])) if image else None,
            "metadata": {"template": "image_label_product_box", "contents": [],
                         "text": data_image.get("alt", "").strip() if data_image else container.get_text(" ", strip=True)},
        })
    for container in soup.select(".product-information"):
        name_element = container.find("p")
        name = name_element.get_text(" ", strip=True) if name_element else ""
        if not name:
            continue
        image = soup.select_one(".eyecatch img[src]")
        metadata = _table_metadata(container)
        metadata["template"] = "article_product_information"
        products.append({
            "local_name": name,
            "product_type": product_type(name),
            "image_url": urljoin(page_url, str(image["src"])) if image else None,
            "metadata": metadata,
        })
    for container in soup.select(".lyt-product"):
        content = container.select_one(".lyt-product-content")
        text = content.get_text(" ", strip=True) if content else ""
        match = re.search(r"(?:商品名稱|商品名称|Nama Produk|ชื่อสินค้า)\s*[：:]\s*(.+?)(?=\s*[●•]|\s*(?:建議|建议|Harga|ราคา)|$)", text)
        if not match:
            continue
        name = match.group(1).strip()
        image = container.select_one(".lyt-product-image img[src]")
        products.append({
            "local_name": name,
            "product_type": product_type(name),
            "image_url": urljoin(page_url, str(image["src"])) if image else None,
            "metadata": {"template": "legacy_lyt_product", "contents": [], "text": text},
        })
    deduplicated: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for product in products:
        key = (str(product["local_name"]).casefold(), str(product.get("image_url") or ""))
        if key not in seen:
            seen.add(key)
            deduplicated.append(product)
    return deduplicated


class Collector:
    def __init__(self, locale: str, runtime_root: Path, delay_seconds: float = 0.35):
        if locale not in SUPPORTED_LOCALES:
            raise ValueError(f"Unsupported locale {locale}")
        self.locale = locale
        self.root = runtime_root.resolve() / "pokemon-asia-products" / locale
        self.raw = self.root / "raw"
        self.raw.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.root / "checkpoint.sqlite")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
        self.delay_seconds = delay_seconds
        self.robot = RobotFileParser(urljoin(BASE_URL, "/robots.txt"))
        self.robot.set_url(urljoin(BASE_URL, "/robots.txt"))
        self.robot.read()

    def close(self) -> None:
        self.connection.close()
        self.session.close()

    def fetch(self, url: str) -> bytes:
        cached = self.connection.execute(
            "select status,storage_path from pages where locale=? and page_url=?", (self.locale, url)
        ).fetchone()
        if cached and cached["status"] == 200 and cached["storage_path"] and Path(cached["storage_path"]).exists():
            return Path(cached["storage_path"]).read_bytes()
        if not self.robot.can_fetch(USER_AGENT, url):
            raise PermissionError(f"robots.txt disallows collection: {url}")
        attempts = int(cached["attempts"] if cached and "attempts" in cached.keys() else 0)
        time.sleep(self.delay_seconds)
        try:
            response = self.session.get(url, timeout=(10, 45), allow_redirects=True)
            content = response.content
            digest = sha256_bytes(content)
            path = self.raw / f"{digest}.html"
            if not path.exists():
                path.write_bytes(content)
            self.connection.execute(
                """insert into pages values (?,?,?,?,?,?,?,?,?,null)
                   on conflict(locale,page_url) do update set status=excluded.status,
                   content_sha256=excluded.content_sha256,byte_size=excluded.byte_size,
                   content_type=excluded.content_type,storage_path=excluded.storage_path,
                   fetched_at=excluded.fetched_at,attempts=excluded.attempts,error=null""",
                (self.locale, url, response.status_code, digest, len(content), response.headers.get("Content-Type"),
                 str(path), utc_now(), attempts + 1),
            )
            self.connection.commit()
            response.raise_for_status()
            return content
        except Exception as error:
            self.connection.execute(
                """insert into pages(locale,page_url,attempts,fetched_at,error) values (?,?,?,?,?)
                   on conflict(locale,page_url) do update set attempts=excluded.attempts,
                   fetched_at=excluded.fetched_at,error=excluded.error""",
                (self.locale, url, attempts + 1, utc_now(), f"{type(error).__name__}: {error}"),
            )
            self.connection.commit()
            raise

    def run(self) -> dict[str, int]:
        run_id = uuid.uuid4().hex
        self.connection.execute(
            "insert into collector_runs(id,locale,status,started_at) values (?,?,'running',?)",
            (run_id, self.locale, utc_now()),
        )
        self.connection.commit()
        counters = {"detail_pages": 0, "products": 0, "pages_without_products": 0}
        try:
            index_url = f"{BASE_URL}/{self.locale}/products/"
            index = self.fetch(index_url).decode("utf-8", errors="replace")
            detail_urls = parse_index(index, self.locale, index_url)
            for detail_url in detail_urls:
                content = self.fetch(detail_url)
                parsed = parse_product_page(content.decode("utf-8", errors="replace"), detail_url)
                digest = sha256_bytes(content)
                counters["detail_pages"] += 1
                if not parsed:
                    counters["pages_without_products"] += 1
                for ordinal, product in enumerate(parsed):
                    product_id = hashlib.sha256(
                        f"{detail_url}\n{ordinal}\n{product['local_name']}".encode("utf-8")
                    ).hexdigest()[:32]
                    self.connection.execute(
                        "insert or replace into products values (?,?,?,?,?,?,?,?,?,?)",
                        (self.locale, product_id, detail_url, ordinal, product["local_name"],
                         product["product_type"], product.get("image_url"),
                         json.dumps(product["metadata"], ensure_ascii=False, sort_keys=True),
                         digest, utc_now()),
                    )
                    counters["products"] += 1
                self.connection.commit()
            counters["indexed_detail_pages"] = len(detail_urls)
            self.connection.execute(
                "update collector_runs set status='completed',completed_at=?,counters_json=? where id=?",
                (utc_now(), json.dumps(counters, sort_keys=True), run_id),
            )
            self.connection.commit()
            return counters
        except Exception as error:
            self.connection.execute(
                "update collector_runs set status='failed',completed_at=?,counters_json=?,error=? where id=?",
                (utc_now(), json.dumps(counters, sort_keys=True), f"{type(error).__name__}: {error}", run_id),
            )
            self.connection.commit()
            raise


__all__ = ["Collector", "SCHEMA", "parse_index", "parse_product_page", "product_type"]
