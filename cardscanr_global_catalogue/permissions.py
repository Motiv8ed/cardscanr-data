from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT=Path(__file__).resolve().parents[1]
TRACKER=ROOT/"reports/global_rollout/provider_permission_tracker.json"
FINAL_STATUSES={"approved","approved_with_conditions","denied","unclear","no_response","pending"}
ALLOW_FIELDS=("metadataRetentionAllowed","imageDownloadAllowed","resizingAllowed","r2StorageAllowed","publicServingAllowed","commercialUseAllowed","indefiniteRetentionAllowed")

def load_tracker(path: Path=TRACKER)->dict[str,Any]: return json.loads(path.read_text(encoding="utf-8"))

def validate_provider_permission(provider: dict[str,Any], *, root: Path=ROOT)->dict[str,Any]:
    status=str(provider.get("finalStatus") or provider.get("status") or "pending")
    if status=="pending_human_review": status="pending"
    errors=[]
    if status not in FINAL_STATUSES: errors.append("invalid_final_status")
    evidence=provider.get("evidenceFile")
    evidence_exists=bool(evidence and (root/str(evidence)).is_file())
    if status in {"approved","approved_with_conditions"} and not evidence_exists: errors.append("approval_requires_evidence_file")
    if status in {"approved","approved_with_conditions"}:
        for field in ALLOW_FIELDS:
            if provider.get(field) is not True: errors.append(f"approval_requires_{field}")
    if status=="approved_with_conditions" and not provider.get("conditions"):
        errors.append("approved_with_conditions_requires_conditions")
    return {"provider":provider.get("provider"),"finalStatus":status,"evidencePresent":evidence_exists,"valid":not errors,"errors":errors,"executionAllowed":status in {"approved","approved_with_conditions"} and not errors}

def permissions_status(path: Path=TRACKER)->dict[str,Any]:
    tracker=load_tracker(path); results=[validate_provider_permission(p) for p in tracker.get("providers",[])]
    return {"classification":"PASS" if all(r["valid"] for r in results) else "FAIL","providers":results}

def image_canary_guard(provider: str, *, tracker_path: Path=TRACKER, dry_run: bool=True,
                       exact_catalogue: bool=True, image_safe: bool=True,
                       credentials_valid: bool=False, budget_writes: int=0,
                       requested_writes: int=0, budget_bytes: int=0,
                       requested_bytes: int=0, language: str|None=None, r2_valid: bool=False,
                       production_publish: bool=False)->list[str]:
    entries={p.get("provider"):p for p in load_tracker(tracker_path).get("providers",[])}; errors=[]
    entry=entries.get(provider)
    if not entry or not validate_provider_permission(entry)["executionAllowed"]: errors.append("provider_permission_not_approved")
    if entry and language and entry.get("permittedLanguages") and language not in entry["permittedLanguages"]:
        errors.append("permission_language_not_permitted")
    if not exact_catalogue: errors.append("catalogue_identity_not_exact")
    if not image_safe: errors.append("image_identity_not_safe")
    if provider in {"pokemon_tcg_api","pokewallet"} and not credentials_valid: errors.append("provider_credentials_invalid")
    if requested_writes>budget_writes: errors.append("write_budget_exceeded")
    if requested_bytes>budget_bytes: errors.append("byte_budget_exceeded")
    if not dry_run and not r2_valid: errors.append("r2_credentials_invalid")
    if production_publish: errors.append("production_publication_forbidden")
    return errors
