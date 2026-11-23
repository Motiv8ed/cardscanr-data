#!/usr/bin/env python3
"""Offline exact-printing source evidence reconciliation. Never claims live links."""
import argparse, collections, json, urllib.parse

HOSTS={"th":{"asia.pokemon-card.com"},"id":{"asia.pokemon-card.com"},"zh-tw":{"asia.pokemon-card.com"},"ko":{"pokemoncard.co.kr"},"zh-cn":{"www.pokemon.com.cn","image.pokemon.com.cn"}}
KEYS=("canonical_id","language","set_code","collector_number","variant")
EXCLUDED_BATCHES={"idremain20261122sv1v","thremaincombined20261122_01","idremaincombined20261122_01","thtrainercombined20261122_01"}

def rows(path):
    with open(path,encoding="utf-8") as f:
        for n,line in enumerate(f,1):
            if line.strip():
                try: yield n,json.loads(line)
                except json.JSONDecodeError: yield n,None

def identity(r):
    return tuple(str(r.get(k,"")).strip() for k in KEYS)

def official(url,language):
    try:
        p=urllib.parse.urlsplit(url)
        return p.scheme=="https" and p.hostname in HOSTS.get(language,set()) and not p.username and not p.password and bool(p.path.strip("/"))
    except (ValueError,TypeError): return False

def reconcile(gaps_path,evidence_path):
    gaps=set(); invalid_gaps=0
    for _,r in rows(gaps_path):
        if not isinstance(r,dict):
            invalid_gaps+=1
            continue
        k=identity(r)
        if not all(k) or k in gaps: invalid_gaps+=1
        else: gaps.add(k)
    eligible={}; blocked=set(); seen=set(); quarantine=[]; counts=collections.Counter()
    for n,r in rows(evidence_path):
        if not isinstance(r,dict):
            quarantine.append({"line":n,"reason":"invalid_evidence_record","identity":None})
            counts["invalid_evidence_record"]+=1
            continue
        k=identity(r); reason=None
        if all(k) and k in seen:
            blocked.add(k)
            if k in eligible:
                prior=eligible.pop(k)
                quarantine.append({"line":prior["line"],"reason":"conflicting_or_duplicate_identity","identity":k})
                counts["conflicting_or_duplicate_identity"]+=1
        if all(k): seen.add(k)
        if not all(k): reason="incomplete_identity"
        elif k not in gaps: reason="not_in_missing_snapshot"
        elif k in blocked: reason="conflicting_or_duplicate_identity"
        elif not isinstance(r.get("batch_id"),str) or not r["batch_id"].strip(): reason="missing_batch_id_replay_evidence"
        elif r.get("batch_id") in EXCLUDED_BATCHES: reason="previously_completed_batch"
        elif r.get("source_kind")!="official_card_detail": reason="not_card_detail"
        elif r.get("rights_status")!="verified": reason="rights_not_attested"
        elif not official(r.get("source_url"),k[1]) or not official(r.get("image_url"),k[1]): reason="nonofficial_host"
        elif not isinstance(r.get("source_sha256"),str) or len(r["source_sha256"])!=64 or any(c not in "0123456789abcdef" for c in r["source_sha256"].lower()): reason="invalid_sha"
        if reason:
            quarantine.append({"line":n,"reason":reason,"identity":k});counts[reason]+=1
        else:
            eligible[k]={"line":n,"identity":k,"source_url":r["source_url"],"image_url":r["image_url"],"source_sha256":r["source_sha256"]}
    summary={"missing_snapshot_distinct":len(gaps),"invalid_gap_rows":invalid_gaps,"eligible_candidate_records":len(eligible),"quarantined_rows":len(quarantine),"reasons":dict(counts),"new_live_links":None,"warning":"Candidate screening only. URL host, self-reported rights and SHA format do not independently prove permissions, source authenticity, response hash or printing identity."}
    return summary,list(eligible.values()),quarantine

def main():
    p=argparse.ArgumentParser()
    for name in ("gaps","evidence","output"): p.add_argument("--"+name,required=True)
    a=p.parse_args();summary,eligible,quarantine=reconcile(a.gaps,a.evidence)
    with open(a.output,"w",encoding="utf-8") as f:json.dump({"summary":summary,"eligible":eligible,"quarantine":quarantine},f,indent=2,ensure_ascii=False)
    print(json.dumps(summary))
if __name__=="__main__":main()
