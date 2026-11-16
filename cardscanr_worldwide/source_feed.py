"""A bounded, user-supplied source feed for local-only Pokémon catalogue research.

The feed is *not* a rights grant, automatic canonical-ID matcher, production image
publisher or unrestricted web crawler. Each source has an explicit host/path scope.
"""
from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import re
import socket
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from filelock import FileLock, Timeout
from PIL import Image

USER_AGENT = "CardScanR-private-catalogue-research/1.0"
MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_QUEUE_ITEMS = 100_000
ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,59}$")
KINDS = {"index", "card", "image"}
ALLOWED_RIGHTS = {"metadata_only", "permission_pending", "approved_for_mirror", "link_only", "restricted"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _url(url: str, hosts: list[str], prefixes: list[str]) -> str:
    """Validate a scoped URL. DNS pinning must also be checked at request time."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    if (parts.scheme != "https" or not host or parts.username or parts.password or
            parts.port not in (None, 443) or not hosts or host not in hosts or
            host in {"localhost", "localhost.localdomain"} or host.endswith(".local")):
        raise ValueError(f"URL outside explicit HTTPS host allowlist: {url}")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("IP-address URLs are not accepted")
    if not prefixes or not any(parts.path == prefix or parts.path.startswith(prefix.rstrip("/") + "/") for prefix in prefixes):
        raise ValueError(f"URL outside source path scope: {url}")
    if not all(prefix.startswith("/") and ".." not in prefix for prefix in prefixes):
        raise ValueError("Invalid source path scope")
    return urlunsplit(("https", host, parts.path or "/", parts.query, ""))


def validate_feed(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if doc.get("schema_version") != 1 or not isinstance(doc.get("sources"), list):
        raise ValueError("Expected schema_version=1 with sources array")
    entries: dict[str, dict[str, Any]] = {}
    for item in doc["sources"]:
        if not isinstance(item, dict) or not ID_RE.fullmatch(str(item.get("id", ""))):
            raise ValueError("Every source needs a stable lowercase slug ID")
        sid = item["id"]
        if sid in entries:
            raise ValueError(f"Duplicate source ID: {sid}")
        for name in ("region", "language", "rights_status"):
            if not isinstance(item.get(name), str) or not item[name]:
                raise ValueError(f"Source {sid} missing {name}")
        if item["rights_status"] not in ALLOWED_RIGHTS:
            raise ValueError(f"Unknown rights_status for {sid}")
        if type(item.get("enabled")) is not bool:
            raise ValueError(f"Source {sid}: enabled must be boolean")
        if item.get("archive_policy") != "private_research_only":
            raise ValueError("Only private_research_only feeds are accepted")
        for key in ("page_hosts", "page_paths", "image_hosts", "image_paths"):
            if (not isinstance(item.get(key), list) or not item[key] or
                    not all(isinstance(v, str) and v for v in item[key])):
                raise ValueError(f"Source {sid} needs explicit {key}")
        if not isinstance(item.get("seeds"), list) or len(item["seeds"]) > 10000:
            raise ValueError(f"Source {sid}: invalid seeds")
        if not isinstance(item.get("selectors", {}), dict):
            raise ValueError(f"Source {sid}: invalid selectors")
        for seed in item["seeds"]:
            if not isinstance(seed, dict) or seed.get("kind") not in KINDS:
                raise ValueError(f"Source {sid}: invalid seed kind")
            kind = seed["kind"]
            _url(seed.get("url", ""), item["image_hosts"] if kind == "image" else item["page_hosts"],
                 item["image_paths"] if kind == "image" else item["page_paths"])
            if kind == "image" and not seed.get("printing_key"):
                raise ValueError("Direct image seeds require user-supplied printing_key")
        entries[sid] = item
    return entries


def load_feed(path: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    return doc, validate_feed(doc)


def add_seed(feed_path: Path, source_id: str, kind: str, url: str, printing_key: str | None = None) -> bool:
    """The owner's simple source inbox; no network access or auto-allowlisting."""
    doc, sources = load_feed(feed_path)
    src = sources[source_id]
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {sorted(KINDS)}")
    valid_url = _url(url, src["image_hosts"] if kind == "image" else src["page_hosts"],
                     src["image_paths"] if kind == "image" else src["page_paths"])
    if kind == "image" and not printing_key:
        raise ValueError("Direct images must have an exact printing_key supplied by owner")
    seed = {"kind": kind, "url": valid_url}
    if printing_key:
        seed["printing_key"] = printing_key
    if any(s["kind"] == kind and s["url"] == valid_url for s in src["seeds"]):
        return False
    src["seeds"].append(seed)
    validate_feed(doc)
    temp = feed_path.with_suffix(feed_path.suffix + ".tmp")
    temp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp.replace(feed_path)
    return True



def add_source(feed_path: Path, *, source_id: str, name: str, region: str, language: str,
               page_host: str, page_path: str, image_host: str, image_path: str) -> None:
    """Register a new, disabled scoped source; this never grants crawl permission."""
    doc, sources = load_feed(feed_path)
    if source_id in sources:
        raise ValueError(f"Source already registered: {source_id}")
    source = {
        "id": source_id, "name": name, "region": region, "language": language,
        "rights_status": "permission_pending", "archive_policy": "private_research_only",
        "enabled": False, "page_hosts": [page_host], "page_paths": [page_path],
        "image_hosts": [image_host], "image_paths": [image_path],
        "selectors": {"card_links": "a[href]", "card_image": "meta[property='og:image']", "fields": {"name": "h1"}},
        "seeds": []
    }
    # Validation before writing, and no arbitrary URL added by registering a provider.
    _url(f"https://{page_host}{page_path}", [page_host], [page_path])
    _url(f"https://{image_host}{image_path}", [image_host], [image_path])
    doc["sources"].append(source)
    validate_feed(doc)
    tmp = feed_path.with_suffix(feed_path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(feed_path)

def _public_dns(host: str) -> None:
    """Fail closed if a host resolves to a local/private address before each request."""
    for entry in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM):
        addr = ipaddress.ip_address(entry[4][0])
        if not addr.is_global:
            raise PermissionError(f"Source host resolves to non-public address: {host}")


SCHEMA = """
CREATE TABLE IF NOT EXISTS queue (
 source_id TEXT NOT NULL, kind TEXT NOT NULL, url TEXT NOT NULL,
 printing_key TEXT, parent_url TEXT, state TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0, error TEXT, updated_at TEXT,
 PRIMARY KEY(source_id, kind, url)
);
CREATE TABLE IF NOT EXISTS artifacts (
 source_id TEXT NOT NULL, kind TEXT NOT NULL, url TEXT NOT NULL,
 sha256 TEXT NOT NULL, byte_size INTEGER NOT NULL, relative_path TEXT NOT NULL,
 metadata_json TEXT NOT NULL, fetched_at TEXT NOT NULL,
 PRIMARY KEY(source_id, kind, url)
);
"""


@dataclass
class FetchResult:
    data: bytes
    mime: str


class FeedRunner:
    def __init__(self, source: dict[str, Any], archive: Path, *, session: Any = None, pace_seconds: float = 1.0):
        self.source = source
        self.archive = archive.resolve()
        self.session = session if session is not None else requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.pace_seconds = max(1.0, pace_seconds)
        self.robots: dict[str, RobotFileParser] = {}
        self.last_request_at = 0.0
        self.db: sqlite3.Connection | None = None

    def __enter__(self) -> "FeedRunner":
        self.archive.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.archive / "source-feed-checkpoint.sqlite")
        self.db.executescript(SCHEMA)
        self.db.commit()
        return self

    def __exit__(self, *_: object) -> None:
        if self.db is not None:
            self.db.close()
        if hasattr(self.session, "close"):
            self.session.close()

    def enqueue(self, kind: str, url: str, printing_key: str | None = None, parent: str | None = None) -> None:
        assert self.db is not None
        s = self.source
        scoped_url = _url(url, s["image_hosts"] if kind == "image" else s["page_hosts"],
                          s["image_paths"] if kind == "image" else s["page_paths"])
        count = self.db.execute("SELECT count(*) FROM queue WHERE source_id=?", (s["id"],)).fetchone()[0]
        if count >= MAX_QUEUE_ITEMS:
            raise RuntimeError("Source discovery queue limit reached")
        self.db.execute("INSERT OR IGNORE INTO queue(source_id,kind,url,printing_key,parent_url,state) "
                        "VALUES (?,?,?,?,?,'pending')", (s["id"], kind, scoped_url, printing_key, parent))
        self.db.commit()

    def _pace(self) -> None:
        delay = self.pace_seconds - (time.monotonic() - self.last_request_at)
        if delay > 0:
            time.sleep(delay)
        self.last_request_at = time.monotonic()

    def _raw_get(self, url: str, max_bytes: int) -> tuple[bytes, int, str]:
        host = urlsplit(url).hostname or ""
        _public_dns(host)
        self._pace()
        # No automatic redirects: revalidate both host and path for every hop.
        response = self.session.get(url, timeout=(10, 35), stream=True, allow_redirects=False)
        try:
            status = response.status_code
            mime = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if status in (301, 302, 303, 307, 308):
                raise PermissionError(f"Redirect requires manual source review: {url}")
            if status != 200:
                raise IOError(f"HTTP {status} for {url}")
            size = 0
            chunks = []
            for chunk in response.iter_content(chunk_size=65536):
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("Response exceeds bounded size")
                chunks.append(chunk)
            return b"".join(chunks), status, mime
        finally:
            response.close()

    def _check_robots(self, url: str) -> None:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if host not in self.robots:
            robots_url = f"https://{host}/robots.txt"
            # Explicit robots endpoint, never follow redirects to other hosts.
            data, _, mime = self._raw_get(robots_url, 512 * 1024)
            if mime not in {"text/plain", "text/html", ""}:
                raise PermissionError(f"Unexpected robots MIME for {host}")
            rules = RobotFileParser()
            rules.parse(data.decode("utf-8", errors="replace").splitlines())
            self.robots[host] = rules
        rules = self.robots[host]
        if not rules.can_fetch(USER_AGENT, url):
            raise PermissionError(f"robots.txt disallows access to {url}")
        crawl_delay = rules.crawl_delay(USER_AGENT)
        if crawl_delay:
            self.pace_seconds = max(self.pace_seconds, float(crawl_delay))

    def fetch(self, kind: str, url: str) -> FetchResult:
        s = self.source
        scope_hosts = s["image_hosts"] if kind == "image" else s["page_hosts"]
        scope_paths = s["image_paths"] if kind == "image" else s["page_paths"]
        valid = _url(url, scope_hosts, scope_paths)
        self._check_robots(valid)
        data, _, mime = self._raw_get(valid, MAX_IMAGE_BYTES if kind == "image" else MAX_HTML_BYTES)
        allowed = mime.startswith("image/") if kind == "image" else mime in ("text/html", "application/xhtml+xml")
        if not allowed:
            raise ValueError(f"Unexpected MIME {mime!r} for {kind}")
        if kind == "image":
            with Image.open(io.BytesIO(data)) as image:
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                if image.width <= 0 or image.height <= 0 or image.width * image.height > 100_000_000:
                    raise ValueError("Unsafe image dimensions")
        return FetchResult(data, mime)

    def _record(self, kind: str, url: str, result: FetchResult, metadata: dict[str, Any]) -> None:
        assert self.db is not None
        checksum = digest(result.data)
        suffix = ".html" if kind != "image" else (".png" if result.mime == "image/png" else
                          ".jpg" if result.mime == "image/jpeg" else ".webp" if result.mime == "image/webp" else ".img")
        relative = Path("sources") / self.source["id"] / kind / f"{checksum}{suffix}"
        dest = self.archive / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            tmp = dest.with_suffix(dest.suffix + ".part")
            tmp.write_bytes(result.data)
            tmp.replace(dest)
        elif digest(dest.read_bytes()) != checksum:
            raise IOError("Existing archived file has a checksum mismatch")
        self.db.execute("INSERT OR REPLACE INTO artifacts VALUES(?,?,?,?,?,?,?,?)",
                        (self.source["id"], kind, url, checksum, len(result.data), str(relative),
                         json.dumps(metadata, sort_keys=True, ensure_ascii=False), utc_now()))
        self.db.commit()

    def _process(self, row: tuple[str, str, str | None, str | None]) -> dict[str, Any]:
        kind, url, printing_key, parent = row
        result = self.fetch(kind, url)
        metadata: dict[str, Any] = {"source_id": self.source["id"], "region": self.source["region"],
               "language": self.source["language"], "url": url, "kind": kind,
               "rights_status": self.source["rights_status"], "printing_key": printing_key,
               "parent_url": parent, "exact_canonical_match": False, "public_release_approved": False}
        if kind == "image":
            with Image.open(io.BytesIO(result.data)) as image:
                metadata["width"], metadata["height"] = image.size
                metadata["image_format"] = image.format
        else:
            soup = BeautifulSoup(result.data, "html.parser")
            sel = self.source.get("selectors", {})
            if kind == "index":
                for name, child_kind in (("card_links", "card"), ("next_page", "index")):
                    if not sel.get(name):
                        continue
                    for node in soup.select(sel[name]):
                        href = node.get("href")
                        if href:
                            try:
                                self.enqueue(child_kind, urljoin(url, str(href)), parent=url)
                            except ValueError:
                                continue  # Never leave explicitly allowed host/path scope.
            else:
                fields = sel.get("fields", {})
                for key, selector in fields.items():
                    if key not in {"name", "set_name", "collector_number", "printed_total", "rarity", "hp", "types", "artist", "description"}:
                        continue
                    element = soup.select_one(selector)
                    metadata[key] = element.get_text(" ", strip=True) if element else None
                for image in soup.select(sel.get("card_image", "meta[property='og:image']")):
                    href = image.get("content") or image.get("data-src") or image.get("src")
                    if href:
                        try:
                            self.enqueue("image", urljoin(url, str(href)), parent=url, printing_key=printing_key)
                        except ValueError:
                            continue
                        break
        self._record(kind, url, result, metadata)
        return metadata

    def run(self, max_items: int = 25) -> dict[str, Any]:
        assert self.db is not None
        if not 1 <= max_items <= 100:
            raise ValueError("max_items must be between 1 and 100")
        for seed in self.source["seeds"]:
            self.enqueue(seed["kind"], seed["url"], seed.get("printing_key"))
        totals = {"attempted": 0, "succeeded": 0, "failed": 0, "new_images": 0, "source": self.source["id"],
                  "archive": str(self.archive), "archive_only": True}
        while totals["attempted"] < max_items:
            row = self.db.execute("SELECT kind,url,printing_key,parent_url FROM queue "
                "WHERE source_id=? AND state='pending' AND attempts < 3 "
                "ORDER BY CASE kind WHEN 'image' THEN 0 WHEN 'card' THEN 1 ELSE 2 END, rowid LIMIT 1",
                (self.source["id"],)).fetchone()
            if row is None:
                break
            kind, url = row[:2]
            totals["attempted"] += 1
            try:
                self._process(row)
                state, error = "completed", None
                totals["succeeded"] += 1
                if kind == "image":
                    totals["new_images"] += 1
            except Exception as exc:
                state, error = "failed", f"{type(exc).__name__}: {exc}"[:500]
                totals["failed"] += 1
            self.db.execute("UPDATE queue SET state=?,attempts=attempts+1,error=?,updated_at=? "
                            "WHERE source_id=? AND kind=? AND url=?",
                            (state, error, utc_now(), self.source["id"], kind, url))
            self.db.commit()
        totals["queued_remaining"] = self.db.execute("SELECT count(*) FROM queue WHERE source_id=? AND state='pending'",
                                                       (self.source["id"],)).fetchone()[0]
        return totals


def run_batch(feed: Path, source_id: str, archive: Path, *, max_items: int = 25, pace: float = 1.0) -> dict[str, Any]:
    _, sources = load_feed(feed)
    src = sources[source_id]
    if not src["enabled"]:
        raise PermissionError(f"Source {source_id!r} is disabled; review source rights and enable explicitly")
    archive = archive.resolve()
    archive.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(str(archive / ".catalogue-feed.lock"), timeout=0):
            with FeedRunner(src, archive, pace_seconds=pace) as runner:
                return runner.run(max_items)
    except Timeout as exc:
        raise RuntimeError("Another catalogue feed worker owns the archive lock") from exc