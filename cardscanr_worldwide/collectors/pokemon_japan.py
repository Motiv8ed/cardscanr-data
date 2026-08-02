"""Resumable official Japanese Pokémon TCG card database collector."""

from __future__ import annotations

import hashlib
import json
import random
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

BASE = "https://www.pokemon-card.com"
RESULT_API = f"{BASE}/card-search/resultAPI.php"
USER_AGENT = "CardScanR-catalogue-research/1.0"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_url(url: str) -> str:
    absolute = urljoin(BASE, url)
    parsed = urlsplit(absolute)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _text(node) -> str | None:
    return " ".join(node.get_text(" ", strip=True).split()) if node else None


def _energy_types(node) -> list[str]:
    types = []
    for icon in node.select(".icon") if node else []:
        for class_name in icon.get("class") or []:
            if class_name.startswith("icon-") and class_name != "icon":
                types.append(class_name.removeprefix("icon-"))
                break
    return types


def parse_result_page(payload: bytes) -> dict[str, object]:
    data = json.loads(payload)
    if data.get("result") != 1 or not isinstance(data.get("cardList"), list):
        raise ValueError(f"Official Japanese card API returned an error: {data.get('errMsg')}")
    return data


def parse_card(html: str, card_id: str) -> dict[str, object]:
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one(".PopupMain .Section")
    image = root.select_one(".LeftBox > img.fit") if root else None
    name = _text(root.select_one("h1.Heading1")) if root else None
    if not root or not image or not name:
        raise ValueError("Official Japanese page does not contain a card detail")
    set_logo = root.select_one(".img-regulation")
    set_code = set_logo.get("alt") if set_logo else Path(urlsplit(str(image.get("src"))).path).parent.name
    rarity_image = root.select_one('.subtext img[src*="/rarity/"]')
    rarity_match = re.search(r"ic_rare_([^./]+)", str(rarity_image.get("src")) if rarity_image else "", re.I)
    subtext = root.select_one(".subtext")
    number_text = _text(subtext) or ""
    number_match = re.search(r"([^/\s]+)\s*/\s*([^\s]+)", number_text)
    hp_text = _text(root.select_one(".hp-num"))
    dex_match = re.search(r"No\.\s*(\d+)", _text(root.select_one(".card h4")) or "")
    description_nodes = root.select(".card p")
    description = _text(description_nodes[-1]) if description_nodes else None
    right = root.select_one(".RightBox-inner")
    attacks = []
    abilities = []
    current_kind = None
    if right:
        for child in right.children:
            if not getattr(child, "name", None):
                continue
            if child.name == "h2":
                label = _text(child) or ""
                current_kind = "attack" if "ワザ" in label else "ability" if "特性" in label else None
            elif child.name == "h4" and current_kind:
                damage = _text(child.select_one(".f_right"))
                if child.select_one(".f_right"):
                    child.select_one(".f_right").extract()
                record = {"name": _text(child), "cost": _energy_types(child), "damage": damage, "effect": None}
                target = attacks if current_kind == "attack" else abilities
                target.append(record)
            elif child.name == "p" and current_kind:
                target = attacks if current_kind == "attack" else abilities
                if target and target[-1]["effect"] is None:
                    target[-1]["effect"] = _text(child)
    stats = root.select_one("table")
    stat_cells = stats.select("tr:nth-of-type(2) td") if stats else []
    weakness = ({"types": _energy_types(stat_cells[0]), "value": _text(stat_cells[0])}
                if len(stat_cells) > 0 and _energy_types(stat_cells[0]) else None)
    resistance = ({"types": _energy_types(stat_cells[1]), "value": _text(stat_cells[1])}
                  if len(stat_cells) > 1 and _energy_types(stat_cells[1]) else None)
    retreat = _energy_types(stat_cells[2]) if len(stat_cells) > 2 else []
    type_node = root.select_one(".TopInfo .type")
    category = _text(type_node)
    if not category:
        category = _text(right.select_one("h2")) if right else None
    return {
        "provider_record_id": str(card_id), "source_url": f"{BASE}/card-search/details.php/card/{card_id}/regu/all",
        "local_name": name, "set_code": set_code, "set_names": [
            _text(node) for node in soup.select(".PopupSub .List_item a") if _text(node)
        ],
        "collector_number": number_match.group(1) if number_match else None,
        "printed_total": number_match.group(2) if number_match else None,
        "rarity": rarity_match.group(1).upper() if rarity_match else None,
        "illustrator": _text(root.select_one(".author a")), "stage": category,
        "hp": int(hp_text) if hp_text and hp_text.isdigit() else None,
        "types": _energy_types(root.select_one(".td-r")), "attacks": attacks, "abilities": abilities,
        "weaknesses": [weakness] if weakness else [], "resistances": [resistance] if resistance else [],
        "retreat_cost": retreat, "national_pokedex_numbers": [int(dex_match.group(1))] if dex_match else [],
        "description": description, "image_url": canonical_url(str(image.get("src"))),
    }


SCHEMA = """
pragma journal_mode=wal;
create table if not exists fetch_log(url text primary key,status integer,sha256 text,byte_size integer,
 content_type text,storage_path text,fetched_at text,error text);
create table if not exists cards(provider_record_id text primary key,source_url text not null,
 local_name text,thumbnail_url text,parsed_json text,raw_sha256 text,status text not null,
 error text,updated_at text not null);
create table if not exists collector_runs(id text primary key,mode text not null,status text not null,
 started_at text not null,completed_at text,counters_json text not null default '{}',error text);
"""


class Collector:
    def __init__(self, runtime_root: Path, delay_seconds: float = 0.35):
        self.root = runtime_root.resolve() / "pokemon-japan"
        self.raw = self.root / "raw"
        self.raw.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.root / "checkpoint.sqlite")
        self.connection.executescript(SCHEMA)
        self.client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Referer": f"{BASE}/card-search/index.php"},
            timeout=45, follow_redirects=True, http2=False,
        )
        self.delay_seconds = delay_seconds
        robots_response = self.client.get(f"{BASE}/robots.txt")
        self.robot = RobotFileParser()
        self.robot.set_url(f"{BASE}/robots.txt")
        self.robot.parse(robots_response.text.splitlines() if robots_response.status_code == 200 else [])
        robots_path = self.raw / f"robots-{robots_response.status_code}-{sha256(robots_response.content)}.txt"
        if not robots_path.exists():
            robots_path.write_bytes(robots_response.content)

    def close(self) -> None:
        self.connection.close()
        self.client.close()

    def fetch(self, url: str) -> bytes:
        cached = self.connection.execute(
            "select storage_path from fetch_log where url=? and status=200", (url,),
        ).fetchone()
        if cached and cached[0] and Path(cached[0]).exists():
            return Path(cached[0]).read_bytes()
        if not self.robot.can_fetch(USER_AGENT, url):
            raise PermissionError(f"robots.txt disallows collection: {url}")
        last_error: Exception | None = None
        for attempt in range(4):
            time.sleep(self.delay_seconds + random.uniform(0, self.delay_seconds * 0.2))
            try:
                response = self.client.get(url)
                response.raise_for_status()
                content = response.content
                checksum = sha256(content)
                suffix = ".json" if "resultAPI.php" in url else ".html"
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
                if attempt < 3:
                    time.sleep(min(2 ** attempt, 4))
        self.connection.execute(
            "insert or replace into fetch_log(url,fetched_at,error) values (?, ?, ?)",
            (url, utc_now(), f"{type(last_error).__name__}: {last_error}"),
        )
        self.connection.commit()
        raise last_error or RuntimeError("Unknown fetch error")

    def inventory(self) -> dict[str, int]:
        first_url = f"{RESULT_API}?regulation_sidebar_form=all&page=1"
        first = parse_result_page(self.fetch(first_url))
        max_page = int(first["maxPage"])
        hit_count = int(first["hitCnt"])
        seen: set[str] = set()
        for page in range(1, max_page + 1):
            data = first if page == 1 else parse_result_page(self.fetch(
                f"{RESULT_API}?regulation_sidebar_form=all&page={page}",
            ))
            for card in data["cardList"]:
                card_id = str(card["cardID"])
                seen.add(card_id)
                detail_url = f"{BASE}/card-search/details.php/card/{card_id}/regu/all"
                self.connection.execute(
                    """insert into cards values (?, ?, ?, ?, null, null, 'enumerated', null, ?)
                    on conflict(provider_record_id) do update set source_url=excluded.source_url,
                    local_name=excluded.local_name,thumbnail_url=excluded.thumbnail_url""",
                    (card_id, detail_url, card.get("cardNameViewText"),
                     canonical_url(card.get("cardThumbFile") or ""), utc_now()),
                )
            self.connection.commit()
        if len(seen) != hit_count:
            raise RuntimeError(f"Japanese official API declared {hit_count} cards but enumerated {len(seen)}")
        return {"declared_cards": hit_count, "enumerated_cards": len(seen), "pages": max_page}

    def details(self) -> dict[str, int]:
        counters = {"parsed_cards": 0, "errors": 0}
        rows = self.connection.execute(
            "select provider_record_id,source_url from cards where status!='parsed' order by cast(provider_record_id as integer)",
        ).fetchall()
        for card_id, source_url in rows:
            try:
                content = self.fetch(source_url)
                parsed = parse_card(content.decode("utf-8"), card_id)
                self.connection.execute(
                    "update cards set parsed_json=?,raw_sha256=?,status='parsed',error=null,updated_at=? where provider_record_id=?",
                    (json.dumps(parsed, ensure_ascii=False, sort_keys=True), sha256(content), utc_now(), card_id),
                )
                counters["parsed_cards"] += 1
            except Exception as error:
                self.connection.execute(
                    "update cards set status='error',error=?,updated_at=? where provider_record_id=?",
                    (f"{type(error).__name__}: {error}", utc_now(), card_id),
                )
                counters["errors"] += 1
            self.connection.commit()
        return counters

    def run(self, mode: str) -> dict[str, int]:
        run_id = sha256(f"{mode}:{utc_now()}".encode())[:24]
        self.connection.execute(
            "insert into collector_runs(id,mode,status,started_at) values (?, ?, 'running', ?)",
            (run_id, mode, utc_now()),
        )
        self.connection.commit()
        try:
            counters = self.inventory()
            if mode == "full":
                counters.update(self.details())
            status = "completed" if not counters.get("errors") else "completed_with_errors"
            self.connection.execute(
                "update collector_runs set status=?,completed_at=?,counters_json=? where id=?",
                (status, utc_now(), json.dumps(counters, sort_keys=True), run_id),
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
