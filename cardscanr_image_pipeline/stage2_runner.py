from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

import requests
from PIL import Image, ImageDraw, ImageFont

from .config import ImagePipelineConfig
from .database import SupabaseImageRecordClient
from .identity import sha256_hex
from .matching import ProviderResolution, resolve_provider_with_trace
from .providers.registry import build_default_provider_chain
from .models import CardImageIdentity, ProcessedCardImages
from .paths import build_storage_paths, public_storage_url, version_directory_name
from .processing import (
    ImageValidationError,
    decode_and_validate_card_image,
    download_image_bytes,
    process_downloaded_image,
)
from .sample_manifest import identities_for_manifest, load_sample_manifest
from .storage import SupabaseImageStorageClient


@dataclass
class Stage2CardReport:
    canonical_base_id: str
    language: str
    set_id: str
    collector_number: str
    bucket: str | None
    edge_case_tag: str | None
    provider: str | None
    fallback_provider: str | None
    provider_card_id: str | None
    provider_set_id: str | None
    source_url: str | None
    source_http_status: int | None
    source_content_type: str | None
    source_byte_count: int | None
    source_sha256: str | None
    decoded_width: int | None
    decoded_height: int | None
    thumb_width: int | None
    thumb_height: int | None
    display_width: int | None
    display_height: int | None
    thumb_byte_count: int | None
    display_byte_count: int | None
    content_hash_sha256: str | None
    thumb_storage_path: str | None
    display_storage_path: str | None
    thumb_public_url: str | None
    display_public_url: str | None
    database_status: str
    failure_reason: str | None
    ambiguous: bool
    expected_db_action: str | None
    elapsed_ms: int
    skipped_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Stage2Runner:
    def __init__(self, config: ImagePipelineConfig) -> None:
        self.config = config
        self.db = SupabaseImageRecordClient(
            supabase_url=config.supabase_url,
            service_role_key=config.supabase_secret_key,
            timeout_seconds=config.timeout_seconds,
        )
        self.storage = SupabaseImageStorageClient(
            supabase_url=config.supabase_url,
            service_role_key=config.supabase_secret_key,
            bucket_name=config.bucket_name,
            timeout_seconds=config.timeout_seconds,
        )
        self.http = requests.Session()
        self.http.headers.update({"User-Agent": "CardScanR-ImagePipeline/0.2"})

    def dry_run_manifest(self, manifest: dict[str, Any]) -> dict[str, Any]:
        entries_by_id = {entry["canonicalBaseId"]: entry for entry in manifest.get("entries") or []}
        identities = identities_for_manifest(manifest)
        cards: list[dict[str, Any]] = []
        ambiguous_count = 0
        duplicate_paths: dict[str, str] = {}
        provider_totals: dict[str, int] = {}
        estimated_source_bytes = 0
        stop_reasons: list[str] = []

        for identity in identities:
            entry = entries_by_id[identity.canonical_base_id]
            started = time.perf_counter()
            resolution = resolve_provider_with_trace(identity, source_card=identity.source_card)
            report = self._base_report(identity, entry, resolution)
            report.elapsed_ms = int((time.perf_counter() - started) * 1000)

            if resolution.ambiguous:
                ambiguous_count += 1
                report.database_status = "ambiguous"
                report.failure_reason = resolution.ambiguity_reason
                stop_reasons.append(f"ambiguous:{identity.canonical_base_id}")
            elif resolution.candidate is None:
                report.database_status = "no_match"
                report.failure_reason = "no_provider_match"
                stop_reasons.append(f"no_match:{identity.canonical_base_id}")
            else:
                report.provider = resolution.candidate.provider
                report.fallback_provider = resolution.fallback_provider
                report.provider_card_id = resolution.candidate.provider_card_id
                report.provider_set_id = resolution.candidate.provider_set_id
                report.source_url = resolution.candidate.source_url_display
                report.database_status = "dry_run_planned_upsert"
                report.expected_db_action = "upsert_completed_record"
                provider_totals[report.provider] = provider_totals.get(report.provider, 0) + 1
                placeholder_hash = sha256_hex(
                    f"{identity.canonical_base_id}|{resolution.candidate.source_url_display}".encode("utf-8")
                )
                thumb_path, display_path = build_storage_paths(
                    identity,
                    content_hash_sha256=placeholder_hash,
                    bucket_name=self.config.bucket_name,
                )
                report.content_hash_sha256 = placeholder_hash
                report.thumb_storage_path = thumb_path
                report.display_storage_path = display_path
                report.thumb_public_url = public_storage_url(self.config.supabase_url, self.config.bucket_name, thumb_path)
                report.display_public_url = public_storage_url(
                    self.config.supabase_url, self.config.bucket_name, display_path
                )
                for path in (thumb_path, display_path):
                    if path in duplicate_paths and duplicate_paths[path] != identity.canonical_base_id:
                        stop_reasons.append(f"duplicate_path:{path}")
                    duplicate_paths[path] = identity.canonical_base_id
                if entry.get("provider") and report.provider != entry.get("provider"):
                    stop_reasons.append(
                        f"provider_mismatch:{identity.canonical_base_id}:{entry.get('provider')}->{report.provider}"
                    )
                estimated_source_bytes += 120_000

            cards.append(report.to_dict())

        return {
            "mode": "dry_run",
            "generatedAtUtc": _utc_now_iso(),
            "cardCount": len(cards),
            "ambiguousCount": ambiguous_count,
            "providerTotals": provider_totals,
            "estimatedSourceBytes": estimated_source_bytes,
            "stopReasons": stop_reasons,
            "shouldStop": bool(stop_reasons),
            "cards": cards,
        }

    def execute_manifest(self, manifest: dict[str, Any]) -> dict[str, Any]:
        entries_by_id = {entry["canonicalBaseId"]: entry for entry in manifest.get("entries") or []}
        identities = identities_for_manifest(manifest)
        cards: list[dict[str, Any]] = []
        for identity in identities:
            entry = entries_by_id[identity.canonical_base_id]
            started = time.perf_counter()
            cards.append(self._execute_identity(identity, entry, started).to_dict())
        return self._summarize_execution(cards)

    def _execute_identity(self, identity: CardImageIdentity, entry: dict[str, Any], started: float) -> Stage2CardReport:
        resolution = resolve_provider_with_trace(identity, source_card=identity.source_card)
        report = self._base_report(identity, entry, resolution)
        existing = self.db.get_record(identity.canonical_base_id)
        if existing and existing.get("status") in {"completed", "verified"}:
            report.database_status = "skipped"
            report.skipped_reason = "already_completed"
            report.elapsed_ms = int((time.perf_counter() - started) * 1000)
            return report

        providers = build_default_provider_chain(identity.language)
        last_error: str | None = None
        for provider_index, provider in enumerate(providers):
            try:
                candidate = provider.resolve(
                    identity if identity.source_card is None else identity.__class__(
                        **{
                            field: getattr(identity, field)
                            for field in (
                                "canonical_base_id",
                                "game",
                                "language",
                                "set_id",
                                "set_code",
                                "collector_number",
                                "printed_card_number",
                                "local_card_number",
                                "set_total",
                                "printed_total",
                                "provider_set_id",
                                "provider_ids",
                                "image_source",
                                "catalogue_image_small",
                                "catalogue_image_large",
                                "serie_id",
                            )
                        },
                        source_card=identity.source_card,
                    )
                )
            except Exception as exc:
                last_error = str(exc)
                continue
            if candidate is None:
                continue
            fallback_provider = None if provider_index == 0 else provider.provider_id
            report.provider = candidate.provider
            report.fallback_provider = fallback_provider
            report.provider_card_id = candidate.provider_card_id
            report.provider_set_id = candidate.provider_set_id
            report.source_url = candidate.source_url_display
            try:
                source_bytes, content_type = self._download_with_display_fallback(candidate)
                report.source_http_status = 200
                report.source_content_type = content_type
                report.source_byte_count = len(source_bytes)
                report.source_sha256 = sha256_hex(source_bytes)
                decoded = decode_and_validate_card_image(source_bytes)
                report.decoded_width, report.decoded_height = decoded.size
                processed = process_downloaded_image(
                    source_bytes,
                    candidate,
                    fallback_provider=fallback_provider,
                    thumb_max_px=self.config.thumb_max_px,
                    display_max_px=self.config.display_max_px,
                )
                self._apply_processed(report, identity, processed, existing)
                report.elapsed_ms = int((time.perf_counter() - started) * 1000)
                return report
            except (ImageValidationError, requests.RequestException) as exc:
                last_error = str(exc)
                if "404" in last_error:
                    continue
                break

        report.database_status = "failed"
        report.failure_reason = last_error or resolution.ambiguity_reason or "no_provider_match"
        self.db.upsert_record(
            self.db.build_record_payload(
                identity,
                status="failed",
                failure_reason=report.failure_reason[:2000],
                retry_count=int(existing.get("retry_count") or 0) + 1 if existing else 1,
                cache_control=self.config.cache_control,
                existing=existing,
            ),
            dry_run=False,
        )
        report.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return report

    def _download_with_display_fallback(self, candidate) -> tuple[bytes, str]:
        urls = [candidate.source_url_display]
        if candidate.source_url_thumb and candidate.source_url_thumb not in urls:
            urls.append(candidate.source_url_thumb)
        last_error: Exception | None = None
        for url in urls:
            try:
                return download_image_bytes(
                    self.http,
                    url,
                    timeout_seconds=self.config.timeout_seconds,
                    max_retries=self.config.max_retries,
                    retry_base_seconds=self.config.retry_base_seconds,
                )
            except requests.RequestException as exc:
                last_error = exc
                if "404" in str(exc):
                    continue
                raise
        if last_error:
            raise last_error
        raise ImageValidationError("no downloadable source URL")

    def _apply_processed(
        self,
        report: Stage2CardReport,
        identity: CardImageIdentity,
        processed: ProcessedCardImages,
        existing: dict[str, Any] | None,
    ) -> None:
        thumb_path, display_path = build_storage_paths(
            identity,
            content_hash_sha256=processed.content_hash_sha256,
            bucket_name=self.config.bucket_name,
        )
        report.content_hash_sha256 = processed.content_hash_sha256
        report.thumb_width = processed.thumb.width
        report.thumb_height = processed.thumb.height
        report.display_width = processed.display.width
        report.display_height = processed.display.height
        report.thumb_byte_count = len(processed.thumb.data)
        report.display_byte_count = len(processed.display.data)
        report.thumb_storage_path = thumb_path
        report.display_storage_path = display_path
        report.thumb_public_url = public_storage_url(self.config.supabase_url, self.config.bucket_name, thumb_path)
        report.display_public_url = public_storage_url(self.config.supabase_url, self.config.bucket_name, display_path)

        thumb_exists_before = self.storage.object_exists(thumb_path)
        display_exists_before = self.storage.object_exists(display_path)
        thumb_status = self.storage.upload_if_absent(
            thumb_path,
            processed.thumb.data,
            content_type=processed.thumb.content_type,
            cache_control=self.config.cache_control,
            max_retries=self.config.max_retries,
            retry_base_seconds=self.config.retry_base_seconds,
            dry_run=False,
        )
        display_status = self.storage.upload_if_absent(
            display_path,
            processed.display.data,
            content_type=processed.display.content_type,
            cache_control=self.config.cache_control,
            max_retries=self.config.max_retries,
            retry_base_seconds=self.config.retry_base_seconds,
            dry_run=False,
        )
        if thumb_exists_before and thumb_status == "uploaded":
            raise ImageValidationError("immutable thumb object would have been overwritten")
        if display_exists_before and display_status == "uploaded":
            raise ImageValidationError("immutable display object would have been overwritten")

        self.db.upsert_record(
            self.db.build_record_payload(
                identity,
                status="completed",
                processed=processed,
                thumb_path=thumb_path,
                display_path=display_path,
                retry_count=int(existing.get("retry_count") or 0) if existing else 0,
                cache_control=self.config.cache_control,
                existing=existing,
            ),
            dry_run=False,
        )
        report.database_status = "completed"

    def _base_report(
        self,
        identity: CardImageIdentity,
        entry: dict[str, Any],
        resolution: ProviderResolution,
    ) -> Stage2CardReport:
        return Stage2CardReport(
            canonical_base_id=identity.canonical_base_id,
            language=identity.language,
            set_id=identity.set_id,
            collector_number=identity.collector_number,
            bucket=entry.get("bucket"),
            edge_case_tag=entry.get("edgeCaseTag"),
            provider=None,
            fallback_provider=None,
            provider_card_id=None,
            provider_set_id=None,
            source_url=None,
            source_http_status=None,
            source_content_type=None,
            source_byte_count=None,
            source_sha256=None,
            decoded_width=None,
            decoded_height=None,
            thumb_width=None,
            thumb_height=None,
            display_width=None,
            display_height=None,
            thumb_byte_count=None,
            display_byte_count=None,
            content_hash_sha256=None,
            thumb_storage_path=None,
            display_storage_path=None,
            thumb_public_url=None,
            display_public_url=None,
            database_status="pending",
            failure_reason=None,
            ambiguous=resolution.ambiguous,
            expected_db_action=None,
            elapsed_ms=0,
        )

    def _summarize_execution(self, cards: list[dict[str, Any]]) -> dict[str, Any]:
        provider_breakdown: dict[str, int] = {}
        language_breakdown: dict[str, int] = {}
        total_source_bytes = 0
        total_thumb_bytes = 0
        total_display_bytes = 0
        for card in cards:
            if card.get("provider"):
                provider_breakdown[card["provider"]] = provider_breakdown.get(card["provider"], 0) + 1
            language_breakdown[card["language"]] = language_breakdown.get(card["language"], 0) + 1
            total_source_bytes += int(card.get("source_byte_count") or 0)
            total_thumb_bytes += int(card.get("thumb_byte_count") or 0)
            total_display_bytes += int(card.get("display_byte_count") or 0)
        return {
            "mode": "execute",
            "generatedAtUtc": _utc_now_iso(),
            "attemptedCount": len(cards),
            "downloadedCount": sum(1 for card in cards if card.get("source_byte_count")),
            "uploadedCount": sum(1 for card in cards if card.get("database_status") == "completed"),
            "verifiedCount": 0,
            "skippedCount": sum(1 for card in cards if card.get("database_status") == "skipped"),
            "failedCount": sum(1 for card in cards if card.get("database_status") == "failed"),
            "ambiguousCount": sum(1 for card in cards if card.get("ambiguous")),
            "providerBreakdown": provider_breakdown,
            "languageBreakdown": language_breakdown,
            "totalSourceBytes": total_source_bytes,
            "totalThumbBytes": total_thumb_bytes,
            "totalDisplayBytes": total_display_bytes,
            "cards": cards,
        }

    def verify_manifest(self, manifest: dict[str, Any], *, execution_cards: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        entries = manifest.get("entries") or []
        issues: list[str] = []
        verified = 0
        checked_cards: list[dict[str, Any]] = []
        path_owner: dict[str, str] = {}
        provider_identity_owner: dict[str, str] = {}

        for entry in entries:
            canonical_id = entry["canonicalBaseId"]
            record = self.db.get_record(canonical_id)
            card_report: dict[str, Any] = {"canonicalBaseId": canonical_id, "issues": []}
            exec_card = next((item for item in (execution_cards or []) if item.get("canonical_base_id") == canonical_id), None)
            expected_completed = exec_card and exec_card.get("database_status") == "completed"

            if expected_completed:
                if not record:
                    card_report["issues"].append("missing_database_record")
                else:
                    for field in (
                        "thumb_storage_path",
                        "display_storage_path",
                        "content_hash_sha256",
                        "thumb_bytes",
                        "display_bytes",
                    ):
                        if not record.get(field):
                            card_report["issues"].append(f"missing_{field}")
                    provider_key = f"{record.get('primary_provider')}|{record.get('provider_card_id')}"
                    if provider_key in provider_identity_owner and provider_identity_owner[provider_key] != canonical_id:
                        card_report["issues"].append("duplicate_provider_identity")
                    provider_identity_owner[provider_key] = canonical_id
                    for label in ("thumb", "display"):
                        path = record.get(f"{label}_storage_path")
                        if not path:
                            continue
                        if path in path_owner and path_owner[path] != canonical_id:
                            card_report["issues"].append(f"duplicate_storage_path_{label}")
                        path_owner[path] = canonical_id
                        public_url = public_storage_url(self.config.supabase_url, self.config.bucket_name, path)
                        response = self.http.get(public_url, timeout=self.config.timeout_seconds)
                        if response.status_code != 200:
                            card_report["issues"].append(f"{label}_public_http_{response.status_code}")
                            continue
                        content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                        if content_type != "image/webp":
                            card_report["issues"].append(f"{label}_content_type_{content_type}")
                        cache_control = response.headers.get("Cache-Control") or ""
                        if "max-age=31536000" not in cache_control and "immutable" not in cache_control:
                            card_report["issues"].append(f"{label}_cache_control")
                        if len(response.content) <= 0:
                            card_report["issues"].append(f"{label}_empty_body")
                        else:
                            try:
                                image = decode_and_validate_card_image(response.content)
                                width, height = image.size
                                if label == "thumb" and max(width, height) > self.config.thumb_max_px + 5:
                                    card_report["issues"].append("thumb_dimensions_too_large")
                                if label == "display" and max(width, height) > self.config.display_max_px + 5:
                                    card_report["issues"].append("display_dimensions_too_large")
                            except ImageValidationError as exc:
                                card_report["issues"].append(f"{label}_decode_failed:{exc}")
                        hash_prefix = version_directory_name(str(record.get("content_hash_sha256")))
                        if hash_prefix not in path:
                            card_report["issues"].append(f"{label}_hash_path_mismatch")
            elif exec_card and exec_card.get("database_status") == "failed":
                if record and record.get("status") in {"completed", "verified"}:
                    card_report["issues"].append("failed_card_marked_completed")

            if card_report["issues"]:
                issues.extend([f"{canonical_id}:{issue}" for issue in card_report["issues"]])
            else:
                if expected_completed:
                    verified += 1
            checked_cards.append(card_report)

        return {
            "generatedAtUtc": _utc_now_iso(),
            "verifiedCount": verified,
            "issueCount": len(issues),
            "issues": issues,
            "cards": checked_cards,
            "passed": not issues,
        }

    def verify_idempotent_rerun(self, manifest: dict[str, Any]) -> dict[str, Any]:
        before = self.execute_manifest(manifest)
        downloaded = before.get("downloadedCount", 0)
        uploaded = before.get("uploadedCount", 0)
        skipped = before.get("skippedCount", 0)
        passed = downloaded == 0 and uploaded == 0 and skipped == before.get("attemptedCount", 0)
        return {
            "passed": passed,
            "downloadedCount": downloaded,
            "uploadedCount": uploaded,
            "skippedCount": skipped,
            "attemptedCount": before.get("attemptedCount", 0),
        }


def build_contact_sheet(cards: list[dict[str, Any]], *, output_path: Path, columns: int = 10) -> Path:
    thumbs: list[tuple[Image.Image, str]] = []
    session = requests.Session()
    for card in cards:
        if card.get("database_status") not in {"completed", "skipped"}:
            continue
        url = card.get("thumb_public_url")
        if not url:
            continue
        response = session.get(url, timeout=30)
        if response.status_code != 200:
            continue
        image = Image.open(BytesIO(response.content)).convert("RGB")
        label = f"{card.get('provider','?')}|{card.get('language','?')}\n{card.get('set_id')}/{card.get('collector_number')}"
        thumbs.append((image, label))
    if not thumbs:
        raise RuntimeError("no thumbnails available for contact sheet")
    thumb_w, thumb_h = 180, 250
    label_h = 36
    rows = (len(thumbs) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * thumb_w, rows * (thumb_h + label_h)), "white")
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
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path, format="PNG")
    return output_path


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_json_report(payload: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
