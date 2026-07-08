from __future__ import annotations

import json
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .constants import DATABASE_BASENAME, SEARCH_OUTPUT_DIR
from .search import SearchRequest, connect_readonly, search_cards


BENCHMARK_QUERIES: list[dict[str, Any]] = [
    {"label": "en_exact_name_charizard", "query": "charizard", "language": "en"},
    {"label": "en_partial_pika", "query": "pika", "language": "en"},
    {"label": "en_collector_base1_4", "query": "4", "language": "en", "set_id": "base1"},
    {"label": "en_set_name_base", "query": "base charizard", "language": "en"},
    {"label": "jp_printed_kunugidama", "query": "クヌギダマ", "language": "jp"},
    {"label": "jp_partial_pika", "query": "ピカ", "language": "jp"},
    {"label": "duplicate_name_charizard", "query": "charizard", "language": "en", "set_id": "xy12"},
    {"label": "alpha_collector", "query": "H3", "language": "en", "set_id": "ecard2"},
    {"label": "slash_collector", "query": "001/081", "language": "jp"},
    {"label": "zero_results", "query": "zzznomatch999", "language": "en"},
    {"label": "broad_pokemon", "query": "pokemon", "language": "en"},
    {"label": "en_set_filter_sv10", "query": "ethan", "language": "en", "set_id": "sv10"},
]


@dataclass(frozen=True)
class BenchmarkResult:
    build_time_seconds: float | None
    database_bytes: int
    manifest_bytes: int
    queries: list[dict[str, Any]]
    cold_median_ms: float
    cold_p95_ms: float
    cold_p99_ms: float
    warm_median_ms: float
    warm_p95_ms: float
    warm_p99_ms: float


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return ordered[index]


def benchmark_search_index(
    *,
    output_dir: Path = SEARCH_OUTPUT_DIR,
    build_time_seconds: float | None = None,
) -> BenchmarkResult:
    db_path = output_dir / DATABASE_BASENAME
    manifest_path = output_dir / f"catalog_search_v1.manifest.json"
    conn = connect_readonly(str(db_path))

    query_results: list[dict[str, Any]] = []
    cold_latencies: list[float] = []
    warm_latencies: list[float] = []

    for spec in BENCHMARK_QUERIES:
        request = SearchRequest(
            query_text=spec["query"],
            language=spec.get("language"),
            set_id=spec.get("set_id"),
            limit=int(spec.get("limit") or 25),
        )
        cold_conn = connect_readonly(str(db_path))
        start = time.perf_counter()
        cold_hits = search_cards(cold_conn, request)
        cold_ms = (time.perf_counter() - start) * 1000.0
        cold_conn.close()
        cold_latencies.append(cold_ms)

        warm_runs: list[float] = []
        warm_hits = []
        for _ in range(5):
            start = time.perf_counter()
            warm_hits = search_cards(conn, request)
            warm_runs.append((time.perf_counter() - start) * 1000.0)
        warm_latencies.extend(warm_runs)
        query_results.append(
            {
                "label": spec["label"],
                "query": spec["query"],
                "language": spec.get("language"),
                "setId": spec.get("set_id"),
                "coldLatencyMs": round(cold_ms, 3),
                "warmMedianLatencyMs": round(statistics.median(warm_runs), 3),
                "resultCount": len(warm_hits),
            }
        )

    conn.close()
    return BenchmarkResult(
        build_time_seconds=build_time_seconds,
        database_bytes=db_path.stat().st_size if db_path.exists() else 0,
        manifest_bytes=manifest_path.stat().st_size if manifest_path.exists() else 0,
        queries=query_results,
        cold_median_ms=statistics.median(cold_latencies) if cold_latencies else 0.0,
        cold_p95_ms=_percentile(cold_latencies, 95),
        cold_p99_ms=_percentile(cold_latencies, 99),
        warm_median_ms=statistics.median(warm_latencies) if warm_latencies else 0.0,
        warm_p95_ms=_percentile(warm_latencies, 95),
        warm_p99_ms=_percentile(warm_latencies, 99),
    )


def write_benchmark_report(result: BenchmarkResult, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
