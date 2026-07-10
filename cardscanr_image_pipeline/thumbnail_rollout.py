from __future__ import annotations

import hashlib
import json
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from .catalogue import DEFAULT_CATALOGUE_ROOT, iter_catalogue_identities
from .config import ImagePipelineConfig
from .identity import sha256_hex
from .matching import resolve_provider_with_trace
from .models import CardImageIdentity, ProviderImageCandidate
from .paths import build_storage_paths, public_storage_url
from .processing import pokewallet_request_headers
from .providers.pokemon_tcg_api import PokemonTcgApiImageProvider
from .providers.pokewallet import PokeWalletAmbiguousMatchError, PokeWalletImageProvider
from .providers.tcgdex import TcgdexImageProvider, build_tcgdex_image_url
from .stage2_runner import Stage2Runner, write_json_report
from .tcgdex_serie_cache import enrich_identity_serie_id, serie_from_tcgdex_asset_url

RUNTIME_DIR = Path("reports/runtime")
THUMB_ROLLOUT_SEED = 20260710
ENGLISH_BATCH_SIZE = 500
HTTP_SAMPLE_PER_PROVIDER = 25
AVG_THUMB_BYTES_FROM_STAGE2 = 13_009  # 1,300,890 / 100 from stage2 sample


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def classify_url_host(url: str | None) -> str | None:
    if not url:
        return None
    host = (urlparse(url).hostname or "").lower()
    if "pokewallet" in host:
        return "pokewallet"
    if "tcgdex" in host:
        return "tcgdex"
    if "pokemontcg.io" in host:
        return "pokemon_tcg_api"
    if "supabase" in host:
        return "supabase"
    return "other"


def is_pokewallet_auth_url(url: str | None) -> bool:
    if not url:
        return False
    host = (urlparse(url).hostname or "").lower()
    return "api.pokewallet.io" in host


def tcgdex_needs_normalization(identity: CardImageIdentity) -> bool:
    small = identity.catalogue_image_small or ""
    large = identity.catalogue_image_large or ""
    if "assets.tcgdex.net" not in small and "assets.tcgdex.net" not in large:
        return False
    # Catalogue URLs that lack serie path segments or use non-canonical language codes.
    for url in (small, large):
        if "assets.tcgdex.net" not in url:
            continue
        parts = [part for part in urlparse(url).path.split("/") if part]
        # Expected: {lang}/{serie}/{set}/{local}/{quality}.webp
        if len(parts) < 5:
            return True
        lang = parts[0].lower()
        if identity.language == "jp" and lang not in {"ja", "jp"}:
            return True
        if identity.language == "en" and lang != "en":
            return True
        serie = serie_from_tcgdex_asset_url(url)
        if not serie and not identity.serie_id:
            return True
    return False


def probe_url(
    session: requests.Session,
    url: str,
    *,
    timeout: int = 20,
    include_pokewallet_auth: bool = False,
) -> dict[str, Any]:
    """Probe a URL. PokeWallet auth headers are off by default so public usability is honest."""
    headers = pokewallet_request_headers(url) if include_pokewallet_auth else {}
    try:
        response = session.get(url, timeout=timeout, stream=True, headers=headers, allow_redirects=True)
        status = response.status_code
        content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        length = int(response.headers.get("Content-Length") or 0)
        prefix = b""
        try:
            for chunk in response.iter_content(chunk_size=1024):
                prefix = chunk
                break
        finally:
            response.close()
        usable = status == 200 and bool(prefix) and (
            not content_type or content_type.startswith("image/") or content_type == "application/octet-stream"
        )
        rate_limited = status == 429
        # 401/403 = auth failure. 429 = rate limit (not missing credentials).
        auth_required = status in {401, 403}
        return {
            "url": url,
            "status": status,
            "contentType": content_type,
            "contentLength": length,
            "usable": usable,
            "authRequired": auth_required,
            "rateLimited": rate_limited,
            "notFound": status == 404,
            "error": None,
        }
    except requests.RequestException as exc:
        return {
            "url": url,
            "status": None,
            "contentType": None,
            "contentLength": 0,
            "usable": False,
            "authRequired": is_pokewallet_auth_url(url),
            "rateLimited": False,
            "notFound": False,
            "error": str(exc),
        }


@dataclass
class Stage1Classification:
    generated_at_utc: str = field(default_factory=utc_now_iso)
    total_cards: int = 0
    by_language: dict[str, int] = field(default_factory=dict)
    catalogue_url_host_counts: dict[str, int] = field(default_factory=dict)
    pokewallet_auth_required_urls: int = 0
    tcgdex_urls_requiring_normalization: int = 0
    supabase_stored_completed: int = 0
    matchable_chain_total: int = 0
    matchable_by_provider: dict[str, int] = field(default_factory=dict)
    matchable_by_language: dict[str, int] = field(default_factory=dict)
    provider_capability: dict[str, int] = field(default_factory=dict)
    verified_public_sibling_mapping: int = 0
    unresolved: int = 0
    ambiguous: int = 0
    live_http_samples: dict[str, Any] = field(default_factory=dict)
    usable_public_url_estimate: dict[str, Any] = field(default_factory=dict)
    known_public_404_estimate: dict[str, Any] = field(default_factory=dict)
    provider_failure_totals: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify_catalogue_image_state(
    *,
    catalogue_root: Path = DEFAULT_CATALOGUE_ROOT,
    supabase_stored_completed: int = 100,
    http_sample_per_provider: int = HTTP_SAMPLE_PER_PROVIDER,
    seed: int = THUMB_ROLLOUT_SEED,
    # Authoritative Stage 2 reconciliation figures (unique chain matchable).
    authoritative_matchable: dict[str, Any] | None = None,
) -> Stage1Classification:
    """
    Stage 1 inspection.

    Matchable totals default to the Stage 2 reconciliation figures (36,542 unique
    chain-matchable cards). This pass focuses on catalogue URL classification and
    live HTTP usability sampling — having a URL alone is never counted as usable.
    """
    rng = random.Random(seed)
    auth = authoritative_matchable or {
        "total": 36542,
        "unresolved": 38036,
        "ambiguous": 7246,
        "byLanguage": {"en": 23375, "jp": 13167},
        "byProvider": {"tcgdex": 6246, "pokemon_tcg_api": 20359, "pokewallet": 9937},
        "providerCapability": {"tcgdex": 6246, "pokemon_tcg_api": 20359, "pokewallet": 9937},
        "verifiedPublicSiblingMapping": None,
    }

    result = Stage1Classification(supabase_stored_completed=supabase_stored_completed)
    result.matchable_chain_total = int(auth["total"])
    result.unresolved = int(auth["unresolved"])
    result.ambiguous = int(auth["ambiguous"])
    result.matchable_by_language = dict(auth["byLanguage"])
    result.matchable_by_provider = dict(auth["byProvider"])
    result.provider_capability = dict(auth["providerCapability"])

    catalogue_samples_by_host: dict[str, list[str]] = {
        "tcgdex": [],
        "pokemon_tcg_api": [],
        "pokewallet": [],
        "other": [],
    }
    provider_candidate_pools: dict[str, list[tuple[str, str]]] = {
        "tcgdex": [],
        "pokemon_tcg_api": [],
        "pokewallet": [],
    }

    for identity in iter_catalogue_identities(catalogue_root, languages=("en", "jp")):
        result.total_cards += 1
        result.by_language[identity.language] = result.by_language.get(identity.language, 0) + 1

        host = classify_url_host(identity.catalogue_image_small) or classify_url_host(identity.catalogue_image_large) or "none"
        result.catalogue_url_host_counts[host] = result.catalogue_url_host_counts.get(host, 0) + 1
        if is_pokewallet_auth_url(identity.catalogue_image_small) or is_pokewallet_auth_url(identity.catalogue_image_large):
            result.pokewallet_auth_required_urls += 1
        if tcgdex_needs_normalization(identity):
            result.tcgdex_urls_requiring_normalization += 1

        for url in (identity.catalogue_image_small, identity.catalogue_image_large):
            url_host = classify_url_host(url)
            if url_host in catalogue_samples_by_host and url and len(catalogue_samples_by_host[url_host]) < http_sample_per_provider * 3:
                if url not in catalogue_samples_by_host[url_host]:
                    catalogue_samples_by_host[url_host].append(url)

        source = identity.image_source or host
        sample_url = identity.catalogue_image_small or identity.catalogue_image_large
        if sample_url and source in provider_candidate_pools and len(provider_candidate_pools[source]) < http_sample_per_provider * 4:
            provider_candidate_pools[source].append((identity.canonical_base_id, sample_url))

    # Sibling mapping is expensive to recount; leave 0 unless explicitly supplied.
    result.verified_public_sibling_mapping = int(auth.get("verifiedPublicSiblingMapping") or 0)
    session = requests.Session()
    session.headers.update({"User-Agent": "CardScanR-ThumbnailRollout/0.1"})
    live: dict[str, Any] = {"seed": seed, "perProvider": http_sample_per_provider, "providers": {}}
    usable_by_provider: dict[str, dict[str, int]] = {}
    failure_totals: dict[str, int] = {}

    for provider, pool in provider_candidate_pools.items():
        rng.shuffle(pool)
        selected = pool[:http_sample_per_provider]
        probes: list[dict[str, Any]] = []
        usable = auth_n = not_found = other_fail = 0
        for canonical_id, url in selected:
            probe = probe_url(session, url)
            probe["canonicalBaseId"] = canonical_id
            probe["provider"] = provider
            probes.append(probe)
            if probe["usable"]:
                usable += 1
            elif probe["authRequired"]:
                auth_n += 1
                failure_totals["auth_401_403"] = failure_totals.get("auth_401_403", 0) + 1
            elif probe["notFound"]:
                not_found += 1
                failure_totals["http_404"] = failure_totals.get("http_404", 0) + 1
            else:
                other_fail += 1
                failure_totals["other_http_or_network"] = failure_totals.get("other_http_or_network", 0) + 1
        live["providers"][provider] = {
            "sampled": len(selected),
            "usable": usable,
            "authRequired": auth_n,
            "notFound": not_found,
            "otherFailures": other_fail,
            "usableRate": round((usable / len(selected)) * 100, 2) if selected else 0.0,
            "probes": probes,
        }
        matchable = int(result.matchable_by_provider.get(provider, 0))
        # PokeWallet catalogue URLs are not public CDN assets for Flutter Manual Add.
        if provider == "pokewallet":
            estimated = 0
        else:
            estimated = int(round(matchable * (usable / len(selected)))) if selected else 0
        usable_by_provider[provider] = {
            "sampled": len(selected),
            "usable": usable,
            "matchable": matchable,
            "estimatedUsable": estimated,
            "publicWithoutAuth": provider != "pokewallet",
        }

    catalogue_live: dict[str, Any] = {}
    for host, urls in catalogue_samples_by_host.items():
        rng.shuffle(urls)
        selected_urls = urls[:http_sample_per_provider]
        usable = auth_n = not_found = other_fail = 0
        probes = []
        for url in selected_urls:
            probe = probe_url(session, url)
            probes.append(probe)
            if probe["usable"]:
                usable += 1
            elif probe["authRequired"]:
                auth_n += 1
            elif probe["notFound"]:
                not_found += 1
            else:
                other_fail += 1
        catalogue_live[host] = {
            "sampled": len(selected_urls),
            "usable": usable,
            "authRequired": auth_n,
            "notFound": not_found,
            "otherFailures": other_fail,
            "usableRate": round((usable / len(selected_urls)) * 100, 2) if selected_urls else 0.0,
            "probes": probes,
        }

    estimated_usable_total = sum(item["estimatedUsable"] for item in usable_by_provider.values())
    result.live_http_samples = {"resolvedProviderCandidates": live, "catalogueUrlsByHost": catalogue_live}
    result.usable_public_url_estimate = {
        "method": "extrapolate_live_http_sample_rates_onto_chain_matchable_counts",
        "byProvider": usable_by_provider,
        "estimatedUsableMatchableTotal": estimated_usable_total,
        "note": "Having a catalogue URL is not counted as usable. Estimates apply only to chain-matchable cards.",
    }
    known_404 = 0
    for host in ("tcgdex", "pokemon_tcg_api"):
        sample = catalogue_live.get(host) or {}
        sampled = int(sample.get("sampled") or 0)
        not_found = int(sample.get("notFound") or 0)
        population = int(result.catalogue_url_host_counts.get(host) or 0)
        if sampled:
            known_404 += int(round(population * (not_found / sampled)))
    result.known_public_404_estimate = {
        "estimatedKnownPublic404s": known_404,
        "basis": "extrapolated from deterministic catalogue URL samples for tcgdex and pokemon_tcg_api hosts",
    }
    result.provider_failure_totals = failure_totals
    result.notes = [
        "Usable image requires live HTTP 200 with image body; catalogue URL presence alone is insufficient.",
        "PokeWallet api.pokewallet.io URLs require authentication and are not public CDN assets.",
        "Supabase currently stores the Stage 2 100-card sample (thumb+display); thumbnail rollout will not import display.webp.",
        "Matchable totals reused from Stage 2 reconciliation (unique chain-matchable canonicalBaseIds).",
        f"Validated chain matchable total: {result.matchable_chain_total}.",
    ]
    return result


def render_stage1_markdown(report: dict[str, Any]) -> str:
    usable = report.get("usable_public_url_estimate") or {}
    known_404 = report.get("known_public_404_estimate") or {}
    lines = [
        "# Thumbnail Rollout — Stage 1 Image State",
        "",
        f"- Generated at (UTC): {report.get('generated_at_utc')}",
        f"- Total cards: {report.get('total_cards')}",
        f"- Validated chain matchable: {report.get('matchable_chain_total')}",
        f"- Estimated usable public URLs (matchable extrapolation): {usable.get('estimatedUsableMatchableTotal')}",
        f"- PokeWallet auth-required catalogue URLs: {report.get('pokewallet_auth_required_urls')}",
        f"- TCGdex URLs requiring normalization: {report.get('tcgdex_urls_requiring_normalization')}",
        f"- Estimated known public 404s: {known_404.get('estimatedKnownPublic404s')}",
        f"- Already stored in Supabase (completed/verified): {report.get('supabase_stored_completed')}",
        f"- Unresolved: {report.get('unresolved')}",
        f"- Ambiguous: {report.get('ambiguous')}",
        f"- Verified public sibling mappings (PokeWallet catalogue + public provider match): {report.get('verified_public_sibling_mapping')}",
        "",
        "## Matchable by provider (exclusive chain selection)",
        "",
    ]
    for provider, count in sorted((report.get("matchable_by_provider") or {}).items()):
        lines.append(f"- `{provider}`: {count}")
    lines.extend(["", "## Provider capability (non-exclusive)", ""])
    for provider, count in sorted((report.get("provider_capability") or {}).items()):
        lines.append(f"- `{provider}`: {count}")
    lines.extend(["", "## Catalogue URL hosts", ""])
    for host, count in sorted((report.get("catalogue_url_host_counts") or {}).items()):
        lines.append(f"- `{host}`: {count}")
    lines.extend(["", "## Live HTTP sample (resolved provider candidates)", ""])
    providers = ((report.get("live_http_samples") or {}).get("resolvedProviderCandidates") or {}).get("providers") or {}
    for provider, stats in sorted(providers.items()):
        lines.append(
            f"- `{provider}`: sampled={stats.get('sampled')}, usable={stats.get('usable')}, "
            f"auth={stats.get('authRequired')}, 404={stats.get('notFound')}, rate={stats.get('usableRate')}%"
        )
    lines.append("")
    return "\n".join(lines)


ENGLISH_BATCH_BUCKETS: dict[str, int] = {
    # EN catalogue has no imageSource=tcgdex cards; TCGdex chain matches are JP-only (6,246).
    "en_pokemon_tcg_api": 400,
    "en_pokewallet": 100,
}


def _english_bucket(identity: CardImageIdentity, card: dict[str, Any]) -> str | None:
    if identity.language != "en":
        return None
    source = str(card.get("imageSource") or identity.image_source or "")
    if source == "pokewallet":
        return "en_pokewallet"
    if source == "pokemon_tcg_api":
        return "en_pokemon_tcg_api"
    if source == "tcgdex":
        return "en_tcgdex"
    return None


def build_english_thumbnail_batch_manifest(
    *,
    catalogue_root: Path = DEFAULT_CATALOGUE_ROOT,
    seed: int = THUMB_ROLLOUT_SEED,
    target_count: int = ENGLISH_BATCH_SIZE,
    require_reachable: bool = False,
) -> dict[str, Any]:
    """Build a deterministic 500-card English thumbnail batch from chain-matchable cards.

    Uses catalogue imageSource as the primary bucket signal (same approach as Stage 2
    stratified sampling). Full provider-chain resolve is only applied when filling
    buckets to confirm exact identity matchability — never by card name alone.
    """
    grouped: dict[str, list[tuple[CardImageIdentity, dict[str, Any]]]] = {key: [] for key in ENGLISH_BATCH_BUCKETS}
    session = requests.Session() if require_reachable else None
    if session is not None:
        session.headers.update({"User-Agent": "CardScanR-ThumbnailRollout/0.1"})

    for identity in iter_catalogue_identities(catalogue_root, languages=("en",)):
        card = identity.source_card or {}
        bucket = _english_bucket(identity, card)
        if bucket not in grouped:
            continue
        grouped[bucket].append((identity, card))

    rng = random.Random(seed)
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    reachability_checked = 0
    reachability_failed = 0

    for bucket, count in ENGLISH_BATCH_BUCKETS.items():
        pool = list(grouped.get(bucket) or [])
        rng.shuffle(pool)
        filled = 0
        for identity, card in pool:
            if filled >= count:
                break
            if identity.canonical_base_id in selected_ids:
                continue
            resolution = resolve_provider_with_trace(identity, source_card=card)
            if resolution.ambiguous or resolution.candidate is None:
                continue
            provider = resolution.candidate.provider
            if bucket == "en_tcgdex" and provider != "tcgdex":
                continue
            if bucket == "en_pokemon_tcg_api" and provider != "pokemon_tcg_api":
                continue
            if bucket == "en_pokewallet" and provider != "pokewallet":
                continue
            if require_reachable and session is not None:
                url = resolution.candidate.source_url_thumb or resolution.candidate.source_url_display
                reachability_checked += 1
                probe = probe_url(session, url)
                if not probe["usable"]:
                    reachability_failed += 1
                    continue
            selected.append(
                {
                    "bucket": bucket,
                    "edgeCaseTag": None,
                    "canonicalBaseId": identity.canonical_base_id,
                    "language": identity.language,
                    "setId": identity.set_id,
                    "collectorNumber": identity.collector_number,
                    "imageSource": identity.image_source,
                    "provider": provider,
                }
            )
            selected_ids.add(identity.canonical_base_id)
            filled += 1
        if filled < count:
            raise RuntimeError(f"Unable to fill English batch bucket {bucket}: needed {count}, found {filled}")

    if len(selected) != target_count:
        raise RuntimeError(f"Unexpected English batch size {len(selected)}; expected {target_count}")

    set_counts: dict[str, int] = {}
    for entry in selected:
        set_counts[entry["setId"]] = set_counts.get(entry["setId"], 0) + 1
    densest_set = max(set_counts.items(), key=lambda item: item[1]) if set_counts else ("", 0)

    return {
        "schemaVersion": "thumb-en-500-1.0.0",
        "generatedAtUtc": utc_now_iso(),
        "seed": seed,
        "cardCount": len(selected),
        "language": "en",
        "importDisplay": False,
        "buckets": ENGLISH_BATCH_BUCKETS,
        "entries": selected,
        "reachability": {
            "checked": reachability_checked,
            "failed": reachability_failed,
            "required": require_reachable,
        },
        "proposedCompleteSetProbe": {
            "setId": densest_set[0],
            "selectedCardsInBatch": densest_set[1],
            "note": "Candidate for Stage 3 step 5 (one complete English set) after 500-card approval.",
        },
        "storagePathTemplate": "pokemon/{language}/{setId}/{collectorNumber}/v/{sha256-prefix}/thumb.webp",
        "thumbMaxPx": 245,
        "cacheControl": "public, max-age=31536000, immutable",
    }


def write_english_batch_manifest(manifest: dict[str, Any], *, path: Path | None = None) -> Path:
    target = path or (RUNTIME_DIR / "thumbnail_rollout_en_500_manifest.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    manifest["sha256"] = digest
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def estimate_thumbnail_storage(*, matchable_en: int, matchable_all: int, avg_thumb_bytes: int = AVG_THUMB_BYTES_FROM_STAGE2) -> dict[str, int]:
    return {
        "avgThumbBytesObservedStage2": avg_thumb_bytes,
        "estimated500BatchBytes": avg_thumb_bytes * 500,
        "estimatedFullEnglishMatchableBytes": avg_thumb_bytes * matchable_en,
        "estimatedFullCatalogueMatchableBytes": avg_thumb_bytes * matchable_all,
        "estimatedFullCatalogueAllCardsBytes": avg_thumb_bytes * 74578,
    }


def dry_run_english_thumbnail_batch(
    *,
    config: ImagePipelineConfig,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Thumb-only dry run for the English 500-card batch. No uploads, no display import."""
    if config.import_display:
        raise ValueError("English thumbnail dry-run must run with import_display=False")
    runner = Stage2Runner(config)
    payload = runner.dry_run_manifest(manifest)
    payload["importDisplay"] = False
    payload["rolloutStage"] = "en_500_dry_run"
    payload["shouldExecuteFullEnglish"] = False
    # Stop conditions for thumbnail rollout.
    stop = list(payload.get("stopReasons") or [])
    if payload.get("ambiguousCount"):
        stop.append("ambiguous_identity_present")
    if int(payload.get("cardCount") or 0) != ENGLISH_BATCH_SIZE:
        stop.append(f"unexpected_batch_size:{payload.get('cardCount')}")
    # Display paths must be absent in thumb-only mode.
    for card in payload.get("cards") or []:
        if card.get("display_storage_path"):
            stop.append(f"display_path_planned:{card.get('canonical_base_id')}")
            break
        thumb = card.get("thumb_storage_path") or ""
        if thumb and not thumb.endswith("/thumb.webp"):
            stop.append(f"non_thumb_path:{card.get('canonical_base_id')}")
            break
    payload["stopReasons"] = stop
    payload["shouldStop"] = bool(stop)
    return payload
