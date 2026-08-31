#!/usr/bin/env python3
"""Stable CardScanR physical product / set identity resolution (V1.1)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SLUG = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class PhysicalProductResolution:
    physical_product_id: str | None
    authority_status: str
    reason: str
    provider_set_id: str | None = None
    canonical_set_id: str | None = None
    product_class: str | None = None
    display_name: str | None = None


class PhysicalProductIdentityRegistry:
    def __init__(self, payload: dict[str, Any]):
        self.payload = payload
        self.by_id: dict[str, dict[str, Any]] = {
            str(p["physicalProductId"]): p
            for p in payload.get("products") or []
            if isinstance(p, dict) and p.get("physicalProductId")
        }
        self.by_provider: dict[str, str] = {}
        for product in self.by_id.values():
            for provider_id in product.get("providerSetIds") or []:
                self.by_provider[str(provider_id)] = str(product["physicalProductId"])
            canonical = str(product.get("canonicalSetId") or "").strip()
            if canonical:
                self.by_provider.setdefault(canonical, str(product["physicalProductId"]))

    @classmethod
    def load(cls, path: Path) -> "PhysicalProductIdentityRegistry":
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def resolve(
        self,
        *,
        provider_set_id: object = None,
        mapped_canonical_set_id: object = None,
        matching_canonical_set_id: object = None,
        match_policy: object = None,
    ) -> PhysicalProductResolution:
        mapped = str(mapped_canonical_set_id or "").strip()
        matching = str(matching_canonical_set_id or "").strip()
        provider = str(provider_set_id or "").strip()
        policy = str(match_policy or "").strip()

        # Prefer explicit mapped canonical / registry entry over raw provider ids.
        for candidate in (mapped, matching, provider):
            if not candidate:
                continue
            if candidate.isdigit():
                pid = self.by_provider.get(candidate)
                if pid:
                    product = self.by_id[pid]
                    return PhysicalProductResolution(
                        physical_product_id=pid,
                        authority_status=str(product.get("authorityStatus") or "PROVEN"),
                        reason="provider_alias_registry",
                        provider_set_id=provider or None,
                        canonical_set_id=str(product.get("canonicalSetId") or "") or None,
                        product_class=str(product.get("productClass") or "") or None,
                        display_name=str(product.get("displayName") or "") or None,
                    )
                # Numeric provider with no registry mapping → pending
                if candidate == provider:
                    return PhysicalProductResolution(
                        physical_product_id=None,
                        authority_status="AUTHORITY_PENDING",
                        reason="unmapped_numeric_provider_set_id",
                        provider_set_id=provider,
                    )
                continue
            # Non-numeric: treat as stable physical product / canonical set id.
            if candidate in self.by_id:
                product = self.by_id[candidate]
                return PhysicalProductResolution(
                    physical_product_id=candidate,
                    authority_status=str(product.get("authorityStatus") or "PROVEN"),
                    reason="registry_physical_product_id",
                    provider_set_id=provider or None,
                    canonical_set_id=str(product.get("canonicalSetId") or candidate) or None,
                    product_class=str(product.get("productClass") or "") or None,
                    display_name=str(product.get("displayName") or "") or None,
                )
            # Unknown non-numeric still acceptable as stable CardScanR set id.
            if not candidate.isdigit():
                return PhysicalProductResolution(
                    physical_product_id=candidate.casefold(),
                    authority_status="PROVEN" if mapped or matching else "HEURISTIC",
                    reason="canonical_or_stable_set_id",
                    provider_set_id=provider or None,
                    canonical_set_id=candidate.casefold(),
                    product_class=None,
                    display_name=None,
                )

        if policy == "PRODUCT_LOCAL_ONLY" and provider:
            return PhysicalProductResolution(
                physical_product_id=None,
                authority_status="AUTHORITY_PENDING",
                reason="product_local_only_without_stable_id",
                provider_set_id=provider,
            )
        return PhysicalProductResolution(
            physical_product_id=None,
            authority_status="AUTHORITY_PENDING",
            reason="unresolved_physical_product",
            provider_set_id=provider or None,
        )


def _slug(value: object) -> str:
    text = _SLUG.sub("_", str(value or "").casefold()).strip("_")
    return text or "unknown"


def build_registry_from_provider_set_registry(
    provider_registry: dict[str, Any],
) -> dict[str, Any]:
    """Derive stable physical product identities from the V5 provider set registry."""
    set_map: dict[str, Any] = {}
    orphan_maps = provider_registry.get("orphanMaps")
    by_provider = provider_registry.get("byProviderSetId")
    # Prefer orphanMaps (full provider-set records). byProviderSetId may hold
    # incomplete stubs that must not overwrite resolved orphan entries.
    if isinstance(by_provider, dict):
        set_map.update(by_provider)
    if isinstance(orphan_maps, dict):
        set_map.update(orphan_maps)
    if not set_map:
        entries = (
            provider_registry.get("sets")
            or provider_registry.get("providerSets")
            or provider_registry
        )
        if isinstance(entries, dict):
            set_map = entries

    products: dict[str, dict[str, Any]] = {}
    for provider_id, entry in set_map.items():
        if not isinstance(entry, dict):
            continue
        provider_id = str(provider_id)
        mapping = entry.get("mapping") if isinstance(entry.get("mapping"), dict) else {}
        canonical = str(mapping.get("canonicalSetId") or "").strip()
        product_type = str(
            mapping.get("productType") or entry.get("productType") or "UNKNOWN"
        ).strip()
        display = str(
            mapping.get("displayName")
            or entry.get("providerSetName")
            or provider_id
        ).strip()
        evidence = str(mapping.get("evidence") or "").strip()
        match_policy = str(mapping.get("matchPolicy") or entry.get("matchPolicy") or "").strip()

        if canonical:
            pid = canonical.casefold()
            authority = (
                "PROVEN"
                if evidence in {"PROVEN", "STRONG_EVIDENCE"} or entry.get("resolved")
                else "HEURISTIC"
            )
        elif match_policy == "PRODUCT_LOCAL_ONLY" or (
            entry.get("resolved") and not canonical
        ):
            # Explicit supplemental CardScanR product id — never the numeric provider id.
            pid = f"csr_supp_{_slug(product_type)}_{_slug(display)}"[:80]
            authority = "SUPPLEMENTAL_STABLE"
        elif entry.get("resolved"):
            pid = f"csr_supp_{_slug(display)}"[:80]
            authority = "SUPPLEMENTAL_STABLE"
        else:
            # Unresolved providers do not receive a durable physical product id.
            continue

        product = products.setdefault(
            pid,
            {
                "physicalProductId": pid,
                "canonicalSetId": canonical.casefold() if canonical else None,
                "productClass": product_type,
                "displayName": display,
                "providerAliases": [],
                "providerSetIds": [],
                "language": "en",
                "authorityStatus": authority,
            },
        )
        if display and display not in product["providerAliases"]:
            product["providerAliases"].append(display)
        if provider_id not in product["providerSetIds"]:
            product["providerSetIds"].append(provider_id)
        # Prefer PROVEN over weaker statuses when merging aliases.
        if authority == "PROVEN":
            product["authorityStatus"] = "PROVEN"

    return {
        "schemaVersion": "physical_product_identity_registry_v1",
        "identityModelVersion": "physical-printing-v1",
        "rule": "Provider numeric set IDs are aliases only; never durable physicalPrintingId set components.",
        "productCount": len(products),
        "products": sorted(products.values(), key=lambda p: str(p["physicalProductId"])),
    }
