#!/usr/bin/env python3
"""
report_market_price_provider_capabilities.py

Writes a capability report for all registered market price providers.

Outputs:
  reports/market_price_provider_capabilities_latest.json
  reports/market_price_provider_capabilities_latest.md

No live network calls.  No secrets required.  Safe for cloud/Codex.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure tools/ is on the import path when run from project root
ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = ROOT / "tools"
sys.path.insert(0, str(TOOLS_DIR))

from market_pricing_provider_contracts import MarketPriceProviderCapabilities  # noqa: E402
from market_price_providers.provider_registry import (  # noqa: E402
    MarketPriceProviderRegistry,
    LIVE_PROVIDER_NAMES,
)

REPORTS_DIR = ROOT / "reports"
JSON_OUT = REPORTS_DIR / "market_price_provider_capabilities_latest.json"
MD_OUT = REPORTS_DIR / "market_price_provider_capabilities_latest.md"


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _caps_to_dict(caps: MarketPriceProviderCapabilities) -> dict[str, Any]:
    return {
        "providerName": caps.provider_name,
        "enabled": caps.enabled,
        "liveNetworkRequired": caps.live_network_required,
        "requiresCredentials": caps.requires_credentials,
        "supportedMarkets": list(caps.supported_markets),
        "supportedLanguages": list(caps.supported_languages),
        "supportedCurrencies": list(caps.supported_currencies),
        "returnsEvidenceListings": caps.returns_evidence_listings,
        "returnsConfidenceScore": caps.returns_confidence_score,
        "safeForCloud": caps.safe_for_cloud,
        "nextImplementationStep": caps.next_implementation_step,
        "notes": caps.notes,
    }


def build_report(registry: MarketPriceProviderRegistry) -> dict[str, Any]:
    all_caps = registry.capabilities()
    enabled = [c for c in all_caps if c.enabled]
    disabled = [c for c in all_caps if not c.enabled]

    return {
        "schemaVersion": "1.0.0",
        "generatedAtUtc": _utc_now(),
        "liveEbayScrapingEnabled": False,
        "liveEbayDisabledNote": (
            "Live eBay access is disabled until provider/legal/terms approach is approved."
        ),
        "summary": {
            "registeredProviders": registry.registered_names(),
            "enabledProviders": [c.provider_name for c in enabled],
            "disabledProviders": [c.provider_name for c in disabled],
            "defaultAllowedProviders": ["mock", "manual"],
            "nextRecommendedProviderStep": (
                "Decide on eBay access method: "
                "(a) eBay Browse API with OAuth, "
                "(b) Apify actor, "
                "(c) local browser worker. "
                "Then implement as a new provider module and add to the registry allow-list "
                "after legal/terms sign-off."
            ),
        },
        "providers": [_caps_to_dict(c) for c in all_caps],
    }


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    a = lines.append
    a("# Market Price Provider Capabilities")
    a("")
    a(f"Generated: {report['generatedAtUtc']}")
    a("")
    a(f"> **Live eBay scraping enabled: no**  ")
    a(f"> {report['liveEbayDisabledNote']}")
    a("")
    summary = report.get("summary", {})
    a("## Summary")
    a("")
    a(f"- Registered: {', '.join(summary.get('registeredProviders', []))}")
    a(f"- Enabled: {', '.join(summary.get('enabledProviders', []))}")
    a(f"- Disabled: {', '.join(summary.get('disabledProviders', []))}")
    a(f"- Default allowed: {', '.join(summary.get('defaultAllowedProviders', []))}")
    a("")
    a("### Next recommended provider step")
    a("")
    a(summary.get("nextRecommendedProviderStep", ""))
    a("")
    a("## Provider details")
    a("")

    for provider in report.get("providers", []):
        enabled_flag = "✅ enabled" if provider["enabled"] else "🚫 disabled"
        a(f"### {provider['providerName']} — {enabled_flag}")
        a("")
        a(f"- Live network required: {'yes' if provider['liveNetworkRequired'] else 'no'}")
        a(f"- Secrets required: {'yes' if provider['requiresCredentials'] else 'no'}")
        a(f"- Safe for cloud/Codex: {'yes' if provider['safeForCloud'] else 'no'}")
        a(f"- Returns evidence listings: {'yes' if provider['returnsEvidenceListings'] else 'no'}")
        a(f"- Returns confidence score: {'yes' if provider['returnsConfidenceScore'] else 'no'}")
        a(f"- Supported markets: {', '.join(provider['supportedMarkets'])}")
        a(f"- Supported languages: {', '.join(provider['supportedLanguages'])}")
        a(f"- Supported currencies: {', '.join(provider['supportedCurrencies'])}")
        a(f"- Next step: {provider['nextImplementationStep']}")
        if provider.get("notes"):
            a(f"- Notes: {provider['notes']}")
        a("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def main() -> None:
    registry = MarketPriceProviderRegistry()
    report = build_report(registry)
    _write_json(JSON_OUT, report)
    MD_OUT.parent.mkdir(parents=True, exist_ok=True)
    MD_OUT.write_text(render_markdown(report), encoding="utf-8")

    print("Provider capability reports written:")
    print(f"  {JSON_OUT.relative_to(ROOT).as_posix()}")
    print(f"  {MD_OUT.relative_to(ROOT).as_posix()}")
    print(f"  enabled providers: {report['summary']['enabledProviders']}")
    print(f"  disabled providers: {report['summary']['disabledProviders']}")
    print("  liveEbayScrapingEnabled: false")


if __name__ == "__main__":
    main()
