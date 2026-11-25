"""Offline compatibility preflight for the existing native Windows pricing adapter.

No browser launch, browser requests, desktop input, environment changes,
credential access, incident updates, scheduler writes, or database access.
A positive report is a source-compatibility observation, never permission
to navigate or evidence that an existing marketplace restriction is resolved.
"""
from __future__ import annotations

import ast

_REQUIRED_NAV_KWARGS = frozenset({
    "reset_homepage", "attempt_id", "price_key_id", "pre_submit_only",
})


def inspect_native_adapter_source(source: str) -> dict:
    """Check the explicit interface consumed by the current pricing worker."""
    reasons = []
    try:
        tree = ast.parse(source)
    except (SyntaxError, TypeError, ValueError):
        return {"source_compatible": False, "reason_codes": ["INVALID_SOURCE"],
                "live_navigation_authorized": False}
    functions = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "navigate_query_to_sold"
    ]
    if len(functions) != 1:
        reasons.append("AMBIGUOUS_OR_MISSING_NATIVE_ENTRYPOINT")
        missing = sorted(_REQUIRED_NAV_KWARGS)
    else:
        args = functions[0].args
        declared = {arg.arg for arg in args.args + args.kwonlyargs}
        missing = sorted(_REQUIRED_NAV_KWARGS - declared)
        if missing:
            reasons.append("NATIVE_ENTRYPOINT_INCOMPATIBLE")
    # Detect the legacy wildcard flag. Do not import/execute Windows code.
    wildcard = any(
        isinstance(node, ast.Constant) and isinstance(node.value, str)
        and "--remote-allow-origins=*" in node.value
        for node in ast.walk(tree)
    )
    # The old launcher embeds the flag in an interpolated PowerShell literal.
    if wildcard:
        reasons.append("WILDCARD_DEBUG_ORIGIN")
    return {
        "source_compatible": not reasons,
        "reason_codes": reasons,
        "missing_navigation_kwargs": missing,
        "wildcard_debug_origin": wildcard,
        "live_navigation_authorized": False,
    }


def evaluate_migration_readiness(*, source_report: dict, gate_allowed: bool,
                                 state_integrity_ok: bool, scheduler_disabled: bool,
                                 worker_count: int, windows_session_verified: bool,
                                 dedicated_listener_verified: bool,
                                 offline_integration_passed: bool,
                                 permitted_marketplace_access_verified: bool) -> dict:
    """Combine independently observed prerequisites without mutating anything.

    This is deliberately not a replacement for evaluate_ebay_browser_work_gate.
    A migration review can be ready without any permission to start live work.
    """
    reasons = []
    if source_report.get("source_compatible") is not True:
        reasons.append("SOURCE_CONTRACT_INCOMPATIBLE")
    if state_integrity_ok is not True or gate_allowed is not True:
        reasons.append("EXISTING_MARKETPLACE_GATE_BLOCKED")
    if scheduler_disabled is not True or type(worker_count) is not int or worker_count != 0:
        reasons.append("PRICING_WORKER_NOT_QUIESCENT")
    if windows_session_verified is not True:
        reasons.append("WINDOWS_SESSION_UNVERIFIED")
    if dedicated_listener_verified is not True:
        reasons.append("DEDICATED_LISTENER_UNVERIFIED")
    if offline_integration_passed is not True:
        reasons.append("OFFLINE_INTEGRATION_UNVERIFIED")
    if permitted_marketplace_access_verified is not True:
        reasons.append("MARKETPLACE_ACCESS_UNVERIFIED")
    return {
        "ready_for_review": not reasons,
        "reason_codes": reasons,
        "live_navigation_authorized": False,
        "scheduler_modified": False,
        "challenge_cleared": False,
        "database_modified": False,
    }
