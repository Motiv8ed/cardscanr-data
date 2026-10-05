"""Structured live-navigation context for COLD_START vs INTER_CARD.

The reliability harness must not be the only layer that knows INTER_CARD.
Production provider/X11/WSL tools read this context explicitly.
"""
from __future__ import annotations

import json
import os
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .browser_lifecycle_policy import (
    RUNTIME_COLD_START,
    RUNTIME_INTER_CARD,
    PriorCardContext,
    prior_from_healthy_job_result,
    required_runtime_mode,
)
from .region_pricing_registry import region_definition

NAV_CONTEXT_ENV = "CARDSCANR_NAV_CONTEXT_JSON"
NAV_CONTEXT_PATH_ENV = "CARDSCANR_NAV_CONTEXT_JSON_PATH"
RUNTIME_MODE_ENV = "CARDSCANR_RUNTIME_MODE"
EXPECTED_PRIOR_JSON_ENV = "CARDSCANR_EXPECTED_PRIOR_JSON"
EXPECTED_PRIOR_PATH_ENV = "CARDSCANR_EXPECTED_PRIOR_JSON_PATH"
PRE_SUBMIT_ONLY_ENV = "CARDSCANR_PRE_SUBMIT_ONLY"

DEFAULT_CONTEXT_PATH = Path(__file__).resolve().parents[1] / "reports" / "runtime" / "nav_runtime_context.json"

INTER_CARD_CONTEXT_NOT_PROPAGATED = "INTER_CARD_CONTEXT_NOT_PROPAGATED"
INTER_CARD_EXPECTED_TARGET_REJECTED = "INTER_CARD_EXPECTED_TARGET_REJECTED"
INTER_CARD_QUERY_INPUT_NOT_FOUND = "INTER_CARD_QUERY_INPUT_NOT_FOUND"
INTER_CARD_QUERY_CLEAR_FAILED = "INTER_CARD_QUERY_CLEAR_FAILED"
INTER_CARD_QUERY_TYPE_FAILED = "INTER_CARD_QUERY_TYPE_FAILED"
X11_CHROME_WINDOW_NOT_FOUND = "X11_CHROME_WINDOW_NOT_FOUND"
X11_FOCUS_FAILED = "X11_FOCUS_FAILED"
X11_PRE_SUBMIT_RUNTIME_FAILURE = "X11_PRE_SUBMIT_RUNTIME_FAILURE"
WSL_NAV_PROCESS_START_FAILURE = "WSL_NAV_PROCESS_START_FAILURE"
WSL_NAV_PROCESS_EXIT_FAILURE = "WSL_NAV_PROCESS_EXIT_FAILURE"
COLD_START_UNEXPECTED_EBAY_TARGET = "COLD_START_UNEXPECTED_EBAY_TARGET"
PRE_SUBMIT_QUERY_READY = "PRE_SUBMIT_QUERY_READY"


def _windows_to_wsl(path: Path | str) -> str:
    raw = str(path).replace("\\", "/")
    if len(raw) >= 2 and raw[1] == ":":
        return f"/mnt/{raw[0].lower()}/{raw[3:]}"
    return raw


@dataclass
class NavigationRuntimeContext:
    runtime_mode: str = RUNTIME_COLD_START
    current_job_id: str | None = None
    current_attempt_id: str | None = None
    current_price_key_id: str | None = None
    current_fingerprint: str | None = None
    current_query: str | None = None
    expected_prior: PriorCardContext | None = None
    pre_submit_only: bool = False
    current_market: str | None = None
    current_currency: str | None = None
    marketplace_home: str | None = None

    def to_dict(self) -> dict[str, Any]:
        prior = self.expected_prior.to_dict() if self.expected_prior is not None else None
        return {
            "runtimeMode": self.runtime_mode,
            "currentJobId": self.current_job_id,
            "currentAttemptId": self.current_attempt_id,
            "currentPriceKeyId": self.current_price_key_id,
            "currentFingerprint": self.current_fingerprint,
            "currentQuery": self.current_query,
            "expectedPrior": prior,
            "expectedPriorTargetId": (prior or {}).get("targetId") if prior else None,
            "preSubmitOnly": self.pre_submit_only,
            "currentMarket": self.current_market,
            "currentCurrency": self.current_currency,
            "marketplaceHome": self.marketplace_home,
        }

    def prior(self) -> PriorCardContext | None:
        return self.expected_prior

    def is_inter_card(self) -> bool:
        return str(self.runtime_mode or "").upper() == RUNTIME_INTER_CARD

    def persist(self, path: Path | None = None) -> Path:
        dest = path or DEFAULT_CONTEXT_PATH
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        return dest

    def env_exports(self, *, path: Path | None = None) -> dict[str, str]:
        dest = self.persist(path)
        prior_dest = dest.with_name(dest.stem + ".prior.json")
        prior_dest.write_text(
            json.dumps(self.expected_prior.to_dict() if self.expected_prior is not None else {}, indent=2)
            + "\n",
            encoding="utf-8",
        )
        exports = {
            RUNTIME_MODE_ENV: self.runtime_mode,
            NAV_CONTEXT_PATH_ENV: _windows_to_wsl(dest),
            EXPECTED_PRIOR_PATH_ENV: _windows_to_wsl(prior_dest),
        }
        if self.current_attempt_id:
            exports["CARDSCANR_LIVE_ATTEMPT_ID"] = str(self.current_attempt_id)
        if self.current_price_key_id:
            exports["CARDSCANR_PRICE_KEY_ID"] = str(self.current_price_key_id)
        if self.current_fingerprint:
            exports["CARDSCANR_FINGERPRINT"] = str(self.current_fingerprint)
        if self.current_job_id:
            exports["CARDSCANR_JOB_ID"] = str(self.current_job_id)
        if self.current_market:
            exports["CARDSCANR_MARKET"] = str(self.current_market)
        if self.current_currency:
            exports["CARDSCANR_CURRENCY"] = str(self.current_currency)
        if self.marketplace_home:
            exports["CARDSCANR_MARKETPLACE_HOME"] = str(self.marketplace_home)
        if self.pre_submit_only:
            exports[PRE_SUBMIT_ONLY_ENV] = "1"
        return exports


def bind_persisted_nav_context_path() -> Path:
    """Point subsequent --once workers at the on-disk INTER_CARD handoff file."""
    raw = (os.environ.get(NAV_CONTEXT_PATH_ENV) or "").strip()
    if raw:
        candidate = Path(raw)
        posix = str(candidate).replace("\\", "/")
        if not posix.startswith("/mnt/") and (candidate.is_file() or candidate.parent.exists()):
            return candidate
    os.environ[NAV_CONTEXT_PATH_ENV] = str(DEFAULT_CONTEXT_PATH)
    return DEFAULT_CONTEXT_PATH


def persist_inter_card_from_healthy_result(
    result: dict[str, Any],
    *,
    provider_result: Any | None = None,
) -> NavigationRuntimeContext | None:
    """After a healthy owned-daily write, persist INTER_CARD prior for the next same-market card."""
    merged: dict[str, Any] = dict(result or {})
    if provider_result is not None:
        meta = getattr(provider_result, "raw_metadata", None)
        if isinstance(meta, dict):
            if not isinstance(merged.get("desktopNav"), dict) and isinstance(meta.get("desktopNav"), dict):
                merged["desktopNav"] = meta.get("desktopNav")
            capture = meta.get("persistedCaptureArtifact") or meta.get("currentJobCapture")
            if isinstance(capture, dict) and not isinstance(merged.get("currentJobCapture"), dict):
                merged["currentJobCapture"] = capture
            if merged.get("x11SoldStateVerified") is None and meta.get("x11SoldStateVerified"):
                merged["x11SoldStateVerified"] = True
    prior = prior_from_healthy_job_result(merged)
    if prior is None:
        return None
    market = str(prior.market or merged.get("marketCountry") or merged.get("market") or "").strip().upper()
    definition = region_definition(market) if market else None
    ctx = NavigationRuntimeContext(
        runtime_mode=RUNTIME_INTER_CARD,
        expected_prior=prior,
        current_market=market or None,
        current_currency=prior.currency,
        marketplace_home=(definition.homepage if definition is not None else None),
        current_job_id=prior.job_id,
        current_price_key_id=prior.price_key_id,
        current_fingerprint=prior.fingerprint,
        current_query=prior.query,
    )
    apply_context_to_environ(ctx)
    return ctx


def apply_context_to_environ(ctx: NavigationRuntimeContext) -> None:
    os.environ[RUNTIME_MODE_ENV] = ctx.runtime_mode
    os.environ[NAV_CONTEXT_ENV] = json.dumps(ctx.to_dict(), separators=(",", ":"))
    if ctx.expected_prior is not None:
        os.environ[EXPECTED_PRIOR_JSON_ENV] = json.dumps(ctx.expected_prior.to_dict(), separators=(",", ":"))
    else:
        os.environ.pop(EXPECTED_PRIOR_JSON_ENV, None)
    if ctx.current_attempt_id:
        os.environ["CARDSCANR_LIVE_ATTEMPT_ID"] = str(ctx.current_attempt_id)
    if ctx.current_price_key_id:
        os.environ["CARDSCANR_PRICE_KEY_ID"] = str(ctx.current_price_key_id)
    if ctx.current_fingerprint:
        os.environ["CARDSCANR_FINGERPRINT"] = str(ctx.current_fingerprint)
    if ctx.current_job_id:
        os.environ["CARDSCANR_JOB_ID"] = str(ctx.current_job_id)
    if ctx.current_market:
        os.environ["CARDSCANR_MARKET"] = str(ctx.current_market)
    if ctx.current_currency:
        os.environ["CARDSCANR_CURRENCY"] = str(ctx.current_currency)
    if ctx.marketplace_home:
        os.environ["CARDSCANR_MARKETPLACE_HOME"] = str(ctx.marketplace_home)
    if ctx.pre_submit_only:
        os.environ[PRE_SUBMIT_ONLY_ENV] = "1"
    else:
        os.environ.pop(PRE_SUBMIT_ONLY_ENV, None)
    ctx.env_exports()
    dest = DEFAULT_CONTEXT_PATH
    raw_path = (os.environ.get(NAV_CONTEXT_PATH_ENV) or "").strip()
    if raw_path:
        candidate = Path(raw_path)
        posix = str(candidate).replace("\\", "/")
        if not posix.startswith("/mnt/"):
            dest = candidate
    os.environ[NAV_CONTEXT_PATH_ENV] = str(dest)


def clear_context_from_environ() -> None:
    for key in (
        RUNTIME_MODE_ENV,
        NAV_CONTEXT_ENV,
        NAV_CONTEXT_PATH_ENV,
        EXPECTED_PRIOR_JSON_ENV,
        EXPECTED_PRIOR_PATH_ENV,
        PRE_SUBMIT_ONLY_ENV,
    ):
        os.environ.pop(key, None)


def _load_json_env(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def load_navigation_runtime_context() -> NavigationRuntimeContext:
    data: dict[str, Any] = {}
    candidates: list[Path] = []
    path = (os.environ.get(NAV_CONTEXT_PATH_ENV) or os.environ.get(EXPECTED_PRIOR_PATH_ENV) or "").strip()
    if path:
        candidates.append(Path(path))
    candidates.append(DEFAULT_CONTEXT_PATH)
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if not candidate.is_file():
            continue
        try:
            loaded = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(loaded, dict):
            continue
        if "runtimeMode" in loaded or "expectedPrior" in loaded or "currentJobId" in loaded:
            data.update(loaded)
            break
        data.setdefault("_priorFile", loaded)
        break
    if (os.environ.get(NAV_CONTEXT_ENV) or "").strip():
        data.update(_load_json_env(os.environ[NAV_CONTEXT_ENV]))
    prior_raw = (os.environ.get(EXPECTED_PRIOR_JSON_ENV) or "").strip()
    prior_dict: dict[str, Any] | None
    if prior_raw:
        prior_dict = _load_json_env(prior_raw)
    elif isinstance(data.get("expectedPrior"), dict):
        prior_dict = data.get("expectedPrior")  # type: ignore[assignment]
    elif isinstance(data.get("_priorFile"), dict):
        prior_dict = data.get("_priorFile")  # type: ignore[assignment]
    else:
        prior_dict = None
    mode = (
        str(os.environ.get(RUNTIME_MODE_ENV) or data.get("runtimeMode") or RUNTIME_COLD_START)
        .strip()
        .upper()
        or RUNTIME_COLD_START
    )
    if mode not in {RUNTIME_COLD_START, RUNTIME_INTER_CARD}:
        mode = RUNTIME_COLD_START
    pre_submit = str(os.environ.get(PRE_SUBMIT_ONLY_ENV) or "").strip() == "1" or bool(
        data.get("preSubmitOnly")
    )
    return NavigationRuntimeContext(
        runtime_mode=mode,
        current_job_id=str(data.get("currentJobId") or os.environ.get("CARDSCANR_JOB_ID") or "").strip()
        or None,
        current_attempt_id=str(
            data.get("currentAttemptId") or os.environ.get("CARDSCANR_LIVE_ATTEMPT_ID") or ""
        ).strip()
        or None,
        current_price_key_id=str(
            data.get("currentPriceKeyId") or os.environ.get("CARDSCANR_PRICE_KEY_ID") or ""
        ).strip()
        or None,
        current_fingerprint=str(
            data.get("currentFingerprint") or os.environ.get("CARDSCANR_FINGERPRINT") or ""
        ).strip()
        or None,
        current_query=str(data.get("currentQuery") or "").strip() or None,
        expected_prior=PriorCardContext.from_dict(prior_dict if isinstance(prior_dict, dict) else None),
        pre_submit_only=pre_submit,
        current_market=str(data.get("currentMarket") or os.environ.get("CARDSCANR_MARKET") or "").strip()
        or None,
        current_currency=str(data.get("currentCurrency") or os.environ.get("CARDSCANR_CURRENCY") or "").strip()
        or None,
        marketplace_home=str(
            data.get("marketplaceHome") or os.environ.get("CARDSCANR_MARKETPLACE_HOME") or ""
        ).strip()
        or None,
    )


def prepare_context_for_market(
    ctx: NavigationRuntimeContext,
    *,
    market: str,
    currency: str | None = None,
    homepage: str | None = None,
) -> NavigationRuntimeContext:
    """Force COLD_START when the prior card belongs to a different marketplace."""
    code = str(market or "").strip().upper()
    definition = region_definition(code)
    ctx.current_market = code or None
    ctx.current_currency = str(currency or definition.currency or "").upper() or None
    ctx.marketplace_home = homepage or definition.homepage or None
    mode = required_runtime_mode(next_market=code, prior=ctx.expected_prior)
    ctx.runtime_mode = mode
    if mode == RUNTIME_COLD_START:
        ctx.expected_prior = None
    return ctx


def pre_submit_only_requested(*, flag: bool = False) -> bool:
    """Require explicit local-test env; production never sets CARDSCANR_PRE_SUBMIT_ONLY."""
    env_on = str(os.environ.get(PRE_SUBMIT_ONLY_ENV) or "").strip() == "1"
    return bool(flag) and env_on


def classify_pre_submit_runtime_error(exc: BaseException, *, runtime_mode: str) -> str:
    text = f"{type(exc).__name__}:{exc}".lower()
    mode = (runtime_mode or RUNTIME_COLD_START).upper()
    if "cdp_has_ebay_target" in text or "linux_chrome_cdp_failed" in text:
        if mode == RUNTIME_INTER_CARD:
            return INTER_CARD_CONTEXT_NOT_PROPAGATED
        return COLD_START_UNEXPECTED_EBAY_TARGET
    if "target_policy_rejected" in text:
        if mode == RUNTIME_INTER_CARD:
            return INTER_CARD_EXPECTED_TARGET_REJECTED
        return COLD_START_UNEXPECTED_EBAY_TARGET
    if "wsl_nav_no_json" in text:
        return WSL_NAV_PROCESS_EXIT_FAILURE
    if "x11_nav_python_missing" in text:
        return X11_PRE_SUBMIT_RUNTIME_FAILURE
    if "no_ebay_chrome_window" in text or "chrome window" in text:
        return X11_CHROME_WINDOW_NOT_FOUND
    if "search_input" in text or "query_input" in text:
        return INTER_CARD_QUERY_INPUT_NOT_FOUND
    return X11_PRE_SUBMIT_RUNTIME_FAILURE


def exception_diagnostics(exc: BaseException) -> dict[str, Any]:
    tb = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ is not None else []
    loc = tb[-1] if tb else None
    tail = traceback.format_exception(type(exc), exc, exc.__traceback__)
    return {
        "errorType": type(exc).__name__,
        "errorMessage": str(exc)[:2000],
        "exceptionLocation": (f"{loc.filename}:{loc.lineno}:{loc.name}" if loc is not None else None),
        "tracebackTail": "".join(tail)[-4000:],
    }
