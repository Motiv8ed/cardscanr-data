"""Wire AU set-sweep shadow audits into the continuous worker and schedulers.

All behaviour is behind EBAY_SCALE_* flags (default off). Set-level browser fetches
count as one live submission against the continuous safety budget.

Live AU worker (owned_daily off): when EBAY_SCALE shadow flags are on and
EBAY_SCALE_SET_SWEEP_LIVE is off, the market worker may run one set-level navigation
after per-card work. Interleaving rule (does not starve per-card jobs): attempt at most
one sweep when the job queue is idle for the cycle, or after every
EBAY_SCALE_SET_SWEEP_EVERY_N_PER_CARD_JOBS per-card submissions (default 5) since the
last sweep attempt that reserved budget. Owned_daily (continuous) behaviour still runs
the hook before per-card jobs when enabled — unchanged.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ..config import REPORTS_DIR
from ..owned_daily_enablement import BUDGET_LEDGER_PATH
from ..models import MarketPriceKey
from ..navigation_runtime_context import (
    RUNTIME_COLD_START,
    apply_context_to_environ,
    load_navigation_runtime_context,
    reconcile_navigation_context_for_browser_recycle,
)
from ..scheduler import utc_iso
from .config import (
    EbayScaleFlags,
    ebay_scale_set_sweep_enabled,
    ebay_scale_shadow_audit_enabled,
    should_write_set_sweep_live_price,
)
from .price_write_gate import apply_set_sweep_pricing_batch
from .set_sweep import median_prices_by_card
from .set_sweep_matcher import SetSweepCatalogueCard
from .set_sweep_safety import (
    append_set_sweep_attempt_receipt,
    classify_set_sweep_fetch_failure,
    classify_set_sweep_html,
    handle_set_sweep_marketplace_outcome,
    load_set_sweep_circuit,
    record_set_sweep_circuit_failure,
    record_set_sweep_circuit_success,
    sweep_worker_gate_blocks,
)
from .shadow_audit import new_sweep_id

if TYPE_CHECKING:
    from ..continuous_safety import ContinuousSafetyBudget

SET_SWEEP_PLAN_PATH = REPORTS_DIR / "runtime" / "ebay_scale_set_sweep_plan.json"
SET_SWEEP_LAST_RUN_PATH = REPORTS_DIR / "runtime" / "ebay_scale_set_sweep_last_run.json"

MinSweepIntervalHoursEnv = "EBAY_SCALE_SET_SWEEP_MIN_INTERVAL_HOURS"
LiveAuSweepEveryNPerCardEnv = "EBAY_SCALE_SET_SWEEP_EVERY_N_PER_CARD_JOBS"


def _parse_positive_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(0.0, value)


def set_sweep_worker_hook_enabled() -> bool:
    flags = EbayScaleFlags.from_env()
    return bool(flags.enabled and flags.set_sweep_mode and flags.shadow_audit)


def live_au_set_sweep_shadow_hook_enabled() -> bool:
    """Shadow set-sweep on the normal live AU worker path (owned_daily off, SET_SWEEP_LIVE off)."""
    flags = EbayScaleFlags.from_env()
    return bool(
        flags.enabled and flags.set_sweep_mode and flags.shadow_audit and not flags.set_sweep_live
    )


def live_au_set_sweep_interleave_due(
    *,
    queue_idle: bool,
    per_card_submissions_since_last_sweep: int,
) -> bool:
    """Return True when the live-AU shadow hook may run without starving per-card work."""
    if queue_idle:
        return True
    raw = os.getenv(LiveAuSweepEveryNPerCardEnv, "5").strip() or "5"
    try:
        every_n = max(1, int(raw))
    except ValueError:
        every_n = 5
    return per_card_submissions_since_last_sweep >= every_n


def build_set_level_query_text(*, set_name: str, set_code: str) -> str:
    name = " ".join(str(set_name or "").split()).strip()
    code = str(set_code or "").strip()
    if name and code:
        return f"{name} {code} pokemon"
    if name:
        return f"{name} pokemon"
    if code:
        return f"{code} pokemon"
    raise ValueError("set_name or set_code required for set sweep query")


def _target_set_code(target: dict[str, Any]) -> str:
    return str(
        target.get("set_code")
        or target.get("set_id")
        or target.get("setCode")
        or ""
    ).strip()


def _target_set_name(target: dict[str, Any]) -> str:
    return str(target.get("set_name") or target.get("setName") or "").strip()


def _target_market(target: dict[str, Any]) -> str:
    return str(target.get("market_country") or target.get("market") or "AU").strip().upper()


def _catalogue_from_targets(targets: list[dict[str, Any]]) -> list[SetSweepCatalogueCard]:
    cards: list[SetSweepCatalogueCard] = []
    seen: set[str] = set()
    for target in targets:
        fp = str(target.get("fingerprint") or target.get("catalogue_card_id") or "").strip()
        cid = str(target.get("catalogue_card_id") or target.get("catalogueCardId") or fp).strip()
        if not cid or cid in seen:
            continue
        seen.add(cid)
        cards.append(
            SetSweepCatalogueCard(
                catalogue_card_id=cid,
                card_name=str(target.get("card_name") or ""),
                normalized_card_name=str(
                    target.get("normalized_card_name")
                    or (target.get("card_name") or "").strip().lower().replace(" ", "_")
                ),
                set_name=_target_set_name(target),
                set_code=_target_set_code(target),
                collector_number=str(target.get("collector_number") or ""),
                language=str(target.get("language") or "en"),
                variant=str(target.get("variant") or "raw"),
                condition=str(target.get("condition") or "raw"),
                rarity=str(target.get("rarity") or ""),
            )
        )
    return cards


def _per_card_prices_from_targets(targets: list[dict[str, Any]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for target in targets:
        cid = str(target.get("catalogue_card_id") or target.get("catalogueCardId") or target.get("fingerprint") or "").strip()
        if not cid:
            continue
        raw = target.get("current_market_price")
        try:
            price = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            price = None
        if price is not None and price > 0:
            out[cid] = price
    return out


def load_owned_targets_for_set(client: Any, *, set_code: str, market_country: str = "AU") -> list[dict[str, Any]]:
    if not hasattr(client, "list_owned_market_pricing_targets"):
        return []
    try:
        payload = client.list_owned_market_pricing_targets(include_zero_owners=True)
    except Exception:
        return []
    targets = payload.get("targets") if isinstance(payload, dict) else []
    if not isinstance(targets, list):
        return []
    code = str(set_code or "").strip().lower()
    market = str(market_country or "AU").strip().upper()
    rows: list[dict[str, Any]] = []
    for row in targets:
        if not isinstance(row, dict):
            continue
        if market and _target_market(row) != market:
            continue
        row_code = _target_set_code(row).lower()
        if code and row_code != code:
            continue
        rows.append(row)
    return rows


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_set_sweep_plan() -> dict[str, Any] | None:
    if not SET_SWEEP_PLAN_PATH.is_file():
        return None
    try:
        data = json.loads(SET_SWEEP_PLAN_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def record_set_sweep_plan_from_target(
    *,
    target: dict[str, Any],
    price_key_id: str | None = None,
    job_id: str | None = None,
    source: str,
) -> dict[str, Any] | None:
    """Schedulers call after picking the next queued card (AU only, flags on)."""
    if not set_sweep_worker_hook_enabled():
        return None
    market = _target_market(target)
    if market != "AU":
        return None
    set_code = _target_set_code(target)
    if not set_code:
        return None
    plan = {
        "setCode": set_code,
        "setName": _target_set_name(target),
        "marketCountry": market,
        "priceKeyId": price_key_id,
        "jobId": job_id,
        "plannedAtUtc": utc_iso(),
        "source": source,
        "consumed": False,
    }
    write_json_atomic(SET_SWEEP_PLAN_PATH, plan)
    return plan


def _last_run_for_set(set_code: str) -> datetime | None:
    if not SET_SWEEP_LAST_RUN_PATH.is_file():
        return None
    try:
        data = json.loads(SET_SWEEP_LAST_RUN_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    runs = data.get("bySetCode") if isinstance(data.get("bySetCode"), dict) else {}
    raw = runs.get(set_code)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _record_set_sweep_run(set_code: str, *, sweep_id: str) -> None:
    payload: dict[str, Any] = {"bySetCode": {}, "lastSweepId": sweep_id, "updatedAtUtc": utc_iso()}
    if SET_SWEEP_LAST_RUN_PATH.is_file():
        try:
            existing = json.loads(SET_SWEEP_LAST_RUN_PATH.read_text(encoding="utf-8"))
            if isinstance(existing, dict) and isinstance(existing.get("bySetCode"), dict):
                payload["bySetCode"] = dict(existing["bySetCode"])
        except (OSError, json.JSONDecodeError):
            pass
    payload["bySetCode"][set_code] = utc_iso()
    write_json_atomic(SET_SWEEP_LAST_RUN_PATH, payload)


def should_attempt_set_sweep(
    *,
    plan: dict[str, Any] | None,
    budget: "ContinuousSafetyBudget",
    now: datetime | None = None,
    worker_state: str | None = None,
    availability_state: str | None = None,
    hard_stop: str | None = None,
    circuit_path: Path | None = None,
) -> tuple[bool, str]:
    if not set_sweep_worker_hook_enabled():
        return False, "flags_off"
    blocked, gate_reason = sweep_worker_gate_blocks(
        worker_state=worker_state,
        availability_state=availability_state,
        hard_stop=hard_stop,
    )
    if blocked:
        return False, gate_reason
    if not plan or plan.get("consumed"):
        return False, "no_plan"
    if str(plan.get("marketCountry") or "").upper() != "AU":
        return False, "non_au"
    current = now or datetime.now(timezone.utc)
    if budget.transient_hard_stop(current):
        return False, "hard_stop"
    if budget.budget_exhausted(current):
        return False, "budget_exhausted"
    circuit = load_set_sweep_circuit(circuit_path)
    if circuit.backoff_active(current):
        return False, "failure_backoff"
    set_code = str(plan.get("setCode") or "")
    min_hours = _parse_positive_float(MinSweepIntervalHoursEnv, 6.0)
    last = _last_run_for_set(set_code)
    if last is not None and current - last < timedelta(hours=min_hours):
        return False, "interval_not_due"
    return True, "ok"


@dataclass(frozen=True)
class SetSweepExecutionResult:
    ok: bool
    reason: str
    submission_consumed: bool = False
    audit: dict[str, Any] | None = None
    sweep_id: str | None = None
    medians_by_card: dict[str, float] | None = None
    hard_stop_reason: str | None = None
    marketplace_outcome: str | None = None
    attempt_receipt_written: bool = False


def _reserve_set_sweep_submission(
    *,
    budget: "ContinuousSafetyBudget",
    market: str,
    set_code: str,
    now: datetime,
    attempt_paths: dict[str, Path] | None = None,
) -> tuple[bool, str | None]:
    ledger_path = (attempt_paths or {}).get("ledger") or BUDGET_LEDGER_PATH
    blocked = budget.reserve_live_submission(path=ledger_path, market=market, now=now)
    if blocked:
        return False, blocked
    attempts_path = (attempt_paths or {}).get("attempts")
    append_set_sweep_attempt_receipt(
        {
            "phase": "reserved",
            "setCode": set_code,
            "marketCountry": market,
            "atUtc": utc_iso(now),
        },
        path=attempts_path,
    )
    return True, None


def _finish_set_sweep_failure(
    *,
    budget: "ContinuousSafetyBudget",
    plan: dict[str, Any],
    set_code: str,
    market: str,
    reason: str,
    marketplace_outcome: str | None,
    now: datetime,
    attempt_paths: dict[str, Path] | None = None,
    logger: Callable[[str], None] | None = None,
) -> SetSweepExecutionResult:
    hard_stop: str | None = None
    ledger_path = (attempt_paths or {}).get("ledger") or BUDGET_LEDGER_PATH
    if marketplace_outcome:
        hard_stop = handle_set_sweep_marketplace_outcome(
            marketplace_outcome,
            budget=budget,
            now=now,
            path=ledger_path,
        )
        budget.persist(ledger_path)
    circuit_path = (attempt_paths or {}).get("circuit")
    record_set_sweep_circuit_failure(load_set_sweep_circuit(circuit_path), now=now, path=circuit_path)
    append_set_sweep_attempt_receipt(
        {
            "phase": "failed",
            "setCode": set_code,
            "marketCountry": market,
            "reason": reason,
            "marketplaceOutcome": marketplace_outcome,
            "hardStop": hard_stop or budget.transient_hard_stop(now),
            "atUtc": utc_iso(now),
        },
        path=(attempt_paths or {}).get("attempts"),
    )
    if logger:
        logger(
            f"[set-sweep] attempt_failed set={set_code} reason={reason} "
            f"outcome={marketplace_outcome or 'none'} hardStop={hard_stop}"
        )
    return SetSweepExecutionResult(
        ok=False,
        reason=reason,
        submission_consumed=True,
        hard_stop_reason=hard_stop or budget.transient_hard_stop(now),
        marketplace_outcome=marketplace_outcome,
        attempt_receipt_written=True,
    )


def execute_set_sweep_shadow(
    *,
    client: Any,
    plan: dict[str, Any],
    fetch_html: Callable[[str, str, str], str],
    budget: "ContinuousSafetyBudget | None" = None,
    logger: Callable[[str], None] | None = None,
    attempt_paths: dict[str, Path] | None = None,
) -> SetSweepExecutionResult:
    """Run one set-level sold capture + local shadow audit (no live Supabase writes)."""
    if not ebay_scale_shadow_audit_enabled():
        return SetSweepExecutionResult(ok=False, reason="shadow_audit_disabled")
    set_code = str(plan.get("setCode") or "")
    set_name = str(plan.get("setName") or "")
    market = str(plan.get("marketCountry") or "AU").upper()
    if market != "AU":
        return SetSweepExecutionResult(ok=False, reason="au_only")

    now = datetime.now(timezone.utc)
    if budget is None:
        return SetSweepExecutionResult(ok=False, reason="budget_required")

    targets = load_owned_targets_for_set(client, set_code=set_code, market_country=market)
    catalogue = _catalogue_from_targets(targets)
    if not catalogue:
        return SetSweepExecutionResult(ok=False, reason="empty_catalogue")

    reserved, reserve_reason = _reserve_set_sweep_submission(
        budget=budget,
        market=market,
        set_code=set_code,
        now=now,
        attempt_paths=attempt_paths,
    )
    if not reserved:
        return SetSweepExecutionResult(ok=False, reason=str(reserve_reason or "budget_exhausted"))

    ledger_path = (attempt_paths or {}).get("ledger") or BUDGET_LEDGER_PATH

    try:
        query = build_set_level_query_text(set_name=set_name, set_code=set_code)
        ctx = load_navigation_runtime_context()
        ctx = reconcile_navigation_context_for_browser_recycle(ctx)
        ctx.runtime_mode = RUNTIME_COLD_START
        ctx.expected_prior = None
        apply_context_to_environ(ctx)

        try:
            html = fetch_html(query, set_code, set_name)
        except Exception as exc:
            outcome = classify_set_sweep_fetch_failure(exc)
            if logger:
                logger(f"[set-sweep] fetch_failed set={set_code} error={type(exc).__name__}:{exc}")
            return _finish_set_sweep_failure(
                budget=budget,
                plan=plan,
                set_code=set_code,
                market=market,
                reason="fetch_failed",
                marketplace_outcome=outcome,
                now=now,
                attempt_paths=attempt_paths,
                logger=logger,
            )

        marketplace_outcome = classify_set_sweep_html(html)
        if marketplace_outcome:
            return _finish_set_sweep_failure(
                budget=budget,
                plan=plan,
                set_code=set_code,
                market=market,
                reason="marketplace_unhealthy_html",
                marketplace_outcome=marketplace_outcome,
                now=now,
                attempt_paths=attempt_paths,
                logger=logger,
            )

        sweep_id = new_sweep_id(set_code, at=now)
        per_card = _per_card_prices_from_targets(targets)
        batch = apply_set_sweep_pricing_batch(
            sweep_id=sweep_id,
            set_code=set_code,
            set_name=set_name,
            html=html,
            catalogue=catalogue,
            per_card_prices=per_card,
        )
        audit = batch.get("audit") if isinstance(batch.get("audit"), dict) else {}
        if not audit.get("recorded"):
            return _finish_set_sweep_failure(
                budget=budget,
                plan=plan,
                set_code=set_code,
                market=market,
                reason=str(audit.get("reason") or "audit_not_recorded"),
                marketplace_outcome=None,
                now=now,
                attempt_paths=attempt_paths,
                logger=logger,
            )

        from .set_sweep import run_set_sweep

        sweep_stats, _ = run_set_sweep(html=html, catalogue=catalogue)
        medians = median_prices_by_card(sweep_stats)
        _ = audit.get("stats")

        _record_set_sweep_run(set_code, sweep_id=sweep_id)
        plan_update = dict(plan)
        plan_update["consumed"] = True
        plan_update["consumedAtUtc"] = utc_iso(now)
        plan_update["sweepId"] = sweep_id
        plan_update["mediansByCatalogueCardId"] = medians
        write_json_atomic(SET_SWEEP_PLAN_PATH, plan_update)

        circuit_path = (attempt_paths or {}).get("circuit")
        record_set_sweep_circuit_success(load_set_sweep_circuit(circuit_path), path=circuit_path)
        append_set_sweep_attempt_receipt(
            {
                "phase": "recorded",
                "setCode": set_code,
                "marketCountry": market,
                "sweepId": sweep_id,
                "atUtc": utc_iso(now),
            },
            path=(attempt_paths or {}).get("attempts"),
        )

        if logger:
            logger(
                f"[set-sweep] shadow_recorded set={set_code} sweep={sweep_id} "
                f"rows={audit.get('rowCount')} liveWrite={batch.get('livePriceWrite')}"
            )
        return SetSweepExecutionResult(
            ok=True,
            reason="recorded",
            submission_consumed=True,
            audit=audit,
            sweep_id=sweep_id,
            medians_by_card=medians,
            attempt_receipt_written=True,
        )
    except Exception as exc:
        if logger:
            logger(f"[set-sweep] post_reserve_failed set={set_code} error={type(exc).__name__}:{exc}")
        return _finish_set_sweep_failure(
            budget=budget,
            plan=plan,
            set_code=set_code,
            market=market,
            reason="post_reserve_failed",
            marketplace_outcome=classify_set_sweep_fetch_failure(exc),
            now=now,
            attempt_paths=attempt_paths,
            logger=logger,
        )


def try_set_sweep_worker_cycle(
    *,
    client: Any,
    budget: "ContinuousSafetyBudget",
    fetch_html: Callable[[str, str, str], str] | None = None,
    logger: Callable[[str], None] | None = None,
    worker_state: str | None = None,
    availability_state: str | None = None,
    hard_stop: str | None = None,
    attempt_paths: dict[str, Path] | None = None,
) -> SetSweepExecutionResult | None:
    if not set_sweep_worker_hook_enabled():
        return None
    circuit_path = (attempt_paths or {}).get("circuit")
    plan = read_set_sweep_plan()
    ok, reason = should_attempt_set_sweep(
        plan=plan,
        budget=budget,
        worker_state=worker_state,
        availability_state=availability_state,
        hard_stop=hard_stop,
        circuit_path=circuit_path,
    )
    if not ok:
        if logger and reason not in {"flags_off", "no_plan"}:
            logger(f"[set-sweep] skip reason={reason}")
        return SetSweepExecutionResult(ok=False, reason=reason)

    if fetch_html is None:

        def fetch_html(query: str, set_code: str, set_name: str) -> str:
            return _default_fetch_set_sweep_html(query, set_code, set_name, budget=budget)

    return execute_set_sweep_shadow(
        client=client,
        plan=plan or {},
        fetch_html=fetch_html,
        budget=budget,
        logger=logger,
        attempt_paths=attempt_paths,
    )


def _default_fetch_set_sweep_html(
    query: str,
    set_code: str,
    set_name: str,
    *,
    budget: "ContinuousSafetyBudget | None" = None,
) -> str:
    """Live PC path: configured desktop sold navigation and authoritative HTML capture."""
    del set_code, set_name
    from ..providers.ebay_browser_provider import EbayBrowserSoldCompsProvider

    provider = EbayBrowserSoldCompsProvider()
    if budget is not None:
        provider.bind_live_submission_budget(budget, first_slot_pre_reserved=True)
    try:
        return provider.fetch_set_sweep_results_html(query_text=query)
    finally:
        provider.clear_live_submission_budget()


def live_set_sweep_median_for_price_key(price_key: MarketPriceKey) -> float | None:
    if not should_write_set_sweep_live_price():
        return None
    plan = read_set_sweep_plan()
    if not plan:
        return None
    set_code = str(plan.get("setCode") or "").strip().lower()
    key_set = str(getattr(price_key, "set_code", "") or "").strip().lower()
    if not set_code or set_code != key_set:
        return None
    medians = plan.get("mediansByCatalogueCardId")
    if not isinstance(medians, dict):
        return None
    cid = str((price_key.raw or {}).get("catalogue_card_id") or price_key.fingerprint or "").strip()
    for candidate in {cid, price_key.fingerprint, price_key.id}:
        if not candidate:
            continue
        raw = medians.get(candidate)
        try:
            if raw is not None:
                value = float(raw)
                if value > 0:
                    return value
        except (TypeError, ValueError):
            continue
    return None


def maybe_apply_set_sweep_live_skip(
    *,
    price_key: MarketPriceKey,
    prior_cache: dict[str, Any] | None,
    job_id: str,
) -> dict[str, Any] | None:
    """When SET_SWEEP_LIVE is on, matched cards may skip per-card browser fetch."""
    if not ebay_scale_set_sweep_enabled() or not should_write_set_sweep_live_price():
        return None
    median = live_set_sweep_median_for_price_key(price_key)
    if median is None:
        return None
    return {
        "jobId": job_id,
        "priceKeyId": price_key.id,
        "status": "completed",
        "outcomeClass": "set_sweep_live_median",
        "ownedDailyOutcome": "set_sweep_live_median",
        "setSweepMedian": median,
        "searchSubmissionStarted": False,
        "lastGoodRetained": bool((prior_cache or {}).get("current_market_price")),
    }