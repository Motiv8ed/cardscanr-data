#!/usr/bin/env python3
"""
Build the CardScanR card image manifest, and optionally download a bounded
local image batch for CDN/object-storage staging.

Default mode is manifest-only. It never downloads binaries unless --download
is explicitly provided.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Iterable
from datetime import datetime, timezone
from io import BytesIO
import json
import os
from pathlib import Path
import re
from typing import Any

import requests


ROOT = Path(__file__).resolve().parent.parent
V1_DIR = ROOT / "public" / "v1"
MANIFEST_PATH = V1_DIR / "images" / "cards-manifest.json"
DEFAULT_OUTPUT_ROOT = ROOT / ".cache" / "cardscanr-images"
POKEWALLET_PROVIDER_ROOT = V1_DIR / "provider-catalog" / "pokewallet" / "cards"
POKEWALLET_API_BASE = "https://api.pokewallet.io"
STATE_FILENAME = "image-cache-state.json"
SCHEMA_VERSION = "1.0.0"
DEFAULT_LANGUAGES = ("en", "jp")
ALLOWED_CACHE_STATUSES = {"remote_only", "cdn_ready", "cached", "failed", "skipped"}


class DownloadError(RuntimeError):
    """Raised for controlled per-image download failures."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> Any:
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def json_bytes(data: Any, *, compact: bool = False) -> bytes:
    if compact:
        return (json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def write_json_if_changed(path: Path, data: Any, *, compact: bool = False) -> bool:
    encoded = json_bytes(data, compact=compact)
    if path.exists() and path.read_bytes() == encoded:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_bytes(encoded)
    os.replace(tmp_path, path)
    return True


def normalize_base_url(value: str | None) -> str | None:
    if not value:
        return None
    stripped = value.strip().rstrip("/")
    return stripped or None


def safe_path_part(value: object) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-._")
    return text or "unknown"


def relative_or_absolute(path: Path, root: Path = ROOT) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def normalize_languages(value: str | None, *, include_zh: bool = False) -> list[str]:
    raw_items = [item.strip().lower() for item in str(value or "").split(",") if item.strip()]
    languages = raw_items or list(DEFAULT_LANGUAGES)
    if include_zh and "zh" not in languages:
        languages.append("zh")
    normalized: list[str] = []
    for language in languages:
        if language not in normalized:
            normalized.append(language)
    return normalized


def provider_endpoint_url(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    endpoint = value.strip()
    if endpoint.startswith(("http://", "https://")):
        return endpoint
    if not endpoint.startswith("/"):
        endpoint = f"/{endpoint}"
    return f"{POKEWALLET_API_BASE}{endpoint}"


def provider_ids_from_card(card: dict[str, Any]) -> dict[str, Any]:
    provider_ids = card.get("providerIds")
    if isinstance(provider_ids, dict):
        merged = dict(provider_ids)
    else:
        merged = {}
    external_ids = card.get("externalIds")
    if isinstance(external_ids, dict):
        merged.update({key: value for key, value in external_ids.items() if value is not None})
    return merged


def iter_catalog_cards(
    v1_dir: Path = V1_DIR,
    *,
    game: str = "pokemon",
    languages: list[str] | None = None,
    set_id: str | None = None,
) -> Iterable[dict[str, Any]]:
    selected_languages = languages or list(DEFAULT_LANGUAGES)
    for lang in selected_languages:
        cards_dir = v1_dir / "catalog" / game / lang / "cards"
        if not cards_dir.exists():
            continue
        for path in sorted(cards_dir.glob("*.json"), key=lambda item: item.name.lower()):
            if set_id and path.stem != set_id:
                continue
            payload = load_json(path)
            if not isinstance(payload, dict):
                continue
            cards = payload.get("cards")
            if not isinstance(cards, list):
                continue
            for card in cards:
                if not isinstance(card, dict):
                    continue
                if card.get("game") != game or card.get("language") != lang:
                    continue
                yield card


def cdn_image_url(cdn_base_url: str, card: dict[str, Any], size: str, image_format: str) -> str:
    game = safe_path_part(card.get("game"))
    language = safe_path_part(card.get("language"))
    set_id = safe_path_part(card.get("setId"))
    canonical_card_id = card.get("canonicalBaseId") or card.get("canonicalCardId")
    safe_card_id = safe_path_part(canonical_card_id)
    return f"{cdn_base_url}/cards/{game}/{language}/{set_id}/{safe_card_id}/{size}.{image_format}"


def local_image_path(output_root: Path, card: dict[str, Any], size: str, image_format: str) -> Path:
    return (
        output_root
        / "cards"
        / safe_path_part(card.get("game"))
        / safe_path_part(card.get("language"))
        / safe_path_part(card.get("setId"))
        / safe_path_part(card.get("canonicalBaseId") or card.get("canonicalCardId"))
        / f"{size}.{image_format}"
    )


def build_manifest_record(
    card: dict[str, Any],
    *,
    now_utc: str,
    cdn_base_url: str | None,
    image_format: str,
) -> dict[str, Any]:
    source_small = card.get("imageSmall")
    source_large = card.get("imageLarge")
    has_source_urls = bool(source_small) and bool(source_large)
    cache_status = "cdn_ready" if cdn_base_url and has_source_urls else "remote_only"
    if not has_source_urls:
        cache_status = "skipped"

    image_small_url = cdn_image_url(cdn_base_url, card, "small", image_format) if cdn_base_url else source_small
    image_large_url = cdn_image_url(cdn_base_url, card, "large", image_format) if cdn_base_url else source_large

    return {
        "canonicalCardId": card.get("canonicalBaseId"),
        "game": card.get("game"),
        "language": card.get("language"),
        "setId": card.get("setId"),
        "setName": card.get("setName"),
        "collectorNumber": card.get("collectorNumber"),
        "normalizedName": card.get("normalizedName"),
        "imageSmallUrl": image_small_url,
        "imageLargeUrl": image_large_url,
        "sourceImageSmallUrl": source_small,
        "sourceImageLargeUrl": source_large,
        "imageSource": card.get("imageSource"),
        "imageCached": False,
        "localImageSmallPath": None,
        "localImageLargePath": None,
        "cacheStatus": cache_status,
        "lastCheckedAtUtc": now_utc,
        "providerIds": provider_ids_from_card(card),
        "sourceType": "app_catalogue",
        "provider": card.get("imageSource"),
        "providerSetId": card.get("setId"),
        "providerSetCode": card.get("setId"),
        "providerCardId": provider_ids_from_card(card).get("pokemonTcgApiId")
        or provider_ids_from_card(card).get("tcgdexCardId")
        or provider_ids_from_card(card).get("pokewallet"),
        "error": None if has_source_urls else "missing_source_image_url",
    }


def iter_provider_catalog_cards(
    v1_dir: Path = V1_DIR,
    *,
    game: str = "pokemon",
    languages: list[str],
    set_id: str | None = None,
    provider: str = "pokewallet",
) -> Iterable[dict[str, Any]]:
    if game != "pokemon" or provider != "pokewallet":
        return
    provider_root = v1_dir / "provider-catalog" / "pokewallet" / "cards"
    for language in languages:
        cards_dir = provider_root / language
        if not cards_dir.exists():
            continue
        for path in sorted(cards_dir.glob("*.json"), key=lambda item: item.name.lower()):
            if set_id and path.stem.lower() != set_id.lower():
                continue
            payload = load_json(path)
            if not isinstance(payload, dict):
                continue
            cards = payload.get("cards")
            if not isinstance(cards, list):
                continue
            for card in cards:
                if isinstance(card, dict):
                    yield card


def build_provider_manifest_record(
    card: dict[str, Any],
    *,
    now_utc: str,
    image_format: str,
) -> dict[str, Any] | None:
    identity_basis = card.get("imageCacheIdentityBasis")
    if not isinstance(identity_basis, dict):
        identity_basis = {}

    language = str(card.get("cardScanRLanguage") or identity_basis.get("language") or "").strip().lower()
    set_id = str(identity_basis.get("setId") or card.get("providerSetCode") or card.get("providerSetId") or "").strip()
    collector_number = str(identity_basis.get("collectorNumber") or card.get("cardNumber") or "").strip()
    normalized_name = str(identity_basis.get("normalizedName") or card.get("cleanName") or card.get("name") or "").strip()
    canonical_id = str(
        card.get("providerCanonicalImageKey")
        or card.get("cardScanRImageCacheCandidateKey")
        or card.get("imageCacheKey")
        or ""
    ).strip()
    if not canonical_id or not language or not set_id:
        return None

    source_small = provider_endpoint_url(card.get("imageEndpointLow") or card.get("imageEndpoint"))
    source_large = provider_endpoint_url(card.get("imageEndpointHigh") or card.get("imageEndpoint"))
    if not source_small or not source_large:
        return None

    return {
        "canonicalCardId": canonical_id,
        "game": "pokemon",
        "language": language,
        "setId": set_id,
        "setName": card.get("providerSetName"),
        "collectorNumber": collector_number,
        "normalizedName": normalized_name,
        "imageSmallUrl": source_small,
        "imageLargeUrl": source_large,
        "sourceImageSmallUrl": source_small,
        "sourceImageLargeUrl": source_large,
        "imageSource": "pokewallet",
        "imageCached": False,
        "localImageSmallPath": None,
        "localImageLargePath": None,
        "cacheStatus": "skipped",
        "lastCheckedAtUtc": now_utc,
        "providerIds": {
            "pokewalletId": card.get("providerCardId"),
            "pokewalletSetId": card.get("providerSetId"),
            "pokewalletSetCode": card.get("providerSetCode"),
            "providerLanguage": card.get("providerLanguage"),
        },
        "sourceType": "provider_catalogue",
        "provider": "pokewallet",
        "providerSetId": card.get("providerSetId"),
        "providerSetCode": card.get("providerSetCode"),
        "providerCardId": card.get("providerCardId"),
        "providerImageEndpointLow": card.get("imageEndpointLow"),
        "providerImageEndpointHigh": card.get("imageEndpointHigh"),
        "imageFormatHint": image_format,
        "error": "pokewallet_image_endpoint_requires_api_key_or_proxy",
    }


def add_manifest_counts(manifest: dict[str, Any]) -> dict[str, Any]:
    records = [record for record in manifest.get("records", []) if isinstance(record, dict)]
    manifest["recordCount"] = len(records)
    manifest["languageCountMap"] = dict(
        sorted(Counter(str(record.get("language") or "unknown") for record in records).items())
    )
    manifest["imageSourceCounts"] = dict(
        sorted(Counter(str(record.get("imageSource") or "unknown") for record in records).items())
    )
    manifest["cacheStatusCounts"] = dict(
        sorted(Counter(str(record.get("cacheStatus") or "missing") for record in records).items())
    )
    manifest["sourceTypeCounts"] = dict(
        sorted(Counter(str(record.get("sourceType") or "unknown") for record in records).items())
    )
    return manifest


def preserve_manifest_timestamps_if_materially_same(path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return manifest
    try:
        previous = load_json(path)
    except (OSError, json.JSONDecodeError):
        return manifest
    if not isinstance(previous, dict):
        return manifest

    def without_volatile(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: without_volatile(item)
                for key, item in value.items()
                if key not in {"generatedAtUtc", "lastCheckedAtUtc", "downloadSummary"}
            }
        if isinstance(value, list):
            return [without_volatile(item) for item in value]
        return value

    if without_volatile(previous) != without_volatile(manifest):
        return manifest

    previous_generated = previous.get("generatedAtUtc")
    if isinstance(previous_generated, str) and previous_generated:
        manifest["generatedAtUtc"] = previous_generated
    previous_records = {
        str(record.get("canonicalCardId")): record
        for record in previous.get("records", [])
        if isinstance(record, dict) and record.get("canonicalCardId")
    }
    for record in manifest.get("records", []):
        if not isinstance(record, dict):
            continue
        previous_record = previous_records.get(str(record.get("canonicalCardId") or ""))
        if isinstance(previous_record, dict) and isinstance(previous_record.get("lastCheckedAtUtc"), str):
            record["lastCheckedAtUtc"] = previous_record["lastCheckedAtUtc"]
    return manifest


def build_manifest(
    *,
    v1_dir: Path = V1_DIR,
    game: str = "pokemon",
    language: str | None = None,
    languages: list[str] | None = None,
    set_id: str | None = None,
    cdn_base_url: str | None = None,
    image_format: str = "webp",
    now_utc: str | None = None,
    provider_languages: list[str] | None = None,
) -> dict[str, Any]:
    now = now_utc or utc_now_iso()
    normalized_cdn = normalize_base_url(cdn_base_url)
    selected_languages = languages or ([language] if language else list(DEFAULT_LANGUAGES))
    records_by_id: dict[str, dict[str, Any]] = {}
    for card in iter_catalog_cards(v1_dir, game=game, languages=selected_languages, set_id=set_id):
        record = build_manifest_record(card, now_utc=now, cdn_base_url=normalized_cdn, image_format=image_format)
        canonical_id = str(record.get("canonicalCardId") or "")
        if canonical_id:
            records_by_id[canonical_id] = record

    for card in iter_provider_catalog_cards(
        v1_dir,
        game=game,
        languages=provider_languages or [],
        set_id=set_id,
    ):
        record = build_provider_manifest_record(card, now_utc=now, image_format=image_format)
        if not record:
            continue
        canonical_id = str(record.get("canonicalCardId") or "")
        if canonical_id and canonical_id not in records_by_id:
            records_by_id[canonical_id] = record

    records = list(records_by_id.values())
    records.sort(
        key=lambda item: (
            str(item.get("game") or ""),
            str(item.get("language") or ""),
            str(item.get("setId") or ""),
            str(item.get("collectorNumber") or ""),
            str(item.get("canonicalCardId") or ""),
        )
    )
    return add_manifest_counts({
        "schemaVersion": SCHEMA_VERSION,
        "generatedAtUtc": now,
        "mode": "manifest_only",
        "cdnBaseUrl": normalized_cdn,
        "imageFormat": image_format,
        "recordCount": len(records),
        "records": records,
    })


def load_state(output_root: Path) -> dict[str, Any]:
    state_path = output_root / STATE_FILENAME
    if not state_path.exists():
        return {"schemaVersion": SCHEMA_VERSION, "completedRecordKeys": []}
    data = load_json(state_path)
    if not isinstance(data, dict):
        return {"schemaVersion": SCHEMA_VERSION, "completedRecordKeys": []}
    if not isinstance(data.get("completedRecordKeys"), list):
        data["completedRecordKeys"] = []
    return data


def write_state(output_root: Path, state: dict[str, Any]) -> None:
    state["updatedAtUtc"] = utc_now_iso()
    write_json(output_root / STATE_FILENAME, state)


def response_image_format(content_type: str) -> str | None:
    normalized = content_type.split(";", 1)[0].strip().lower()
    if normalized in {"image/jpeg", "image/jpg"}:
        return "jpg"
    if normalized == "image/png":
        return "png"
    if normalized == "image/webp":
        return "webp"
    if normalized == "image/gif":
        return "gif"
    return None


def convert_image_bytes(raw: bytes, source_format: str | None, target_format: str) -> bytes:
    if source_format == target_format or (source_format == "jpg" and target_format == "jpg"):
        return raw
    try:
        from PIL import Image  # type: ignore
    except ImportError as exc:
        raise DownloadError(
            f"format conversion from {source_format or 'unknown'} to {target_format} requires Pillow"
        ) from exc

    with Image.open(BytesIO(raw)) as image:
        output = BytesIO()
        if target_format == "jpg":
            if image.mode in {"RGBA", "LA"}:
                background = Image.new("RGB", image.size, (255, 255, 255))
                background.paste(image, mask=image.getchannel("A"))
                image = background
            else:
                image = image.convert("RGB")
            image.save(output, format="JPEG", quality=90, optimize=True)
        elif target_format == "webp":
            image.save(output, format="WEBP", quality=85, method=6)
        else:
            raise DownloadError(f"unsupported target image format: {target_format}")
        return output.getvalue()


def download_image(url: str, output_path: Path, *, image_format: str, timeout_seconds: int = 20) -> None:
    try:
        response = requests.get(url, stream=True, timeout=timeout_seconds, headers={"User-Agent": "CardScanR-image-cache/1.0"})
    except requests.Timeout as exc:
        raise DownloadError("timeout") from exc
    except requests.RequestException as exc:
        raise DownloadError(f"request_error: {exc}") from exc

    if response.status_code in {403, 404, 429}:
        raise DownloadError(f"http_{response.status_code}")
    if response.status_code >= 400:
        raise DownloadError(f"http_{response.status_code}")

    content_type = response.headers.get("content-type", "")
    if not content_type.lower().startswith("image/"):
        raise DownloadError(f"unexpected_content_type: {content_type or 'missing'}")

    raw = response.content
    if not raw:
        raise DownloadError("empty_response_body")

    source_format = response_image_format(content_type)
    converted = convert_image_bytes(raw, source_format, image_format)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as fh:
        fh.write(converted)


def update_manifest_for_downloads(
    manifest: dict[str, Any],
    *,
    output_root: Path,
    batch_size: int,
    max_images: int | None,
    image_format: str,
) -> dict[str, Any]:
    records = manifest.get("records")
    if not isinstance(records, list):
        raise ValueError("manifest records must be a list")

    max_to_download = max_images if max_images is not None else max(batch_size * 2, 1)
    state = load_state(output_root)
    completed = {str(item) for item in state.get("completedRecordKeys", [])}
    attempted_records = 0
    downloaded_images = 0
    failed_images = 0

    for record in records:
        if attempted_records >= batch_size or downloaded_images + failed_images >= max_to_download:
            break
        if not isinstance(record, dict):
            continue
        record_key = str(record.get("canonicalCardId") or "")
        if not record_key or record_key in completed:
            continue
        if record.get("cacheStatus") == "skipped":
            completed.add(record_key)
            continue

        attempted_records += 1
        record_error: str | None = None
        for size, source_field, local_field in [
            ("small", "sourceImageSmallUrl", "localImageSmallPath"),
            ("large", "sourceImageLargeUrl", "localImageLargePath"),
        ]:
            if downloaded_images + failed_images >= max_to_download:
                break
            source_url = record.get(source_field)
            if not isinstance(source_url, str) or not source_url:
                record_error = f"missing_{source_field}"
                failed_images += 1
                continue
            path = local_image_path(output_root, record, size, image_format)
            try:
                download_image(source_url, path, image_format=image_format)
                record[local_field] = relative_or_absolute(path)
                downloaded_images += 1
            except DownloadError as exc:
                record_error = f"{size}: {exc}"
                failed_images += 1

        if record_error:
            record["cacheStatus"] = "failed"
            record["imageCached"] = False
            record["error"] = record_error
        elif record.get("localImageSmallPath") and record.get("localImageLargePath"):
            record["cacheStatus"] = "cached"
            record["imageCached"] = True
            record["error"] = None
            completed.add(record_key)
        record["lastCheckedAtUtc"] = utc_now_iso()
        state["lastRecordKey"] = record_key
        state["completedRecordKeys"] = sorted(completed)
        state["lastRun"] = {
            "attemptedRecords": attempted_records,
            "downloadedImages": downloaded_images,
            "failedImages": failed_images,
            "batchSize": batch_size,
            "maxImages": max_to_download,
        }
        write_state(output_root, state)

    manifest["mode"] = "download" if attempted_records else "manifest_only"
    manifest["recordCount"] = len(records)
    manifest["downloadSummary"] = {
        "attemptedRecords": attempted_records,
        "downloadedImages": downloaded_images,
        "failedImages": failed_images,
        "outputRoot": relative_or_absolute(output_root),
        "statePath": relative_or_absolute(output_root / STATE_FILENAME),
    }
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build CardScanR card image manifest/cache metadata.")
    parser.add_argument("--manifest-only", action="store_true", help="Generate only cards-manifest.json. This is the default.")
    parser.add_argument("--download", action="store_true", help="Download a bounded local batch and update manifest records.")
    parser.add_argument("--batch-size", type=int, default=20, help="Maximum card records to attempt in one download run.")
    parser.add_argument("--max-images", type=int, default=None, help="Maximum individual image files to download in one run.")
    parser.add_argument("--set-id", default=None, help="Limit to one catalogue set id.")
    parser.add_argument("--language", default=None, help="Limit to one language.")
    parser.add_argument("--languages", default=None, help="Comma-separated languages to include. Defaults to en,jp.")
    parser.add_argument("--include-zh", action="store_true", help="Include ZH provider image references when app support is not enabled.")
    parser.add_argument(
        "--include-provider-languages",
        default=None,
        help="Comma-separated provider-catalogue languages to include as image references.",
    )
    parser.add_argument("--game", default="pokemon", help="Limit to one game. Currently pokemon is supported.")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT), help="Local image output root for --download.")
    parser.add_argument("--format", choices=("jpg", "webp"), default="webp", help="Target cached/CDN image format.")
    parser.add_argument("--manifest-path", default=str(MANIFEST_PATH), help="Output manifest path.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.batch_size <= 0:
        raise SystemExit("--batch-size must be greater than zero")
    if args.max_images is not None and args.max_images <= 0:
        raise SystemExit("--max-images must be greater than zero")
    if args.game != "pokemon":
        raise SystemExit("Only --game pokemon is currently supported")

    languages_value = args.language or args.languages
    languages = normalize_languages(languages_value, include_zh=False)
    provider_languages = (
        normalize_languages(args.include_provider_languages, include_zh=False)
        if args.include_provider_languages
        else []
    )
    if args.include_zh and "zh" not in provider_languages:
        provider_languages.append("zh")

    manifest_path = Path(args.manifest_path)
    if not manifest_path.is_absolute():
        manifest_path = ROOT / manifest_path
    cdn_base_url = os.getenv("CARDSCANR_IMAGE_CDN_BASE_URL")
    if not cdn_base_url and manifest_path.exists():
        previous_manifest = load_json(manifest_path)
        if isinstance(previous_manifest, dict) and isinstance(previous_manifest.get("cdnBaseUrl"), str):
            cdn_base_url = previous_manifest.get("cdnBaseUrl")

    manifest = build_manifest(
        game=args.game,
        languages=languages,
        set_id=args.set_id,
        cdn_base_url=cdn_base_url,
        image_format=args.format,
        provider_languages=provider_languages,
    )

    if args.download:
        output_root = Path(args.output_root)
        if not output_root.is_absolute():
            output_root = ROOT / output_root
        manifest = update_manifest_for_downloads(
            manifest,
            output_root=output_root,
            batch_size=args.batch_size,
            max_images=args.max_images,
            image_format=args.format,
        )

    manifest = preserve_manifest_timestamps_if_materially_same(manifest_path, manifest)
    changed = write_json_if_changed(manifest_path, manifest, compact=True)
    action = "Wrote" if changed else "Unchanged"
    print(f"{action} {relative_or_absolute(manifest_path)}")
    print(f"Records: {manifest.get('recordCount', 0)}")
    print(f"Languages: {manifest.get('languageCountMap', {})}")
    print(f"Mode: {manifest.get('mode')}")
    print(f"Downloads enabled: {'yes' if args.download else 'no'}")


if __name__ == "__main__":
    main()
