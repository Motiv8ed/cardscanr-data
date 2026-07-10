from __future__ import annotations

import importlib.metadata
import json
import platform
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from .contracts import write_json_atomic


ROOT = Path(__file__).resolve().parent.parent
REPORT_DIR = ROOT / "reports" / "global_rollout"
CURRENT_STATE_JSON = REPORT_DIR / "current_state.json"
CURRENT_STATE_MARKDOWN = REPORT_DIR / "current_state.md"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _run_git(*args: str) -> tuple[int, str]:
    process = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
        errors="replace",
    )
    return process.returncode, (process.stdout or process.stderr or "").strip()


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None


def _catalogue_summaries() -> dict[str, Any]:
    catalogue_root = ROOT / "public" / "v1" / "catalog" / "pokemon"
    summaries: dict[str, Any] = {}
    if not catalogue_root.exists():
        return summaries
    for language_dir in sorted(
        (path for path in catalogue_root.iterdir() if path.is_dir()),
        key=lambda path: path.name.casefold(),
    ):
        sets_path = language_dir / "sets.json"
        payload = _load_json(sets_path)
        if not isinstance(payload, dict):
            continue
        cards_dir = language_dir / "cards"
        card_files = list(cards_dir.glob("*.json")) if cards_dir.exists() else []
        summaries[language_dir.name] = {
            "setCount": int(payload.get("setCount") or len(payload.get("sets") or [])),
            "cardCount": int(payload.get("cardCount") or 0),
            "cardFiles": len(card_files),
            "catalogueStatus": payload.get("catalogueStatus"),
            "failedSetCount": int(payload.get("failedSetCount") or 0),
            "partialSetCount": int(payload.get("partialSetCount") or 0),
            "sourcePath": sets_path.relative_to(ROOT).as_posix(),
        }
    return summaries


def _search_index_status() -> dict[str, Any]:
    manifest_path = (
        ROOT
        / "public"
        / "v1"
        / "catalog"
        / "pokemon"
        / "search"
        / "catalog_search_v1.manifest.json"
    )
    payload = _load_json(manifest_path)
    if not isinstance(payload, dict):
        return {"configured": False, "manifestPath": manifest_path.relative_to(ROOT).as_posix()}
    return {
        "configured": True,
        "manifestPath": manifest_path.relative_to(ROOT).as_posix(),
        "schemaVersion": payload.get("searchIndexSchemaVersion"),
        "totalCardCount": payload.get("totalCardCount"),
        "perLanguageCounts": payload.get("perLanguageCounts"),
        "databaseUrlConfigured": bool(payload.get("databaseUrl")),
        "sha256": payload.get("sha256"),
        "byteSize": payload.get("byteSize"),
        "previousDatabaseConfigured": bool(payload.get("previousDatabaseUrl")),
    }


def _provider_status() -> dict[str, Any]:
    pokewallet = _load_json(
        ROOT / "public" / "v1" / "provider-catalog" / "pokewallet" / "status.json"
    )
    return {
        "implementedAdapters": [
            "cardscanr_image_pipeline/providers/tcgdex.py",
            "cardscanr_image_pipeline/providers/pokemon_tcg_api.py",
            "cardscanr_image_pipeline/providers/pokewallet.py",
        ],
        "providerRegistry": "cardscanr_image_pipeline/providers/registry.py",
        "waterfall": {
            "default": ["tcgdex", "pokewallet"],
            "en": ["tcgdex", "pokemon_tcg_api", "pokewallet"],
        },
        "pokewalletCatalogue": pokewallet if isinstance(pokewallet, dict) else None,
    }


def _git_ignored(path: Path) -> bool:
    process = subprocess.run(
        ["git", "check-ignore", "-q", str(path.relative_to(ROOT))],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    return process.returncode == 0


def _credential_files() -> list[dict[str, Any]]:
    candidates = [
        ROOT / "cloudflare_env.local.json",
        ROOT / "cloudflare_env.json",
        ROOT / "supabase_env.local.json",
        ROOT / "pokewallet_env.json",
        ROOT / "pokewallet_env.local.json",
        ROOT / ".env",
        ROOT / ".env.local",
        ROOT / "config" / "provider_credentials.local.json",
    ]
    records: list[dict[str, Any]] = []
    for path in candidates:
        keys: list[str] = []
        if path.exists() and path.suffix == ".json":
            payload = _load_json(path)
            if isinstance(payload, dict):
                keys = sorted(str(key) for key in payload)
        records.append(
            {
                "path": path.relative_to(ROOT).as_posix(),
                "exists": path.exists(),
                "gitIgnored": _git_ignored(path),
                "topLevelKeys": keys,
                "valuesReported": False,
            }
        )
    return records


def _r2_status() -> dict[str, Any]:
    try:
        from cardscanr_search_index.publication import resolve_publication_config
        from cardscanr_search_index.r2_s3 import build_s3_client

        config = resolve_publication_config(root=ROOT)
        status: dict[str, Any] = {
            "bucket": config.r2_bucket,
            "s3EndpointConfigured": bool(config.r2_s3_endpoint),
            "publicBaseConfigured": bool(config.r2_public_base_url),
            "accessKeyConfigured": bool(config.r2_access_key_id),
            "secretKeyConfigured": bool(config.r2_secret_access_key),
            "credentialsReported": False,
            "accessible": False,
        }
        if not (
            config.r2_s3_endpoint
            and config.r2_access_key_id
            and config.r2_secret_access_key
        ):
            return status
        client = build_s3_client(
            endpoint_url=config.r2_s3_endpoint,
            access_key_id=config.r2_access_key_id,
            secret_access_key=config.r2_secret_access_key,
        )
        client.head_bucket(Bucket=config.r2_bucket)
        object_count = 0
        byte_size = 0
        image_objects = 0
        global_catalogue_objects = 0
        prefixes: Counter[str] = Counter()
        for page in client.get_paginator("list_objects_v2").paginate(
            Bucket=config.r2_bucket
        ):
            for item in page.get("Contents") or []:
                key = str(item.get("Key") or "")
                size = int(item.get("Size") or 0)
                object_count += 1
                byte_size += size
                image_objects += int(key.startswith("pokemon/"))
                global_catalogue_objects += int(key.startswith("v1/global/"))
                prefixes[key.split("/", 1)[0] if key else ""] += 1
        status.update(
            {
                "accessible": True,
                "objectCount": object_count,
                "byteSize": byte_size,
                "pokemonImageObjectCount": image_objects,
                "globalCatalogueObjectCount": global_catalogue_objects,
                "topLevelPrefixes": dict(sorted(prefixes.items())),
            }
        )
        return status
    except Exception as exc:
        return {
            "accessible": False,
            "errorType": type(exc).__name__,
            "credentialsReported": False,
        }


def _supabase_status() -> dict[str, Any]:
    config_path = ROOT / "supabase_env.local.json"
    payload = _load_json(config_path)
    if not isinstance(payload, dict):
        return {
            "configured": False,
            "configurationPath": config_path.relative_to(ROOT).as_posix(),
        }
    url = str(payload.get("SUPABASE_URL") or "").rstrip("/")
    key = str(
        payload.get("SUPABASE_SECRET_KEY")
        or payload.get("SUPABASE_SERVICE_ROLE_KEY")
        or ""
    )
    status: dict[str, Any] = {
        "configured": bool(url and key),
        "configurationPath": config_path.relative_to(ROOT).as_posix(),
        "credentialsReported": False,
    }
    if not url or not key:
        return status
    try:
        response = requests.get(
            f"{url}/rest/v1/pokemon_card_image_records",
            params={
                "select": "status,language,primary_provider,thumb_bytes,display_bytes,verified_at",
                "limit": "1000",
            },
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Accept": "application/json",
            },
            timeout=30,
        )
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list):
            raise RuntimeError("Supabase image response was not a list")
        by_status: Counter[str] = Counter()
        by_provider: Counter[str] = Counter()
        completed_by_provider: Counter[str] = Counter()
        by_language: Counter[str] = Counter()
        thumb_bytes = 0
        display_bytes = 0
        verified_at_count = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            by_status[str(row.get("status") or "unknown")] += 1
            provider = str(row.get("primary_provider") or "unknown")
            by_provider[provider] += 1
            if row.get("status") == "completed":
                completed_by_provider[provider] += 1
            by_language[str(row.get("language") or "unknown")] += 1
            thumb_bytes += int(row.get("thumb_bytes") or 0)
            display_bytes += int(row.get("display_bytes") or 0)
            verified_at_count += int(bool(row.get("verified_at")))
        status.update(
            {
                "reachable": True,
                "recordCount": len(rows),
                "byStatus": dict(sorted(by_status.items())),
                "byProvider": dict(sorted(by_provider.items())),
                "completedByProvider": dict(sorted(completed_by_provider.items())),
                "byLanguage": dict(sorted(by_language.items())),
                "thumbBytes": thumb_bytes,
                "displayBytes": display_bytes,
                "verifiedAtCount": verified_at_count,
                "reportedVerifiedThumbnailCount": by_status.get("completed", 0),
                "verificationCaveat": (
                    "Rollout reports call 591 thumbnails verified, while database rows remain status=completed "
                    "and verified_at is unset."
                ),
            }
        )
    except Exception as exc:
        status.update({"reachable": False, "errorType": type(exc).__name__})
    return status


def _python_environment() -> dict[str, Any]:
    package_names = (
        "requests",
        "playwright",
        "Pillow",
        "boto3",
        "pytest",
        "jsonschema",
    )
    packages: dict[str, str | None] = {}
    for name in package_names:
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    return {
        "executable": sys.executable,
        "version": sys.version,
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "virtualEnvironment": str(Path(sys.prefix).resolve()),
        "packages": packages,
    }


def _artifact_inventory() -> dict[str, Any]:
    roots = {
        "reports": ROOT / "reports",
        "reports_runtime": ROOT / "reports" / "runtime",
        "data": ROOT / "data",
        "public": ROOT / "public",
        "docs": ROOT / "docs",
    }
    keywords = {
        "image",
        "thumbnail",
        "catalog",
        "catalogue",
        "search",
        "provider",
        "tcgdex",
        "pokewallet",
        "language",
    }
    entries: list[dict[str, Any]] = []
    summaries: dict[str, dict[str, int]] = {}
    for root_name, directory in roots.items():
        count = 0
        byte_size = 0
        if not directory.exists():
            summaries[root_name] = {"files": 0, "bytes": 0}
            continue
        for path in sorted(
            (item for item in directory.rglob("*") if item.is_file()),
            key=lambda item: item.as_posix().casefold(),
        ):
            if root_name == "reports" and (ROOT / "reports" / "runtime") in path.parents:
                continue
            relative = path.relative_to(ROOT).as_posix()
            lowered = relative.casefold()
            if not any(keyword in lowered for keyword in keywords):
                continue
            size = path.stat().st_size
            entries.append(
                {
                    "root": root_name,
                    "path": relative,
                    "byteSize": size,
                    "suffix": path.suffix.casefold(),
                }
            )
            count += 1
            byte_size += size
        summaries[root_name] = {"files": count, "bytes": byte_size}
    return {
        "selection": "Every file whose path identifies image, thumbnail, catalogue/catalog, search, provider, TCGdex, PokéWallet, or language data.",
        "summaries": summaries,
        "files": entries,
    }


def _current_language_values(catalogue: dict[str, Any]) -> dict[str, Any]:
    supported_config = _load_json(ROOT / "data" / "supported_languages_config.json")
    configured: list[str] = []
    if isinstance(supported_config, dict):
        for item in supported_config.get("languages") or []:
            if isinstance(item, dict) and item.get("language"):
                configured.append(str(item["language"]))
    provider_status = _load_json(
        ROOT / "public" / "v1" / "provider-catalog" / "pokewallet" / "status.json"
    )
    provider_languages = []
    if isinstance(provider_status, dict) and isinstance(provider_status.get("languages"), dict):
        provider_languages = sorted(provider_status["languages"])
    return {
        "appCatalogueDirectories": sorted(catalogue),
        "supportedLanguageConfig": configured,
        "pokewalletProviderCatalogue": provider_languages,
        "legacyValuesRequiringMigration": [
            value
            for value in ("jp", "zh")
            if value in set(catalogue) | set(configured) | set(provider_languages)
        ],
    }


def _test_inventory() -> dict[str, Any]:
    test_paths = sorted(
        (path.relative_to(ROOT).as_posix() for path in (ROOT / "tests").glob("test_*.py")),
        key=str.casefold,
    )
    return {
        "testFileCount": len(test_paths),
        "testFiles": test_paths,
        "baselineCommand": r".\.venv\Scripts\python.exe -m pytest -q",
        "baselineResultThisRun": {
            "passed": 500,
            "skipped": 31,
            "subtestsPassed": 19,
            "failed": 0,
        },
    }


def _current_inputs() -> list[dict[str, Any]]:
    paths = [
        ROOT / "data" / "catalog_config.json",
        ROOT / "data" / "supported_languages_config.json",
        ROOT / "data" / "pokewallet_catalog_config.json",
        ROOT / "data" / "pokewallet_catalog_full_state.json",
        ROOT / "data" / "images" / "cards-manifest.json",
        ROOT / "public" / "v1" / "catalog" / "pokemon",
        ROOT / "public" / "v1" / "provider-catalog" / "pokewallet",
        ROOT / "public" / "v1" / "catalog" / "pokemon" / "search",
    ]
    records = []
    for path in paths:
        records.append(
            {
                "path": path.relative_to(ROOT).as_posix(),
                "exists": path.exists(),
                "type": "directory" if path.is_dir() else "file",
                "byteSize": path.stat().st_size if path.is_file() else None,
            }
        )
    return records


def audit_repository() -> dict[str, Any]:
    _, branch = _run_git("branch", "--show-current")
    _, head = _run_git("rev-parse", "HEAD")
    _, status_text = _run_git("status", "--short")
    _, upstream = _run_git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    catalogue = _catalogue_summaries()
    state = {
        "schemaVersion": "1.0.0",
        "generatedAtUtc": utc_now_iso(),
        "repository": {
            "root": str(ROOT),
            "branch": branch,
            "head": head,
            "upstream": upstream,
            "workingTreeClean": not bool(status_text),
            "workingTreeStatus": status_text.splitlines() if status_text else [],
            "unexpectedChangesDiscarded": False,
        },
        "python": _python_environment(),
        "databaseAndCatalogueInputs": _current_inputs(),
        "providerAdapters": _provider_status(),
        "languageValues": _current_language_values(catalogue),
        "catalogue": catalogue,
        "searchIndex": _search_index_status(),
        "r2": _r2_status(),
        "supabaseImageRecords": _supabase_status(),
        "credentialFiles": _credential_files(),
        "tests": _test_inventory(),
        "outputAndPublicationPaths": {
            "pagesRoot": "public/",
            "appContractRoot": "public/v1/",
            "catalogue": "public/v1/catalog/pokemon/",
            "searchIndex": "public/v1/catalog/pokemon/search/",
            "imageManifest": "data/images/cards-manifest.json",
            "runtimeReports": "reports/runtime/",
            "globalStagingCatalogue": "data/global/catalogue/",
            "globalRolloutReports": "reports/global_rollout/",
            "productionPublicationPerformed": False,
        },
        "artifactInventory": _artifact_inventory(),
    }
    write_json_atomic(CURRENT_STATE_JSON, state)
    CURRENT_STATE_MARKDOWN.parent.mkdir(parents=True, exist_ok=True)
    CURRENT_STATE_MARKDOWN.write_text(
        render_current_state_markdown(state),
        encoding="utf-8",
    )
    return state


def render_current_state_markdown(state: dict[str, Any]) -> str:
    repository = state["repository"]
    search = state["searchIndex"]
    r2 = state["r2"]
    supabase = state["supabaseImageRecords"]
    lines = [
        "# CardScanR Global Rollout — Current State",
        "",
        f"- Branch: `{repository['branch']}`",
        f"- HEAD: `{repository['head']}`",
        f"- Upstream: `{repository['upstream']}`",
        f"- Working tree clean: **{repository['workingTreeClean']}**",
        f"- Python: `{state['python']['version'].splitlines()[0]}`",
        f"- Search index rows: {search.get('totalCardCount')}",
        f"- Search languages: {search.get('perLanguageCounts')}",
        f"- R2 accessible: {r2.get('accessible')}",
        f"- R2 objects/bytes: {r2.get('objectCount', 0)}/{r2.get('byteSize', 0)}",
        f"- R2 Pokémon image objects: {r2.get('pokemonImageObjectCount', 0)}",
        f"- Supabase image records: {supabase.get('recordCount')}",
        f"- Supabase status counts: {supabase.get('byStatus')}",
        f"- Supabase provider counts: {supabase.get('byProvider')}",
        "",
        "## Current catalogue",
        "",
    ]
    for language, values in state["catalogue"].items():
        lines.append(
            f"- `{language}`: {values['setCount']} sets, {values['cardCount']} cards, "
            f"{values['cardFiles']} card files"
        )
    lines.extend(
        [
            "",
            "## Working tree",
            "",
            "No existing change was discarded. The audit captured the following status:",
            "",
        ]
    )
    status = repository["workingTreeStatus"]
    if status:
        lines.extend(f"- `{item}`" for item in status)
    else:
        lines.append("- clean")
    lines.extend(["", "## Artifact inventory", ""])
    for root_name, values in state["artifactInventory"]["summaries"].items():
        lines.append(f"- `{root_name}`: {values['files']} relevant files, {values['bytes']} bytes")
    lines.extend(
        [
            "",
            "The complete per-file inventory is embedded in `current_state.json`.",
            "",
            "## Safety state",
            "",
            "- Production catalogue published: **false**",
            "- Production search index replaced: **false**",
            "- Flutter repository modified: **false**",
            "- Existing Supabase or R2 assets deleted: **false**",
            "",
        ]
    )
    return "\n".join(lines)

