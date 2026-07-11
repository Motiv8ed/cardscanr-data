from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from .contracts import write_json_atomic


ROOT = Path(__file__).resolve().parent.parent
REPORT_DIR = ROOT / "reports" / "global_rollout"
LOCAL_CREDENTIAL_PATH = ROOT / "config" / "provider_credentials.local.json"
TERMS_REVIEW_DATE = "2026-07-11"
USER_AGENT = "CardScanR-GlobalRollout-CredentialPreflight/1.0"


PROVIDER_LEDGER: tuple[dict[str, Any], ...] = (
    {
        "provider": "tcgdex",
        "officialDocumentationUrl": "https://tcgdex.dev/",
        "termsUrl": "https://github.com/tcgdex/cards-database",
        "supportedLanguages": [
            "en",
            "ja",
            "zh-Hans",
            "zh-Hant",
            "ko",
            "th",
            "id",
            "fr",
            "de",
            "it",
            "es",
            "es-419",
            "pt-BR",
            "pt-PT",
            "nl",
            "pl",
            "ru",
        ],
        "supportedRegions": [
            "GLOBAL",
            "JP",
            "CN",
            "MULTI:TW,HK",
            "KR",
            "TH",
            "ID",
            "FR",
            "DE",
            "IT",
            "ES",
            "LATAM",
            "BR",
            "PT",
            "NL",
            "PL",
            "RU",
        ],
        "metadataCoverage": "multilingual cards, sets, series, release dates, collector numbers; completion varies",
        "imageCoverage": "provider-hosted low/high card assets where contributed",
        "authenticationMethod": "none",
        "requiredEnvironmentVariables": [],
        "freePlanLimits": "Free; no published hard request limit. Provider asks bulk users to cache responses.",
        "paidPlanRequirements": "none",
        "requestLimits": "No published hard limit; CardScanR uses serial conservative pacing.",
        "pagination": "optional REST filtering/pagination; list endpoints are unpaginated by default",
        "bulkExportCapability": "complete list endpoints plus set details; persistent source cache required",
        "stableProviderIds": True,
        "imageHostBehaviour": "assets.tcgdex.net; explicit low/high and WebP/PNG/JPEG variants",
        "cachingPolicy": "FAQ explicitly recommends local response caching for bulk data",
        "selfHostingOrRehostingPolicy": "Card metadata repository is MIT; artwork redistribution is not expressly licensed.",
        "attributionRequirements": "No explicit API attribution requirement found; preserve provenance.",
        "commercialUseStatus": "metadata appears permitted by MIT database license; artwork rights remain third-party",
        "termsReviewDate": TERMS_REVIEW_DATE,
        "termsStatus": "pending_human_review",
        "metadataTermsStatus": "approved_with_conditions",
        "imageRehostingStatus": "pending_human_review",
        "adapterImplementationStatus": "implemented_global_metadata_and_public_image_preflight",
        "notes": [
            "An open-source metadata licence is not treated as permission to redistribute Pokémon artwork.",
            "Traditional Chinese provider data does not distinguish Taiwan from Hong Kong.",
        ],
    },
    {
        "provider": "pokemon_tcg_api",
        "officialDocumentationUrl": "https://docs.pokemontcg.io/",
        "termsUrl": "https://dev.pokemontcg.io/terms",
        "supportedLanguages": ["en"],
        "supportedRegions": ["GLOBAL"],
        "metadataCoverage": "English cards and sets",
        "imageCoverage": "English small and large image URLs",
        "authenticationMethod": "optional X-Api-Key; anonymous access allowed at lower limits",
        "requiredEnvironmentVariables": ["POKEMON_TCG_API_KEY"],
        "freePlanLimits": "Anonymous: 1,000 requests/day and 30/minute. Free API key: 20,000/day by default.",
        "paidPlanRequirements": "none documented for default keyed access",
        "requestLimits": "Key-dependent; 429 responses must be respected",
        "pagination": "page/pageSize, maximum documented pageSize 250",
        "bulkExportCapability": "paginated card and set endpoints",
        "stableProviderIds": True,
        "imageHostBehaviour": "images.pokemontcg.io; known honest 404s for some recently indexed cards",
        "cachingPolicy": "API documentation encourages economical request use; no explicit artwork archive grant found",
        "selfHostingOrRehostingPolicy": "not expressly documented for the complete artwork archive",
        "attributionRequirements": "not expressly documented; preserve provenance and third-party ownership notices",
        "commercialUseStatus": "API use allowed subject to acceptable-use terms; artwork redistribution remains unclear",
        "termsReviewDate": TERMS_REVIEW_DATE,
        "termsStatus": "pending_human_review",
        "metadataTermsStatus": "approved_with_conditions",
        "imageRehostingStatus": "pending_human_review",
        "adapterImplementationStatus": "existing_english_adapter_and_catalogue",
    },
    {
        "provider": "pokewallet",
        "officialDocumentationUrl": "https://www.pokewallet.io/api-docs",
        "termsUrl": "https://www.pokewallet.io/terms-conditions",
        "supportedLanguages": ["en", "ja", "zh-Hans", "zh-Hant", "ko", "fr", "de", "it", "es", "pt-BR"],
        "supportedRegions": ["GLOBAL", "JP", "CN", "TW", "HK", "KR", "FR", "DE", "IT", "ES", "BR"],
        "metadataCoverage": "50,000+ cards with stable card/set IDs; local cache currently contains EN/JP/ZH",
        "imageCoverage": "authenticated low/high endpoints and selected localized European images",
        "authenticationMethod": "X-API-Key",
        "requiredEnvironmentVariables": ["POKEWALLET_API_KEY"],
        "freePlanLimits": "100 requests/hour, 1,000 requests/day, $0/month",
        "paidPlanRequirements": "Pro is €20/month for 5,000/hour and 50,000/day; never auto-upgrade",
        "requestLimits": "plan-specific hourly and daily limits",
        "pagination": "set detail pages; search limits documented",
        "bulkExportCapability": "no single bulk archive; resumable set-by-set export",
        "stableProviderIds": True,
        "imageHostBehaviour": "authenticated binary endpoint; some provider records return permanent 404",
        "cachingPolicy": "image responses advertise long immutable caching; terms do not expressly grant archive redistribution",
        "selfHostingOrRehostingPolicy": "commercial service use is licensed, but complete image rehosting is not explicit",
        "attributionRequirements": "preserve provenance and third-party ownership; no removal of notices",
        "commercialUseStatus": "limited revocable commercial service-use licence",
        "termsReviewDate": TERMS_REVIEW_DATE,
        "termsStatus": "pending_human_review",
        "metadataTermsStatus": "approved_with_conditions",
        "imageRehostingStatus": "pending_human_review",
        "adapterImplementationStatus": "existing_metadata_image_adapter_and_global_rate_limiter",
        "notes": [
            "Terms prohibit automated systems without explicit permission while API docs invite programmatic API use.",
            "Bulk image mirroring therefore requires written clarification or explicit human acceptance of the risk.",
        ],
    },
    {
        "provider": "scrydex",
        "officialDocumentationUrl": "https://scrydex.com/docs",
        "termsUrl": "https://scrydex.com/terms",
        "supportedLanguages": ["en", "ja"],
        "supportedRegions": ["GLOBAL", "JP"],
        "metadataCoverage": "English and Japanese cards/expansions, translations where available",
        "imageCoverage": "high-fidelity image URLs",
        "authenticationMethod": "X-Api-Key plus X-Team-ID",
        "requiredEnvironmentVariables": ["SCRYDEX_API_KEY", "SCRYDEX_TEAM_ID"],
        "freePlanLimits": "No $0 catalogue plan found on the current pricing page.",
        "paidPlanRequirements": "Starter: US$29/month, 5,000 credits, US$0.006 per overage credit",
        "requestLimits": "100 requests/second documented; requests consume credits",
        "pagination": "page/page_size and query filtering",
        "bulkExportCapability": "paginated card and expansion endpoints; credit-metered",
        "stableProviderIds": True,
        "imageHostBehaviour": "images.scrydex.com; image access advertised for application UI",
        "cachingPolicy": "caching recommended, but terms restrict mirroring and wholesale competing data use",
        "selfHostingOrRehostingPolicy": "terms prohibit redistribution or mirroring of the Services without prior written authorization",
        "attributionRequirements": "third-party rights remain with their owners",
        "commercialUseStatus": "paid commercial API subject to anti-redistribution and fair-use restrictions",
        "termsReviewDate": TERMS_REVIEW_DATE,
        "termsStatus": "prohibited",
        "metadataTermsStatus": "pending_human_review",
        "imageRehostingStatus": "prohibited_without_written_authorization",
        "adapterImplementationStatus": "credential_preflight_prepared_no_paid_requests_executed",
    },
    {
        "provider": "ximilar",
        "officialDocumentationUrl": "https://docs.ximilar.com/collectibles/recognition",
        "termsUrl": "https://www.ximilar.com/",
        "supportedLanguages": ["recognition_not_catalogue_source"],
        "supportedRegions": [],
        "metadataCoverage": "recognition result only; not a canonical catalogue source",
        "imageCoverage": "none licensed for catalogue ingestion",
        "authenticationMethod": "Authorization: Token",
        "requiredEnvironmentVariables": ["XIMILAR_API_TOKEN"],
        "freePlanLimits": "1,000 credits/month; TCG identification currently costs 10 credits",
        "paidPlanRequirements": "paid credit plans/packs for scale; never auto-purchase",
        "requestLimits": "credit-metered",
        "pagination": "not applicable to recognition-only use",
        "bulkExportCapability": "none",
        "stableProviderIds": False,
        "imageHostBehaviour": "accepts user-supplied image URLs or bytes for recognition",
        "cachingPolicy": "not applicable to artwork acquisition",
        "selfHostingOrRehostingPolicy": "not an artwork source",
        "attributionRequirements": "subject to Ximilar account terms",
        "commercialUseStatus": "recognition only, subject to credits and account terms",
        "termsReviewDate": TERMS_REVIEW_DATE,
        "termsStatus": "approved_with_conditions",
        "metadataTermsStatus": "prohibited_as_catalogue_source",
        "imageRehostingStatus": "not_applicable",
        "adapterImplementationStatus": "credential_preflight_only_recognition_reserved",
    },
    {
        "provider": "official_regional_pokemon_catalogues",
        "officialDocumentationUrl": "https://www.pokemon.com/",
        "termsUrl": "provider_and_region_specific",
        "supportedLanguages": ["regional_web_catalogues"],
        "supportedRegions": ["CN", "TW", "HK", "KR", "TH", "ID", "JP"],
        "metadataCoverage": "varies by regional consumer website",
        "imageCoverage": "consumer-site images",
        "authenticationMethod": "no documented developer API discovered",
        "requiredEnvironmentVariables": [],
        "freePlanLimits": "not applicable",
        "paidPlanRequirements": "not applicable",
        "requestLimits": "not documented for developer ingestion",
        "pagination": "not documented",
        "bulkExportCapability": "none documented",
        "stableProviderIds": False,
        "imageHostBehaviour": "consumer-facing websites only",
        "cachingPolicy": "no documented redistribution permission found",
        "selfHostingOrRehostingPolicy": "unclear",
        "attributionRequirements": "official intellectual-property notices apply",
        "commercialUseStatus": "unclear",
        "termsReviewDate": TERMS_REVIEW_DATE,
        "termsStatus": "pending_human_review",
        "metadataTermsStatus": "pending_human_review",
        "imageRehostingStatus": "pending_human_review",
        "adapterImplementationStatus": "not_implemented_no_scraping_or_browser_automation",
    },
)


@dataclass(frozen=True)
class CredentialDefinition:
    provider: str
    environment_variables: tuple[str, ...]
    required_for_current_metadata: bool
    account_url: str
    plan: str
    free_quota: str
    required_scopes: tuple[str, ...]
    reason: str


CREDENTIAL_DEFINITIONS: tuple[CredentialDefinition, ...] = (
    CredentialDefinition(
        "tcgdex",
        (),
        False,
        "https://tcgdex.dev/",
        "No account required",
        "Free; no published hard rate limit",
        (),
        "No credential is required.",
    ),
    CredentialDefinition(
        "pokemon_tcg_api",
        ("POKEMON_TCG_API_KEY",),
        False,
        "https://dev.pokemontcg.io/",
        "Free key (optional for higher quota)",
        "Anonymous 1,000/day and 30/minute; keyed default 20,000/day",
        ("read-only cards and sets API access",),
        "Optional higher-quota validation and English metadata reconciliation.",
    ),
    CredentialDefinition(
        "pokewallet",
        ("POKEWALLET_API_KEY",),
        True,
        "https://www.pokewallet.io/dashboard",
        "Free",
        "100/hour and 1,000/day",
        ("read-only cards, sets, search, and image endpoints",),
        "Authenticated localized metadata/image coverage; bulk image use remains at the terms gate.",
    ),
    CredentialDefinition(
        "scrydex",
        ("SCRYDEX_API_KEY", "SCRYDEX_TEAM_ID"),
        True,
        "https://scrydex.com/register",
        "Paid Starter, US$29/month",
        "No current $0 plan found; Starter includes 5,000 credits",
        ("read-only Pokémon cards, expansions, and image URLs",),
        "English/Japanese supplemental coverage, only after paid-spend and written-authorization gates.",
    ),
    CredentialDefinition(
        "ximilar",
        ("XIMILAR_API_TOKEN",),
        False,
        "https://app.ximilar.com/",
        "Free recognition plan",
        "1,000 credits/month; 10 credits per TCG identification",
        ("TCG recognition endpoint only",),
        "Optional recognition of user-owned captures; never used as an artwork source.",
    ),
)


def provider_ledger_payload() -> dict[str, Any]:
    return {
        "schemaVersion": "1.0.0",
        "generatedAtUtc": f"{TERMS_REVIEW_DATE}T00:00:00Z",
        "programmePolicy": {
            "metadataLicenceDoesNotGrantArtworkRights": True,
            "fullImageArchiveRequiresApprovedImageRehostingStatus": True,
            "paidProviderSpendUsdWithoutApproval": 0,
        },
        "providers": list(PROVIDER_LEDGER),
    }


def render_provider_ledger_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Global Provider and Terms Ledger",
        "",
        f"Terms reviewed: **{TERMS_REVIEW_DATE}**",
        "",
        "Metadata access and artwork redistribution are evaluated separately. No provider is treated as "
        "granting artwork redistribution merely because its metadata or client code is open source.",
        "",
    ]
    for provider in payload["providers"]:
        lines.extend(
            [
                f"## {provider['provider']}",
                f"- Terms status: **{provider['termsStatus']}**",
                f"- Metadata: {provider['metadataTermsStatus']}",
                f"- Image rehosting: {provider['imageRehostingStatus']}",
                f"- Authentication: {provider['authenticationMethod']}",
                f"- Required environment variables: {', '.join(provider['requiredEnvironmentVariables']) or 'none'}",
                f"- Free limits: {provider['freePlanLimits']}",
                f"- Paid requirements: {provider['paidPlanRequirements']}",
                f"- Adapter: {provider['adapterImplementationStatus']}",
                f"- Documentation: {provider['officialDocumentationUrl']}",
                f"- Terms: {provider['termsUrl']}",
                "",
            ]
        )
    return "\n".join(lines)


def write_provider_ledger() -> dict[str, Any]:
    payload = provider_ledger_payload()
    json_path = REPORT_DIR / "provider_ledger.json"
    markdown_path = REPORT_DIR / "provider_ledger.md"
    write_json_atomic(json_path, payload)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(
        render_provider_ledger_markdown(payload),
        encoding="utf-8",
    )
    return payload


def _load_local_credentials() -> dict[str, Any]:
    if not LOCAL_CREDENTIAL_PATH.exists():
        return {}
    try:
        payload = json.loads(LOCAL_CREDENTIAL_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    providers = payload.get("providers")
    return providers if isinstance(providers, dict) else payload


def _credential_value(
    definition: CredentialDefinition,
    variable: str,
    local: dict[str, Any],
) -> str:
    environment_value = os.getenv(variable, "").strip()
    if environment_value:
        return environment_value
    provider_payload = local.get(definition.provider)
    if isinstance(provider_payload, dict):
        value = provider_payload.get(variable)
        if value and not str(value).startswith("<"):
            return str(value).strip()
    value = local.get(variable)
    if value and not str(value).startswith("<"):
        return str(value).strip()
    return ""


def _quota_headers(response: requests.Response) -> dict[str, int | str]:
    result: dict[str, int | str] = {}
    names = (
        "X-RateLimit-Limit-Hour",
        "X-RateLimit-Remaining-Hour",
        "X-RateLimit-Limit-Day",
        "X-RateLimit-Remaining-Day",
        "X-RateLimit-Limit",
        "X-RateLimit-Remaining",
        "X-RateLimit-Reset",
        "Retry-After",
    )
    for name in names:
        value = response.headers.get(name)
        if value is None:
            continue
        try:
            result[name] = int(value)
        except ValueError:
            result[name] = "present"
    return result


def _validate_credential(
    definition: CredentialDefinition,
    values: dict[str, str],
) -> tuple[str, dict[str, Any]]:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        if definition.provider == "tcgdex":
            response = session.get("https://api.tcgdex.net/status", timeout=30)
        elif definition.provider == "pokemon_tcg_api":
            headers = {}
            if values.get("POKEMON_TCG_API_KEY"):
                headers["X-Api-Key"] = values["POKEMON_TCG_API_KEY"]
            response = session.get(
                "https://api.pokemontcg.io/v2/types",
                headers=headers,
                timeout=30,
            )
        elif definition.provider == "pokewallet":
            if not values.get("POKEWALLET_API_KEY"):
                return "not tested", {"state": "missing_key"}
            response = session.get(
                "https://api.pokewallet.io/search",
                params={"q": "__cardscanr_credential_probe__", "limit": 1},
                headers={"X-API-Key": values["POKEWALLET_API_KEY"]},
                timeout=30,
            )
        elif definition.provider == "scrydex":
            if not values.get("SCRYDEX_API_KEY") or not values.get("SCRYDEX_TEAM_ID"):
                return "not tested", {"state": "missing_key_or_team_id"}
            response = session.get(
                "https://api.scrydex.com/pokemon/v1/expansions",
                params={"page": 1, "page_size": 1},
                headers={
                    "X-Api-Key": values["SCRYDEX_API_KEY"],
                    "X-Team-ID": values["SCRYDEX_TEAM_ID"],
                },
                timeout=30,
            )
        else:
            return "not tested", {"state": "validation_not_implemented"}
    except requests.RequestException as exc:
        return "failed", {"state": "network_error", "errorType": type(exc).__name__}

    account_state: dict[str, Any] = {
        "state": "reachable" if response.status_code < 500 else "provider_unavailable",
        "httpStatus": response.status_code,
        "quota": _quota_headers(response),
    }
    if response.status_code == 429:
        account_state["state"] = "rate_limited"
    if response.status_code in {401, 403}:
        account_state["state"] = "credential_rejected_or_plan_blocked"
        return "failed", account_state
    if 200 <= response.status_code < 300:
        return "passed", account_state
    return "failed", account_state


def credential_status(
    *,
    validate: bool = False,
    provider_filter: str | None = None,
) -> dict[str, Any]:
    local = _load_local_credentials()
    providers: list[dict[str, Any]] = []
    for definition in CREDENTIAL_DEFINITIONS:
        if provider_filter and definition.provider != provider_filter:
            continue
        values = {
            variable: _credential_value(definition, variable, local)
            for variable in definition.environment_variables
        }
        credential_not_required = not definition.environment_variables
        present = (
            all(bool(values.get(variable)) for variable in definition.environment_variables)
            if definition.environment_variables
            else False
        )
        validation = "not tested"
        account_state: dict[str, Any] = {
            "state": (
                "no_account_required"
                if not definition.environment_variables
                else "configured"
                if present
                else "not_configured"
            )
        }
        if validate and (present or not definition.environment_variables or definition.provider == "pokemon_tcg_api"):
            validation, account_state = _validate_credential(definition, values)
        providers.append(
            {
                "provider": definition.provider,
                "keyPresent": (
                    "not required"
                    if credential_not_required
                    else "yes" if present else "no"
                ),
                "keyValidation": validation,
                "accountQuotaState": account_state,
                "requiredEnvironmentVariables": list(definition.environment_variables),
                "requiredForCurrentMetadata": definition.required_for_current_metadata,
                "accountUrl": definition.account_url,
                "plan": definition.plan,
                "freeQuota": definition.free_quota,
                "requiredPermissionScopes": list(definition.required_scopes),
                "reason": definition.reason,
                "configurationFile": LOCAL_CREDENTIAL_PATH.relative_to(ROOT).as_posix(),
                "validationCommand": (
                    "python tools/global_rollout.py credentials-status "
                    f"--provider {definition.provider} --validate"
                ),
                "resumeCommand": "python tools/global_rollout.py resume",
            }
        )
    return {
        "schemaVersion": "1.0.0",
        "secretsRedacted": True,
        "providers": providers,
    }


def write_credential_status_reports(payload: dict[str, Any]) -> None:
    write_json_atomic(REPORT_DIR / "credential_status.json", payload)
    lines = [
        "# Provider Credential Status",
        "",
        "No secret value or partial key is included in this report.",
        "",
    ]
    for item in payload["providers"]:
        lines.extend(
            [
                f"## {item['provider']}",
                f"- Key present: **{item['keyPresent']}**",
                f"- Validation: {item['keyValidation']}",
                f"- Account/quota state: `{item['accountQuotaState']}`",
                f"- Account: {item['accountUrl']}",
                f"- Plan: {item['plan']}",
                f"- Free quota: {item['freeQuota']}",
                f"- Environment variables: {', '.join(item['requiredEnvironmentVariables']) or 'none'}",
                f"- Required scopes: {', '.join(item['requiredPermissionScopes']) or 'none'}",
                f"- Reason: {item['reason']}",
                f"- Local configuration: `{item['configurationFile']}`",
                f"- Validation command: `{item['validationCommand']}`",
                f"- Resume command: `{item['resumeCommand']}`",
                "",
            ]
        )
    (REPORT_DIR / "credential_status.md").write_text(
        "\n".join(lines),
        encoding="utf-8",
    )
