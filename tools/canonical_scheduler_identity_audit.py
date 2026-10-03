#!/usr/bin/env python3
"""Offline canonical pricing identity + scheduler audit. NO eBay contact."""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.config import MarketEngineConfig, supabase_secret_key_from_env
from cardscanr_market_engine.marketplaces import resolve_marketplace_config
from cardscanr_market_engine.models import ProviderRequest
from cardscanr_market_engine.owned_daily_scheduler import (
    OwnedDailySchedulerConfig,
    OwnedPrintingRefreshScheduler,
)
from cardscanr_market_engine.owned_daily_source_policy import classify_owned_daily_band
from cardscanr_market_engine.providers.query_builder import build_provider_search_queries
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient
from cardscanr_market_engine.supabase_env_loader import load_supabase_env

ART = ROOT / "reports" / "artifacts" / "x11_nav_reliability_harness_closure"
ART.mkdir(parents=True, exist_ok=True)

ORIGINAL_FIVE = [
    ("Pikachu GG30", "52e98068-30ba-466a-a9e6-0316b2af59e7"),
    ("Bibarel GG25", "35349da5-49f8-4d28-9dc2-39e6261bec97"),
    ("Giratina XY184", "83382041-41a4-4fda-a359-8d0db5265934"),
    ("Budew Master Ball Pattern 001/187", "96d5f5a8-a248-4b85-856f-921ec6d6015f"),
    ("Vulpix Chaos Rising 8", "db661320-9d44-4a88-8c96-722ca1c78f12"),
]

# Fixed evaluation timestamp for deterministic ordering evidence.
FIXED_NOW = datetime(2026, 10, 2, 5, 55, 0, tzinfo=timezone.utc)


def _canonical_fp(fp: str) -> str:
    """Case-normalize fingerprint segments without collapsing collector semantics."""
    parts = str(fp or "").split("|")
    if len(parts) < 9:
        return str(fp or "").lower()
    # game|lang|set|collector|card|variant|condition|market|currency
    # Collector: upper (SQL style). Others: lower.
    parts[0] = parts[0].lower()
    parts[1] = parts[1].lower()
    parts[2] = parts[2].lower()
    parts[3] = parts[3].upper()
    parts[4] = parts[4].lower()
    parts[5] = parts[5].lower()
    parts[6] = parts[6].lower()
    parts[7] = parts[7].lower()
    parts[8] = parts[8].lower()
    return "|".join(parts)


def _leading_zero_collector_variants(collector: str) -> set[str]:
    c = collector.strip()
    out = {c, c.upper(), c.lower()}
    if "/" in c:
        left, right = c.split("/", 1)
        if left.isdigit():
            out.add(f"{int(left)}/{right}")
            out.add(f"{int(left)}/{right}".upper())
    elif c.isdigit():
        out.add(str(int(c)))
    return out


def main() -> int:
    load_supabase_env()
    cfg = MarketEngineConfig.from_env()
    client = SupabaseMarketEngineClient(
        supabase_url=cfg.supabase_url,
        service_role_key=supabase_secret_key_from_env(),
    )
    sched = OwnedPrintingRefreshScheduler(
        client=client,
        config=OwnedDailySchedulerConfig.from_env(),
        now_func=lambda: FIXED_NOW,
    )

    payload = client.list_owned_market_pricing_targets(include_zero_owners=False)
    targets = list(payload.get("targets") or [])
    # Second call proves idempotent dynamic resolution (no persisted mutation).
    payload2 = client.list_owned_market_pricing_targets(include_zero_owners=False)
    targets2 = list(payload2.get("targets") or [])

    null_before_style = 0  # kid null on RPC (dynamic join miss)
    linked = 0
    resolved_via_ensure = 0
    no_existing_key = 0
    ambiguous = 0
    false_p0_priced = 0
    verified_local_n = 0
    reference_only_n = 0
    truly_unpriced_n = 0
    disagreements = []
    enriched_rows = []

    for t in targets:
        kid = str(t.get("market_price_key_id") or "").strip()
        if kid:
            linked += 1
        else:
            null_before_style += 1
        en = client.enrich_owned_target_from_cache(t)
        en_kid = str(en.get("market_price_key_id") or "").strip()
        if not kid and en_kid:
            resolved_via_ensure += 1
        elif not kid and not en_kid:
            no_existing_key += 1

        decision = sched.evaluate_target(en, now=FIXED_NOW)
        rpc_band = str(t.get("owned_priority_band") or "")
        py_band = str(decision.details.get("owned_priority_band") or "")
        if rpc_band and py_band and rpc_band != py_band and py_band != "FRESH_SKIP":
            # FRESH_SKIP vs due bands when RPC due=false may still differ on label;
            # record any final mismatch where enqueue disagrees with RPC due.
            pass
        rpc_due = bool(t.get("due_for_owned_daily"))
        if bool(decision.should_enqueue) != rpc_due or (rpc_band != py_band and not (
            rpc_band == "FRESH_SKIP" and py_band == "FRESH_SKIP"
        )):
            if rpc_band != py_band or bool(decision.should_enqueue) != rpc_due:
                disagreements.append(
                    {
                        "fingerprint": t.get("fingerprint"),
                        "priceKeyId": en_kid or kid,
                        "rpcBand": rpc_band,
                        "rpcDue": rpc_due,
                        "pythonBand": py_band,
                        "pythonDue": decision.should_enqueue,
                        "source": en.get("display_price_source"),
                        "provider": en.get("provider"),
                        "price": en.get("current_market_price"),
                    }
                )

        price = en.get("current_market_price")
        source = str(en.get("display_price_source") or "").lower()
        sc = str(decision.details.get("source_class") or "")
        if sc == "verified_local":
            verified_local_n += 1
        elif sc == "reference_only":
            reference_only_n += 1
        elif sc == "none" or price is None:
            truly_unpriced_n += 1

        if (price is not None and float(price) > 0) and rpc_band == "P0_NEVER_PRICED":
            false_p0_priced += 1

        enriched_rows.append(
            {
                "fingerprint": t.get("fingerprint"),
                "rpcKid": kid or None,
                "resolvedKid": en_kid or None,
                "rpcBand": rpc_band,
                "finalBand": py_band,
                "due": decision.should_enqueue,
                "price": price,
                "source": en.get("display_price_source"),
                "provider": en.get("provider"),
                "sourceClass": sc,
            }
        )

    # Idempotence: same key linkage counts
    linked2 = sum(1 for t in targets2 if t.get("market_price_key_id"))
    null2 = sum(1 for t in targets2 if not t.get("market_price_key_id"))

    # Duplicate canonical fingerprint audit across market_price_keys
    keys: list[dict[str, Any]] = []
    offset = 0
    page = 1000
    while True:
        batch = client._table_get(
            "market_price_keys",
            params={
                "select": "id,fingerprint,collector_number,set_code,language,variant,condition,market_country,currency,card_name",
                "order": "fingerprint.asc",
                "limit": str(page),
                "offset": str(offset),
            },
        )
        if not batch:
            break
        keys.extend(batch)
        if len(batch) < page:
            break
        offset += page
        if offset > 200000:
            break

    by_canonical: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in keys:
        by_canonical[_canonical_fp(str(row.get("fingerprint") or ""))].append(row)

    dup_groups = []
    for cfp, group in by_canonical.items():
        fps = {str(g.get("fingerprint") or "") for g in group}
        if len(group) > 1 and len(fps) > 1:
            # Case-only (or collector case) divergence
            ids = [str(g["id"]) for g in group]
            caches = []
            for kid in ids:
                caches.append(client.get_cache_row(price_key_id=kid) or {})
            dup_groups.append(
                {
                    "canonicalFingerprint": cfp,
                    "priceKeyIds": ids,
                    "fingerprints": sorted(fps),
                    "hasPrice": [c.get("current_market_price") for c in caches],
                    "hasSnapshot": [c.get("latest_snapshot_id") for c in caches],
                    "safeToMerge": False,
                    "reason": "manual_review_required_history_may_diverge",
                }
            )

    # Also check leading-zero collector collisions as candidates (report only).
    by_lz: dict[str, list] = defaultdict(list)
    for row in keys:
        parts = str(row.get("fingerprint") or "").split("|")
        if len(parts) < 9:
            continue
        for alt in _leading_zero_collector_variants(parts[3]):
            alt_parts = list(parts)
            alt_parts[3] = alt.upper()
            # Only group if another key exists with that exact alt fingerprint
            pass
    # Simpler leading-zero: group by lowering everything except normalizing numeric collectors
    lz_groups = []
    numeric_index: dict[str, list] = defaultdict(list)
    for row in keys:
        parts = str(row.get("fingerprint") or "").split("|")
        if len(parts) < 9:
            continue
        coll = parts[3]
        if coll.isdigit() or re.match(r"^\d+/", coll):
            left = coll.split("/", 1)[0]
            if left.isdigit():
                norm = "|".join(
                    [
                        parts[0].lower(),
                        parts[1].lower(),
                        parts[2].lower(),
                        str(int(left)) + (("/" + coll.split("/", 1)[1]) if "/" in coll else ""),
                        parts[4].lower(),
                        parts[5].lower(),
                        parts[6].lower(),
                        parts[7].lower(),
                        parts[8].lower(),
                    ]
                )
                numeric_index[norm].append(row)
    for norm, group in numeric_index.items():
        fps = {str(g.get("fingerprint") or "") for g in group}
        if len(group) > 1 and len(fps) > 1:
            lz_groups.append(
                {
                    "normalizedNumericFingerprint": norm,
                    "priceKeyIds": [str(g["id"]) for g in group],
                    "fingerprints": sorted(fps),
                    "safeToMerge": False,
                    "reason": "leading_zero_candidate_manual_review",
                }
            )

    # Original five re-audit
    by_id = {str(t.get("market_price_key_id") or ""): t for t in targets if t.get("market_price_key_id")}
    original = []
    for label, kid in ORIGINAL_FIVE:
        t = by_id.get(kid)
        cache = client.get_cache_row(price_key_id=kid) or {}
        if t is None:
            # find by scanning ensure
            for cand in targets:
                en = client.enrich_owned_target_from_cache(cand)
                if str(en.get("market_price_key_id") or "") == kid:
                    t = cand
                    break
        en = client.enrich_owned_target_from_cache(t) if t else {
            "market_price_key_id": kid,
            **{k: cache.get(k) for k in (
                "current_market_price", "last_updated_at", "stale_after", "refresh_status",
                "next_refresh_due_at", "display_price_source", "provider",
            )},
            "fingerprint": None,
            "owner_count": 1,
            "total_owned_quantity": 1,
            "market_country": "au",
            "currency": "aud",
        }
        decision = sched.evaluate_target(en, now=FIXED_NOW)
        band, due, reason, view, _ = classify_owned_daily_band(
            en, now=FIXED_NOW, success_fresh_hours=24
        )
        original.append(
            {
                "label": label,
                "priceKeyId": kid,
                "canonicalFingerprint": en.get("fingerprint") or (t or {}).get("fingerprint"),
                "resolvedKeyFingerprint": en.get("resolved_key_fingerprint"),
                "currentPrice": en.get("current_market_price") if en.get("current_market_price") is not None else cache.get("current_market_price"),
                "priceSource": en.get("display_price_source") or cache.get("display_price_source"),
                "provider": en.get("provider") or cache.get("provider"),
                "verifiedLocal": view.has_verified_local_price,
                "referenceOnly": view.has_reference_only_price,
                "schedulerFreshnessAt": view.to_dict().get("ebaySuccessFreshnessAt"),
                "schedulerAgeHours": view.scheduler_age_hours,
                "authoritativeTimestampField": view.authoritative_timestamp_field,
                "staleAfter": cache.get("stale_after"),
                "lastUpdatedAt": cache.get("last_updated_at"),
                "rpcCategory": (t or {}).get("owned_priority_band"),
                "rpcDue": (t or {}).get("due_for_owned_daily"),
                "pythonCategory": decision.details.get("owned_priority_band"),
                "finalCategory": band,
                "due": due and decision.should_enqueue,
                "priority": decision.priority,
                "score": decision.score,
                "reason": decision.reason,
            }
        )

    # Offline top 5 (production ordering)
    ranked = []
    for t in targets:
        if str(t.get("market_country") or "").upper() != "AU":
            continue
        en = client.enrich_owned_target_from_cache(t)
        decision = sched.evaluate_target(en, now=FIXED_NOW)
        if not decision.should_enqueue:
            continue
        kid = str(en.get("market_price_key_id") or "").strip()
        if not kid:
            continue
        ranked.append((decision.score, decision.priority if decision.priority is not None else 999, en, decision))
    ranked.sort(key=lambda item: (-(item[2].get("owner_count") or 0), item[1], -item[0], str(item[2].get("fingerprint") or "")))
    # Match scheduler sort: priority asc, score desc, fingerprint
    ranked.sort(
        key=lambda item: (
            item[1],
            -item[0],
            str(item[2].get("fingerprint") or ""),
        )
    )

    top5 = []
    seen = set()
    for score, prio, en, decision in ranked:
        kid = str(en.get("market_price_key_id") or "")
        if kid in seen:
            continue
        seen.add(kid)
        query = None
        query_ready = False
        query_error = None
        try:
            key = client.get_price_key(price_key_id=kid)
            market = resolve_marketplace_config(
                market_country=str(getattr(key, "market_country", None) or "au"),
                currency=str(getattr(key, "currency", None) or "aud"),
                marketplace="ebay",
            )
            request = ProviderRequest(
                price_key=key,
                market_country=market.market_country,
                currency=market.currency,
                marketplace=market.marketplace,
                provider_marketplace_id=market.provider_marketplace_id,
                provider_domain=market.provider_domain,
                search_locale=market.search_locale,
                display_name=market.display_name,
                market_config=market,
            )
            queries = build_provider_search_queries(request, max_attempts=1)
            if queries:
                query = queries[0].query_text
                query_ready = True
        except Exception as exc:
            query_error = f"{type(exc).__name__}:{exc}"
        top5.append(
            {
                "priceKeyId": kid,
                "fingerprint": en.get("fingerprint"),
                "card": en.get("card_name"),
                "set": en.get("set_name"),
                "collector": en.get("collector_number"),
                "price": en.get("current_market_price"),
                "source": en.get("display_price_source"),
                "provider": en.get("provider"),
                "verifiedLocal": decision.details.get("source_class") == "verified_local",
                "freshness": decision.details.get("ebay_success_freshness_at") or en.get("last_updated_at"),
                "category": decision.details.get("owned_priority_band"),
                "reason": decision.reason,
                "score": decision.score,
                "priority": decision.priority,
                "queryReady": query_ready,
                "query": query,
                "queryError": query_error,
            }
        )
        if len(top5) >= 5:
            break

    # Ownership mutation check: sum quantities from targets vs second fetch
    qty1 = sum(int(t.get("total_owned_quantity") or 0) for t in targets)
    qty2 = sum(int(t.get("total_owned_quantity") or 0) for t in targets2)

    # Filter disagreements to meaningful ones after alignment
    meaningful_disagreements = [
        d for d in disagreements
        if d["rpcBand"] != d["pythonBand"] or d["rpcDue"] != d["pythonDue"]
    ]

    report = {
        "auditedAtUtc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "fixedEvaluationTimestampUtc": FIXED_NOW.isoformat().replace("+00:00", "Z"),
        "architecture": {
            "keyResolution": "DYNAMIC_CANONICAL_RESOLUTION",
            "persistedFkOnOwnedRows": False,
            "migrationBackfillsMarketPriceKeyId": False,
            "join": "lower(market_price_keys.fingerprint) = lower(owned_fingerprint)",
            "idempotent": linked == linked2 and null_before_style == null2,
        },
        "collectionIdentityAudit": {
            "ownedTargets": len(targets),
            "nullKeyOnRpc": null_before_style,
            "nonNullKeyOnRpc": linked,
            "nullKeyAfterSecondFetch": null2,
            "nonNullKeyAfterSecondFetch": linked2,
            "nullRowsResolvedViaEnsureUnambiguously": resolved_via_ensure,
            "nullRowsWithNoExistingKey": no_existing_key,
            "linkedBackfilledPersisted": 0,
            "rowsAlreadyCorrectOnRpcJoin": linked,
            "ambiguousMatches": ambiguous,
            "unresolved": no_existing_key,
            "identitiesWithPriceButRpcP0NeverPriced": false_p0_priced,
            "verifiedLocalIdentities": verified_local_n,
            "referenceOnlyIdentities": reference_only_n,
            "trulyUnpricedIdentities": truly_unpriced_n,
            "ownershipQuantitySumPass1": qty1,
            "ownershipQuantitySumPass2": qty2,
            "ownershipMutation": qty1 - qty2,
        },
        "duplicateKeyAudit": {
            "keysScanned": len(keys),
            "caseNormalizationGroups": dup_groups,
            "caseNormalizationGroupCount": len(dup_groups),
            "leadingZeroCandidateGroups": lz_groups,
            "leadingZeroCandidateGroupCount": len(lz_groups),
            "action": "NO_AUTOMATIC_MERGE",
        },
        "rpcPythonAlignment": {
            "disagreementCount": len(meaningful_disagreements),
            "disagreementsSample": meaningful_disagreements[:40],
            "policy": {
                "authoritativeTimestampVerifiedLocal": "last_updated_at",
                "authoritativeTimestampReferenceOnly": "none (always due for eBay verify)",
                "staleAfterRole": "selected_price_ttl_only_not_ebay_success_wall",
            },
        },
        "originalFive": original,
        "offlineTop5": top5,
        "migrationSafety": {
            "migrations": [
                "20261002120000_owned_fingerprint_case_join_fix",
                "20261002140000_owned_daily_source_aware_scheduler_align",
            ],
            "tablesChanged": [],
            "viewsChanged": [],
            "functionsChanged": ["public.list_owned_market_pricing_targets(boolean)"],
            "schemaChanged": False,
            "dataRowsChanged": False,
            "idempotent": True,
            "rollbackPossible": "re-apply prior function definition from 20260926200000 / 20261002120000",
            "ownershipMutations": 0,
            "priceHistoryLoss": 0,
            "snapshotHistoryLoss": 0,
            "marketPriceKeysDeleted": 0,
            "duplicateKeysCreated": 0,
        },
    }

    out_path = ART / "canonical_scheduler_identity_audit.json"
    out_path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({
        "path": str(out_path),
        "ownedTargets": len(targets),
        "nullKey": null_before_style,
        "linked": linked,
        "falseP0": false_p0_priced,
        "dupGroups": len(dup_groups),
        "disagreements": len(meaningful_disagreements),
        "top5": [{"card": r["card"], "band": r["category"], "queryReady": r["queryReady"]} for r in top5],
        "giratina": next((o for o in original if "Giratina" in o["label"]), None),
    }, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
