"""Resumable collector for official Korean TCG pages preserved by the Internet Archive."""

from __future__ import annotations

import hashlib
import json
import random
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

USER_AGENT = "CardScanR-catalogue-preservation/1.0"
CDX_URL = (
    "https://web.archive.org/cdx/search/cdx?url=pokemoncard.co.kr/cards/detail/*"
    "&from=2019&to=2026&output=json&filter=statuscode:200&fl=timestamp,original,digest,statuscode"
    "&collapse=urlkey&sort=reverse&limit=5000"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_url(url: str) -> str:
    parsed = urlsplit(url)
    scheme = parsed.scheme or "https"
    return urlunsplit((scheme, parsed.netloc, parsed.path, "", ""))


def parse_cdx(payload: bytes) -> list[dict[str, str]]:
    data = json.loads(payload)
    if not data or data[0] != ["timestamp", "original", "digest", "statuscode"]:
        raise ValueError("Unexpected Korean archive CDX response")
    rows = [dict(zip(data[0], values)) for values in data[1:]]
    result = []
    for row in rows:
        if not re.fullmatch(r"https?://(?:www\.)?pokemoncard\.co\.kr/cards/detail/[A-Za-z0-9_-]+", row["original"]):
            continue
        row["provider_record_id"] = row["original"].rstrip("/").rsplit("/", 1)[-1]
        row["replay_url"] = f"https://web.archive.org/web/{row['timestamp']}id_/{row['original']}"
        result.append(row)
    return sorted(result, key=lambda row: row["provider_record_id"])


def _text(node) -> str | None:
    return " ".join(node.get_text(" ", strip=True).split()) if node else None


def parse_card(html: str, source_url: str, provider_record_id: str) -> dict[str, object]:
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one("#heaer_top")
    image = root.select_one("img.feature_image") if root else None
    name_node = root.select_one(".card-hp.title") if root else None
    if not root or not image or not name_node:
        raise ValueError("Archived official page does not contain a card detail")
    image_url = canonical_url(str(image.get("src")))
    image_match = re.search(r"/wmimages/[^/]+/([^/]+)/[^/]+$", image_url, re.I)
    set_code = image_match.group(1) if image_match else None
    pnum = root.select_one(".p_num")
    rarity_node = root.select_one("#no_wrap_by_admin")
    rarity = _text(rarity_node)
    number_text = _text(pnum) or ""
    if rarity:
        number_text = number_text.rsplit(rarity, 1)[0].strip()
    number_match = re.match(r"([^/\s]+)\s*/\s*([^\s]+)", number_text)
    illustrator_text = _text(root.select_one(".illustrator")) or ""
    hp_match = re.search(r"(\d+)", _text(root.select_one(".hp_num")) or "")
    info = _text(root.select_one(".pokemon-info")) or ""
    stage = info.split(":", 1)[1].strip() if ":" in info else info or None
    header_types = [node.get("title") for node in root.select(".header img[title]") if node.get("title")]
    attacks = []
    for ability in root.select(".pokemon-abilities .ability"):
        area = ability.select_one(".area-parent")
        attacks.append({
            "name": _text(ability.select_one(".skil_name")) or _text(ability.select_one("h4.label")),
            "cost": [node.get("title") for node in area.select("img[title]")] if area else [],
            "damage": _text(ability.select_one(".plus")),
            "effect": _text(ability.select_one("p")),
        })
    stats: dict[str, dict[str, object]] = {}
    for stat in root.select(".pokemon-stats .stat"):
        label = _text(stat.select_one("h4"))
        if label:
            stats[label] = {
                "types": [node.get("title") for node in stat.select("img[title]") if node.get("title")],
                "value": _text(stat.select_one("span")), "title": stat.get("title"),
            }
    retreat_match = re.search(r"(\d+)", str((stats.get("후퇴") or {}).get("title") or ""))
    dex_match = re.search(r"No\.\s*(\d+)", _text(root.select_one(".pokemon-detail .col-md-4")) or "")
    symbol_images = root.select(".pre_info_wrap > img")
    regulation_mark = None
    if len(symbol_images) > 1:
        regulation_mark = Path(urlsplit(str(symbol_images[1].get("src"))).path).stem
        if regulation_mark.casefold() == "none":
            regulation_mark = None
    return {
        "provider_record_id": provider_record_id,
        "source_url": source_url,
        "local_name": _text(name_node),
        "set_code": set_code,
        "set_names": [_text(node) for node in root.select(".pokemon-detail .search_href") if _text(node)],
        "collector_number": number_match.group(1) if number_match else number_text or None,
        "printed_total": number_match.group(2) if number_match else None,
        "rarity": rarity or None,
        "illustrator": illustrator_text.removeprefix("일러스트").strip() or None,
        "stage": stage, "hp": int(hp_match.group(1)) if hp_match else None,
        "types": header_types, "attacks": attacks,
        "weaknesses": [stats["약점"]] if stats.get("약점", {}).get("types") else [],
        "resistances": [stats["저항력"]] if stats.get("저항력", {}).get("types") else [],
        "retreat_cost": ["무색"] * (int(retreat_match.group(1)) if retreat_match else 0),
        "regulation_mark": regulation_mark,
        "national_pokedex_numbers": [int(dex_match.group(1))] if dex_match else [],
        "description": _text(root.select_one(".pokemon-detail .colsit > p")),
        "image_url": image_url,
        "image_source_url": str(image.get("src")),
    }


SCHEMA = """
pragma journal_mode=wal;
create table if not exists fetch_log(url text primary key,status integer,sha256 text,byte_size integer,
 content_type text,storage_path text,fetched_at text,error text);
create table if not exists cards(provider_record_id text primary key,source_url text not null,
 replay_url text not null,archive_timestamp text not null,archive_digest text not null,
 parsed_json text,raw_sha256 text,status text not null,error text,updated_at text not null);
create table if not exists collector_runs(id text primary key,status text not null,started_at text not null,
 completed_at text,counters_json text not null default '{}',error text);
"""


class Collector:
    def __init__(self, runtime_root: Path, delay_seconds: float = 0.5):
        self.root = runtime_root.resolve() / "pokemon-korea-archive"
        self.raw = self.root / "raw"
        self.raw.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.root / "checkpoint.sqlite")
        self.connection.executescript(SCHEMA)
        self.client = httpx.Client(
            headers={"User-Agent": USER_AGENT}, timeout=60, follow_redirects=True, http2=False,
        )
        self.delay_seconds = delay_seconds

    def close(self) -> None:
        self.connection.close()
        self.client.close()

    def fetch(self, url: str, *, suffix: str = ".html") -> bytes:
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

    def fallback_captures(self, provider_record_id: str) -> list[dict[str, str]]:
        query = urlencode({
            "url": f"pokemoncard.co.kr/cards/detail/{provider_record_id}",
            "from": "2019", "to": "2026", "output": "json", "filter": "statuscode:200",
            "fl": "timestamp,original,digest,statuscode", "collapse": "digest", "sort": "reverse", "limit": "50",
        })
        data = json.loads(self.fetch(f"https://web.archive.org/cdx/search/cdx?{query}", suffix=".json"))
        header = ["timestamp", "original", "digest", "statuscode"]
        if not data or data[0] != header:
            return []
        rows = []
        for values in data[1:]:
            row = dict(zip(header, values))
            row["provider_record_id"] = provider_record_id
            row["replay_url"] = f"https://web.archive.org/web/{row['timestamp']}id_/{row['original']}"
            rows.append(row)
        return rows

    def run(self) -> dict[str, int]:
        run_id = sha256(utc_now().encode())[:24]
        self.connection.execute(
            "insert into collector_runs(id,status,started_at) values (?, 'running', ?)", (run_id, utc_now()),
        )
        self.connection.commit()
        counters = {"indexed_cards": 0, "parsed_cards": 0, "fetch_errors": 0, "parse_errors": 0}
        try:
            entries = parse_cdx(self.fetch(CDX_URL, suffix=".json"))
            counters["indexed_cards"] = len(entries)
            for entry in entries:
                try:
                    content = self.fetch(entry["replay_url"])
                    try:
                        parsed = parse_card(content.decode("utf-8"), entry["original"], entry["provider_record_id"])
                    except Exception as initial_error:
                        parsed = None
                        try:
                            fallbacks = self.fallback_captures(entry["provider_record_id"])
                        except Exception:
                            fallbacks = []
                        for fallback in fallbacks:
                            if (fallback["timestamp"], fallback["original"]) == (entry["timestamp"], entry["original"]):
                                continue
                            try:
                                content = self.fetch(fallback["replay_url"])
                                parsed = parse_card(
                                    content.decode("utf-8"), fallback["original"], entry["provider_record_id"],
                                )
                                entry = {**entry, **fallback}
                                break
                            except Exception:
                                continue
                        if parsed is None:
                            raise initial_error
                    self.connection.execute(
                        "insert or replace into cards values (?, ?, ?, ?, ?, ?, ?, 'parsed', null, ?)",
                        (entry["provider_record_id"], entry["original"], entry["replay_url"], entry["timestamp"],
                         entry["digest"], json.dumps(parsed, ensure_ascii=False, sort_keys=True), sha256(content), utc_now()),
                    )
                    counters["parsed_cards"] += 1
                except (httpx.HTTPError, OSError) as error:
                    self.connection.execute(
                        "insert or replace into cards values (?, ?, ?, ?, ?, null, null, 'fetch_error', ?, ?)",
                        (entry["provider_record_id"], entry["original"], entry["replay_url"], entry["timestamp"],
                         entry["digest"], f"{type(error).__name__}: {error}", utc_now()),
                    )
                    counters["fetch_errors"] += 1
                except Exception as error:
                    self.connection.execute(
                        "insert or replace into cards values (?, ?, ?, ?, ?, null, ?, 'parse_error', ?, ?)",
                        (entry["provider_record_id"], entry["original"], entry["replay_url"], entry["timestamp"],
                         entry["digest"], sha256(content), f"{type(error).__name__}: {error}", utc_now()),
                    )
                    counters["parse_errors"] += 1
                self.connection.commit()
            status = "completed" if not counters["fetch_errors"] and not counters["parse_errors"] else "completed_with_errors"
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
