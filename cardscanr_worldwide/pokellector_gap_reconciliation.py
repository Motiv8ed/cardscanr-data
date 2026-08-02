"""Resumable exact-identity reconciliation for the legacy English image gap."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup


BASE_URL = "https://www.pokellector.com"
USER_AGENT = "CardScanR-catalogue-preservation/1.0"
SET_ROOTS = {
    "mcd14": "/McDonalds-Collection-2014-Expansion/",
    "mcd15": "/McDonalds-Collection-2015-Expansion/",
    "mcd17": "/McDonalds-Collection-2017-Expansion/",
    "mcd18": "/McDonalds-Collection-2018-Expansion/",
    "hsp": "/HeartGold-SoulSilver-Promos-Expansion/",
    "svp": "/Scarlet-Violet-English-Promos-Expansion/",
}

SCHEMA = """
pragma journal_mode=wal;
create table if not exists fetch_log(
 url text primary key,status integer,sha256 text,byte_size integer,content_type text,
 storage_path text,fetched_at text,error text
);
create table if not exists evidence(
 target_variant_id text primary key,target_printing_id text not null,set_id text not null,
 set_name text not null,collector_number text not null,card_name text not null,
 page_url text,image_url text,status text not null,error text,updated_at text not null
);
create table if not exists runs(
 id text primary key,status text not null,started_at text not null,completed_at text,
 counters_json text not null default '{}',error text
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def normalized(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).casefold()
    return "".join(character for character in value if character.isalnum())


def parse_set_links(html: str, root_path: str) -> dict[int, tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    pattern = re.compile(re.escape(root_path) + r"([^/]+)-Card-(\d+)$", re.I)
    result: dict[int, tuple[str, str]] = {}
    for link in soup.find_all("a", href=True):
        match = pattern.search(str(link["href"]))
        if match:
            result[int(match.group(2))] = (match.group(1).replace("-", " "), urljoin(BASE_URL, link["href"]))
    return result


def parse_card_page(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.find("meta", property="og:title")
    image = soup.find("meta", property="og:image")
    if not title or not title.get("content") or not image or not image.get("content"):
        raise ValueError("Pokellector page lacks exact title or image metadata")
    return str(title["content"]), str(image["content"])


def load_targets(report_path: Path) -> list[dict[str, str]]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    targets = []
    for row in [*(report.get("details") or []), *(report.get("unmatched_items") or [])]:
        printing_id = str(row.get("target_printing_id") or "")
        variant_id = str(row["target_variant_id"])
        if not printing_id:
            printing_id = variant_id.split(":unspecified", 1)[0].replace("_en_printing_", ":en:printing:")
        match = re.search(r"printing:([^-:]+)-", printing_id)
        if not match or match.group(1) not in SET_ROOTS:
            raise ValueError(f"Unsupported exact target set identity: {printing_id}")
        targets.append({
            "target_printing_id": printing_id, "target_variant_id": variant_id,
            "set_id": match.group(1), "set_name": str(row["set_name"]),
            "collector_number": str(row["collector_number"]), "card_name": str(row["card_name"]),
        })
    return sorted(targets, key=lambda row: row["target_variant_id"])


class Collector:
    def __init__(self, runtime_root: Path, delay_seconds: float = 0.3):
        self.root = runtime_root.resolve() / "pokellector-english-gaps"
        self.raw = self.root / "raw"; self.raw.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.root / "checkpoint.sqlite")
        self.connection.executescript(SCHEMA)
        self.client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=60, follow_redirects=True)
        self.delay_seconds = delay_seconds

    def close(self) -> None:
        self.client.close(); self.connection.close()

    def fetch(self, url: str) -> bytes:
        cached = self.connection.execute("select storage_path from fetch_log where url=? and status=200", (url,)).fetchone()
        if cached and cached[0] and Path(cached[0]).exists(): return Path(cached[0]).read_bytes()
        last_error = None
        for attempt in range(4):
            try:
                time.sleep(self.delay_seconds)
                response = self.client.get(url); response.raise_for_status(); content = response.content
                checksum = sha256(content); path = self.raw / f"{checksum}.html"
                if not path.exists(): path.write_bytes(content)
                self.connection.execute("insert or replace into fetch_log values (?,?,?,?,?,?,?,null)",
                    (url,response.status_code,checksum,len(content),response.headers.get("content-type"),str(path),utc_now()))
                self.connection.commit(); return content
            except Exception as error:
                last_error = error; time.sleep(min(2 ** attempt, 8))
        self.connection.execute("insert or replace into fetch_log(url,fetched_at,error) values (?,?,?)",
                                (url,utc_now(),f"{type(last_error).__name__}: {last_error}"))
        self.connection.commit(); raise last_error or RuntimeError("unknown fetch error")

    def run(self, report_path: Path) -> dict[str, int]:
        run_id = sha256(utc_now().encode())[:24]
        self.connection.execute("insert into runs(id,status,started_at) values (?,'running',?)", (run_id,utc_now()))
        self.connection.commit(); counters = {"targets": 0, "resolved": 0, "errors": 0}
        try:
            targets = load_targets(report_path); counters["targets"] = len(targets)
            links = {}
            for set_id in sorted({target["set_id"] for target in targets}):
                root_path = SET_ROOTS[set_id]
                links[set_id] = parse_set_links(self.fetch(urljoin(BASE_URL, root_path)).decode("utf-8"), root_path)
            for target in targets:
                number_match = re.search(r"\d+", target["collector_number"])
                number = int(number_match.group()) if number_match else -1
                try:
                    link_name, page_url = links[target["set_id"]][number]
                    if normalized(link_name) != normalized(target["card_name"]):
                        raise ValueError(f"set index name mismatch: {link_name!r}")
                    title, image_url = parse_card_page(self.fetch(page_url).decode("utf-8"))
                    if normalized(target["card_name"]) not in normalized(title):
                        raise ValueError(f"card page title mismatch: {title!r}")
                    self.connection.execute("insert or replace into evidence values (?,?,?,?,?,?,?,?, 'resolved',null,?)",
                        (target["target_variant_id"],target["target_printing_id"],target["set_id"],target["set_name"],
                         target["collector_number"],target["card_name"],page_url,image_url,utc_now()))
                    counters["resolved"] += 1
                except Exception as error:
                    self.connection.execute("insert or replace into evidence values (?,?,?,?,?,?,null,null,'error',?,?)",
                        (target["target_variant_id"],target["target_printing_id"],target["set_id"],target["set_name"],
                         target["collector_number"],target["card_name"],f"{type(error).__name__}: {error}",utc_now()))
                    counters["errors"] += 1
                self.connection.commit()
            status = "completed" if not counters["errors"] else "completed_with_errors"
            self.connection.execute("update runs set status=?,completed_at=?,counters_json=? where id=?",
                (status,utc_now(),json.dumps(counters,sort_keys=True),run_id)); self.connection.commit()
            return counters
        except Exception as error:
            self.connection.execute("update runs set status='failed',completed_at=?,error=? where id=?",
                (utc_now(),f"{type(error).__name__}: {error}",run_id)); self.connection.commit(); raise
