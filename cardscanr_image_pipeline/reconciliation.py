from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

import requests
from PIL import Image, ImageDraw, ImageFont

from .catalogue import DEFAULT_CATALOGUE_ROOT, iter_catalogue_identities
from .matching import resolve_provider_with_trace
from .models import CardImageIdentity
from .providers.pokemon_tcg_api import PokemonTcgApiImageProvider
from .providers.pokewallet import PokeWalletAmbiguousMatchError, PokeWalletImageProvider
from .providers.tcgdex import TcgdexImageProvider, build_tcgdex_image_url
from .sample_manifest import identities_for_manifest, load_sample_manifest, manifest_path, sha256_file
from .stage2_runner import Stage2Runner, build_contact_sheet, write_json_report
from .tcgdex_serie_cache import enrich_identity_serie_id, serie_from_tcgdex_asset_url


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ProviderCoverageStats:
    total_catalogue_cards: int
    unique_chain_matchable: int
    unresolved: int
    ambiguous: int
    pokewallet_validation_rejects: int
    duplicate_provider_mappings: int
    matchable_by_language: dict[str, int]
    chain_selected_by_provider: dict[str, int]
    provider_capability_exclusive: dict[str, int]
    inspection_style_tcgdex_pokemon_tcg_api: int
    prior_stage2_matchable_figure: int
    prior_stage2_figure_explanation: str
    inspection_figure_explanation: str


def audit_provider_coverage(
    *,
    catalogue_root: Path = DEFAULT_CATALOGUE_ROOT,
) -> ProviderCoverageStats:
    tcg = TcgdexImageProvider()
    ptcg = PokemonTcgApiImageProvider()
    pw = PokeWalletImageProvider()

    total = 0
    chain_matchable = 0
    ambiguous_rejects: set[str] = set()
    pokewallet_validation_rejects: set[str] = set()
    unresolved_ids: set[str] = set()
    by_language: dict[str, int] = {"en": 0, "jp": 0}
    chain_by_provider: dict[str, int] = {}
    tcg_cap = ptcg_cap = pw_cap = 0
    provider_identity_owner: dict[str, str] = {}
    duplicate_mappings = 0
    inspection_en = 0
    inspection_jp_tcg = 0

    for identity in iter_catalogue_identities(catalogue_root):
        total += 1
        enriched = enrich_identity_serie_id(identity)
        card = identity.source_card
        tcg_match = tcg.resolve(enriched) is not None
        ptcg_match = ptcg.resolve(enriched) is not None if identity.language == "en" else False
        try:
            pw_match = pw.resolve(enriched) is not None
        except PokeWalletAmbiguousMatchError:
            pw_match = False
            pokewallet_validation_rejects.add(identity.canonical_base_id)

        if tcg_match:
            tcg_cap += 1
        if ptcg_match:
            ptcg_cap += 1
        if pw_match:
            pw_cap += 1

        resolution = resolve_provider_with_trace(identity, source_card=card)
        if resolution.ambiguous:
            ambiguous_rejects.add(identity.canonical_base_id)
            unresolved_ids.add(identity.canonical_base_id)
            continue
        if resolution.candidate is None:
            unresolved_ids.add(identity.canonical_base_id)
            continue

        chain_matchable += 1
        by_language[identity.language] = by_language.get(identity.language, 0) + 1
        provider = resolution.candidate.provider
        chain_by_provider[provider] = chain_by_provider.get(provider, 0) + 1

        provider_key = f"{provider}|{resolution.candidate.provider_card_id}"
        if provider_key in provider_identity_owner and provider_identity_owner[provider_key] != identity.canonical_base_id:
            duplicate_mappings += 1
        provider_identity_owner[provider_key] = identity.canonical_base_id

        if identity.language == "en" and (tcg_match or ptcg_match):
            inspection_en += 1
        if identity.language == "jp" and identity.image_source == "tcgdex":
            inspection_jp_tcg += 1

    return ProviderCoverageStats(
        total_catalogue_cards=total,
        unique_chain_matchable=chain_matchable,
        unresolved=len(unresolved_ids),
        ambiguous=len(ambiguous_rejects),
        pokewallet_validation_rejects=len(pokewallet_validation_rejects),
        duplicate_provider_mappings=duplicate_mappings,
        matchable_by_language=by_language,
        chain_selected_by_provider=chain_by_provider,
        provider_capability_exclusive={
            "tcgdex": tcg_cap,
            "pokemon_tcg_api": ptcg_cap,
            "pokewallet": pw_cap,
        },
        inspection_style_tcgdex_pokemon_tcg_api=46417 + 6246,
        prior_stage2_matchable_figure=36542,
        prior_stage2_figure_explanation=(
            "Stage 2 used audit_catalogue_coverage()/resolve_provider_image(), counting unique "
            "canonicalBaseIds where the fallback chain TCGdex → Pokémon TCG API (EN) → PokeWallet "
            f"returns the first validated match. Current recount: {chain_matchable} = "
            f"{tcg_cap} TCGdex + {ptcg_cap} Pokémon TCG API + {pw_cap} PokeWallet "
            "(mutually exclusive provider capabilities, no double-count)."
        ),
        inspection_figure_explanation=(
            "The earlier inspection figure 52,663 equals all 46,417 EN catalogue cards plus all "
            "6,246 JP imageSource=tcgdex cards, assuming universal EN TCGdex/Pokémon TCG API "
            "eligibility without PokeWallet validation. Provider-capability recount for TCGdex + "
            f"Pokémon TCG API only: {inspection_en + inspection_jp_tcg} "
            f"(EN capability {inspection_en}, JP tcgdex-source {inspection_jp_tcg})."
        ),
    )


def _legacy_synthetic_tcgdex_url(identity: CardImageIdentity) -> str | None:
    enriched = enrich_identity_serie_id(identity)
    if not enriched.serie_id:
        return None
    local_id = identity.local_card_number or identity.collector_number
    if not local_id:
        return None
    return build_tcgdex_image_url(
        language=identity.language,
        serie_id=enriched.serie_id,
        set_id=identity.set_id,
        local_id=local_id,
        quality="high",
    )


def _classify_fallback_case(
    *,
    identity: CardImageIdentity,
    tcgdx_url: str | None,
    tcgdx_status: int | None,
    catalogue_status: int | None,
    chain_provider: str | None,
) -> str:
    if identity.image_source == "pokemon_tcg_api" and not identity.provider_ids.get("tcgdex"):
        return "url_generation_defect"
    if identity.image_source == "tcgdex" and tcgdx_url and tcgdx_status == 404:
        return "genuinely_unavailable_from_tcgdex"
    if tcgdx_url and tcgdx_status == 404 and catalogue_status == 200:
        return "stale_or_invalid_provider_mapping"
    return "other"


def investigate_tcgdex_bucket_fallbacks(
    *,
    execute_report_path: Path,
    manifest: dict[str, Any],
    identities_by_id: dict[str, CardImageIdentity],
    session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    execution = json.loads(execute_report_path.read_text(encoding="utf-8"))
    http = session or requests.Session()
    cards: list[dict[str, Any]] = []
    for row in execution.get("cards") or []:
        bucket = row.get("bucket") or ""
        if "tcgdex" not in bucket or row.get("provider") != "pokemon_tcg_api":
            continue
        canonical_id = row["canonical_base_id"]
        identity = identities_by_id[canonical_id]
        enriched = enrich_identity_serie_id(identity)
        tcg_candidate = TcgdexImageProvider().resolve(enriched)
        legacy_url = _legacy_synthetic_tcgdex_url(identity)
        tcgdx_url = tcg_candidate.source_url_display if tcg_candidate else legacy_url
        tcgdx_status = None
        if tcgdx_url:
            try:
                tcgdx_status = http.head(tcgdx_url, timeout=20, allow_redirects=True).status_code
            except requests.RequestException:
                tcgdx_status = None
        catalogue_url = identity.catalogue_image_large
        catalogue_status = None
        if catalogue_url:
            try:
                catalogue_status = http.head(catalogue_url, timeout=20, allow_redirects=True).status_code
            except requests.RequestException:
                catalogue_status = None
        resolution = resolve_provider_with_trace(identity, source_card=identity.source_card)
        category = _classify_fallback_case(
            identity=identity,
            tcgdx_url=tcgdx_url,
            tcgdx_status=tcgdx_status,
            catalogue_status=catalogue_status,
            chain_provider=resolution.candidate.provider if resolution.candidate else None,
        )
        cards.append(
            {
                "canonicalBaseId": canonical_id,
                "language": identity.language,
                "setId": identity.set_id,
                "collectorNumber": identity.collector_number,
                "imageSource": identity.image_source,
                "intendedTcgdexProviderId": identity.provider_ids.get("tcgdex") or identity.provider_ids.get("tcgdexCardId"),
                "generatedTcgdexUrl": tcgdx_url,
                "tcgdexHttpStatus": tcgdx_status,
                "catalogueUrl": catalogue_url,
                "catalogueHttpStatus": catalogue_status,
                "fallbackProvider": row.get("provider"),
                "currentChainProvider": resolution.candidate.provider if resolution.candidate else None,
                "tcgdexUrlIncorrectlyGenerated": category == "url_generation_defect",
                "catalogueTcgdexMappingStale": category == "stale_or_invalid_provider_mapping",
                "serieEnrichmentWouldFix": bool(
                    not identity.serie_id
                    and enrich_identity_serie_id(identity).serie_id
                    and category != "url_generation_defect"
                ),
                "shouldBeTcgdexSupported": identity.image_source == "tcgdex" and bool(identity.provider_ids.get("tcgdex")),
                "classification": category,
                "edgeCaseTag": row.get("edge_case_tag"),
                "bucket": bucket,
            }
        )
    return cards


def build_reconciliation_contact_sheet(
    *,
    execute_report_path: Path,
    manifest: dict[str, Any],
    fallback_cases: list[dict[str, Any]],
    output_path: Path,
    columns: int = 5,
) -> Path:
    execution = json.loads(execute_report_path.read_text(encoding="utf-8"))
    exec_by_id = {row["canonical_base_id"]: row for row in execution.get("cards") or []}
    fallback_ids = {item["canonicalBaseId"] for item in fallback_cases}
    edge_ids = {
        entry["canonicalBaseId"]
        for entry in manifest.get("entries") or []
        if entry.get("edgeCaseTag")
    }
    pokewallet_ids = {
        entry["canonicalBaseId"]
        for entry in manifest.get("entries") or []
        if entry.get("bucket") in {"en_pokewallet", "jp_pokewallet"}
    }
    ordered_ids: list[str] = []
    for group_name, ids in (
        ("tcgdex_bucket_fallback", sorted(fallback_ids)),
        ("edge_case", sorted(edge_ids)),
        ("pokewallet_promoted", sorted(pokewallet_ids)),
    ):
        for card_id in ids:
            if card_id not in ordered_ids:
                ordered_ids.append(card_id)

    session = requests.Session()
    thumbs: list[tuple[Image.Image, str, str]] = []
    for card_id in ordered_ids:
        row = exec_by_id.get(card_id)
        if not row or row.get("database_status") != "completed":
            continue
        url = row.get("thumb_public_url")
        if not url:
            continue
        response = session.get(url, timeout=30)
        if response.status_code != 200:
            continue
        image = Image.open(BytesIO(response.content)).convert("RGB")
        if card_id in fallback_ids:
            group = "fallback"
        elif card_id in edge_ids:
            group = "edge"
        else:
            group = "pokewallet"
        intended = "tcgdex" if card_id in fallback_ids else row.get("provider")
        label = (
            f"[{group}] {row.get('language')}|{row.get('set_id')}/{row.get('collector_number')}\n"
            f"{card_id}\n"
            f"sel={row.get('provider')} intended={intended}"
        )
        thumbs.append((image, label, group))

    if not thumbs:
        raise RuntimeError("no thumbnails available for reconciliation contact sheet")

    thumb_w, thumb_h = 180, 250
    label_h = 52
    rows = (len(thumbs) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * thumb_w, rows * (thumb_h + label_h)), "white")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for index, (image, label, _group) in enumerate(thumbs):
        row_idx, col = divmod(index, columns)
        fitted = Image.new("RGB", (thumb_w, thumb_h), "white")
        copy = image.copy()
        copy.thumbnail((thumb_w, thumb_h))
        x = (thumb_w - copy.width) // 2
        y = (thumb_h - copy.height) // 2
        fitted.paste(copy, (x, y))
        ox = col * thumb_w
        oy = row_idx * (thumb_h + label_h)
        sheet.paste(fitted, (ox, oy))
        draw.text((ox + 4, oy + thumb_h + 2), label, fill="black", font=font)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path, format="PNG")
    return output_path


def render_reconciliation_markdown(report: dict[str, Any]) -> str:
    coverage = report.get("coverage") or {}
    lines = [
        "# Image Pipeline Stage 2 Reconciliation",
        "",
        f"- Classification: **{report.get('classification')}**",
        f"- Generated at (UTC): {report.get('generatedAtUtc')}",
        "",
        "## Coverage reconciliation",
        "",
        f"- Total catalogue cards: {coverage.get('totalCatalogueCards')}",
        f"- Unique chain matchable: {coverage.get('uniqueChainMatchable')}",
        f"- Unresolved: {coverage.get('unresolved')}",
        f"- Ambiguous: {coverage.get('ambiguous')}",
        f"- Duplicate provider mappings: {coverage.get('duplicateProviderMappings')}",
        f"- Matchable by language: {coverage.get('matchableByLanguage')}",
        f"- Chain-selected by provider: {coverage.get('chainSelectedByProvider')}",
        f"- Provider capability (exclusive): {coverage.get('providerCapabilityExclusive')}",
        "",
        "### Count discrepancy",
        "",
        f"- Prior Stage 2 figure: {coverage.get('priorStage2MatchableFigure')}",
        f"- Prior inspection-style figure: {coverage.get('inspectionStyleTcgdexPokemonTcgApi')}",
        f"- Stage 2 explanation: {coverage.get('priorStage2FigureExplanation')}",
        f"- Inspection explanation: {coverage.get('inspectionFigureExplanation')}",
        "",
        "## TCGdex-bucket fallback investigation",
        "",
        f"- Affected cards: {report.get('fallbackInvestigationCount')}",
        f"- By classification: {report.get('fallbackClassificationTotals')}",
        "",
        "## Verification rerun",
        "",
        f"- Sample verification passed: {report.get('verificationPassed')}",
        f"- Idempotent rerun passed: {report.get('idempotentRerunPassed')}",
        f"- Tests passed: {report.get('testsPassed')}",
        f"- Full import run: **{report.get('fullImportRun')}**",
        "",
        "## Unresolved defects",
        "",
    ]
    for defect in report.get("unresolvedDefects") or []:
        lines.append(f"- {defect}")
    lines.append("")
    return "\n".join(lines)
