from __future__ import annotations

import hashlib
import heapq
import io
import json
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

import requests
from PIL import Image, ImageDraw

from cardscanr_image_pipeline.paths import safe_path_segment

from .contracts import LANGUAGE_DEFINITIONS, canonicalize_language, write_json_atomic
from .metadata import parse_retry_after
from .providers import PROVIDER_LEDGER
from .reconciliation import iter_jsonl


ROOT = Path(__file__).resolve().parent.parent
CATALOGUE_DIR = ROOT / "data" / "global" / "catalogue"
REPORT_DIR = ROOT / "reports" / "global_rollout"
USER_AGENT = "CardScanR-GlobalRollout-ImagePreflight/1.0"
DIRECT_IMAGE_CATALOGUE_PATH = CATALOGUE_DIR / "direct_images.jsonl"
PERMANENT_FAILURE_REGISTRY_PATH = REPORT_DIR / "direct_image_permanent_failures.json"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_global_image_path(
    *,
    language: str,
    region: str,
    canonical_set_id: str,
    canonical_printing_id: str,
    content_hash_sha256: str,
    variant: str,
) -> str:
    digest = str(content_hash_sha256 or "").strip().casefold()
    if len(digest) < 16 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("content_hash_sha256 must contain at least 16 hexadecimal characters")
    if variant not in {"thumb", "display"}:
        raise ValueError("variant must be thumb or display")
    region_segment = "global" if region == "GLOBAL" else region.casefold()
    return "/".join(
        (
            "pokemon",
            safe_path_segment(language),
            safe_path_segment(region_segment),
            safe_path_segment(canonical_set_id),
            safe_path_segment(canonical_printing_id),
            "v",
            digest[:16],
            f"{variant}.webp",
        )
    )


def _provider_policy(provider: str) -> dict[str, Any] | None:
    return next(
        (item for item in PROVIDER_LEDGER if item["provider"] == provider),
        None,
    )


def _image_candidate(row: dict[str, Any]) -> dict[str, Any] | None:
    for item in row.get("imageProvenance") or []:
        if not isinstance(item, dict):
            continue
        source_url = item.get("thumbSourceUrl") or item.get("sourceUrl")
        if source_url:
            return {
                "provider": str(item.get("provider") or "unknown"),
                "sourceUrl": str(source_url),
            }
    return None


def _direct_image_record(row: dict[str, Any]) -> dict[str, Any] | None:
    candidate = _image_candidate(row)
    if candidate is None:
        return None
    provenance = next(
        (
            item
            for item in row.get("imageProvenance") or []
            if isinstance(item, dict) and str(item.get("provider") or "") == candidate["provider"]
        ),
        {},
    )
    provider = candidate["provider"]
    policy = _provider_policy(provider) or {}
    authentication = str(policy.get("authenticationMethod") or "unknown")
    public_direct = authentication == "none"
    mirror_status = str(policy.get("imageRehostingStatus") or "unclear")
    if not public_direct:
        technical_status = "auth_server_only"
    elif mirror_status in {"approved", "approved_with_conditions"}:
        technical_status = "direct_ready"
    else:
        technical_status = "direct_ready_permission_to_mirror_pending"
    provider_card_ids = row.get("providerCardIds") or {}
    provider_set_ids = row.get("providerSetIds") or {}
    return {
        "schemaVersion": "1.0.0",
        "canonicalPrintingId": row["canonicalPrintingId"],
        "language": row["language"],
        "region": row["region"],
        "canonicalSetId": row["canonicalSetId"],
        "printedCollectorNumber": row["printedCollectorNumber"],
        "normalizedCollectorNumber": row["normalizedCollectorNumber"],
        "provider": provider,
        "providerCardId": provider_card_ids.get(provider),
        "providerSetId": provider_set_ids.get(provider),
        "originalDocumentedImageUrl": provenance.get("sourceUrl") or candidate["sourceUrl"],
        "normalizedThumbnailUrl": provenance.get("thumbSourceUrl") or candidate["sourceUrl"],
        "normalizedDisplayUrl": provenance.get("displaySourceUrl") or provenance.get("sourceUrl") or candidate["sourceUrl"],
        "authenticationRequirement": "not_required" if public_direct else authentication,
        "directUseTechnicalStatus": technical_status,
        "mirrorPermissionStatus": mirror_status,
        "latestHttpStatus": None,
        "contentType": None,
        "contentLength": None,
        "lastValidationTimestamp": None,
        "permanentFailureState": None,
        "fallbackPriority": 1,
    }


def build_direct_image_catalogue() -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    by_language: Counter[str] = Counter()
    by_state: Counter[str] = Counter()
    for row in iter_jsonl(CATALOGUE_DIR / "cards.jsonl"):
        record = _direct_image_record(row)
        if record is None:
            continue
        records.append(record)
        by_language[str(record["language"])] += 1
        by_state[str(record["directUseTechnicalStatus"])] += 1
    records.sort(key=lambda item: str(item["canonicalPrintingId"]))
    DIRECT_IMAGE_CATALOGUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with DIRECT_IMAGE_CATALOGUE_PATH.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            handle.write(encoded)
            digest.update(encoded.encode("utf-8"))
    payload = {
        "schemaVersion": "1.0.0",
        "generatedAtUtc": utc_now_iso(),
        "classification": "PASS",
        "records": len(records),
        "byLanguage": dict(sorted(by_language.items())),
        "byState": dict(sorted(by_state.items())),
        "path": DIRECT_IMAGE_CATALOGUE_PATH.relative_to(ROOT).as_posix(),
        "sha256": digest.hexdigest(),
        "authenticatedUrlsExposedToFlutter": False,
        "r2Writes": 0,
    }
    write_json_atomic(REPORT_DIR / "direct_image_catalogue.json", payload)
    return payload


def run_direct_image_canaries(
    *,
    sample_size: int = 100,
    request_interval_seconds: float = 0.2,
    seed: str = "cardscanr-direct-image-canary-v1",
) -> dict[str, Any]:
    samples, candidate_counts, _ = _deterministic_samples(sample_size=sample_size, seed=seed)
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "image/webp,image/*;q=0.8"})
    results: list[dict[str, Any]] = []
    contact_images: dict[tuple[str, str], list[tuple[Image.Image, str]]] = defaultdict(list)
    last_request: float | None = None
    permanent_registry = {}
    try:
        registry_payload = json.loads(PERMANENT_FAILURE_REGISTRY_PATH.read_text(encoding="utf-8-sig"))
        permanent_registry = {
            str(item["canonicalPrintingId"]): item
            for item in registry_payload.get("failures") or []
            if isinstance(item, dict) and item.get("canonicalPrintingId")
        }
    except (OSError, json.JSONDecodeError):
        pass
    for pair in sorted(samples):
        for card in samples[pair]:
            cached_failure = permanent_registry.get(str(card["canonicalPrintingId"]))
            if cached_failure and cached_failure.get("state") == "permanent_404":
                results.append(
                    {
                        "canonicalPrintingId": card["canonicalPrintingId"],
                        "language": card["language"],
                        "region": card["region"],
                        "provider": card["provider"],
                        "sourceUrlSha256": cached_failure.get("sourceUrlSha256"),
                        "httpStatus": 404,
                        "state": "permanent_404_cached",
                        "networkRequestPerformed": False,
                    }
                )
                continue
            if last_request is not None:
                delay = request_interval_seconds - (time.monotonic() - last_request)
                if delay > 0:
                    time.sleep(delay)
            last_request = time.monotonic()
            result: dict[str, Any] = {
                "canonicalPrintingId": card["canonicalPrintingId"],
                "language": card["language"],
                "region": card["region"],
                "provider": card["provider"],
                "sourceUrlSha256": hashlib.sha256(card["sourceUrl"].encode("utf-8")).hexdigest(),
            }
            try:
                response = session.get(card["sourceUrl"], timeout=30)
                result["httpStatus"] = response.status_code
                result["contentType"] = response.headers.get("Content-Type")
                if response.status_code == 404:
                    result["state"] = "permanent_404"
                elif response.status_code == 429:
                    result["state"] = "rate_limited"
                elif response.status_code != 200 or not str(result["contentType"] or "").startswith("image/"):
                    result["state"] = "unsuitable_response"
                else:
                    with Image.open(io.BytesIO(response.content)) as decoded:
                        decoded.load()
                        width, height = decoded.size
                        ratio = width / height if height else 0
                        plausible = 0.60 <= ratio <= 0.80 and width >= 100 and height >= 140
                        result.update({"width": width, "height": height, "aspectRatio": round(ratio, 4)})
                        result["sha256"] = hashlib.sha256(response.content).hexdigest()
                        result["state"] = "valid" if plausible else "implausible_aspect_ratio"
                        if plausible:
                            thumb = decoded.convert("RGB")
                            thumb.thumbnail((147, 202))
                            contact_images[pair].append((thumb.copy(), str(card["collectorNumber"])))
            except (requests.RequestException, OSError, ValueError) as exc:
                result.update({"state": "temporary_failure", "errorType": type(exc).__name__})
            results.append(result)
    contact_paths: list[str] = []
    for (language, region), images in sorted(contact_images.items()):
        if not images:
            continue
        columns = 10
        cell_w, cell_h = 157, 226
        rows = (len(images) + columns - 1) // columns
        sheet = Image.new("RGB", (columns * cell_w, rows * cell_h), "white")
        draw = ImageDraw.Draw(sheet)
        for index, (thumb, label) in enumerate(images):
            x, y = (index % columns) * cell_w, (index // columns) * cell_h
            sheet.paste(thumb, (x + (147 - thumb.width) // 2, y))
            draw.text((x + 3, y + 205), label[:22], fill="black")
        path = REPORT_DIR / f"direct_canary_{language}_{region}_contact_sheet.jpg"
        sheet.save(path, format="JPEG", quality=82, optimize=True)
        contact_paths.append(path.relative_to(ROOT).as_posix())
    counts = Counter(str(item["state"]) for item in results)
    generated_at = utc_now_iso()
    for item in results:
        if item["state"] == "permanent_404":
            canonical_id = str(item["canonicalPrintingId"])
            previous = permanent_registry.get(canonical_id) or {}
            permanent_registry[canonical_id] = {
                "canonicalPrintingId": canonical_id,
                "language": item["language"],
                "region": item["region"],
                "provider": item["provider"],
                "sourceUrlSha256": item["sourceUrlSha256"],
                "httpStatus": 404,
                "state": "permanent_404",
                "firstObservedAtUtc": previous.get("firstObservedAtUtc") or generated_at,
                "lastObservedAtUtc": generated_at,
            }
    write_json_atomic(
        PERMANENT_FAILURE_REGISTRY_PATH,
        {
            "schemaVersion": "1.0.0",
            "updatedAtUtc": generated_at,
            "failureCount": len(permanent_registry),
            "failures": [permanent_registry[key] for key in sorted(permanent_registry)],
        },
    )
    by_language: dict[str, dict[str, Any]] = {}
    for language in sorted({str(item["language"]) for item in results}):
        language_results = [item for item in results if item["language"] == language]
        valid = sum(item["state"] == "valid" for item in language_results)
        rate = valid / len(language_results) if language_results else 0
        classification = "PASS" if rate >= 0.95 else "PARTIAL" if valid else "FAIL"
        by_language[language] = {"classification": classification, "tested": len(language_results), "valid": valid, "validPercent": round(rate * 100, 2)}
    overall = "FAIL" if any(item["classification"] == "FAIL" for item in by_language.values()) else "PARTIAL" if any(item["classification"] == "PARTIAL" for item in by_language.values()) else "PASS"
    payload = {
        "schemaVersion": "1.0.0",
        "generatedAtUtc": generated_at,
        "classification": overall,
        "sampleSizePerLanguageRegion": sample_size,
        "candidateCountsByLanguageRegion": {f"{k[0]}|{k[1]}": v for k, v in sorted(candidate_counts.items())},
        "tested": len(results),
        "stateCounts": dict(sorted(counts.items())),
        "byLanguage": by_language,
        "contactSheets": contact_paths,
        "humanVisualApproval": False,
        "imageBodiesRetained": False,
        "r2Writes": 0,
        "results": results,
    }
    _apply_direct_validation_results(results, validated_at=generated_at)
    write_json_atomic(REPORT_DIR / "direct_image_canary_report.json", payload)
    return payload


def _apply_direct_validation_results(results: list[dict[str, Any]], *, validated_at: str) -> None:
    if not DIRECT_IMAGE_CATALOGUE_PATH.exists():
        return
    result_by_id = {str(item["canonicalPrintingId"]): item for item in results}
    temporary_path = DIRECT_IMAGE_CATALOGUE_PATH.with_suffix(".jsonl.tmp")
    with DIRECT_IMAGE_CATALOGUE_PATH.open("r", encoding="utf-8") as source, temporary_path.open(
        "w", encoding="utf-8", newline="\n"
    ) as destination:
        for line in source:
            record = json.loads(line)
            result = result_by_id.get(str(record["canonicalPrintingId"]))
            if result is not None:
                state = str(result.get("state") or "")
                record["latestHttpStatus"] = result.get("httpStatus")
                record["contentType"] = result.get("contentType")
                record["lastValidationTimestamp"] = validated_at
                if result.get("sha256"):
                    record["checksumSha256"] = result["sha256"]
                if result.get("width") and result.get("height"):
                    record["imageDimensions"] = {
                        "width": result["width"],
                        "height": result["height"],
                    }
                if state in {"permanent_404", "permanent_404_cached"}:
                    record["directUseTechnicalStatus"] = "permanent_404"
                    record["permanentFailureState"] = "permanent_404"
            destination.write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    temporary_path.replace(DIRECT_IMAGE_CATALOGUE_PATH)
    digest = hashlib.sha256()
    validated = 0
    permanent_failures = 0
    with DIRECT_IMAGE_CATALOGUE_PATH.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    for result in results:
        if result.get("state") == "valid":
            validated += 1
        elif result.get("state") in {"permanent_404", "permanent_404_cached"}:
            permanent_failures += 1
    try:
        catalogue_report = json.loads(
            (REPORT_DIR / "direct_image_catalogue.json").read_text(encoding="utf-8-sig")
        )
    except (OSError, json.JSONDecodeError):
        catalogue_report = {"schemaVersion": "1.0.0"}
    catalogue_report.update(
        {
            "currentSha256": digest.hexdigest(),
            "lastValidationTimestamp": validated_at,
            "validatedDirectUrls": validated,
            "permanentFailures": permanent_failures,
        }
    )
    write_json_atomic(REPORT_DIR / "direct_image_catalogue.json", catalogue_report)


def _sample_rank(canonical_printing_id: str, seed: str) -> int:
    return int.from_bytes(
        hashlib.sha256(f"{seed}\0{canonical_printing_id}".encode("utf-8")).digest(),
        "big",
    )


def _deterministic_samples(
    *,
    sample_size: int,
    seed: str,
) -> tuple[
    dict[tuple[str, str], list[dict[str, Any]]],
    Counter[tuple[str, str]],
    Counter[str],
]:
    heaps: dict[tuple[str, str], list[tuple[int, str, dict[str, Any]]]] = defaultdict(list)
    candidate_counts: Counter[tuple[str, str]] = Counter()
    candidate_provider_counts: Counter[str] = Counter()
    cards_path = CATALOGUE_DIR / "cards.jsonl"
    for row in iter_jsonl(cards_path):
        candidate = _image_candidate(row)
        if candidate is None:
            continue
        pair = (str(row["language"]), str(row["region"]))
        candidate_counts[pair] += 1
        candidate_provider_counts[candidate["provider"]] += 1
        compact = {
            "canonicalPrintingId": row["canonicalPrintingId"],
            "canonicalSetId": row["canonicalSetId"],
            "language": row["language"],
            "region": row["region"],
            "nativeCardName": row["nativeCardName"],
            "englishCardName": row["englishCardName"],
            "nativeSetName": row["nativeSetName"],
            "collectorNumber": row["printedCollectorNumber"],
            "provider": candidate["provider"],
            "sourceUrl": candidate["sourceUrl"],
        }
        rank = _sample_rank(row["canonicalPrintingId"], seed)
        heap_item = (-rank, str(row["canonicalPrintingId"]), compact)
        heap = heaps[pair]
        if len(heap) < sample_size:
            heapq.heappush(heap, heap_item)
        elif heap_item > heap[0]:
            heapq.heapreplace(heap, heap_item)
    results: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for pair, heap in heaps.items():
        results[pair] = [
            item[2]
            for item in sorted(
                heap,
                key=lambda item: (-item[0], item[1]),
            )
        ]
    return results, candidate_counts, candidate_provider_counts


def create_multilingual_canary_plan(
    *,
    sample_size: int = 100,
    seed: str = "cardscanr-global-canary-v1",
) -> dict[str, Any]:
    samples, candidate_counts, provider_counts = _deterministic_samples(
        sample_size=sample_size,
        seed=seed,
    )
    batches: list[dict[str, Any]] = []
    blocked_by_terms: set[str] = set()
    any_blocker = False
    target_pairs = {
        (definition.language, definition.default_region)
        for definition in LANGUAGE_DEFINITIONS
    } | set(samples)
    for pair in sorted(target_pairs):
        language, region = pair
        cards = samples.get(pair, [])
        providers = sorted({card["provider"] for card in cards})
        blockers: list[str] = []
        if not cards:
            blockers.append("no_public_image_candidates")
        for provider in providers:
            policy = _provider_policy(provider)
            image_status = policy.get("imageRehostingStatus") if policy else "unknown"
            if image_status not in {"approved", "approved_with_conditions"}:
                blockers.append(f"{provider}:image_rehosting_{image_status}")
                blocked_by_terms.add(provider)
        any_blocker = any_blocker or bool(blockers)
        public_cards = [
            {
                key: value
                for key, value in card.items()
                if key != "sourceUrl"
            }
            for card in cards
        ]
        batches.append(
            {
                "language": language,
                "region": region,
                "candidateCount": candidate_counts[pair],
                "plannedCardCount": len(cards),
                "providers": providers,
                "selectionAlgorithm": "lowest SHA-256 rank of seed + canonicalPrintingId",
                "selectionSeed": seed,
                "canaryExecutionAllowed": not blockers,
                "blockers": blockers,
                "cards": public_cards,
            }
        )
    total_planned = sum(batch["plannedCardCount"] for batch in batches)
    payload = {
        "schemaVersion": "1.0.0",
        "generatedAtUtc": utc_now_iso(),
        "classification": "BLOCKED" if any_blocker else "PASS",
        "scope": "deterministic_multilingual_image_canary_plan",
        "sampleSizePerLanguageRegion": sample_size,
        "candidateCountsByProvider": dict(sorted(provider_counts.items())),
        "batchCount": len(batches),
        "totalPlannedCards": total_planned,
        "expectedSourceRequests": total_planned * 2,
        "expectedR2Writes": total_planned * 2,
        "expectedApiCredits": 0,
        "expectedPaidProviderSpendUsd": 0,
        "executionPerformed": False,
        "verifiedR2Thumbs": 0,
        "verifiedR2Displays": 0,
        "blockedProviders": sorted(blocked_by_terms),
        "batches": batches,
        "executionCommandExposed": False,
        "gateRecheckCommand": "python tools/global_rollout.py resume",
    }
    path = REPORT_DIR / "multilingual_100_card_canary_plan.json"
    write_json_atomic(path, payload)
    markdown_path = REPORT_DIR / "multilingual_100_card_canary_plan.md"
    markdown_path.write_text(render_canary_plan_markdown(payload), encoding="utf-8")
    return payload


def render_canary_plan_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Multilingual 100-card Image Canary Plan",
        "",
        f"Classification: **{payload['classification']}**",
        "",
        f"- Batches: {payload['batchCount']}",
        f"- Planned cards: {payload['totalPlannedCards']}",
        f"- Expected source requests: {payload['expectedSourceRequests']}",
        f"- Expected R2 writes: {payload['expectedR2Writes']}",
        f"- Paid provider spend: US${payload['expectedPaidProviderSpendUsd']}",
        f"- Execution performed: {payload['executionPerformed']}",
        "",
    ]
    for batch in payload["batches"]:
        lines.append(
            f"- `{batch['language']}` / `{batch['region']}`: "
            f"{batch['plannedCardCount']} of {batch['candidateCount']} candidates; "
            f"allowed={batch['canaryExecutionAllowed']}; blockers={batch['blockers']}"
        )
    lines.extend(
        [
            "",
            "The plan contains exact canonical IDs but intentionally omits provider source URLs.",
            "Execution remains blocked until image-rehosting permission and the explicit R2 write gate pass.",
            "",
        ]
    )
    return "\n".join(lines)


def _preflight_request(
    session: requests.Session,
    source_url: str,
    *,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    source_hash = hashlib.sha256(source_url.encode("utf-8")).hexdigest()
    try:
        response = session.head(
            source_url,
            allow_redirects=True,
            timeout=timeout_seconds,
        )
        method = "HEAD"
        if response.status_code in {400, 403, 405}:
            response.close()
            response = session.get(
                source_url,
                headers={"Range": "bytes=0-0"},
                stream=True,
                allow_redirects=True,
                timeout=timeout_seconds,
            )
            method = "GET_HEADERS_ONLY"
        status_code = response.status_code
        retry_after = parse_retry_after(response.headers.get("Retry-After"))
        content_type = str(response.headers.get("Content-Type") or "")
        content_length = response.headers.get("Content-Length")
        response.close()
        state = "available" if 200 <= status_code < 300 and content_type.startswith("image/") else "unexpected_response"
        if status_code == 404:
            state = "source_http_404"
        elif status_code == 401:
            state = "source_http_401"
        elif status_code == 403:
            state = "source_http_403"
        elif status_code == 429:
            state = "provider_rate_limited"
        return {
            "sourceUrlSha256": source_hash,
            "method": method,
            "httpStatus": status_code,
            "state": state,
            "contentType": content_type or None,
            "contentLength": int(content_length) if str(content_length).isdigit() else None,
            "cacheControlPresent": bool(response.headers.get("Cache-Control")),
            "retryAfterSeconds": retry_after,
            "bodyDownloaded": False,
        }
    except requests.RequestException as exc:
        return {
            "sourceUrlSha256": source_hash,
            "state": "provider_unavailable",
            "errorType": type(exc).__name__,
            "bodyDownloaded": False,
        }


def run_public_image_preflight(
    *,
    samples_per_language_region: int = 3,
    seed: str = "cardscanr-global-public-preflight-v1",
    request_interval_seconds: float = 0.25,
) -> dict[str, Any]:
    samples, candidate_counts, provider_counts = _deterministic_samples(
        sample_size=samples_per_language_region,
        seed=seed,
    )
    plan = {
        "schemaVersion": "1.0.0",
        "generatedAtUtc": utc_now_iso(),
        "samplesPerLanguageRegion": samples_per_language_region,
        "expectedRequests": sum(len(cards) for cards in samples.values()),
        "expectedApiCostUsd": 0,
        "expectedR2Writes": 0,
        "expectedR2StorageBytes": 0,
        "requestIntervalSeconds": request_interval_seconds,
        "bodyDownloadsPlanned": 0,
    }
    write_json_atomic(REPORT_DIR / "public_image_preflight_plan.json", plan)

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "image/*,*/*;q=0.1"})
    results: list[dict[str, Any]] = []
    state_counts: Counter[str] = Counter()
    last_request: float | None = None
    stop_reason: str | None = None
    for pair in sorted(samples):
        for card in samples[pair]:
            if last_request is not None:
                delay = request_interval_seconds - (time.monotonic() - last_request)
                if delay > 0:
                    time.sleep(delay)
            last_request = time.monotonic()
            result = _preflight_request(session, card["sourceUrl"])
            state_counts[result["state"]] += 1
            results.append(
                {
                    "canonicalPrintingId": card["canonicalPrintingId"],
                    "language": card["language"],
                    "region": card["region"],
                    "provider": card["provider"],
                    **result,
                }
            )
            retry_after = result.get("retryAfterSeconds")
            if result["state"] == "provider_rate_limited" and (
                retry_after is None or retry_after > 60
            ):
                stop_reason = "provider_rate_limit_wait_exceeds_preflight_bound"
                break
            if result["state"] == "provider_rate_limited" and retry_after:
                time.sleep(float(retry_after))
        if stop_reason:
            break

    available = state_counts["available"]
    performed = len(results)
    classification = (
        "PASS"
        if performed == plan["expectedRequests"] and available == performed
        else "PARTIAL"
    )
    payload = {
        "schemaVersion": "1.0.0",
        "generatedAtUtc": utc_now_iso(),
        "classification": classification,
        "scope": "HTTP-header preflight only; no image body retained or rehosted",
        "expectedRequests": plan["expectedRequests"],
        "requestsPerformed": performed,
        "available": available,
        "stateCounts": dict(sorted(state_counts.items())),
        "candidateCountsByLanguageRegion": [
            {
                "language": language,
                "region": region,
                "candidateCount": count,
            }
            for (language, region), count in sorted(candidate_counts.items())
        ],
        "candidateCountsByProvider": dict(sorted(provider_counts.items())),
        "stopReason": stop_reason,
        "sourceUrlsReported": False,
        "imageBodiesRetained": False,
        "r2Writes": 0,
        "results": results,
    }
    write_json_atomic(REPORT_DIR / "public_image_preflight_report.json", payload)
    (REPORT_DIR / "public_image_preflight_report.md").write_text(
        render_preflight_markdown(payload),
        encoding="utf-8",
    )
    return payload


def render_preflight_markdown(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Public Image Provider Preflight",
            "",
            f"Classification: **{payload['classification']}**",
            "",
            f"- Requests performed: {payload['requestsPerformed']} / {payload['expectedRequests']}",
            f"- Available image responses: {payload['available']}",
            f"- States: {payload['stateCounts']}",
            f"- Image bodies retained: {payload['imageBodiesRetained']}",
            f"- R2 writes: {payload['r2Writes']}",
            f"- Stop reason: {payload['stopReason']}",
            "",
            "Provider source URLs are represented only by SHA-256 in the JSON report.",
            "",
        ]
    )


def _load_supabase_config() -> tuple[str, str]:
    path = ROOT / "supabase_env.local.json"
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    url = str(payload.get("SUPABASE_URL") or "").rstrip("/")
    key = str(
        payload.get("SUPABASE_SECRET_KEY")
        or payload.get("SUPABASE_SERVICE_ROLE_KEY")
        or ""
    )
    if not url or not key:
        raise RuntimeError("Supabase URL/service credential is not configured")
    return url, key


def _supabase_image_rows() -> list[dict[str, Any]]:
    url, key = _load_supabase_config()
    response = requests.get(
        f"{url}/rest/v1/pokemon_card_image_records",
        params={
            "select": (
                "canonical_base_id,game,language,set_id,set_code,collector_number,"
                "provider_card_id,primary_provider,status,content_hash_sha256,"
                "thumb_storage_path,thumb_width,thumb_height,thumb_bytes,"
                "display_storage_path,display_width,display_height,display_bytes,verified_at"
            ),
            "status": "eq.completed",
            "order": "canonical_base_id.asc",
            "limit": "1000",
        },
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
        },
        timeout=60,
    )
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list):
        raise RuntimeError("Supabase image record response was not a list")
    sanitized_rows = [row for row in rows if isinstance(row, dict)]
    for row in sanitized_rows:
        thumb_path = str(row.get("thumb_storage_path") or "").lstrip("/")
        display_path = str(row.get("display_storage_path") or "").lstrip("/")
        row["_thumb_public_url"] = (
            f"{url}/storage/v1/object/public/pokemon-card-images/"
            f"{quote(thumb_path, safe='/')}"
            if thumb_path
            else ""
        )
        row["_display_public_url"] = (
            f"{url}/storage/v1/object/public/pokemon-card-images/"
            f"{quote(display_path, safe='/')}"
            if display_path
            else ""
        )
    return sanitized_rows


def _crosswalk_index() -> dict[tuple[str, str, str], set[tuple[str, str]]]:
    index: dict[tuple[str, str, str], set[tuple[str, str]]] = defaultdict(set)
    for row in iter_jsonl(CATALOGUE_DIR / "provider_crosswalk.jsonl"):
        provider = str(row.get("provider") or "")
        provider_card_id = str(row.get("providerCardId") or "")
        language = str(row.get("language") or "")
        printing_id = str(row.get("canonicalPrintingId") or "")
        region = str(row.get("region") or "")
        if provider and provider_card_id and language and printing_id:
            index[(provider, language, provider_card_id)].add(
                (printing_id, region)
            )
    return index


def _printing_set_index() -> dict[str, tuple[str, str]]:
    return {
        str(row["canonicalPrintingId"]): (
            str(row["canonicalSetId"]),
            str(row.get("cardVariant") or "unspecified"),
        )
        for row in iter_jsonl(CATALOGUE_DIR / "cards.jsonl")
    }


def _stream_public_object_checksum(
    session: requests.Session,
    url: str,
    *,
    timeout_seconds: int = 45,
) -> dict[str, Any]:
    try:
        response = session.get(url, stream=True, timeout=timeout_seconds)
        status = response.status_code
        if status != 200:
            response.close()
            return {"available": False, "httpStatus": status}
        digest = hashlib.sha256()
        byte_size = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if chunk:
                digest.update(chunk)
                byte_size += len(chunk)
        content_type = str(response.headers.get("Content-Type") or "")
        response.close()
        return {
            "available": True,
            "httpStatus": status,
            "sha256": digest.hexdigest(),
            "byteSize": byte_size,
            "contentType": content_type,
        }
    except requests.RequestException as exc:
        return {
            "available": False,
            "errorType": type(exc).__name__,
        }


def plan_supabase_to_r2_migration(
    *,
    verify_source_objects: bool = True,
) -> dict[str, Any]:
    rows = _supabase_image_rows()
    previous_payload: dict[str, Any] = {}
    previous_path = REPORT_DIR / "supabase_to_r2_migration.json"
    if previous_path.exists():
        try:
            loaded = json.loads(previous_path.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                previous_payload = loaded
        except (OSError, json.JSONDecodeError):
            previous_payload = {}
    previous_by_identity = {
        str(item.get("cardIdentity") or ""): item
        for item in previous_payload.get("records") or []
        if isinstance(item, dict) and item.get("cardIdentity")
    }
    crosswalk = _crosswalk_index()
    printing_sets = _printing_set_index()
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    results: list[dict[str, Any]] = []
    state_counts: Counter[str] = Counter()
    provider_counts: Counter[str] = Counter()
    identity_state_by_provider: dict[str, Counter[str]] = defaultdict(Counter)
    checksum_verified_by_provider: Counter[str] = Counter()
    source_available = 0
    display_source_available = 0
    source_checksum_verified = 0
    thumb_actual_checksums_computed = 0
    display_actual_checksums_computed = 0
    canonical_group_matched = 0

    for row in rows:
        provider = str(row.get("primary_provider") or "")
        provider_card_id = str(row.get("provider_card_id") or "")
        try:
            language = canonicalize_language(str(row.get("language") or ""))
        except ValueError:
            language = str(row.get("language") or "unknown")
        provider_counts[provider or "unknown"] += 1
        matches = crosswalk.get((provider, language, provider_card_id), set())
        identity_state = "identity_unresolved"
        target: tuple[str, str] | None = None
        if len(matches) == 1:
            target = next(iter(matches))
            canonical_group_matched += 1
            identity_state = "canonical_group_matched_variant_unresolved"
        elif len(matches) > 1:
            identity_state = "identity_ambiguous"
        identity_state_by_provider[provider or "unknown"][identity_state] += 1

        source_audit: dict[str, Any] = {"tested": False}
        display_audit: dict[str, Any] = {"tested": False}
        previous = previous_by_identity.get(str(row.get("canonical_base_id") or ""), {})
        thumb_url = str(row.get("_thumb_public_url") or "")
        if verify_source_objects and thumb_url:
            previous_thumb = previous.get("sourceAudit")
            if (
                previous.get("sourceThumbPath") == row.get("thumb_storage_path")
                and isinstance(previous_thumb, dict)
                and previous_thumb.get("available")
                and previous_thumb.get("sha256")
            ):
                source_audit = {
                    **previous_thumb,
                    "tested": True,
                    "reusedFromPriorImmutablePathAudit": True,
                }
            else:
                source_audit = {
                    "tested": True,
                    **_stream_public_object_checksum(session, thumb_url),
                }
            source_available += int(bool(source_audit.get("available")))
            expected_sha = str(row.get("content_hash_sha256") or "").casefold()
            actual_sha = str(source_audit.get("sha256") or "").casefold()
            hash_basis = (
                "display"
                if row.get("display_storage_path")
                else "thumb"
            )
            thumb_actual_checksums_computed += int(bool(actual_sha))
            source_audit["recordedContentHashBasis"] = hash_basis
            source_audit["recordedSha256Matches"] = (
                bool(expected_sha and actual_sha and expected_sha == actual_sha)
                if hash_basis == "thumb"
                else None
            )
            source_audit["recordedByteSizeMatches"] = bool(
                source_audit.get("available")
                and int(row.get("thumb_bytes") or 0) == int(source_audit.get("byteSize") or -1)
            )
        display_url = str(row.get("_display_public_url") or "")
        if verify_source_objects and display_url:
            previous_display = previous.get("sourceDisplayAudit")
            if (
                previous.get("sourceDisplayPath") == row.get("display_storage_path")
                and isinstance(previous_display, dict)
                and previous_display.get("available")
                and previous_display.get("sha256")
            ):
                display_audit = {
                    **previous_display,
                    "tested": True,
                    "reusedFromPriorImmutablePathAudit": True,
                }
            else:
                display_audit = {
                    "tested": True,
                    **_stream_public_object_checksum(session, display_url),
                }
            display_source_available += int(bool(display_audit.get("available")))
            expected_sha = str(row.get("content_hash_sha256") or "").casefold()
            actual_sha = str(display_audit.get("sha256") or "").casefold()
            display_actual_checksums_computed += int(bool(actual_sha))
            display_audit["recordedSha256Matches"] = bool(
                expected_sha and actual_sha and expected_sha == actual_sha
            )
            display_audit["recordedByteSizeMatches"] = bool(
                display_audit.get("available")
                and int(row.get("display_bytes") or 0)
                == int(display_audit.get("byteSize") or -1)
            )
        content_hash_verified = bool(
            display_audit.get("recordedSha256Matches")
            if row.get("display_storage_path")
            else source_audit.get("recordedSha256Matches")
        )
        source_checksum_verified += int(content_hash_verified)
        checksum_verified_by_provider[provider or "unknown"] += int(content_hash_verified)

        planned_path = None
        if target is not None and row.get("content_hash_sha256"):
            printing_id, region = target
            set_and_variant = printing_sets.get(printing_id)
            if set_and_variant is not None:
                planned_path = build_global_image_path(
                    language=language,
                    region=region,
                    canonical_set_id=set_and_variant[0],
                    canonical_printing_id=printing_id,
                    content_hash_sha256=str(row["content_hash_sha256"]),
                    variant="thumb",
                )

        if identity_state == "identity_ambiguous":
            migration_state = "blocked_identity_ambiguous"
        elif identity_state == "identity_unresolved":
            migration_state = "blocked_identity_unresolved"
        elif target and printing_sets.get(target[0], (None, "unspecified"))[1] == "unspecified":
            migration_state = "blocked_variant_evidence_and_terms_review"
        elif not content_hash_verified:
            migration_state = "blocked_source_verification"
        else:
            migration_state = "blocked_terms_review"
        state_counts[migration_state] += 1

        results.append(
            {
                "cardIdentity": row.get("canonical_base_id"),
                "language": language,
                "provider": provider,
                "providerCardId": provider_card_id,
                "supabaseStatus": row.get("status"),
                "identityState": identity_state,
                "canonicalPrintingId": target[0] if target else None,
                "canonicalRegion": target[1] if target else None,
                "sourceThumbPath": row.get("thumb_storage_path"),
                "sourceAudit": source_audit,
                "sourceDisplayPath": row.get("display_storage_path"),
                "sourceDisplayAudit": display_audit,
                "recordedContentHashVerified": content_hash_verified,
                "plannedR2ThumbPath": planned_path,
                "displaySourceAvailable": bool(
                    row.get("display_storage_path") and row.get("display_bytes")
                ),
                "migrationState": migration_state,
                "r2Verified": False,
            }
        )

    payload = {
        "schemaVersion": "1.0.0",
        "generatedAtUtc": utc_now_iso(),
        "classification": "BLOCKED",
        "scope": "non-destructive audit and migration plan",
        "supabaseRecordsAudited": len(rows),
        "reportedExpectedRecords": 591,
        "recordCountMatchesReported": len(rows) == 591,
        "providerBreakdown": dict(sorted(provider_counts.items())),
        "identityStateByProvider": {
            provider: dict(sorted(counts.items()))
            for provider, counts in sorted(identity_state_by_provider.items())
        },
        "checksumVerifiedByProvider": dict(
            sorted(checksum_verified_by_provider.items())
        ),
        "canonicalPrintingGroupsMatched": canonical_group_matched,
        "sourceObjectsAvailable": source_available,
        "displaySourceObjectsAvailable": display_source_available,
        "thumbActualChecksumsComputed": thumb_actual_checksums_computed,
        "displayActualChecksumsComputed": display_actual_checksums_computed,
        "sourceChecksumsVerified": source_checksum_verified,
        "migrationStateCounts": dict(sorted(state_counts.items())),
        "migratedAndR2Verified": 0,
        "r2WritesPerformed": 0,
        "existingSupabaseObjectsDeleted": 0,
        "blockers": [
            "All canonical TCGdex records still have cardVariant=unspecified; exact physical finish identity is not proven.",
            "Provider artwork rehosting permission remains pending human review.",
            "No object may be marked migrated until an R2 HEAD/checksum verification succeeds.",
        ],
        "estimatedFutureR2Writes": {
            "thumbs": sum(int(result["plannedR2ThumbPath"] is not None) for result in results),
            "displays": sum(int(result["displaySourceAvailable"]) for result in results),
        },
        "records": results,
        "writeExecutionCommandExposed": False,
        "gateRecheckCommand": "python tools/global_rollout.py plan-migration",
    }
    write_json_atomic(REPORT_DIR / "supabase_to_r2_migration.json", payload)
    (REPORT_DIR / "supabase_to_r2_migration.md").write_text(
        render_migration_markdown(payload),
        encoding="utf-8",
    )
    return payload


def render_migration_markdown(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Supabase-to-R2 Thumbnail Migration Plan",
            "",
            f"Classification: **{payload['classification']}**",
            "",
            f"- Supabase records audited: {payload['supabaseRecordsAudited']}",
            f"- Matches reported 591: {payload['recordCountMatchesReported']}",
            f"- Canonical printing groups matched: {payload['canonicalPrintingGroupsMatched']}",
            f"- Public source objects available: {payload['sourceObjectsAvailable']}",
            f"- Source checksums verified: {payload['sourceChecksumsVerified']}",
            f"- Migrated and R2 verified: {payload['migratedAndR2Verified']}",
            f"- R2 writes performed: {payload['r2WritesPerformed']}",
            f"- State counts: {payload['migrationStateCounts']}",
            "",
            "Migration is intentionally not executed while physical variant identity and artwork rehosting terms remain unresolved.",
            "",
        ]
    )
