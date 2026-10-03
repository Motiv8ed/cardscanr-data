"""Non-sensitive GUI search telemetry for Linux X11 eBay navigation.

Never records cookies, tokens, passwords, or authorization headers.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class PreSubmitDiagnostic:
    timestampUtc: str
    currentUrl: str | None = None
    pageTitle: str | None = None
    queryExpected: str | None = None
    queryVisiblyConfirmed: bool | None = None
    focusedElementRole: str | None = None
    searchFieldGeometry: dict[str, Any] | None = None
    searchButtonGeometry: dict[str, Any] | None = None
    selectedCategoryLabel: str | None = None
    formAction: str | None = None
    tabCount: int | None = None
    modifierKeyState: str | None = None
    fsmState: str | None = None
    searchOriginUrl: str | None = None
    searchSurfaceClass: str | None = None
    searchScopeLabel: str | None = None
    searchSurfaceValidated: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class PostNavigationDiagnostic:
    timestampUtc: str
    resultingUrl: str | None = None
    title: str | None = None
    mainDocumentStatus: int | None = None
    redirectCount: int | None = None
    routeClassification: str | None = None
    sorryDetected: bool = False
    challengeDetected: bool = False
    ordinaryResults: bool = False
    ebayLive: bool = False
    soldControlAvailable: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GuiAttemptTimings:
    """Stage timestamps (epoch seconds) for one pricing attempt."""

    fields: dict[str, float] = field(default_factory=dict)

    def mark(self, name: str, when: float | None = None) -> None:
        self.fields[name] = float(when if when is not None else time.time())

    def elapsed_ms(self) -> dict[str, int | None]:
        order = [
            "T0_job_claimed",
            "T1_browser_ready",
            "T2_search_surface_ready",
            "T3_search_input_focused",
            "T4_query_visible_confirmed",
            "T5_search_submitted",
            "T6_results_confirmed",
            "T7_sold_control_located",
            "T8_sold_activated",
            "T9_sold_state_verified",
            "T10_html_data_captured",
            "T11_exact_comp_parse_complete",
            "T12_pricing_calculation_complete",
            "T13_db_cache_snapshot_write_complete",
            "T14_job_finalized",
        ]
        out: dict[str, int | None] = {}
        prev: float | None = None
        for key in order:
            ts = self.fields.get(key)
            if ts is None:
                out[key] = None
                continue
            if prev is None:
                out[key] = 0
            else:
                out[key] = int(round((ts - prev) * 1000))
            prev = ts
        if "T0_job_claimed" in self.fields and "T14_job_finalized" in self.fields:
            out["totalMs"] = int(round((self.fields["T14_job_finalized"] - self.fields["T0_job_claimed"]) * 1000))
        return out

    def to_dict(self) -> dict[str, Any]:
        return {"marks": self.fields, "elapsedMs": self.elapsed_ms()}


def build_pre_submit_snapshot(
    *,
    current_url: str | None,
    page_title: str | None,
    query_expected: str | None,
    query_visibly_confirmed: bool | None,
    search_field_geometry: dict[str, Any] | None = None,
    search_button_geometry: dict[str, Any] | None = None,
    selected_category_label: str | None = None,
    form_action: str | None = None,
    tab_count: int | None = None,
    modifier_key_state: str | None = None,
    fsm_state: str | None = None,
    focused_element_role: str | None = None,
    search_origin_url: str | None = None,
    search_surface_class: str | None = None,
    search_scope_label: str | None = None,
    search_surface_validated: bool | None = None,
) -> dict[str, Any]:
    return PreSubmitDiagnostic(
        timestampUtc=_utc_now(),
        currentUrl=current_url,
        pageTitle=page_title,
        queryExpected=query_expected,
        queryVisiblyConfirmed=query_visibly_confirmed,
        focusedElementRole=focused_element_role,
        searchFieldGeometry=search_field_geometry,
        searchButtonGeometry=search_button_geometry,
        selectedCategoryLabel=selected_category_label,
        formAction=form_action,
        tabCount=tab_count,
        modifierKeyState=modifier_key_state,
        fsmState=fsm_state,
        searchOriginUrl=search_origin_url if search_origin_url is not None else current_url,
        searchSurfaceClass=search_surface_class,
        searchScopeLabel=search_scope_label if search_scope_label is not None else selected_category_label,
        searchSurfaceValidated=search_surface_validated,
    ).to_dict()


def build_post_navigation_snapshot(
    *,
    resulting_url: str | None,
    title: str | None,
    main_document_status: int | None = None,
    redirect_count: int | None = None,
    route_classification: str | None = None,
    sorry_detected: bool = False,
    challenge_detected: bool = False,
    ordinary_results: bool = False,
    ebay_live: bool = False,
    sold_control_available: bool | None = None,
) -> dict[str, Any]:
    return PostNavigationDiagnostic(
        timestampUtc=_utc_now(),
        resultingUrl=resulting_url,
        title=title,
        mainDocumentStatus=main_document_status,
        redirectCount=redirect_count,
        routeClassification=route_classification,
        sorryDetected=sorry_detected,
        challengeDetected=challenge_detected,
        ordinaryResults=ordinary_results,
        ebayLive=ebay_live,
        soldControlAvailable=sold_control_available,
    ).to_dict()
