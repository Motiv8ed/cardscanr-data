from __future__ import annotations

import email.utils
import hashlib
import json
import os
import re
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from .contracts import (
    LANGUAGE_BY_TAG,
    LANGUAGE_DEFINITIONS,
    AmbiguousLanguageError,
    build_printing_record,
    build_set_record,
    canonicalize_language,
    normalize_collector_number,
    replace_file_with_retry,
    sha256_json,
    write_json_atomic,
    write_jsonl_atomic,
)


ROOT = Path(__file__).resolve().parent.parent
GLOBAL_DATA_DIR = ROOT / "data" / "global"
CATALOGUE_DIR = GLOBAL_DATA_DIR / "catalogue"
SOURCE_CACHE_DIR = GLOBAL_DATA_DIR / "source_cache" / "tcgdex" / "v2"
REPORT_DIR = ROOT / "reports" / "global_rollout"
CHECKPOINT_PATH = REPORT_DIR / "checkpoints" / "tcgdex_metadata.json"
EXECUTION_PLAN_PATH = REPORT_DIR / "metadata_execution_plan.json"
SOURCE_MANIFEST_PATH = SOURCE_CACHE_DIR / "manifest.json"
TCGDEX_BASE_URL = "https://api.tcgdex.net/v2"
USER_AGENT = "CardScanR-GlobalRollout/1.0 (+metadata-cache; contact via project owner)"
DEFAULT_REQUEST_INTERVAL_SECONDS = 0.20
DEFAULT_MAX_RETRIES = 4


class PermanentProviderError(RuntimeError):
    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class RequestBudgetExhausted(RuntimeError):
    pass


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_retry_after(value: str | None, *, now: float | None = None) -> float | None:
    if not value:
        return None
    text = value.strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        target = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    current = datetime.fromtimestamp(now if now is not None else time.time(), tz=timezone.utc)
    return max(0.0, (target - current).total_seconds())


@dataclass
class ProviderRateLimiter:
    min_interval_seconds: float
    _last_request_monotonic: float | None = None
    _blocked_until_monotonic: float = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        required = self._blocked_until_monotonic
        if self._last_request_monotonic is not None:
            required = max(required, self._last_request_monotonic + self.min_interval_seconds)
        delay = required - now
        if delay > 0:
            time.sleep(delay)
        self._last_request_monotonic = time.monotonic()

    def globally_pause(self, seconds: float) -> None:
        self._blocked_until_monotonic = max(
            self._blocked_until_monotonic,
            time.monotonic() + max(0.0, seconds),
        )


@dataclass
class FetchStats:
    network_requests: int = 0
    cache_hits: int = 0
    downloaded_bytes: int = 0
    retries: int = 0
    permanent_404s: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)


class TcgdexClient:
    def __init__(
        self,
        *,
        request_interval_seconds: float = DEFAULT_REQUEST_INTERVAL_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_network_requests: int | None = None,
        timeout_seconds: int = 60,
    ) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            }
        )
        self.rate_limiter = ProviderRateLimiter(max(0.0, request_interval_seconds))
        self.max_retries = max(0, max_retries)
        self.max_network_requests = max_network_requests
        self.timeout_seconds = timeout_seconds
        self.stats = FetchStats()

    def get_json(self, url: str) -> Any:
        if (
            self.max_network_requests is not None
            and self.stats.network_requests >= self.max_network_requests
        ):
            raise RequestBudgetExhausted(
                f"TCGdex request budget exhausted at {self.stats.network_requests} requests"
            )

        attempt = 0
        while True:
            self.rate_limiter.wait()
            self.stats.network_requests += 1
            try:
                response = self.session.get(url, timeout=self.timeout_seconds)
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt >= self.max_retries:
                    raise
                wait_seconds = min(60.0, 2.0**attempt)
                self.stats.retries += 1
                self.rate_limiter.globally_pause(wait_seconds)
                attempt += 1
                continue

            if response.status_code == 404:
                self.stats.permanent_404s += 1
                raise PermanentProviderError(f"permanent HTTP 404 for {url}", status_code=404)

            if response.status_code == 429:
                if attempt >= self.max_retries:
                    response.raise_for_status()
                wait_seconds = parse_retry_after(response.headers.get("Retry-After"))
                if wait_seconds is None:
                    wait_seconds = 60.0
                self.stats.retries += 1
                self.rate_limiter.globally_pause(wait_seconds)
                attempt += 1
                continue

            if response.status_code in {408, 425, 500, 502, 503, 504}:
                if attempt >= self.max_retries:
                    response.raise_for_status()
                wait_seconds = parse_retry_after(response.headers.get("Retry-After"))
                if wait_seconds is None:
                    wait_seconds = min(60.0, 2.0**attempt)
                self.stats.retries += 1
                self.rate_limiter.globally_pause(wait_seconds)
                attempt += 1
                continue

            response.raise_for_status()
            self.stats.downloaded_bytes += len(response.content)
            try:
                return response.json()
            except ValueError as exc:
                raise RuntimeError(f"TCGdex returned invalid JSON for {url}") from exc


def _safe_cache_stem(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._") or "set"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"{clean[:80]}.{digest}"


def _load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cached_payload(entry: dict[str, Any] | None) -> Any | None:
    if not isinstance(entry, dict):
        return None
    relative = entry.get("file")
    expected = entry.get("sha256")
    if not isinstance(relative, str) or not isinstance(expected, str):
        return None
    path = ROOT / relative
    if not path.exists() or _sha256_file(path) != expected:
        return None
    return _load_json(path)


def _store_versioned_payload(
    *,
    directory: Path,
    stem: str,
    payload: Any,
) -> dict[str, Any]:
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    path = directory / f"{stem}.{digest[:16]}.json"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(encoded)
        replace_file_with_retry(temporary, path)
    return {
        "file": path.relative_to(ROOT).as_posix(),
        "sha256": digest,
        "byteSize": len(encoded),
        "fetchedAtUtc": utc_now_iso(),
    }


def _initial_source_manifest() -> dict[str, Any]:
    return {
        "schemaVersion": "1.0.0",
        "provider": "tcgdex",
        "apiVersion": "v2",
        "documentationUrl": "https://tcgdex.dev/",
        "languages": {},
        "updatedAtUtc": None,
    }


def load_source_manifest() -> dict[str, Any]:
    payload = _load_json(SOURCE_MANIFEST_PATH)
    return payload if isinstance(payload, dict) else _initial_source_manifest()


def _language_order() -> dict[str, int]:
    return {
        code: index
        for index, definition in enumerate(LANGUAGE_DEFINITIONS)
        for code in definition.tcgdex_codes
    }


def tcgdex_source_languages() -> list[str]:
    return [
        code
        for definition in LANGUAGE_DEFINITIONS
        for code in definition.tcgdex_codes
    ]


def tcgdex_set_url(source_language: str, provider_set_id: str) -> str:
    return (
        f"{TCGDEX_BASE_URL}/{quote(source_language, safe='')}/sets/"
        f"{quote(provider_set_id, safe='')}"
    )


def _checkpoint_payload(
    *,
    source_manifest: dict[str, Any],
    client: TcgdexClient,
    state: str,
    completed_sets: int,
    expected_sets: int,
    current_language: str | None,
    current_set: str | None,
) -> dict[str, Any]:
    return {
        "schemaVersion": "1.0.0",
        "provider": "tcgdex",
        "state": state,
        "updatedAtUtc": utc_now_iso(),
        "completedSets": completed_sets,
        "expectedSets": expected_sets,
        "remainingSets": max(0, expected_sets - completed_sets),
        "currentLanguage": current_language,
        "currentSet": current_set,
        "networkRequests": client.stats.network_requests,
        "cacheHits": client.stats.cache_hits,
        "downloadedBytes": client.stats.downloaded_bytes,
        "retries": client.stats.retries,
        "permanent404s": client.stats.permanent_404s,
        "failures": client.stats.failures[-100:],
        "sourceManifest": SOURCE_MANIFEST_PATH.relative_to(ROOT).as_posix(),
        "resumeCommand": "python tools/global_rollout.py ingest-metadata --provider tcgdex --resume",
    }


def fetch_tcgdex_metadata(
    *,
    refresh: bool = False,
    max_network_requests: int | None = None,
    request_interval_seconds: float = DEFAULT_REQUEST_INTERVAL_SECONDS,
) -> tuple[dict[str, Any], FetchStats, dict[str, Any]]:
    client = TcgdexClient(
        request_interval_seconds=request_interval_seconds,
        max_network_requests=max_network_requests,
    )
    manifest = load_source_manifest()
    languages_manifest = manifest.setdefault("languages", {})
    source_languages = tcgdex_source_languages()
    indexes: dict[str, list[dict[str, Any]]] = {}
    duplicate_set_index_ids: list[dict[str, Any]] = []

    for source_language in source_languages:
        language_entry = languages_manifest.setdefault(source_language, {"sets": {}, "failures": {}})
        index_entry = language_entry.get("index")
        payload = None if refresh else _cached_payload(index_entry)
        if payload is not None:
            client.stats.cache_hits += 1
        else:
            payload = client.get_json(f"{TCGDEX_BASE_URL}/{source_language}/sets")
            if not isinstance(payload, list):
                raise RuntimeError(f"TCGdex sets response for {source_language} was not a list")
            stored = _store_versioned_payload(
                directory=SOURCE_CACHE_DIR / source_language,
                stem="sets-index",
                payload=payload,
            )
            stored["setCount"] = len(payload)
            stored["declaredCardCount"] = sum(
                int(((item.get("cardCount") or {}).get("total") or 0))
                for item in payload
                if isinstance(item, dict)
            )
            language_entry["index"] = stored
        deduplicated_index: list[dict[str, Any]] = []
        seen_set_ids: Counter[str] = Counter()
        for item in payload:
            if not isinstance(item, dict):
                continue
            provider_set_id = str(item.get("id") or "").strip()
            if provider_set_id:
                seen_set_ids[provider_set_id] += 1
                if seen_set_ids[provider_set_id] > 1:
                    continue
            deduplicated_index.append(item)
        duplicate_set_index_ids.extend(
            {
                "sourceLanguage": source_language,
                "providerSetId": provider_set_id,
                "candidateCount": count,
            }
            for provider_set_id, count in sorted(seen_set_ids.items())
            if count > 1
        )
        indexes[source_language] = deduplicated_index
        manifest["updatedAtUtc"] = utc_now_iso()
        write_json_atomic(SOURCE_MANIFEST_PATH, manifest)

    expected_sets = sum(len(items) for items in indexes.values())
    missing_sets = 0
    for source_language, items in indexes.items():
        set_entries = languages_manifest[source_language].setdefault("sets", {})
        for item in items:
            provider_set_id = str(item.get("id") or "").strip()
            if not provider_set_id:
                continue
            if refresh or _cached_payload(set_entries.get(provider_set_id)) is None:
                missing_sets += 1

    execution_plan = {
        "classification": "PASS",
        "generatedAtUtc": utc_now_iso(),
        "provider": "tcgdex",
        "languages": source_languages,
        "setDetailRequestsTotal": expected_sets,
        "sourceSetIndexRows": expected_sets
        + sum(item["candidateCount"] - 1 for item in duplicate_set_index_ids),
        "duplicateProviderSetIndexIds": duplicate_set_index_ids,
        "setDetailRequestsMissingFromCache": missing_sets,
        "expectedNetworkRequests": missing_sets,
        "requestIntervalSeconds": request_interval_seconds,
        "minimumExpectedDurationSeconds": round(missing_sets * request_interval_seconds, 1),
        "expectedApiCostUsd": 0,
        "expectedR2Writes": 0,
        "expectedR2StorageBytes": 0,
        "providerQuota": "No published hard limit; serial conservative pacing and persistent cache enabled.",
        "paidUsageActivated": False,
    }
    write_json_atomic(EXECUTION_PLAN_PATH, execution_plan)

    completed_sets = 0
    for source_language in sorted(source_languages, key=_language_order().get):
        language_entry = languages_manifest[source_language]
        set_entries = language_entry.setdefault("sets", {})
        failures = language_entry.setdefault("failures", {})
        set_items = sorted(
            indexes[source_language],
            key=lambda item: str(item.get("id") or "").casefold(),
        )
        for item in set_items:
            provider_set_id = str(item.get("id") or "").strip()
            if not provider_set_id:
                continue
            cached = None if refresh else _cached_payload(set_entries.get(provider_set_id))
            if cached is not None:
                client.stats.cache_hits += 1
                completed_sets += 1
                continue
            try:
                detail = client.get_json(tcgdex_set_url(source_language, provider_set_id))
                if not isinstance(detail, dict):
                    raise RuntimeError("set detail was not an object")
                stored = _store_versioned_payload(
                    directory=SOURCE_CACHE_DIR / source_language / "sets",
                    stem=_safe_cache_stem(provider_set_id),
                    payload=detail,
                )
                cards = detail.get("cards")
                stored["cardCount"] = len(cards) if isinstance(cards, list) else 0
                set_entries[provider_set_id] = stored
                failures.pop(provider_set_id, None)
                completed_sets += 1
            except RequestBudgetExhausted:
                manifest["updatedAtUtc"] = utc_now_iso()
                write_json_atomic(SOURCE_MANIFEST_PATH, manifest)
                write_json_atomic(
                    CHECKPOINT_PATH,
                    _checkpoint_payload(
                        source_manifest=manifest,
                        client=client,
                        state="request_budget_exhausted",
                        completed_sets=completed_sets,
                        expected_sets=expected_sets,
                        current_language=source_language,
                        current_set=provider_set_id,
                    ),
                )
                raise
            except PermanentProviderError as exc:
                failure = {
                    "status": "source_http_404",
                    "statusCode": exc.status_code,
                    "recordedAtUtc": utc_now_iso(),
                }
                failures[provider_set_id] = failure
                client.stats.failures.append(
                    {
                        "language": source_language,
                        "setId": provider_set_id,
                        **failure,
                    }
                )
            except (requests.RequestException, RuntimeError) as exc:
                failure = {
                    "status": "provider_unavailable",
                    "errorType": type(exc).__name__,
                    "recordedAtUtc": utc_now_iso(),
                }
                failures[provider_set_id] = failure
                client.stats.failures.append(
                    {
                        "language": source_language,
                        "setId": provider_set_id,
                        **failure,
                    }
                )

            manifest["updatedAtUtc"] = utc_now_iso()
            write_json_atomic(SOURCE_MANIFEST_PATH, manifest)
            if completed_sets % 25 == 0 or client.stats.failures:
                write_json_atomic(
                    CHECKPOINT_PATH,
                    _checkpoint_payload(
                        source_manifest=manifest,
                        client=client,
                        state="in_progress",
                        completed_sets=completed_sets,
                        expected_sets=expected_sets,
                        current_language=source_language,
                        current_set=provider_set_id,
                    ),
                )

    write_json_atomic(
        CHECKPOINT_PATH,
        _checkpoint_payload(
            source_manifest=manifest,
            client=client,
            state="complete",
            completed_sets=completed_sets,
            expected_sets=expected_sets,
            current_language=None,
            current_set=None,
        ),
    )
    return manifest, client.stats, execution_plan


def iter_cached_set_details(
    manifest: dict[str, Any] | None = None,
) -> Iterator[tuple[str, str, dict[str, Any]]]:
    manifest = manifest or load_source_manifest()
    language_entries = manifest.get("languages")
    if not isinstance(language_entries, dict):
        return
    order = _language_order()
    for source_language in sorted(language_entries, key=lambda item: order.get(item, 999)):
        language_entry = language_entries.get(source_language)
        if not isinstance(language_entry, dict):
            continue
        set_entries = language_entry.get("sets")
        if not isinstance(set_entries, dict):
            continue
        for provider_set_id in sorted(set_entries, key=str.casefold):
            payload = _cached_payload(set_entries.get(provider_set_id))
            if isinstance(payload, dict):
                yield source_language, provider_set_id, payload


def _record_sort_key(card: dict[str, Any]) -> tuple[str, str]:
    return (
        normalize_collector_number(str(card.get("localId") or "")),
        str(card.get("id") or "").casefold(),
    )


def _printing_from_card(
    source_language: str,
    provider_set_id: str,
    set_payload: dict[str, Any],
    card: dict[str, Any],
) -> dict[str, Any]:
    counts = set_payload.get("cardCount")
    counts = counts if isinstance(counts, dict) else {}
    serie = set_payload.get("serie")
    serie = serie if isinstance(serie, dict) else {}
    return build_printing_record(
        source_language=source_language,
        provider="tcgdex",
        provider_set_id=provider_set_id,
        provider_card_id=str(card.get("id") or ""),
        native_set_name=str(set_payload.get("name") or provider_set_id),
        native_card_name=str(card.get("name") or ""),
        collector_number=str(card.get("localId") or ""),
        official_set_total=_optional_int(counts.get("official")),
        release_date=_optional_str(set_payload.get("releaseDate")),
        image_url=_optional_str(card.get("image")),
        serie_id=_optional_str(serie.get("id")),
        serie_name=_optional_str(serie.get("name")),
    )


def _set_from_payload(
    source_language: str,
    provider_set_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    counts = payload.get("cardCount")
    counts = counts if isinstance(counts, dict) else {}
    serie = payload.get("serie")
    serie = serie if isinstance(serie, dict) else {}
    return build_set_record(
        source_language=source_language,
        provider="tcgdex",
        provider_set_id=provider_set_id,
        native_set_name=str(payload.get("name") or provider_set_id),
        official_total=_optional_int(counts.get("official")),
        total=_optional_int(counts.get("total")),
        release_date=_optional_str(payload.get("releaseDate")),
        serie_id=_optional_str(serie.get("id")),
        serie_name=_optional_str(serie.get("name")),
    )


def _iter_provider_cards(
    manifest: dict[str, Any],
) -> Iterator[tuple[str, str, dict[str, Any], dict[str, Any]]]:
    for source_language, provider_set_id, set_payload in iter_cached_set_details(manifest):
        cards = set_payload.get("cards")
        if not isinstance(cards, list):
            continue
        for card in sorted(
            (item for item in cards if isinstance(item, dict)),
            key=_record_sort_key,
        ):
            yield source_language, provider_set_id, set_payload, card


def _current_catalogue_card_paths() -> Iterator[tuple[str, Path]]:
    root = ROOT / "public" / "v1" / "catalog" / "pokemon"
    if not root.exists():
        return
    for legacy_language_dir in sorted(
        (item for item in root.iterdir() if item.is_dir()),
        key=lambda item: item.name.casefold(),
    ):
        cards_dir = legacy_language_dir / "cards"
        if not cards_dir.is_dir():
            continue
        for path in sorted(cards_dir.glob("*.json"), key=lambda item: item.name.casefold()):
            yield legacy_language_dir.name, path


def _provider_ids_from_existing(card: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    provider_ids = card.get("providerIds")
    if isinstance(provider_ids, dict):
        aliases = {
            "tcgdex": "tcgdex",
            "pokemonTcgApi": "pokemon_tcg_api",
            "pokemon_tcg_api": "pokemon_tcg_api",
            "pokewallet": "pokewallet",
        }
        for key, provider in aliases.items():
            value = provider_ids.get(key)
            if value:
                result[provider] = str(value)
    external_ids = card.get("externalIds")
    if isinstance(external_ids, dict):
        for key, provider in (
            ("tcgdexCardId", "tcgdex"),
            ("pokemonTcgApiId", "pokemon_tcg_api"),
            ("pokewallet", "pokewallet"),
        ):
            value = external_ids.get(key)
            if value and provider not in result:
                result[provider] = str(value)
    promo = card.get("promotionMetadata")
    if isinstance(promo, dict) and promo.get("providerCardId") and promo.get("provider"):
        result.setdefault(str(promo["provider"]), str(promo["providerCardId"]))
    return result


def _canonical_existing_language(
    legacy_language: str,
    card: dict[str, Any],
) -> str:
    if legacy_language != "zh":
        return canonicalize_language(legacy_language)
    promo = card.get("promotionMetadata")
    provider_language = promo.get("providerLanguage") if isinstance(promo, dict) else None
    if provider_language:
        return canonicalize_language(str(provider_language))
    return canonicalize_language(legacy_language)


def _existing_crosswalk_rows(
    *,
    tcgdex_by_provider_id: dict[tuple[str, str], str],
    tcgdex_by_exact_key: dict[tuple[str, str, str], str | None],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    crosswalk: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    seen_rows: set[tuple[str, str, str]] = set()
    for legacy_language, path in _current_catalogue_card_paths():
        payload = _load_json(path)
        if not isinstance(payload, dict):
            continue
        for card in payload.get("cards") or []:
            if not isinstance(card, dict):
                continue
            try:
                language = _canonical_existing_language(legacy_language, card)
            except (AmbiguousLanguageError, ValueError) as exc:
                unresolved.append(
                    {
                        "source": "existing_app_catalogue",
                        "sourceFile": path.relative_to(ROOT).as_posix(),
                        "legacyCanonicalBaseId": card.get("canonicalBaseId"),
                        "reason": "language_or_region_ambiguous",
                        "detail": str(exc),
                    }
                )
                continue

            provider_ids = _provider_ids_from_existing(card)
            target_id = None
            evidence = None
            tcgdex_id = provider_ids.get("tcgdex")
            pokemon_id = provider_ids.get("pokemon_tcg_api")
            if tcgdex_id and (language, tcgdex_id) in tcgdex_by_provider_id:
                target_id = tcgdex_by_provider_id[(language, tcgdex_id)]
                evidence = "explicit_tcgdex_provider_card_id"
            elif pokemon_id and (language, pokemon_id) in tcgdex_by_provider_id:
                target_id = tcgdex_by_provider_id[(language, pokemon_id)]
                evidence = "identical_stable_provider_card_id"
            elif str(card.get("imageSource") or "") == "tcgdex":
                exact_key = (
                    language,
                    str(card.get("setId") or "").casefold(),
                    normalize_collector_number(str(card.get("collectorNumber") or "")),
                )
                target_id = tcgdex_by_exact_key.get(exact_key)
                evidence = "tcgdex_source_set_and_collector_unique" if target_id else None

            if target_id:
                for provider, provider_id in sorted(provider_ids.items()):
                    key = (target_id, provider, provider_id)
                    if key in seen_rows:
                        continue
                    seen_rows.add(key)
                    crosswalk.append(
                        {
                            "canonicalPrintingId": target_id,
                            "language": language,
                            "region": LANGUAGE_BY_TAG[language].default_region,
                            "provider": provider,
                            "providerCardId": provider_id,
                            "evidence": evidence,
                            "source": "existing_app_catalogue",
                        }
                    )
                continue

            unresolved.append(
                {
                    "source": "existing_app_catalogue",
                    "sourceFile": path.relative_to(ROOT).as_posix(),
                    "legacyCanonicalBaseId": card.get("canonicalBaseId"),
                    "language": language,
                    "setId": card.get("setId"),
                    "collectorNumber": card.get("collectorNumber"),
                    "providerIds": provider_ids,
                    "reason": "provider_set_crosswalk_or_variant_evidence_missing",
                }
            )
    return crosswalk, unresolved


def normalize_global_catalogue(
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest = manifest or load_source_manifest()
    identity_counts: Counter[str] = Counter()
    provider_id_to_printing: dict[tuple[str, str], str] = {}
    exact_key_to_printing: dict[tuple[str, str, str], str | None] = {}
    structural_unresolved: list[dict[str, Any]] = []

    for source_language, provider_set_id, set_payload in iter_cached_set_details(manifest):
        cards = set_payload.get("cards")
        counts = set_payload.get("cardCount")
        counts = counts if isinstance(counts, dict) else {}
        declared_total = _optional_int(counts.get("total")) or 0
        if declared_total > 0 and (not isinstance(cards, list) or not cards):
            structural_unresolved.append(
                {
                    "source": "tcgdex",
                    "sourceLanguage": source_language,
                    "providerSetId": provider_set_id,
                    "expectedCardCount": declared_total,
                    "reason": "provider_set_card_list_empty",
                }
            )
    language_entries = manifest.get("languages")
    if isinstance(language_entries, dict):
        for source_language, language_entry in sorted(language_entries.items()):
            index_payload = (
                _cached_payload(language_entry.get("index"))
                if isinstance(language_entry, dict)
                else None
            )
            if isinstance(index_payload, list):
                index_counts = Counter(
                    str(item.get("id") or "").strip()
                    for item in index_payload
                    if isinstance(item, dict) and item.get("id")
                )
                for provider_set_id, candidate_count in sorted(index_counts.items()):
                    if candidate_count > 1:
                        structural_unresolved.append(
                            {
                                "source": "tcgdex",
                                "sourceLanguage": source_language,
                                "providerSetId": provider_set_id,
                                "candidateCount": candidate_count,
                                "reason": "duplicate_provider_set_index_id",
                            }
                        )
            failures = language_entry.get("failures") if isinstance(language_entry, dict) else None
            if not isinstance(failures, dict):
                continue
            for provider_set_id, failure in sorted(failures.items()):
                structural_unresolved.append(
                    {
                        "source": "tcgdex",
                        "sourceLanguage": source_language,
                        "providerSetId": provider_set_id,
                        "reason": "provider_set_detail_unavailable",
                        "providerState": (
                            failure.get("status")
                            if isinstance(failure, dict)
                            else "unknown"
                        ),
                    }
                )

    for source_language, provider_set_id, set_payload, card in _iter_provider_cards(manifest):
        provider_card_id = str(card.get("id") or "").strip()
        collector = str(card.get("localId") or "").strip()
        native_name = str(card.get("name") or "").strip()
        if not provider_card_id or not collector or not native_name:
            structural_unresolved.append(
                {
                    "source": "tcgdex",
                    "sourceLanguage": source_language,
                    "providerSetId": provider_set_id,
                    "providerCardId": provider_card_id or None,
                    "reason": "missing_exact_identity_field",
                }
            )
            continue
        record = _printing_from_card(
            source_language,
            provider_set_id,
            set_payload,
            card,
        )
        printing_id = record["canonicalPrintingId"]
        identity_counts[printing_id] += 1
        provider_id_to_printing[(record["language"], provider_card_id)] = printing_id
        exact_key = (
            record["language"],
            provider_set_id.casefold(),
            record["normalizedCollectorNumber"],
        )
        previous = exact_key_to_printing.get(exact_key)
        if previous is None and exact_key in exact_key_to_printing:
            pass
        elif previous is not None and previous != printing_id:
            exact_key_to_printing[exact_key] = None
        else:
            exact_key_to_printing[exact_key] = printing_id

    duplicate_ids = {key for key, count in identity_counts.items() if count > 1}
    conflicts: list[dict[str, Any]] = []
    for printing_id in sorted(duplicate_ids):
        conflicts.append(
            {
                "canonicalPrintingId": printing_id,
                "reason": "duplicate_exact_identity_without_distinct_variant_evidence",
                "candidateCount": identity_counts[printing_id],
                "action": "quarantined",
            }
        )

    set_rows = [
        _set_from_payload(source_language, provider_set_id, payload)
        for source_language, provider_set_id, payload in iter_cached_set_details(manifest)
    ]
    set_rows.sort(
        key=lambda row: (
            next(
                (
                    index
                    for index, definition in enumerate(LANGUAGE_DEFINITIONS)
                    if definition.language == row["language"]
                ),
                999,
            ),
            row["canonicalSetId"].casefold(),
        )
    )

    def card_rows() -> Iterator[dict[str, Any]]:
        for source_language, provider_set_id, set_payload, card in _iter_provider_cards(manifest):
            if not card.get("id") or not card.get("localId") or not card.get("name"):
                continue
            record = _printing_from_card(
                source_language,
                provider_set_id,
                set_payload,
                card,
            )
            if record["canonicalPrintingId"] not in duplicate_ids:
                yield record

    cards_count, cards_sha = write_jsonl_atomic(CATALOGUE_DIR / "cards.jsonl", card_rows())
    sets_count, sets_sha = write_jsonl_atomic(CATALOGUE_DIR / "sets.jsonl", set_rows)

    base_crosswalk = [
        {
            "canonicalPrintingId": printing_id,
            "language": language,
            "region": LANGUAGE_BY_TAG[language].default_region,
            "provider": "tcgdex",
            "providerCardId": provider_id,
            "evidence": "direct_source_record",
            "source": "tcgdex",
        }
        for (language, provider_id), printing_id in sorted(provider_id_to_printing.items())
        if printing_id not in duplicate_ids
    ]
    existing_crosswalk, existing_unresolved = _existing_crosswalk_rows(
        tcgdex_by_provider_id=provider_id_to_printing,
        tcgdex_by_exact_key=exact_key_to_printing,
    )
    all_crosswalk_by_key: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in base_crosswalk + existing_crosswalk:
        key = (
            row["canonicalPrintingId"],
            row["language"],
            row["provider"],
            row["providerCardId"],
        )
        previous = all_crosswalk_by_key.get(key)
        if previous is None or row["source"] == "tcgdex":
            all_crosswalk_by_key[key] = row
    all_crosswalk = list(all_crosswalk_by_key.values())
    all_crosswalk.sort(
        key=lambda row: (
            row["canonicalPrintingId"],
            row["language"],
            row["provider"],
            row["providerCardId"],
        )
    )
    crosswalk_count, crosswalk_sha = write_jsonl_atomic(
        CATALOGUE_DIR / "provider_crosswalk.jsonl",
        all_crosswalk,
    )

    for conflict in conflicts:
        structural_unresolved.append(
            {
                **conflict,
                "source": "tcgdex",
                "reason": conflict["reason"],
            }
        )
    all_unresolved = structural_unresolved + existing_unresolved
    all_unresolved.sort(
        key=lambda row: (
            str(row.get("source") or ""),
            str(row.get("language") or row.get("sourceLanguage") or ""),
            str(row.get("legacyCanonicalBaseId") or row.get("canonicalPrintingId") or ""),
        )
    )
    unresolved_count, unresolved_sha = write_jsonl_atomic(
        CATALOGUE_DIR / "unresolved.jsonl",
        all_unresolved,
    )
    conflicts_count, conflicts_sha = write_jsonl_atomic(
        CATALOGUE_DIR / "conflicts.jsonl",
        conflicts,
    )

    output_manifest = {
        "schemaVersion": "1.0.0",
        "generatedAtUtc": utc_now_iso(),
        "classification": "PARTIAL" if all_unresolved or conflicts else "PASS",
        "provider": "tcgdex",
        "canonicalPrintingGroups": cards_count,
        "canonicalSets": sets_count,
        "providerCrosswalkRows": crosswalk_count,
        "unresolvedRows": unresolved_count,
        "conflictRows": conflicts_count,
        "identityCaveat": (
            "TCGdex set responses expose card-level printing groups but not complete physical finish "
            "evidence; cardVariant remains unspecified and every record is provisional."
        ),
        "outputs": {
            "cards.jsonl": {"sha256": cards_sha, "rows": cards_count},
            "sets.jsonl": {"sha256": sets_sha, "rows": sets_count},
            "provider_crosswalk.jsonl": {
                "sha256": crosswalk_sha,
                "rows": crosswalk_count,
            },
            "unresolved.jsonl": {"sha256": unresolved_sha, "rows": unresolved_count},
            "conflicts.jsonl": {"sha256": conflicts_sha, "rows": conflicts_count},
        },
        "productionPublished": False,
        "flutterModified": False,
    }
    write_json_atomic(CATALOGUE_DIR / "manifest.json", output_manifest)
    return output_manifest


def ingest_tcgdex(
    *,
    refresh: bool = False,
    max_network_requests: int | None = None,
    request_interval_seconds: float = DEFAULT_REQUEST_INTERVAL_SECONDS,
) -> dict[str, Any]:
    manifest, stats, execution_plan = fetch_tcgdex_metadata(
        refresh=refresh,
        max_network_requests=max_network_requests,
        request_interval_seconds=request_interval_seconds,
    )
    catalogue = normalize_global_catalogue(manifest)
    return {
        "classification": catalogue["classification"],
        "provider": "tcgdex",
        "fetch": {
            "networkRequests": stats.network_requests,
            "cacheHits": stats.cache_hits,
            "downloadedBytes": stats.downloaded_bytes,
            "retries": stats.retries,
            "permanent404s": stats.permanent_404s,
            "failures": len(stats.failures),
        },
        "executionPlan": execution_plan,
        "catalogue": catalogue,
    }


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None

