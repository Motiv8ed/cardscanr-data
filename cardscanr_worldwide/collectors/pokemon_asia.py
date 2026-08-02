"""Resumable collector for the public Pokémon Card Game Asia trainer sites."""

from __future__ import annotations

import hashlib
import json
import random
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://asia.pokemon-card.com"
SUPPORTED_LOCALES = {"hk", "tw", "th", "id", "sg", "my", "ph"}
USER_AGENT = "CardScanR-catalogue-research/1.0"


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_product_index(html: str, locale: str) -> tuple[list[dict[str, str]], bool]:
    soup = BeautifulSoup(html, "html.parser")
    products: dict[str, dict[str, str]] = {}
    next_page = False
    product_pattern = re.compile(rf"^/{re.escape(locale)}/card-search/list/\?")
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"])
        if product_pattern.match(href):
            code = parse_qs(urlparse(href.replace("&amp;", "&")).query).get("expansionCodes", [None])[0]
            if code:
                local_name = anchor.get_text(" ", strip=True)
                if not local_name:
                    label = anchor.find_parent(["label", "li", "div"])
                    local_name = label.get_text(" ", strip=True) if label else code
                products[code] = {"code": code, "local_name": local_name or code, "href": href}
        if re.search(rf"^/{re.escape(locale)}/card-search/\?pageNo=\d+", href):
            text = anchor.get_text(" ", strip=True).casefold()
            if "next" in text or "次" in text or "ถัด" in text or "berikut" in text:
                next_page = True
    return sorted(products.values(), key=lambda value: value["code"]), next_page


def parse_card_list(html: str, locale: str) -> tuple[list[str], bool]:
    soup = BeautifulSoup(html, "html.parser")
    card_ids: set[str] = set()
    next_page = False
    detail_pattern = re.compile(rf"^/{re.escape(locale)}/card-search/detail/(\d+)/")
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"])
        match = detail_pattern.match(href)
        if match:
            card_ids.add(match.group(1))
        if re.search(rf"^/{re.escape(locale)}/card-search/list/\?", href):
            query = parse_qs(urlparse(href.replace("&amp;", "&")).query)
            text = anchor.get_text(" ", strip=True).casefold()
            if "pageNo" in query and ("next" in text or "次" in text or "ถัด" in text or "berikut" in text):
                next_page = True
    return sorted(card_ids, key=int), next_page


def parse_card_detail(html: str, page_url: str) -> dict[str, object]:
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find("h1")
    title = heading.get_text(" ", strip=True) if heading else ""
    stage_element = heading.select_one(".evolveMarker") if heading else None
    stage = stage_element.get_text(" ", strip=True) if stage_element else None
    if stage and title.startswith(stage):
        title = title[len(stage):].strip()
    image_url = None
    for image in soup.find_all("img", src=True):
        source = str(image["src"])
        if "/card-img/" in source and "/mark/" not in source:
            image_url = urljoin(page_url, source)
            break
    def energy_values(element) -> list[str]:
        values = []
        if not element:
            return values
        for image in element.find_all("img", src=True):
            name = Path(urlparse(str(image["src"])).path).stem
            if name:
                values.append(name)
        return values

    attacks = []
    for skill in soup.select(".skillInformation .skill"):
        name_element = skill.select_one(".skillName")
        damage_element = skill.select_one(".skillDamage")
        effect_element = skill.select_one(".skillEffect")
        attacks.append({
            "name": name_element.get_text(" ", strip=True) if name_element else None,
            "damage": damage_element.get_text(" ", strip=True) if damage_element else None,
            "effect": effect_element.get_text(" ", strip=True) if effect_element else None,
            "cost": energy_values(skill.select_one(".skillHeader")),
        })
    collector_text_element = soup.select_one(".collectorNumber")
    collector_text = collector_text_element.get_text(" ", strip=True) if collector_text_element else None
    collector_number = collector_text.split("/", 1)[0] if collector_text and "/" in collector_text else collector_text
    printed_set_code = collector_text.split("/", 1)[1] if collector_text and "/" in collector_text else None
    hp_element = soup.select_one(".mainInfomation .number")
    alpha_element = soup.select_one(".expansionColumn .alpha")
    illustrator_element = soup.select_one(".illustrator")
    illustrator = illustrator_element.get_text(" ", strip=True) if illustrator_element else None
    if illustrator:
        illustrator = re.sub(r"^[^A-Z0-9]+", "", illustrator).strip()
        illustrator = re.sub(r"^(?:Illustrator|Ilustrator)\s+", "", illustrator, flags=re.IGNORECASE)
    dex_match = re.search(r"\bNo\.\s*(\d+)", soup.select_one(".extraInformation").get_text(" ", strip=True)
                          if soup.select_one(".extraInformation") else "")
    weak = soup.select_one(".subInformation .weakpoint")
    resist = soup.select_one(".subInformation .resist")
    weak_types = energy_values(weak)
    resist_types = energy_values(resist)
    return {
        "page_url": page_url,
        "page_title": soup.title.get_text(" ", strip=True) if soup.title else None,
        "local_name": title,
        "stage": stage,
        "hp": int(hp_element.get_text(strip=True)) if hp_element and hp_element.get_text(strip=True).isdigit() else None,
        "types": energy_values(soup.select_one(".mainInfomation")),
        "attacks": attacks,
        "weaknesses": ([{"type": weak_types[0] if weak_types else None,
                         "value": weak.get_text(" ", strip=True)}] if weak else []),
        "resistances": ([{"type": resist_types[0] if resist_types else None,
                          "value": resist.get_text(" ", strip=True)}] if resist else []),
        "retreat_cost": energy_values(soup.select_one(".subInformation .escape")),
        "regulation_mark": alpha_element.get_text(" ", strip=True) if alpha_element else None,
        "collector_number": collector_number,
        "printed_set_code": printed_set_code,
        "national_pokedex_numbers": [int(dex_match.group(1))] if dex_match else [],
        "illustrator": illustrator,
        "description": (soup.select_one(".discription").get_text(" ", strip=True)
                        if soup.select_one(".discription") else None),
        "image_url": image_url,
        "text": soup.get_text(" ", strip=True),
    }


SCHEMA = """
pragma journal_mode=wal;
create table if not exists fetch_log (
  url text primary key, status integer, content_sha256 text, byte_size integer,
  content_type text, storage_path text, fetched_at text, attempts integer not null default 0,
  error text
);
create table if not exists products (
  locale text not null, expansion_code text not null, local_name text not null,
  source_url text not null, first_seen_at text not null, primary key(locale, expansion_code)
);
create table if not exists product_cards (
  locale text not null, expansion_code text not null, card_id text not null,
  source_url text not null, primary key(locale, expansion_code, card_id)
);
create table if not exists cards (
  locale text not null, card_id text not null, source_url text not null,
  local_name text, image_url text, parsed_json text, raw_sha256 text,
  status text not null, updated_at text not null, primary key(locale, card_id)
);
create table if not exists collector_runs (
  id text primary key, locale text not null, mode text not null, status text not null,
  started_at text not null, completed_at text, counters_json text not null default '{}', error text
);
"""


@dataclass
class CachedResponse:
    status: int
    content: bytes
    content_type: str | None
    storage_path: Path
    from_cache: bool


class Collector:
    def __init__(self, locale: str, runtime_root: Path, delay_seconds: float = 0.35):
        if locale not in SUPPORTED_LOCALES:
            raise ValueError(f"Unsupported locale {locale!r}; expected one of {sorted(SUPPORTED_LOCALES)}")
        self.locale = locale
        self.root = runtime_root.resolve() / "pokemon-asia" / locale
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

    def fetch(self, url: str) -> CachedResponse:
        cached = self.connection.execute(
            "select status,content_type,storage_path from fetch_log where url=? and status=200", (url,),
        ).fetchone()
        if cached and cached["storage_path"] and Path(cached["storage_path"]).exists():
            path = Path(cached["storage_path"])
            return CachedResponse(cached["status"], path.read_bytes(), cached["content_type"], path, True)
        if not self.robot.can_fetch(USER_AGENT, url):
            raise PermissionError(f"robots.txt disallows collection: {url}")
        attempts = (self.connection.execute("select attempts from fetch_log where url=?", (url,)).fetchone() or [0])[0]
        time.sleep(self.delay_seconds + random.uniform(0, self.delay_seconds * 0.2))
        try:
            response = self.session.get(url, timeout=(10, 45), allow_redirects=True)
            content = response.content
            checksum = sha256(content)
            suffix = ".html" if "html" in response.headers.get("Content-Type", "") else ".bin"
            path = self.raw / f"{checksum}{suffix}"
            if not path.exists():
                path.write_bytes(content)
            self.connection.execute(
                """insert into fetch_log values (?, ?, ?, ?, ?, ?, ?, ?, null)
                on conflict(url) do update set status=excluded.status,content_sha256=excluded.content_sha256,
                byte_size=excluded.byte_size,content_type=excluded.content_type,storage_path=excluded.storage_path,
                fetched_at=excluded.fetched_at,attempts=excluded.attempts,error=null""",
                (url, response.status_code, checksum, len(content), response.headers.get("Content-Type"),
                 str(path), utc_now(), attempts + 1),
            )
            self.connection.commit()
            response.raise_for_status()
            return CachedResponse(response.status_code, content, response.headers.get("Content-Type"), path, False)
        except Exception as error:
            self.connection.execute(
                """insert into fetch_log(url,attempts,fetched_at,error) values (?, ?, ?, ?)
                on conflict(url) do update set attempts=excluded.attempts,fetched_at=excluded.fetched_at,error=excluded.error""",
                (url, attempts + 1, utc_now(), f"{type(error).__name__}: {error}"),
            )
            self.connection.commit()
            raise

    def inventory_products(self, max_pages: int = 100) -> int:
        seen: set[str] = set()
        for page in range(1, max_pages + 1):
            url = f"{BASE_URL}/{self.locale}/card-search/?{urlencode({'pageNo': page})}"
            response = self.fetch(url)
            products, has_next = parse_product_index(response.content.decode("utf-8"), self.locale)
            new_codes = 0
            for product in products:
                if product["code"] not in seen:
                    new_codes += 1
                seen.add(product["code"])
                self.connection.execute(
                    "insert or replace into products values (?, ?, ?, ?, coalesce((select first_seen_at from products where locale=? and expansion_code=?), ?))",
                    (self.locale, product["code"], product["local_name"], url,
                     self.locale, product["code"], utc_now()),
                )
            self.connection.commit()
            # A page with no new identities is terminal even if a site leaves a
            # stale "next" control enabled or redirects an out-of-range page.
            if new_codes == 0 or not has_next:
                break
        return len(seen)

    def inventory_cards(self, max_pages_per_product: int = 500) -> int:
        products = self.connection.execute(
            "select expansion_code from products where locale=? order by expansion_code", (self.locale,),
        ).fetchall()
        for product in products:
            code = product["expansion_code"]
            seen_for_product: set[str] = set()
            for page in range(1, max_pages_per_product + 1):
                query = urlencode({"pageNo": page, "expansionCodes": code})
                url = f"{BASE_URL}/{self.locale}/card-search/list/?{query}"
                response = self.fetch(url)
                card_ids, has_next = parse_card_list(response.content.decode("utf-8"), self.locale)
                new_ids = set(card_ids) - seen_for_product
                for card_id in card_ids:
                    seen_for_product.add(card_id)
                    self.connection.execute(
                        "insert or replace into product_cards values (?, ?, ?, ?)",
                        (self.locale, code, card_id, url),
                    )
                self.connection.commit()
                if not new_ids or not has_next:
                    break
        return self.connection.execute("select count(distinct card_id) from product_cards where locale=?", (self.locale,)).fetchone()[0]

    def collect_details(self) -> int:
        card_ids = self.connection.execute(
            """select distinct pc.card_id from product_cards pc
            left join cards c on c.locale=pc.locale and c.card_id=pc.card_id and c.status='parsed'
            where pc.locale=? and c.card_id is null order by cast(pc.card_id as integer)""", (self.locale,),
        ).fetchall()
        for row in card_ids:
            card_id = row["card_id"]
            url = f"{BASE_URL}/{self.locale}/card-search/detail/{card_id}/"
            response = self.fetch(url)
            parsed = parse_card_detail(response.content.decode("utf-8"), url)
            self.connection.execute(
                "insert or replace into cards values (?, ?, ?, ?, ?, ?, ?, 'parsed', ?)",
                (self.locale, card_id, url, parsed["local_name"], parsed["image_url"],
                 json.dumps(parsed, ensure_ascii=False, sort_keys=True), sha256(response.content), utc_now()),
            )
            self.connection.commit()
        return self.connection.execute("select count(*) from cards where locale=? and status='parsed'", (self.locale,)).fetchone()[0]

    def run(self, mode: str) -> dict[str, int]:
        if mode not in {"inventory", "full"}:
            raise ValueError("mode must be inventory or full")
        run_id = hashlib.sha256(f"{self.locale}:{mode}:{utc_now()}".encode()).hexdigest()[:24]
        self.connection.execute(
            "insert into collector_runs(id,locale,mode,status,started_at) values (?, ?, ?, 'running', ?)",
            (run_id, self.locale, mode, utc_now()),
        )
        self.connection.commit()
        try:
            counters = {"products": self.inventory_products(), "unique_cards": self.inventory_cards()}
            if mode == "full":
                counters["parsed_details"] = self.collect_details()
            self.connection.execute(
                "update collector_runs set status='completed',completed_at=?,counters_json=? where id=?",
                (utc_now(), json.dumps(counters, sort_keys=True), run_id),
            )
            self.connection.commit()
            return counters
        except Exception as error:
            self.connection.execute(
                "update collector_runs set status='failed',completed_at=?,error=? where id=?",
                (utc_now(), f"{type(error).__name__}: {error}", run_id),
            )
            self.connection.commit()
            raise
