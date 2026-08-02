#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, sqlite3
from datetime import datetime, timezone
from pathlib import Path

PROVIDER="pokellector-english-gap-evidence"

def main() -> int:
    parser=argparse.ArgumentParser(); parser.add_argument("--database",required=True,type=Path)
    parser.add_argument("--checkpoint",required=True,type=Path); parser.add_argument("--json",required=True,type=Path)
    parser.add_argument("--markdown",required=True,type=Path); args=parser.parse_args()
    source=sqlite3.connect(f"file:{args.checkpoint.resolve()}?mode=ro",uri=True)
    staging=sqlite3.connect(f"file:{args.database.resolve()}?mode=ro",uri=True)
    report={
      "schema_version":1,"generated_at_utc":datetime.now(timezone.utc).isoformat(),
      "classification":"EXACT_CANDIDATES_ACQUIRED_EXTERNAL_RIGHTS_AND_WATERMARK_BLOCKERS",
      "collector_evidence":dict(source.execute("select status,count(*) from evidence group by status").fetchall()),
      "candidate_statuses":dict(staging.execute("select validation_status,count(*) from card_image_candidate where provider_id=? group by validation_status",(PROVIDER,)).fetchall()),
      "validation_statuses":dict(staging.execute("""select ivr.status,count(*) from image_validation_result ivr
        where ivr.card_image_candidate_id in (select id from card_image_candidate where provider_id=?) group by ivr.status""",(PROVIDER,)).fetchall()),
      "unresolved_statuses":dict(staging.execute("""select ui.status,count(*) from unresolved_item ui where ui.entity_id in
        (select card_variant_id from card_image_candidate where provider_id=?) and ui.issue_class='card_image_identity_review'
        group by ui.status""",(PROVIDER,)).fetchall()),
      "affected_variants":staging.execute("select count(distinct card_variant_id) from card_image_candidate where provider_id=?",(PROVIDER,)).fetchone()[0],
      "rights_status":"permission_pending","watermark_status":"known_provider_watermark_detected",
      "publication_eligible":False,
      "next_owner_action":"Obtain written redistribution permission and clean non-watermarked source scans, or approve another rights-cleared source.",
    }
    source.close(); staging.close(); args.json.parent.mkdir(parents=True,exist_ok=True)
    args.json.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8",newline="\n")
    lines=["# English legacy gap community-image reconciliation","",
      f"- Classification: **{report['classification']}**",f"- Exact affected variants: `{report['affected_variants']}`",
      f"- Collector evidence: `{report['collector_evidence']}`",f"- Candidate statuses: `{report['candidate_statuses']}`",
      f"- Validation statuses: `{report['validation_statuses']}`",f"- Unresolved statuses: `{report['unresolved_statuses']}`",
      f"- Rights status: `{report['rights_status']}`",f"- Watermark status: `{report['watermark_status']}`",
      f"- Publication eligible: `{str(report['publication_eligible']).lower()}`","",report["next_owner_action"]]
    args.markdown.write_text("\n".join(lines)+"\n",encoding="utf-8",newline="\n")
    print(json.dumps({"classification":report["classification"],"affected_variants":report["affected_variants"]},indent=2)); return 0
if __name__ == "__main__": raise SystemExit(main())
