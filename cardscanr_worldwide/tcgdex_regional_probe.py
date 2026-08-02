"""Probe language-specific TCGdex set rosters for derived regional printings."""

from __future__ import annotations

import hashlib
import json
import random
import sqlite3
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


API_BASE = "https://api.tcgdex.net/v2"
USER_AGENT = "CardScanR-catalogue-research/1.0"
DERIVATION_PROVIDER = "cardscanr-regional-roster-derivations"

SCHEMA = """
pragma journal_mode=wal;
create table if not exists fetch_log(
  url text primary key,
  status integer,
  sha256 text,
  byte_size integer,
  content_type text,
  storage_path text,
  fetched_at text,
  attempts integer not null default 0,
  error text
);
create table if not exists set_probe(
  language text not null,
  set_release_id text not null,
  set_code text not null,
  expected_count integer not null,
  status text not null,
  http_status integer,
  api_card_count integer,
  api_official_count integer,
  source_url text not null,
  response_sha256 text,
  checked_at text not null,
  error text,
  primary key(language,set_release_id)
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def classify_roster(payload: Any, expected_count: int) -> tuple[str, int, int | None]:
    if not isinstance(payload, dict):
        return "invalid_payload", 0, None
    cards = payload.get("cards")
    if not isinstance(cards, list):
        return "invalid_payload", 0, None
    official = (payload.get("cardCount") or {}).get("official")
    official_count = int(official) if isinstance(official, int) else None
    card_count = len(cards)
    if card_count == expected_count and (official_count is None or official_count == expected_count):
        return "exact_roster", card_count, official_count
    if card_count == 0:
        return "empty_roster", card_count, official_count
    if card_count < expected_count:
        return "partial_roster", card_count, official_count
    return "count_mismatch", card_count, official_count


def derived_releases(database: Path) -> list[dict[str, Any]]:
    connection = sqlite3.connect(f"file:{database.resolve().as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(
            """select sr.language_code language,sr.id set_release_id,sr.release_code set_code,
                      sr.official_count expected_count
                 from set_release sr join card_printing cp on cp.set_release_id=sr.id
                 join source_record src on src.id=cp.source_record_id
                where src.provider_id=?
                group by sr.id order by sr.language_code,sr.release_code,sr.id""",
            (DERIVATION_PROVIDER,),
        )]
    finally:
        connection.close()


class Probe:
    def __init__(self, runtime_root: Path, delay_seconds: float = 0.2):
        self.root = runtime_root.resolve()
        self.raw = self.root / "raw"
        self.raw.mkdir(parents=True, exist_ok=True)
        self.checkpoint = self.root / "checkpoint.sqlite"
        self.connection = sqlite3.connect(self.checkpoint)
        self.connection.executescript(SCHEMA)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        self.delay_seconds = delay_seconds

    def close(self) -> None:
        self.session.close()
        self.connection.close()

    def _fetch(self, url: str) -> tuple[int, bytes, str | None, str | None]:
        cached = self.connection.execute(
            "select status,storage_path,content_type,error from fetch_log where url=?", (url,),
        ).fetchone()
        if cached and cached[1] and Path(cached[1]).exists():
            return int(cached[0] or 0), Path(cached[1]).read_bytes(), cached[2], cached[3]
        attempts = int((self.connection.execute(
            "select attempts from fetch_log where url=?", (url,),
        ).fetchone() or [0])[0])
        time.sleep(self.delay_seconds + random.uniform(0, self.delay_seconds * 0.2))
        try:
            response = self.session.get(url, timeout=(10, 45))
            body = response.content
            digest = sha256_bytes(body)
            path = self.raw / f"{digest}.json"
            if not path.exists():
                path.write_bytes(body)
            error = None if response.status_code == 200 else f"http_status:{response.status_code}"
            self.connection.execute(
                """insert into fetch_log values (?,?,?,?,?,?,?,?,?)
                   on conflict(url) do update set status=excluded.status,sha256=excluded.sha256,
                   byte_size=excluded.byte_size,content_type=excluded.content_type,
                   storage_path=excluded.storage_path,fetched_at=excluded.fetched_at,
                   attempts=excluded.attempts,error=excluded.error""",
                (url, response.status_code, digest, len(body), response.headers.get("Content-Type"),
                 str(path), utc_now(), attempts + 1, error),
            )
            self.connection.commit()
            return response.status_code, body, response.headers.get("Content-Type"), error
        except Exception as error:
            message = f"{type(error).__name__}:{error}"
            self.connection.execute(
                """insert into fetch_log(url,fetched_at,attempts,error) values(?,?,?,?)
                   on conflict(url) do update set fetched_at=excluded.fetched_at,
                   attempts=excluded.attempts,error=excluded.error""",
                (url, utc_now(), attempts + 1, message),
            )
            self.connection.commit()
            return 0, b"", None, message

    def run(self, database: Path) -> dict[str, int]:
        counters: Counter[str] = Counter()
        for release in derived_releases(database):
            language = str(release["language"])
            set_code = str(release["set_code"])
            expected_count = int(release["expected_count"])
            url = f"{API_BASE}/{language}/sets/{set_code}"
            http_status, body, _, fetch_error = self._fetch(url)
            response_sha = sha256_bytes(body) if body else None
            api_count = 0
            official_count = None
            error = fetch_error
            if http_status == 200:
                try:
                    status, api_count, official_count = classify_roster(
                        json.loads(body.decode("utf-8")), expected_count,
                    )
                except (UnicodeDecodeError, json.JSONDecodeError) as parse_error:
                    status = "invalid_payload"
                    error = f"{type(parse_error).__name__}:{parse_error}"
            elif http_status == 404:
                status = "not_found"
            else:
                status = "fetch_failed"
            self.connection.execute(
                """insert or replace into set_probe values(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (language, release["set_release_id"], set_code, expected_count, status,
                 http_status or None, api_count, official_count, url, response_sha, utc_now(), error),
            )
            self.connection.commit()
            counters[status] += 1
            counters["expected_printings"] += expected_count
            counters["api_cards"] += api_count
        return dict(counters)


def report(checkpoint: Path) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{checkpoint.resolve().as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return {
            "status": {row["status"]: row["rows"] for row in connection.execute(
                "select status,count(*) rows from set_probe group by status order by status"
            )},
            "languages": [dict(row) for row in connection.execute(
                """select language,status,count(*) sets,sum(expected_count) expected_printings,
                          sum(api_card_count) api_cards
                     from set_probe group by language,status order by language,status"""
            )],
            "sets": [dict(row) for row in connection.execute(
                "select * from set_probe order by language,set_code,set_release_id"
            )],
        }
    finally:
        connection.close()

