from __future__ import annotations

import hashlib
import json
import random
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from PIL import Image, ImageDraw, ImageFont

from .catalogue import DEFAULT_CATALOGUE_ROOT, iter_catalogue_identities
from .config import ImagePipelineConfig
from .database import utc_now_iso
from .matching import resolve_provider_with_trace
from .paths import public_storage_url
from .processing import decode_and_validate_card_image, download_image_bytes
from .providers.pokemon_tcg_api import PokemonTcgApiImageProvider
from .sample_manifest import identities_for_manifest
from .stage2_runner import Stage2Runner, build_contact_sheet, write_json_report
from .tcgdex_serie_cache import enrich_identity_serie_id
from .thumbnail_execute import (
    APPROVED_MANIFEST_PATH,
    APPROVED_MANIFEST_SHA256,
    POKEMON_TCG_HOST,
    enrich_skipped_public_urls,
    filter_manifest_entries,
    gate_b_authenticated_probe,
    load_approved_manifest,
    pokewallet_credential_status,
    select_pokewallet_canary,
    summarize_execution,
    verify_successful_cards,
)
from .thumbnail_rollout import RUNTIME_DIR, THUMB_ROLLOUT_SEED, probe_url

FAILED_GATE_A_IDS = (
    "pokemon|en|me3|19|dewgong",
    "pokemon|en|me2pt5|256|boss_s_orders",
    "pokemon|en|me2pt5|92|rotom",
    "pokemon|en|me2pt5|214|urbain",
    "pokemon|en|me2pt5|123|gastly",
    "pokemon|en|me3|59|klefki",
    "pokemon|en|me3|67|furfrou",
    "pokemon|en|me2pt5|253|mega_audino_ex",
    "pokemon|en|me2pt5|77|team_rocket_s_exeggcute",
)
REPLACEMENT_SEED = 20260710_9
NEXT_RETRY_DAYS = 30


def classify_gate_a_failure(identity, *, constructed_url: str | None, http_status: int | None) -> str:
    provider_id = (identity.provider_ids or {}).get("pokemonTcgApi") or (identity.provider_ids or {}).get(
        "pokemonTcgApiId"
    )
    has_other_approved = bool(
        (identity.provider_ids or {}).get("tcgdex")
        or (identity.provider_ids or {}).get("tcgdexCardId")
        or (identity.provider_ids or {}).get("pokewallet")
    )
    if not provider_id:
        return "stale_provider_id"
    expected_prefix = f"{identity.set_id}-"
    if not str(provider_id).startswith(expected_prefix) and str(provider_id).lower() != f"{identity.set_id}-{identity.collector_number}".lower():
        # Still allow me2pt5-123 style exact matches.
        if f"{identity.set_id}-{identity.collector_number}".lower() != str(provider_id).lower():
            return "stale_provider_id"
    if constructed_url and (urlparse(constructed_url).hostname or "").lower() != POKEMON_TCG_HOST:
        return "incorrect_url_construction"
    if http_status == 404 and provider_id:
        if has_other_approved:
            return "exact_image_available_from_another_already_approved_provider"
        return "provider_metadata_exists_but_image_cdn_unavailable"
    if http_status and http_status >= 400:
        return "provider_metadata_exists_but_image_cdn_unavailable"
    return "unresolved"


def classify_and_persist_failures(
    runner: Stage2Runner,
    *,
    execute_report_path: Path,
) -> dict[str, Any]:
    execution = json.loads(execute_report_path.read_text(encoding="utf-8"))
    failed_rows = [card for card in execution.get("cards") or [] if card.get("database_status") == "failed"]
    identities = {
        identity.canonical_base_id: identity
        for identity in iter_catalogue_identities(DEFAULT_CATALOGUE_ROOT, languages=("en",))
        if identity.canonical_base_id in FAILED_GATE_A_IDS
    }
    provider = PokemonTcgApiImageProvider()
    session = requests.Session()
    session.headers.update({"User-Agent": "CardScanR-GateA-Remediation/0.1"})
    classifications: list[dict[str, Any]] = []
    now = utc_now_iso()
    next_retry = (datetime.now(timezone.utc) + timedelta(days=NEXT_RETRY_DAYS)).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )

    for row in failed_rows:
        canonical_id = row["canonical_base_id"]
        identity = identities[canonical_id]
        enriched = enrich_identity_serie_id(identity)
        candidate = provider.resolve(enriched)
        constructed = candidate.source_url_thumb if candidate else None
        http_status = None
        if constructed:
            probe = probe_url(session, constructed, include_pokewallet_auth=False)
            http_status = probe.get("status")
        classification = classify_gate_a_failure(identity, constructed_url=constructed, http_status=http_status)
        payload = {
            "canonical_base_id": identity.canonical_base_id,
            "game": identity.game,
            "language": identity.language,
            "set_id": identity.set_id,
            "set_code": identity.set_code,
            "collector_number": identity.collector_number,
            "printed_card_number": identity.printed_card_number,
            "local_card_number": identity.local_card_number,
            "set_total": identity.set_total,
            "printed_total": identity.printed_total,
            "provider_set_id": identity.provider_set_id or identity.set_id,
            "status": "provider_image_unavailable",
            "failure_reason": (
                f"classification={classification}; http_status={http_status}; "
                f"source={constructed}; next_retry_eligible_at={next_retry}; "
                "scrydex_mirror_not_used=true"
            )[:2000],
            "retry_count": int(row.get("retry_count") or 0),
            "primary_provider": "pokemon_tcg_api",
            "source_image_url": constructed,
            "source_image_url_display": candidate.source_url_display if candidate else None,
            "provider_card_id": candidate.provider_card_id if candidate else None,
            "provider_image_set_id": candidate.provider_set_id if candidate else identity.set_id,
            "cache_control": runner.config.cache_control,
            "last_attempt_at": now,
            "updated_at": now,
        }
        runner.db.upsert_record(payload, dry_run=False)
        classifications.append(
            {
                "canonicalBaseId": canonical_id,
                "language": identity.language,
                "setId": identity.set_id,
                "providerSetId": identity.provider_set_id or identity.set_id,
                "collectorNumber": identity.collector_number,
                "sourceUrl": constructed,
                "httpResponse": http_status,
                "catalogueImageSource": identity.image_source,
                "catalogueImageSmall": identity.catalogue_image_small,
                "providerIds": identity.provider_ids,
                "exactFailureClassification": classification,
                "statusPersisted": "provider_image_unavailable",
                "nextRetryEligibleAt": next_retry,
                "scrydexUsed": False,
            }
        )

    return {
        "generatedAtUtc": now,
        "failedCount": len(classifications),
        "classifications": classifications,
        "classificationTotals": _count_by(classifications, "exactFailureClassification"),
        "note": "Scrydex mirrors were not used. Cards remain unresolved under Pokémon TCG API.",
    }


def _count_by(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    totals: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "unknown")
        totals[value] = totals.get(value, 0) + 1
    return totals


def _preflight_pokemon_tcg_url(session: requests.Session, url: str, *, timeout: int = 25) -> dict[str, Any]:
    try:
        data, content_type = download_image_bytes(
            session,
            url,
            timeout_seconds=timeout,
            max_retries=2,
            retry_base_seconds=0.5,
        )
        image = decode_and_validate_card_image(data)
        width, height = image.size
        return {
            "usable": True,
            "status": 200,
            "contentType": content_type,
            "bytes": len(data),
            "width": width,
            "height": height,
            "error": None,
        }
    except Exception as exc:
        return {
            "usable": False,
            "status": None,
            "contentType": None,
            "bytes": 0,
            "width": None,
            "height": None,
            "error": str(exc),
        }


def build_replacement_manifest(
    *,
    original_manifest: dict[str, Any],
    seed: int = REPLACEMENT_SEED,
    count: int = 9,
) -> dict[str, Any]:
    excluded = {entry["canonicalBaseId"] for entry in (original_manifest.get("entries") or []) if entry.get("provider") == "pokemon_tcg_api"}
    excluded.update(FAILED_GATE_A_IDS)
    provider = PokemonTcgApiImageProvider()
    session = requests.Session()
    session.headers.update({"User-Agent": "CardScanR-GateA-Remediation/0.1"})

    pools: dict[str, list[Any]] = {
        "recent_sv": [],
        "unusual_collector": [],
        "full_artish": [],
        "general": [],
    }
    for identity in iter_catalogue_identities(DEFAULT_CATALOGUE_ROOT, languages=("en",)):
        if identity.canonical_base_id in excluded:
            continue
        if identity.image_source != "pokemon_tcg_api":
            continue
        enriched = enrich_identity_serie_id(identity)
        candidate = provider.resolve(enriched)
        if candidate is None:
            continue
        host = (urlparse(candidate.source_url_display).hostname or "").lower()
        if host != POKEMON_TCG_HOST:
            continue
        collector = identity.collector_number or ""
        item = (identity, candidate)
        if identity.set_id.startswith("sv") or identity.set_id.startswith("zsv") or identity.set_id.startswith("me"):
            pools["recent_sv"].append(item)
        if "/" in collector or collector.startswith("0") or any(ch.isalpha() for ch in collector) or collector.isdigit() and int(collector) >= 180:
            pools["unusual_collector"].append(item)
        if int(collector) >= 150 if collector.isdigit() else False:
            pools["full_artish"].append(item)
        pools["general"].append(item)

    rng = random.Random(seed)
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()

    def _try_add(identity, candidate, reason: str) -> bool:
        if len(selected) >= count or identity.canonical_base_id in selected_ids:
            return False
        url = candidate.source_url_thumb or candidate.source_url_display
        preflight = _preflight_pokemon_tcg_url(session, url)
        if not preflight["usable"]:
            return False
        selected.append(
            {
                "bucket": "en_pokemon_tcg_api_replacement",
                "edgeCaseTag": reason,
                "canonicalBaseId": identity.canonical_base_id,
                "language": identity.language,
                "setId": identity.set_id,
                "collectorNumber": identity.collector_number,
                "imageSource": identity.image_source,
                "provider": "pokemon_tcg_api",
                "sourceUrl": url,
                "preflightStatus": preflight["status"],
                "preflightContentType": preflight["contentType"],
                "preflightBytes": preflight["bytes"],
                "preflightWidth": preflight["width"],
                "preflightHeight": preflight["height"],
                "replacementReason": reason,
            }
        )
        selected_ids.add(identity.canonical_base_id)
        return True

    # Deterministic stratified picks.
    for reason, pool_name in (
        ("recent_set", "recent_sv"),
        ("unusual_collector", "unusual_collector"),
        ("high_number_variant", "full_artish"),
    ):
        pool = list(pools[pool_name])
        pool.sort(key=lambda item: item[0].canonical_base_id)
        rng.shuffle(pool)
        for identity, candidate in pool:
            if _try_add(identity, candidate, reason):
                break

    general = list(pools["general"])
    general.sort(key=lambda item: item[0].canonical_base_id)
    rng.shuffle(general)
    for identity, candidate in general:
        if len(selected) >= count:
            break
        # Prefer set diversity.
        used_sets = {entry["setId"] for entry in selected}
        if identity.set_id in used_sets and len(selected) < count - 1:
            continue
        _try_add(identity, candidate, "deterministic_fill")

    if len(selected) < count:
        for identity, candidate in general:
            if len(selected) >= count:
                break
            _try_add(identity, candidate, "deterministic_fill_fallback")

    if len(selected) != count:
        raise RuntimeError(f"Unable to select {count} replacements; got {len(selected)}")

    manifest = {
        "schemaVersion": "gate-a-replacements-9-1.0.0",
        "generatedAtUtc": utc_now_iso(),
        "seed": seed,
        "cardCount": len(selected),
        "language": "en",
        "provider": "pokemon_tcg_api",
        "importDisplay": False,
        "originalFailedCanonicalBaseIds": list(FAILED_GATE_A_IDS),
        "entries": selected,
        "note": "Replacements for Gate A provider_image_unavailable cards. Original approved 500-card manifest was not mutated.",
        "originalApprovedManifestSha256": APPROVED_MANIFEST_SHA256,
    }
    return manifest


def write_replacement_manifest(manifest: dict[str, Any], *, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest["sha256"] = digest
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def dry_run_replacements(runner: Stage2Runner, manifest: dict[str, Any]) -> dict[str, Any]:
    payload = runner.dry_run_manifest(manifest)
    stop: list[str] = list(payload.get("stopReasons") or [])
    if payload.get("ambiguousCount"):
        stop.append("ambiguous_count_gt_0")
    path_owner: dict[str, str] = {}
    for card in payload.get("cards") or []:
        cid = card.get("canonical_base_id")
        if card.get("display_storage_path"):
            stop.append(f"display_webp_planned:{cid}")
        source = card.get("source_url") or ""
        host = (urlparse(source).hostname or "").lower()
        if card.get("database_status") == "dry_run_planned_upsert" and host != POKEMON_TCG_HOST:
            stop.append(f"unexpected_host:{cid}:{host}")
        thumb = card.get("thumb_storage_path")
        if thumb:
            if thumb in path_owner and path_owner[thumb] != cid:
                stop.append(f"duplicate_path:{thumb}")
            path_owner[thumb] = cid
    # Live preflight already done in manifest; re-check planned upserts quickly.
    session = requests.Session()
    session.headers.update({"User-Agent": "CardScanR-GateA-Remediation/0.1"})
    for entry in manifest.get("entries") or []:
        probe = probe_url(session, entry["sourceUrl"], include_pokewallet_auth=False)
        if not probe.get("usable"):
            stop.append(f"preflight_not_200:{entry['canonicalBaseId']}:{probe.get('status')}")
    deduped: list[str] = []
    for reason in stop:
        if reason not in deduped:
            deduped.append(reason)
    payload["stopReasons"] = deduped
    payload["shouldStop"] = bool(deduped)
    payload["importDisplay"] = False
    return payload


def build_reconciled_contact_sheet(
    *,
    original_success_cards: list[dict[str, Any]],
    replacement_cards: list[dict[str, Any]],
    unresolved_failures: list[dict[str, Any]],
    output_path: Path,
) -> Path:
    session = requests.Session()
    thumbs: list[tuple[Image.Image, str]] = []
    for card in original_success_cards:
        url = card.get("thumb_public_url")
        if not url:
            continue
        response = session.get(url, timeout=30)
        if response.status_code != 200:
            continue
        image = Image.open(BytesIO(response.content)).convert("RGB")
        label = f"ORIG|{card.get('set_id')}/{card.get('collector_number')}"
        thumbs.append((image, label))
    for card in replacement_cards:
        url = card.get("thumb_public_url")
        if not url:
            continue
        response = session.get(url, timeout=30)
        if response.status_code != 200:
            continue
        image = Image.open(BytesIO(response.content)).convert("RGB")
        label = f"REPL|{card.get('set_id')}/{card.get('collector_number')}"
        thumbs.append((image, label))

    columns = 20
    thumb_w, thumb_h, label_h = 180, 250, 36
    unresolved_rows = max(1, (len(unresolved_failures) + columns - 1) // columns)
    success_rows = (len(thumbs) + columns - 1) // columns if thumbs else 0
    section_gap = 40
    unresolved_block_h = unresolved_rows * 28 + 50
    sheet_h = success_rows * (thumb_h + label_h) + section_gap + unresolved_block_h + 30
    sheet = Image.new("RGB", (columns * thumb_w, max(sheet_h, 200)), "white")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for index, (image, label) in enumerate(thumbs):
        row, col = divmod(index, columns)
        fitted = Image.new("RGB", (thumb_w, thumb_h), "white")
        copy = image.copy()
        copy.thumbnail((thumb_w, thumb_h))
        x = (thumb_w - copy.width) // 2
        y = (thumb_h - copy.height) // 2
        fitted.paste(copy, (x, y))
        ox = col * thumb_w
        oy = row * (thumb_h + label_h)
        sheet.paste(fitted, (ox, oy))
        draw.text((ox + 4, oy + thumb_h + 2), label, fill="black", font=font)

    y0 = success_rows * (thumb_h + label_h) + 10
    draw.text((8, y0), "UNRESOLVED ORIGINALS (provider_image_unavailable — not imported)", fill="black", font=font)
    y = y0 + 20
    for index, item in enumerate(unresolved_failures):
        text = (
            f"{item.get('canonicalBaseId')} | {item.get('exactFailureClassification')} | "
            f"HTTP {item.get('httpResponse')}"
        )
        draw.text((8, y + index * 14), text[:140], fill="black", font=font)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path, format="PNG")
    return output_path


def load_original_gate_a_successes(execute_path: Path, runner: Stage2Runner) -> list[dict[str, Any]]:
    execution = json.loads(execute_path.read_text(encoding="utf-8"))
    cards = enrich_skipped_public_urls(runner, execution.get("cards") or [])
    return [card for card in cards if card.get("database_status") in {"completed", "skipped"}]
