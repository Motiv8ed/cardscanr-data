from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from .normalization import is_collector_number_query, normalize_collector_number, normalize_search_text


MATCH_CLASS_EXACT_SET_COLLECTOR = 1
MATCH_CLASS_EXACT_NAME = 2
MATCH_CLASS_NAME_PREFIX = 3
MATCH_CLASS_ALIAS_PREFIX = 4
MATCH_CLASS_SET_AND_NAME = 5
MATCH_CLASS_FTS = 6
MATCH_CLASS_SUBSTRING = 7


@dataclass(frozen=True)
class SearchRequest:
    query_text: str
    language: str | None = None
    set_id: str | None = None
    limit: int = 50
    offset: int = 0


@dataclass(frozen=True)
class SearchHit:
    canonical_base_id: str
    language: str
    set_id: str
    collector_number: str
    name: str | None
    localized_name: str | None
    set_name: str
    thumbnail_url: str | None
    large_image_url: str | None
    image_source: str | None
    image_cached: bool
    match_class: int
    score: float


def connect_readonly(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _card_row_to_hit(row: sqlite3.Row, *, match_class: int, score: float) -> SearchHit:
    return SearchHit(
        canonical_base_id=row["canonical_base_id"],
        language=row["language"],
        set_id=row["set_id"],
        collector_number=row["collector_number"],
        name=row["canonical_english_name"] or row["localized_name"],
        localized_name=row["localized_name"],
        set_name=row["set_name"],
        thumbnail_url=row["thumbnail_url"],
        large_image_url=row["large_image_url"],
        image_source=row["image_source"],
        image_cached=bool(row["image_cached"]),
        match_class=match_class,
        score=score,
    )


def _filters(request: SearchRequest) -> tuple[str, list[Any]]:
    clauses = ["1=1"]
    params: list[Any] = []
    if request.language:
        clauses.append("c.language = ?")
        params.append(request.language)
    if request.set_id:
        clauses.append("c.set_id = ?")
        params.append(request.set_id)
    return " AND ".join(clauses), params


def _fetch(conn: sqlite3.Connection, sql: str, params: list[Any], *, match_class: int, score: float) -> list[SearchHit]:
    return [_card_row_to_hit(row, match_class=match_class, score=score) for row in conn.execute(sql, params)]


def _merge_hits(hits: list[SearchHit]) -> list[SearchHit]:
    seen: set[str] = set()
    ordered: list[SearchHit] = []
    for hit in sorted(
        hits,
        key=lambda item: (
            item.match_class,
            0 if item.language == "en" else 1,
            item.set_id,
            item.collector_number,
            item.canonical_base_id,
        ),
    ):
        if hit.canonical_base_id in seen:
            continue
        seen.add(hit.canonical_base_id)
        ordered.append(hit)
    return ordered


def _best_class(hits: list[SearchHit]) -> int:
    if not hits:
        return 99
    return min(item.match_class for item in hits)


def _should_return(hits: list[SearchHit], *, norm_query: str, need: int) -> bool:
    merged = _merge_hits(hits)
    if not merged:
        return False
    best = _best_class(hits)
    if best <= MATCH_CLASS_EXACT_SET_COLLECTOR:
        return True
    if best <= MATCH_CLASS_EXACT_NAME:
        return True
    if best <= MATCH_CLASS_SET_AND_NAME and " " in norm_query:
        return True
    if len(merged) >= need:
        return True
    if best <= MATCH_CLASS_NAME_PREFIX:
        return True
    if best <= MATCH_CLASS_ALIAS_PREFIX:
        return True
    return False


def _slice_results(hits: list[SearchHit], request: SearchRequest) -> list[SearchHit]:
    merged = _merge_hits(hits)
    return merged[request.offset : request.offset + request.limit]


def search_cards(conn: sqlite3.Connection, request: SearchRequest) -> list[SearchHit]:
    query = (request.query_text or "").strip()
    if not query:
        return []
    norm_query = normalize_search_text(query)
    if not norm_query:
        return []

    norm_collector = normalize_collector_number(query) if is_collector_number_query(query) else ""
    prefix = f"{norm_query}%"
    like = f"%{norm_query}%"
    where, params = _filters(request)
    hits: list[SearchHit] = []
    need = max(request.limit + request.offset, 1)
    multi_word = " " in norm_query

    if norm_collector:
        hits.extend(
            _fetch(
                conn,
                f"SELECT c.* FROM cards c WHERE {where} AND c.normalized_collector_number = ? LIMIT ?",
                params + [norm_collector, need],
                match_class=MATCH_CLASS_EXACT_SET_COLLECTOR,
                score=100.0,
            )
        )
        if hits and ("/" in query or query.isdigit() or norm_collector != norm_query):
            return _slice_results(hits, request)

    if multi_word:
        set_token, name_token = norm_query.split(" ", 1)
        if set_token and name_token:
            hits.extend(
                _fetch(
                    conn,
                    f"""
                    SELECT c.* FROM cards c
                    WHERE {where}
                      AND c.normalized_set_name LIKE ?
                      AND (c.normalized_canonical_name LIKE ? OR c.normalized_localized_name LIKE ?)
                    LIMIT ?
                    """,
                    params + [f"{set_token}%", f"{name_token}%", f"{name_token}%", need],
                    match_class=MATCH_CLASS_SET_AND_NAME,
                    score=60.0,
                )
            )
            if _should_return(hits, norm_query=norm_query, need=need):
                return _slice_results(hits, request)

    if request.language == "jp":
        exact_clauses = [
            ("c.normalized_localized_name = ?", norm_query),
            (
                "c.normalized_canonical_name = ? AND c.normalized_canonical_name != c.normalized_localized_name",
                norm_query,
            ),
        ]
    else:
        exact_clauses = [("c.normalized_canonical_name = ?", norm_query)]

    for clause, value in exact_clauses:
        hits.extend(
            _fetch(
                conn,
                f"""
                SELECT c.* FROM cards c
                WHERE {where} AND {clause}
                LIMIT ?
                """,
                params + [value, need],
                match_class=MATCH_CLASS_EXACT_NAME,
                score=90.0,
            )
        )
        if _should_return(hits, norm_query=norm_query, need=need):
            return _slice_results(hits, request)
        if not hits and len(norm_query) >= 12:
            return []

    if request.language == "jp":
        prefix_clauses = [
            "c.normalized_localized_name LIKE ?",
            "c.normalized_canonical_name LIKE ? AND c.normalized_canonical_name != c.normalized_localized_name",
        ]
    else:
        prefix_clauses = ["c.normalized_canonical_name LIKE ?"]

    for clause in prefix_clauses:
        hits.extend(
            _fetch(
                conn,
                f"""
                SELECT c.* FROM cards c
                WHERE {where} AND {clause}
                LIMIT ?
                """,
                params + [prefix, need],
                match_class=MATCH_CLASS_NAME_PREFIX,
                score=80.0,
            )
        )
        if _should_return(hits, norm_query=norm_query, need=need):
            return _slice_results(hits, request)

    if not hits and len(norm_query) >= 8:
        return []

    if len(norm_query) <= 32:
        hits.extend(
            _fetch(
                conn,
                f"""
                SELECT c.*
                FROM card_aliases a
                JOIN cards c ON c.canonical_base_id = a.canonical_base_id
                WHERE {where} AND a.normalized_alias LIKE ?
                LIMIT ?
                """,
                params + [prefix, need],
                match_class=MATCH_CLASS_ALIAS_PREFIX,
                score=70.0,
            )
        )
        if _should_return(hits, norm_query=norm_query, need=need):
            return _slice_results(hits, request)

    fts_query = " ".join(f'"{token}"*' for token in norm_query.split() if token)
    if fts_query and len(_merge_hits(hits)) < need:
        hits.extend(
            _fetch(
                conn,
                f"""
                SELECT c.*
                FROM cards_fts f
                JOIN cards c ON c.canonical_base_id = f.canonical_base_id
                WHERE {where} AND cards_fts MATCH ?
                LIMIT ?
                """,
                params + [fts_query, need],
                match_class=MATCH_CLASS_FTS,
                score=50.0,
            )
        )
        if _should_return(hits, norm_query=norm_query, need=need):
            return _slice_results(hits, request)

    if hits and len(norm_query) >= 4 and len(_merge_hits(hits)) < need:
        hits.extend(
            _fetch(
                conn,
                f"""
                SELECT c.* FROM cards c
                WHERE {where}
                  AND (
                    c.normalized_canonical_name LIKE ?
                    OR c.normalized_localized_name LIKE ?
                    OR c.normalized_set_name LIKE ?
                  )
                LIMIT ?
                """,
                params + [like, like, like, need],
                match_class=MATCH_CLASS_SUBSTRING,
                score=40.0,
            )
        )

    return _slice_results(hits, request)


def lookup_exact_identity(
    conn: sqlite3.Connection,
    *,
    language: str,
    set_id: str,
    collector_number: str,
) -> SearchHit | None:
    row = conn.execute(
        """
        SELECT *
        FROM cards
        WHERE language = ? AND set_id = ? AND collector_number = ?
        ORDER BY canonical_base_id
        LIMIT 1
        """,
        (language, set_id, collector_number),
    ).fetchone()
    if row is None:
        norm_collector = normalize_collector_number(collector_number)
        row = conn.execute(
            """
            SELECT *
            FROM cards
            WHERE language = ? AND set_id = ? AND normalized_collector_number = ?
            ORDER BY canonical_base_id
            LIMIT 1
            """,
            (language, set_id, norm_collector),
        ).fetchone()
    if row is None:
        return None
    return _card_row_to_hit(row, match_class=MATCH_CLASS_EXACT_SET_COLLECTOR, score=100.0)


def hit_to_dict(hit: SearchHit) -> dict[str, Any]:
    return {
        "canonicalBaseId": hit.canonical_base_id,
        "language": hit.language,
        "setId": hit.set_id,
        "collectorNumber": hit.collector_number,
        "name": hit.name,
        "localizedName": hit.localized_name,
        "setName": hit.set_name,
        "thumbnailUrl": hit.thumbnail_url,
        "largeImageUrl": hit.large_image_url,
        "imageSource": hit.image_source,
        "imageCached": hit.image_cached,
        "matchClass": hit.match_class,
        "score": hit.score,
    }
