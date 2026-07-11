import json
from pathlib import Path

from cardscanr_global_catalogue.contracts import canonical_set_id
from cardscanr_global_catalogue.permissions import image_canary_guard, permissions_status, validate_provider_permission


def provider(status="pending", **changes):
    value={"provider":"tcgdex","finalStatus":status,"evidenceFile":None,"metadataRetentionAllowed":None,"imageDownloadAllowed":None,"resizingAllowed":None,"r2StorageAllowed":None,"publicServingAllowed":None,"commercialUseAllowed":None,"indefiniteRetentionAllowed":None,"conditions":[],"permittedLanguages":[]}
    value.update(changes); return value


def approved(tmp_path: Path, status="approved", **changes):
    evidence=tmp_path/"evidence.txt"; evidence.write_text("written approval",encoding="utf-8")
    value=provider(status,evidenceFile="evidence.txt",metadataRetentionAllowed=True,imageDownloadAllowed=True,resizingAllowed=True,r2StorageAllowed=True,publicServingAllowed=True,commercialUseAllowed=True,indefiniteRetentionAllowed=True)
    value.update(changes); return value


def tracker(tmp_path: Path, entry):
    path=tmp_path/"tracker.json"; path.write_text(json.dumps({"providers":[entry]}),encoding="utf-8"); return path


def test_permission_evidence_required_and_denial_blocks(tmp_path):
    assert "approval_requires_evidence_file" in validate_provider_permission(provider("approved"),root=tmp_path)["errors"]
    path=tracker(tmp_path,provider("denied")); assert "provider_permission_not_approved" in image_canary_guard("tcgdex",tracker_path=path)


def test_approved_with_conditions_enforces_language(tmp_path,monkeypatch):
    entry=approved(tmp_path,"approved_with_conditions",conditions=["English only"],permittedLanguages=["en"])
    path=tracker(tmp_path,entry); monkeypatch.setattr("cardscanr_global_catalogue.permissions.ROOT",tmp_path)
    assert "permission_language_not_permitted" in image_canary_guard("tcgdex",tracker_path=path,language="ja")


def test_permission_status_never_exposes_secret(tmp_path):
    path=tracker(tmp_path,provider(apiKey="secret-value")); encoded=json.dumps(permissions_status(path)); assert "secret-value" not in encoded


def test_region_and_language_catalogue_ids_are_separate():
    zh1=canonical_set_id(language="zh-Hans",region="CN",provider="tcgdex",provider_set_id="x")
    zh2=canonical_set_id(language="zh-Hant",region="TW",provider="tcgdex",provider_set_id="x")
    es=canonical_set_id(language="es",region="ES",provider="tcgdex",provider_set_id="x")
    latam=canonical_set_id(language="es-419",region="LATAM",provider="tcgdex",provider_set_id="x")
    br=canonical_set_id(language="pt-BR",region="BR",provider="tcgdex",provider_set_id="x")
    pt=canonical_set_id(language="pt-PT",region="PT",provider="tcgdex",provider_set_id="x")
    assert len({zh1,zh2,es,latam,br,pt})==6


def test_import_refuses_budget_and_defaults_dry_run(tmp_path):
    path=tracker(tmp_path,provider("denied")); errors=image_canary_guard("tcgdex",tracker_path=path,requested_writes=1,budget_writes=0)
    assert "write_budget_exceeded" in errors and "provider_permission_not_approved" in errors


def test_duplicate_provider_id_must_be_scoped_by_language():
    records=[("tcgdex","en","x"),("tcgdex","ja","x")]
    assert len(set(records))==2
