from __future__ import annotations

import hashlib
import json
import os
import random
import re
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from .catalogue import DEFAULT_CATALOGUE_ROOT, iter_catalogue_identities
from .config import ImagePipelineConfig
from .matching import resolve_provider_with_trace
from .paths import public_storage_url
from .processing import pokewallet_request_headers
from .sample_manifest import identities_for_manifest
from .stage2_runner import Stage2Runner, build_contact_sheet, write_json_report
from .thumbnail_rollout import (
    RUNTIME_DIR,
    THUMB_ROLLOUT_SEED,
    is_pokewallet_auth_url,
    probe_url,
    tcgdex_needs_normalization,
    utc_now_iso,
)

APPROVED_MANIFEST_SHA256 = "2e84741b1921388baac2346387040f65a7dbea0acf11924076809d7ec2438f5e"
APPROVED_MANIFEST_PATH = RUNTIME_DIR / "thumbnail_rollout_en_500_manifest.json"
POKEMON_TCG_HOST = "images.pokemontcg.io"


def load_approved_manifest(path: Path | None = None) -> dict[str, Any]:
    target = path or APPROVED_MANIFEST_PATH
    payload = json.loads(target.read_text(encoding="utf-8"))
    embedded = str(payload.get("sha256") or "")
    if embedded != APPROVED_MANIFEST_SHA256:
        raise RuntimeError(
            f"Approved manifest SHA mismatch: expected {APPROVED_MANIFEST_SHA256}, got {embedded}"
        )
    return payload


def filter_manifest_entries(
    manifest: dict[str, Any],
    *,
    provider: str | None = None,
    canonical_ids: set[str] | None = None,
) -> dict[str, Any]:
    entries = list(manifest.get("entries") or [])
    if provider:
        entries = [entry for entry in entries if entry.get("provider") == provider]
    if canonical_ids is not None:
        entries = [entry for entry in entries if entry.get("canonicalBaseId") in canonical_ids]
    filtered = dict(manifest)
    filtered["entries"] = entries
    filtered["cardCount"] = len(entries)
    filtered["filteredFrom"] = {
        "originalCardCount": manifest.get("cardCount"),
        "provider": provider,
        "canonicalIdFilterCount": len(canonical_ids) if canonical_ids is not None else None,
    }
    return filtered


def pokewallet_credential_status() -> dict[str, Any]:
    present = bool((os.getenv("POKEWALLET_API_KEY") or os.getenv("CARDSCANR_POKEWALLET_API_KEY") or "").strip())
    return {
        "availability": "present" if present else "absent",
        "usedOnlyByIngestionProcess": True,
        "neverWrittenToCatalogueFlutterPublicMetadataLogsReportsOrPublicUrls": True,
        "note": "Credential presence only; value is never reported.",
    }


def reconcile_against_supabase(
    runner: Stage2Runner,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    already_imported: list[str] = []
    already_verified: list[str] = []
    pending: list[str] = []
    conflicts: list[dict[str, Any]] = []
    path_owner: dict[str, str] = {}
    duplicate_targets: list[dict[str, str]] = []

    for entry in manifest.get("entries") or []:
        canonical_id = entry["canonicalBaseId"]
        record = runner.db.get_record(canonical_id)
        if not record:
            pending.append(canonical_id)
            continue
        status = str(record.get("status") or "")
        if status == "verified":
            already_verified.append(canonical_id)
            already_imported.append(canonical_id)
        elif status == "completed":
            already_imported.append(canonical_id)
        else:
            pending.append(canonical_id)

        # Identity conflict checks against manifest entry.
        if record.get("language") and record.get("language") != entry.get("language"):
            conflicts.append({"canonicalBaseId": canonical_id, "reason": "language_mismatch"})
        if record.get("set_id") and record.get("set_id") != entry.get("setId"):
            conflicts.append({"canonicalBaseId": canonical_id, "reason": "set_id_mismatch"})
        if record.get("collector_number") and record.get("collector_number") != entry.get("collectorNumber"):
            conflicts.append({"canonicalBaseId": canonical_id, "reason": "collector_number_mismatch"})

        thumb_path = record.get("thumb_storage_path")
        if thumb_path:
            if thumb_path in path_owner and path_owner[thumb_path] != canonical_id:
                duplicate_targets.append(
                    {
                        "path": thumb_path,
                        "owner": path_owner[thumb_path],
                        "duplicate": canonical_id,
                    }
                )
            path_owner[thumb_path] = canonical_id

    return {
        "generatedAtUtc": utc_now_iso(),
        "manifestCardCount": len(manifest.get("entries") or []),
        "alreadyImportedCount": len(already_imported),
        "alreadyVerifiedCount": len(already_verified),
        "pendingCount": len(pending),
        "conflictCount": len(conflicts),
        "duplicateImmutableTargetCount": len(duplicate_targets),
        "alreadyImported": already_imported,
        "alreadyVerified": already_verified,
        "pending": pending,
        "conflicts": conflicts,
        "duplicateImmutableTargets": duplicate_targets,
    }


def gate_a_dry_run_checks(runner: Stage2Runner, manifest: dict[str, Any]) -> dict[str, Any]:
    payload = runner.dry_run_manifest(manifest)
    stop_reasons: list[str] = list(payload.get("stopReasons") or [])
    if int(payload.get("ambiguousCount") or 0) > 0:
        stop_reasons.append("ambiguous_count_gt_0")

    path_owner: dict[str, str] = {}
    for card in payload.get("cards") or []:
        canonical_id = card.get("canonical_base_id") or ""
        if not card.get("language") or not card.get("set_id") or not card.get("collector_number"):
            stop_reasons.append(f"missing_identity:{canonical_id}")
        if card.get("display_storage_path"):
            stop_reasons.append(f"display_webp_planned:{canonical_id}")
        source_url = card.get("source_url") or ""
        host = (urlparse(source_url).hostname or "").lower()
        if source_url and host != POKEMON_TCG_HOST:
            # Dry-run uses display URL; also accept empty for no_match already stopped.
            if card.get("database_status") == "dry_run_planned_upsert":
                stop_reasons.append(f"unexpected_source_host:{canonical_id}:{host}")
        thumb_path = card.get("thumb_storage_path")
        if thumb_path:
            if thumb_path in path_owner and path_owner[thumb_path] != canonical_id:
                stop_reasons.append(f"duplicate_target_path:{thumb_path}")
            path_owner[thumb_path] = canonical_id
        if card.get("database_status") == "dry_run_planned_upsert" and not str(thumb_path or "").endswith("/thumb.webp"):
            stop_reasons.append(f"non_thumb_path:{canonical_id}")

    # Deduplicate while preserving order.
    deduped: list[str] = []
    for reason in stop_reasons:
        if reason not in deduped:
            deduped.append(reason)
    payload["stopReasons"] = deduped
    payload["shouldStop"] = bool(deduped)
    payload["gate"] = "A"
    payload["importDisplay"] = False
    return payload


def enrich_skipped_public_urls(runner: Stage2Runner, cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for card in cards:
        row = dict(card)
        if row.get("database_status") == "skipped" and not row.get("thumb_public_url"):
            record = runner.db.get_record(row["canonical_base_id"])
            if record and record.get("thumb_storage_path"):
                row["thumb_storage_path"] = record["thumb_storage_path"]
                row["thumb_public_url"] = public_storage_url(
                    runner.config.supabase_url,
                    runner.config.bucket_name,
                    record["thumb_storage_path"],
                )
                row["content_hash_sha256"] = record.get("content_hash_sha256")
                row["provider"] = record.get("primary_provider") or row.get("provider")
        enriched.append(row)
    return enriched


def _ordered_pokewallet_candidates(
    pool: list[dict[str, Any]],
    *,
    seed: int,
    existing_ids: set[str],
) -> list[dict[str, Any]]:
    """Deterministic candidate order covering promoted sets, unusual numbers, and no-existing."""
    rng = random.Random(seed)
    promoted = [entry for entry in pool if str(entry.get("setId") or "").isdigit()]
    unusual = [
        entry
        for entry in pool
        if "/" in str(entry.get("collectorNumber") or "")
        or str(entry.get("collectorNumber") or "").startswith("0")
        or any(ch.isalpha() for ch in str(entry.get("collectorNumber") or ""))
    ]
    no_existing = [entry for entry in pool if entry["canonicalBaseId"] not in existing_ids]

    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _push(entry: dict[str, Any]) -> None:
        cid = entry["canonicalBaseId"]
        if cid in seen:
            return
        seen.add(cid)
        ordered.append(entry)

    for group in (promoted, unusual, no_existing):
        for entry in sorted(group, key=lambda item: item["canonicalBaseId"]):
            _push(entry)

    by_set: dict[str, list[dict[str, Any]]] = {}
    for entry in pool:
        by_set.setdefault(str(entry.get("setId")), []).append(entry)
    for set_id in sorted(by_set.keys()):
        candidates = sorted(by_set[set_id], key=lambda item: item["canonicalBaseId"])
        if candidates:
            _push(candidates[0])

    remaining = [entry for entry in pool if entry["canonicalBaseId"] not in seen]
    remaining.sort(key=lambda item: item["canonicalBaseId"])
    rng.shuffle(remaining)
    for entry in remaining:
        _push(entry)
    return ordered


def _probe_pokewallet_entry(
    session: requests.Session,
    entry: dict[str, Any],
    identities: dict[str, Any],
    *,
    max_attempts: int = 3,
    inter_request_delay_s: float = 2.5,
) -> dict[str, Any]:
    """Authenticated probe with long backoff on HTTP 429 (hourly quota aware)."""
    import time

    identity = identities[entry["canonicalBaseId"]]
    resolution = resolve_provider_with_trace(identity, source_card=identity.source_card)
    if resolution.ambiguous:
        return {"canonicalBaseId": entry["canonicalBaseId"], "usable": False, "error": "ambiguous"}
    if resolution.candidate is None:
        return {"canonicalBaseId": entry["canonicalBaseId"], "usable": False, "error": "no_match"}
    url = resolution.candidate.source_url_thumb or resolution.candidate.source_url_display
    probe: dict[str, Any] | None = None
    for attempt in range(max_attempts):
        if attempt == 0:
            time.sleep(inter_request_delay_s)
        probe = probe_url(session, url, include_pokewallet_auth=True)
        if probe.get("usable"):
            break
        if probe.get("authRequired") or probe.get("status") in {401, 403}:
            break
        if probe.get("rateLimited") or probe.get("status") == 429:
            # Hourly limit is 100; avoid burning the next window with rapid retries.
            wait_s = 3600.0 if attempt == 0 else 300.0
            print(
                f"Gate B rate-limited on {entry['canonicalBaseId']} "
                f"attempt={attempt + 1}/{max_attempts}; sleeping {int(wait_s)}s",
                flush=True,
            )
            time.sleep(wait_s)
            continue
        break
    assert probe is not None
    probe["canonicalBaseId"] = entry["canonicalBaseId"]
    probe.pop("headers", None)
    return probe


def select_pokewallet_canary(
    manifest: dict[str, Any],
    *,
    seed: int = THUMB_ROLLOUT_SEED,
    count: int = 25,
    existing_ids: set[str] | None = None,
    require_authenticated_reachable: bool = False,
    preselected_ids: set[str] | None = None,
    skip_ids: set[str] | None = None,
) -> dict[str, Any]:
    existing = existing_ids or set()
    preselected = set(preselected_ids or set())
    skipped = set(skip_ids or set())
    pool = [entry for entry in (manifest.get("entries") or []) if entry.get("provider") == "pokewallet"]
    if len(pool) < count:
        raise RuntimeError(f"PokeWallet pool too small: {len(pool)} < {count}")

    by_id = {entry["canonicalBaseId"]: entry for entry in pool}
    candidates = _ordered_pokewallet_candidates(pool, seed=seed, existing_ids=existing)
    if not require_authenticated_reachable:
        selected = candidates[:count]
        if len(selected) != count:
            raise RuntimeError(f"Unable to select PokeWallet canary of {count}; got {len(selected)}")
        return filter_manifest_entries(
            manifest,
            provider="pokewallet",
            canonical_ids={entry["canonicalBaseId"] for entry in selected},
        )

    identities = {
        identity.canonical_base_id: identity
        for identity in identities_for_manifest(filter_manifest_entries(manifest, provider="pokewallet"))
    }
    session = requests.Session()
    session.headers.update({"User-Agent": "CardScanR-ThumbnailRollout/0.2"})

    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    for cid in sorted(preselected):
        entry = by_id.get(cid)
        if entry is None or cid in selected_ids:
            continue
        selected.append(entry)
        selected_ids.add(cid)
        print(f"Gate B canary resume {len(selected)}/{count}: {cid}", flush=True)
        if len(selected) >= count:
            break

    auth_failures = 0

    for entry in candidates:
        if len(selected) >= count:
            break
        cid = entry["canonicalBaseId"]
        if cid in selected_ids or cid in skipped:
            continue
        probe = _probe_pokewallet_entry(session, entry, identities)
        if probe.get("usable"):
            selected.append(entry)
            selected_ids.add(cid)
            print(
                f"Gate B canary pick {len(selected)}/{count}: {cid} "
                f"status={probe.get('status')}",
                flush=True,
            )
            continue
        if probe.get("authRequired") or probe.get("status") in {401, 403}:
            auth_failures += 1
            print(f"Gate B canary auth failure: {cid} status={probe.get('status')}", flush=True)
            if auth_failures >= 3:
                raise RuntimeError(
                    "PokeWallet authentication failed repeatedly "
                    f"(card={cid} status={probe.get('status')})"
                )
            continue
        if probe.get("rateLimited") or probe.get("status") == 429:
            print(f"Gate B skip after rate-limit wait: {cid} status={probe.get('status')}", flush=True)
            continue
        print(
            f"Gate B canary skip: {cid} status={probe.get('status')} "
            f"rateLimited={probe.get('rateLimited')}",
            flush=True,
        )

    if len(selected) != count:
        raise RuntimeError(f"Unable to select PokeWallet canary of {count}; got {len(selected)}")

    return filter_manifest_entries(
        manifest,
        provider="pokewallet",
        canonical_ids={entry["canonicalBaseId"] for entry in selected},
    )


def gate_b_authenticated_probe(manifest: dict[str, Any]) -> dict[str, Any]:
    session = requests.Session()
    session.headers.update({"User-Agent": "CardScanR-ThumbnailRollout/0.2"})
    identities = {identity.canonical_base_id: identity for identity in identities_for_manifest(manifest)}
    probes: list[dict[str, Any]] = []
    stop_reasons: list[str] = []
    usable = 0
    for entry in manifest.get("entries") or []:
        if not entry.get("language") or not entry.get("setId") or not entry.get("collectorNumber"):
            stop_reasons.append(f"missing_identity:{entry['canonicalBaseId']}")
            continue
        probe = _probe_pokewallet_entry(session, entry, identities)
        if probe.get("error") == "ambiguous":
            stop_reasons.append(f"ambiguous:{entry['canonicalBaseId']}")
            continue
        if probe.get("error") == "no_match":
            stop_reasons.append(f"no_match:{entry['canonicalBaseId']}")
            continue
        probes.append(probe)
        if probe.get("rateLimited") or probe.get("status") == 429:
            stop_reasons.append(f"rate_limited:{entry['canonicalBaseId']}")
        elif probe.get("authRequired") or probe.get("status") in {401, 403}:
            stop_reasons.append(f"auth_failed:{entry['canonicalBaseId']}")
        elif not probe.get("usable"):
            stop_reasons.append(
                f"not_image_or_unreachable:{entry['canonicalBaseId']}:status={probe.get('status')}"
            )
        else:
            usable += 1
            content_type = str(probe.get("contentType") or "")
            if content_type and not content_type.startswith("image/") and content_type != "application/octet-stream":
                stop_reasons.append(f"non_image_content_type:{entry['canonicalBaseId']}:{content_type}")

    deduped: list[str] = []
    for reason in stop_reasons:
        if reason not in deduped:
            deduped.append(reason)
    return {
        "generatedAtUtc": utc_now_iso(),
        "cardCount": len(manifest.get("entries") or []),
        "usableCount": usable,
        "shouldStop": bool(deduped),
        "stopReasons": deduped,
        "probes": probes,
        "credentialUsedInProbe": True,
        "credentialValueReported": False,
    }


def build_tcgdex_diagnostic(*, catalogue_root: Path = DEFAULT_CATALOGUE_ROOT) -> dict[str, Any]:
    normalization_candidates: list[dict[str, str]] = []
    sample_404s: list[dict[str, Any]] = []
    session = requests.Session()
    session.headers.update({"User-Agent": "CardScanR-ThumbnailRollout/0.2"})
    tcgdex_urls: list[tuple[str, str]] = []

    for identity in iter_catalogue_identities(catalogue_root, languages=("en", "jp")):
        if tcgdex_needs_normalization(identity):
            if len(normalization_candidates) < 200:
                normalization_candidates.append(
                    {
                        "canonicalBaseId": identity.canonical_base_id,
                        "language": identity.language,
                        "setId": identity.set_id,
                        "collectorNumber": identity.collector_number,
                        "imageSmall": identity.catalogue_image_small or "",
                    }
                )
        host = (urlparse(identity.catalogue_image_small or "").hostname or "").lower()
        if "tcgdex" in host and identity.catalogue_image_small:
            tcgdex_urls.append((identity.canonical_base_id, identity.catalogue_image_small))

    rng = random.Random(THUMB_ROLLOUT_SEED)
    rng.shuffle(tcgdex_urls)
    for canonical_id, url in tcgdex_urls[:25]:
        probe = probe_url(session, url, include_pokewallet_auth=False)
        if probe.get("notFound") or probe.get("status") == 404:
            sample_404s.append(
                {
                    "canonicalBaseId": canonical_id,
                    "url": url,
                    "status": probe.get("status"),
                }
            )

    return {
        "generatedAtUtc": utc_now_iso(),
        "normalizationCandidateCountObserved": len(normalization_candidates),
        "normalizationCandidatesSample": normalization_candidates[:103],
        "liveSample404Count": len(sample_404s),
        "liveSample404s": sample_404s,
        "note": "Diagnostic only. TCGdex is excluded from this English thumbnail execution.",
        "blocksGateA": False,
    }


def summarize_execution(cards: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "attempted": len(cards),
        "skipped": sum(1 for card in cards if card.get("database_status") == "skipped"),
        "uploaded": sum(1 for card in cards if card.get("database_status") == "completed"),
        "failed": sum(1 for card in cards if card.get("database_status") == "failed"),
        "ambiguous": sum(1 for card in cards if card.get("ambiguous")),
        "totalSourceBytes": sum(int(card.get("source_byte_count") or 0) for card in cards),
        "totalThumbBytes": sum(int(card.get("thumb_byte_count") or 0) for card in cards),
        "displayPathsPresent": sum(1 for card in cards if card.get("display_storage_path")),
    }


def verify_successful_cards(
    runner: Stage2Runner,
    manifest: dict[str, Any],
    execution_cards: list[dict[str, Any]],
) -> dict[str, Any]:
    """Verify completed uploads and idempotent skips (existing completed/verified records)."""
    # Treat skipped successes as completed for independent verification.
    adjusted: list[dict[str, Any]] = []
    for card in execution_cards:
        row = dict(card)
        if row.get("database_status") == "skipped":
            row["database_status"] = "completed"
        adjusted.append(row)
    return runner.verify_manifest(manifest, execution_cards=adjusted)


def count_supabase_thumbs(runner: Stage2Runner, *, languages: tuple[str, ...] = ("en", "jp")) -> dict[str, Any]:
    # Lightweight count via REST with prefer count.
    response = runner.db.session.get(
        f"{runner.db.supabase_url}/rest/v1/pokemon_card_image_records",
        params={
            "select": "canonical_base_id,language,primary_provider,status,thumb_storage_path,display_storage_path",
            "status": "in.(completed,verified)",
            "thumb_storage_path": "not.is.null",
        },
        headers={"Prefer": "count=exact"},
        timeout=runner.db.timeout_seconds,
    )
    response.raise_for_status()
    rows = response.json()
    providers = Counter(row.get("primary_provider") or "unknown" for row in rows)
    with_display = sum(1 for row in rows if row.get("display_storage_path"))
    return {
        "combinedVerifiedOrCompletedWithThumb": len(rows),
        "withDisplayPath": with_display,
        "providerBreakdown": dict(providers),
        "byLanguage": dict(Counter(row.get("language") or "unknown" for row in rows)),
    }
