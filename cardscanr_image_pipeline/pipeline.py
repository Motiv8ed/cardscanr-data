from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from .catalogue import DEFAULT_CATALOGUE_ROOT, iter_catalogue_identities
from .config import ImagePipelineConfig
from .database import SupabaseImageRecordClient
from .matching import resolve_provider_image
from .models import CardImageIdentity, PipelineCardResult
from .paths import build_storage_paths
from .processing import ImageValidationError, process_provider_candidate
from .storage import SupabaseImageStorageClient


@dataclass
class PipelineRunSummary:
    dry_run: bool
    total_candidates: int = 0
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    resumed: int = 0
    uploaded: int = 0
    already_present: int = 0
    results: list[PipelineCardResult] = field(default_factory=list)
    failures_by_reason: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dryRun": self.dry_run,
            "totalCandidates": self.total_candidates,
            "completed": self.completed,
            "failed": self.failed,
            "skipped": self.skipped,
            "resumed": self.resumed,
            "uploaded": self.uploaded,
            "alreadyPresent": self.already_present,
            "failuresByReason": dict(sorted(self.failures_by_reason.items())),
        }


class ImageIngestionPipeline:
    def __init__(self, config: ImagePipelineConfig, *, catalogue_root: Path | None = None) -> None:
        self.config = config
        self.catalogue_root = catalogue_root or DEFAULT_CATALOGUE_ROOT
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
        self.http.headers.update({"User-Agent": "CardScanR-ImagePipeline/0.1"})

    def iter_identities(self, *, set_id: str | None = None) -> list[CardImageIdentity]:
        return list(
            iter_catalogue_identities(
                self.catalogue_root,
                languages=self.config.languages,
                set_id=set_id,
                sample_limit=self.config.sample_limit,
            )
        )

    def process_identity(self, identity: CardImageIdentity) -> PipelineCardResult:
        existing = None if self.config.dry_run else self.db.get_record(identity.canonical_base_id)
        if existing and existing.get("status") in {"completed", "verified"}:
            return PipelineCardResult(
                identity=identity,
                status="skipped",
                failure_reason="already_completed",
                retry_count=int(existing.get("retry_count") or 0),
                dry_run=self.config.dry_run,
            )

        candidate, fallback_provider = resolve_provider_image(identity)
        if candidate is None:
            return self._record_failure(
                identity,
                existing=existing,
                reason="no_provider_match",
                retry_count=int(existing.get("retry_count") or 0) if existing else 0,
            )

        if self.config.dry_run:
            return PipelineCardResult(
                identity=identity,
                status="completed",
                failure_reason=None,
                retry_count=int(existing.get("retry_count") or 0) if existing else 0,
                dry_run=True,
            )

        try:
            processed = process_provider_candidate(
                self.http,
                candidate,
                fallback_provider=fallback_provider,
                thumb_max_px=self.config.thumb_max_px,
                display_max_px=self.config.display_max_px,
                timeout_seconds=self.config.timeout_seconds,
                max_retries=self.config.max_retries,
                retry_base_seconds=self.config.retry_base_seconds,
            )
        except (ImageValidationError, requests.RequestException) as exc:
            retry_count = int(existing.get("retry_count") or 0) + 1 if existing else 1
            return self._record_failure(identity, existing=existing, reason=str(exc), retry_count=retry_count)

        thumb_path, display_path = build_storage_paths(
            identity,
            content_hash_sha256=processed.content_hash_sha256,
            bucket_name=self.config.bucket_name,
        )

        if existing and existing.get("content_hash_sha256") == processed.content_hash_sha256:
            self.db.upsert_record(
                self.db.build_record_payload(
                    identity,
                    status="completed",
                    processed=processed,
                    thumb_path=thumb_path,
                    display_path=display_path,
                    retry_count=int(existing.get("retry_count") or 0),
                    cache_control=self.config.cache_control,
                    existing=existing,
                ),
                dry_run=self.config.dry_run,
            )
            return PipelineCardResult(
                identity=identity,
                status="resumed",
                processed=processed,
                retry_count=int(existing.get("retry_count") or 0),
                dry_run=self.config.dry_run,
            )

        thumb_status = self.storage.upload_if_absent(
            thumb_path,
            processed.thumb.data,
            content_type=processed.thumb.content_type,
            cache_control=self.config.cache_control,
            max_retries=self.config.max_retries,
            retry_base_seconds=self.config.retry_base_seconds,
            dry_run=self.config.dry_run,
        )
        display_status = self.storage.upload_if_absent(
            display_path,
            processed.display.data,
            content_type=processed.display.content_type,
            cache_control=self.config.cache_control,
            max_retries=self.config.max_retries,
            retry_base_seconds=self.config.retry_base_seconds,
            dry_run=self.config.dry_run,
        )

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
            dry_run=self.config.dry_run,
        )
        result = PipelineCardResult(
            identity=identity,
            status="completed",
            processed=processed,
            retry_count=int(existing.get("retry_count") or 0) if existing else 0,
            dry_run=self.config.dry_run,
        )
        result_upload_note = thumb_status, display_status
        _ = result_upload_note
        return result

    def run(self, *, set_id: str | None = None) -> PipelineRunSummary:
        identities = self.iter_identities(set_id=set_id)
        summary = PipelineRunSummary(dry_run=self.config.dry_run, total_candidates=len(identities))
        if not identities:
            return summary

        with ThreadPoolExecutor(max_workers=self.config.network_concurrency) as executor:
            futures = {executor.submit(self.process_identity, identity): identity for identity in identities}
            for future in as_completed(futures):
                result = future.result()
                summary.results.append(result)
                if result.status == "completed":
                    summary.completed += 1
                elif result.status == "resumed":
                    summary.resumed += 1
                    summary.completed += 1
                elif result.status == "skipped":
                    summary.skipped += 1
                else:
                    summary.failed += 1
                    reason = result.failure_reason or "unknown"
                    summary.failures_by_reason[reason] = summary.failures_by_reason.get(reason, 0) + 1
        return summary

    def _record_failure(
        self,
        identity: CardImageIdentity,
        *,
        existing: dict[str, Any] | None,
        reason: str,
        retry_count: int,
    ) -> PipelineCardResult:
        self.db.upsert_record(
            self.db.build_record_payload(
                identity,
                status="failed",
                failure_reason=reason[:2000],
                retry_count=retry_count,
                cache_control=self.config.cache_control,
                existing=existing,
            ),
            dry_run=self.config.dry_run,
        )
        return PipelineCardResult(
            identity=identity,
            status="failed",
            failure_reason=reason,
            retry_count=retry_count,
            dry_run=self.config.dry_run,
        )
