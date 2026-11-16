from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

from .audit_paths import audit_csv_path, audit_jsonl_path, ebay_scale_audit_dir
from .config import ebay_scale_shadow_audit_enabled, should_write_set_sweep_live_price
from .set_sweep import SetSweepStats, median_prices_by_card, run_set_sweep
from .set_sweep_matcher import SetSweepCatalogueCard, match_sold_row_to_catalogue
from .sold_row_html import SoldResultsRow, parse_sold_results_html

MIN_COMPS_FOR_TRUST = 2
OUTLIER_VS_MEDIAN_RATIO = 2.5
OUTLIER_VS_PER_CARD_PCT = 50.0


def _listing_item_id(url: str) -> str:
    match = re.search(r"/itm/(?:[^/]+/)?([0-9]+)", url or "")
    return match.group(1) if match else ""


def _pct_diff(sweep: float | None, per_card: float | None) -> float | None:
    if sweep is None or per_card is None or per_card == 0:
        return None
    return round(abs(sweep - per_card) / per_card * 100.0, 2)


def _abs_diff(sweep: float | None, per_card: float | None) -> float | None:
    if sweep is None or per_card is None:
        return None
    return round(abs(sweep - per_card), 2)


@dataclass
class MatchedListing:
    title: str
    item_id: str
    sold_price: float | None
    sold_date: str
    listing_url: str
    match_confidence: str
    match_reason: str


@dataclass
class SetSweepAuditRow:
    recorded_at: str
    sweep_id: str
    set_code: str
    set_name: str
    catalogue_card_id: str
    card_name: str
    collector_number: str
    rarity: str
    variant: str
    matched_listings: list[MatchedListing] = field(default_factory=list)
    set_sweep_median: float | None = None
    comp_count: int = 0
    per_card_price: float | None = None
    abs_diff_aud: float | None = None
    pct_diff: float | None = None
    suspected_mismatch: bool = False
    suspected_mismatch_reasons: list[str] = field(default_factory=list)
    validation_per_card_required: bool = False
    live_price_write_allowed: bool = False
    live_price_written: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "recordedAt": self.recorded_at,
            "sweepId": self.sweep_id,
            "setCode": self.set_code,
            "setName": self.set_name,
            "catalogueCardId": self.catalogue_card_id,
            "cardName": self.card_name,
            "collectorNumber": self.collector_number,
            "rarity": self.rarity,
            "variant": self.variant,
            "matchedListings": [
                {
                    "title": m.title,
                    "itemId": m.item_id,
                    "soldPrice": m.sold_price,
                    "soldDate": m.sold_date,
                    "listingUrl": m.listing_url,
                    "matchConfidence": m.match_confidence,
                    "matchReason": m.match_reason,
                }
                for m in self.matched_listings
            ],
            "setSweepMedian": self.set_sweep_median,
            "compCount": self.comp_count,
            "perCardPrice": self.per_card_price,
            "absDiffAud": self.abs_diff_aud,
            "pctDiff": self.pct_diff,
            "suspectedMismatch": self.suspected_mismatch,
            "suspectedMismatchReasons": list(self.suspected_mismatch_reasons),
            "validationPerCardRequired": self.validation_per_card_required,
            "livePriceWriteAllowed": self.live_price_write_allowed,
            "livePriceWritten": self.live_price_written,
        }


def _title_suspect_reasons(title: str, card: SetSweepCatalogueCard) -> list[str]:
    t = title.lower()
    reasons: list[str] = []
    markers = {
        "wrong_variant_reverse_holo": ("reverse holo", "rev holo"),
        "wrong_variant_first_ed": ("1st ed", "first edition", "1st edition"),
        "wrong_variant_graded": ("psa ", "bgs ", "cgc ", "graded"),
        "wrong_variant_lot": (" lot ", " lot of ", " bundle "),
        "wrong_variant_proxy": (" proxy ", " replica "),
    }
    for reason, terms in markers.items():
        if any(term in t for term in terms):
            if card.variant == "raw" and "graded" not in reason:
                reasons.append(reason)
            elif "graded" in reason and "graded" not in card.condition.lower():
                reasons.append(reason)
    if re.search(r"\b\d+\s*/\s*\d+\b", title):
        card_num = card.collector_number.split("/")[0].strip()
        if card_num and card_num not in title.replace(" ", ""):
            reasons.append("wrong_number_in_title")
    return reasons


def _suspected_mismatch(
    *,
    card: SetSweepCatalogueCard,
    prices: list[float],
    listings: list[MatchedListing],
    per_card_price: float | None,
    sweep_median: float | None,
) -> list[str]:
    reasons: list[str] = []
    if len(prices) < MIN_COMPS_FOR_TRUST:
        reasons.append("too_few_comps")
    if sweep_median is not None and prices:
        med = float(median(prices))
        for p in prices:
            if med > 0 and (p / med >= OUTLIER_VS_MEDIAN_RATIO or med / max(p, 0.01) >= OUTLIER_VS_MEDIAN_RATIO):
                reasons.append("outlier_comp_vs_median")
                break
    if per_card_price is not None and sweep_median is not None and per_card_price > 0:
        pct = abs(sweep_median - per_card_price) / per_card_price * 100.0
        if pct >= OUTLIER_VS_PER_CARD_PCT:
            reasons.append("outlier_vs_per_card_pipeline")
    for listing in listings:
        for r in _title_suspect_reasons(listing.title, card):
            if r not in reasons:
                reasons.append(r)
    return reasons


def build_set_sweep_audit_rows(
    *,
    sweep_id: str,
    set_code: str,
    set_name: str,
    html: str,
    catalogue: list[SetSweepCatalogueCard],
    per_card_prices: dict[str, float],
    validation_card_ids: set[str],
    exact_card_threshold_aud: float = 20.0,
    recorded_at: datetime | None = None,
) -> tuple[list[SetSweepAuditRow], SetSweepStats]:
    now = recorded_at or datetime.now(timezone.utc)
    stamp = now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    stats, _rows = run_set_sweep(
        html=html,
        catalogue=catalogue,
        exact_card_threshold_aud=exact_card_threshold_aud,
    )
    medians = median_prices_by_card(stats)
    parsed_rows = parse_sold_results_html(html)
    listings_by_card: dict[str, list[MatchedListing]] = {c.catalogue_card_id: [] for c in catalogue}

    for index, row in enumerate(parsed_rows):
        match = match_sold_row_to_catalogue(row, catalogue=catalogue, index=index)
        if match is None or match.confidence != "high":
            continue
        listings_by_card.setdefault(match.catalogue_card_id, []).append(
            MatchedListing(
                title=row.title,
                item_id=_listing_item_id(row.listing_url),
                sold_price=row.sold_price,
                sold_date=row.sold_date,
                listing_url=row.listing_url,
                match_confidence=match.confidence,
                match_reason="name_number_set_identity",
            )
        )

    live_allowed = should_write_set_sweep_live_price()
    audit_rows: list[SetSweepAuditRow] = []
    card_by_id = {c.catalogue_card_id: c for c in catalogue}
    for card_id, prices in stats.card_prices.items():
        card = card_by_id.get(card_id)
        if card is None:
            continue
        sweep_med = medians.get(card_id)
        per_card = per_card_prices.get(card_id)
        listings = listings_by_card.get(card_id, [])
        mismatch_reasons = _suspected_mismatch(
            card=card,
            prices=prices,
            listings=listings,
            per_card_price=per_card,
            sweep_median=sweep_med,
        )
        audit_rows.append(
            SetSweepAuditRow(
                recorded_at=stamp,
                sweep_id=sweep_id,
                set_code=set_code,
                set_name=set_name,
                catalogue_card_id=card_id,
                card_name=card.card_name,
                collector_number=card.collector_number,
                rarity=card.rarity or "",
                variant=card.variant,
                matched_listings=listings,
                set_sweep_median=sweep_med,
                comp_count=len(prices),
                per_card_price=per_card,
                abs_diff_aud=_abs_diff(sweep_med, per_card),
                pct_diff=_pct_diff(sweep_med, per_card),
                suspected_mismatch=bool(mismatch_reasons),
                suspected_mismatch_reasons=mismatch_reasons,
                validation_per_card_required=card_id in validation_card_ids,
                live_price_write_allowed=live_allowed and not mismatch_reasons,
                live_price_written=False,
            )
        )
    return audit_rows, stats


CSV_FIELDS = [
    "recordedAt",
    "sweepId",
    "setCode",
    "catalogueCardId",
    "collectorNumber",
    "setSweepMedian",
    "compCount",
    "perCardPrice",
    "absDiffAud",
    "pctDiff",
    "suspectedMismatch",
    "suspectedMismatchReasons",
    "validationPerCardRequired",
    "livePriceWriteAllowed",
    "livePriceWritten",
]


def append_audit_log(rows: list[SetSweepAuditRow], *, audit_dir: Path | None = None) -> Path:
    base = audit_dir or ebay_scale_audit_dir()
    base.mkdir(parents=True, exist_ok=True)
    jsonl = base / "set_sweep_audit.jsonl" if audit_dir else audit_jsonl_path()
    csv_path = base / "set_sweep_audit.csv" if audit_dir else audit_csv_path()
    write_header = not csv_path.exists()
    with jsonl.open("a", encoding="utf-8") as jf:
        for row in rows:
            jf.write(json.dumps(row.to_dict(), ensure_ascii=False) + "\n")
    with csv_path.open("a", encoding="utf-8", newline="") as cf:
        writer = csv.DictWriter(cf, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        for row in rows:
            flat = row.to_dict()
            flat["suspectedMismatchReasons"] = ",".join(row.suspected_mismatch_reasons)
            writer.writerow({k: flat.get(k) for k in CSV_FIELDS})
    return jsonl


def record_set_sweep_shadow_audit(
    *,
    sweep_id: str,
    set_code: str,
    set_name: str,
    html: str,
    catalogue: list[SetSweepCatalogueCard],
    per_card_prices: dict[str, float],
    validation_card_ids: set[str],
    audit_dir: Path | None = None,
) -> dict[str, Any]:
    """Record audit rows locally when shadow mode is on; never touches Supabase."""
    if not ebay_scale_shadow_audit_enabled():
        return {"recorded": False, "reason": "shadow_audit_disabled"}
    rows, stats = build_set_sweep_audit_rows(
        sweep_id=sweep_id,
        set_code=set_code,
        set_name=set_name,
        html=html,
        catalogue=catalogue,
        per_card_prices=per_card_prices,
        validation_card_ids=validation_card_ids,
    )
    log_path = append_audit_log(rows, audit_dir=audit_dir)
    return {
        "recorded": True,
        "rowCount": len(rows),
        "logPath": str(log_path),
        "liveWriteEnabled": should_write_set_sweep_live_price(),
        "stats": stats.to_public_dict(),
    }


def new_sweep_id(set_code: str, *, at: datetime | None = None) -> str:
    when = (at or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    digest = hashlib.sha256(f"{set_code}|{when}".encode()).hexdigest()[:8]
    return f"{set_code}-{when}-{digest}"