#!/usr/bin/env python3
"""Read-only exact live KO/TW missing identity join against official-source review rows.

Never outputs importable batches. Source artwork rights and finish/variant must
be independently verified. Only unique literal joins are review candidates.
"""
from __future__ import annotations
import argparse, collections, hashlib, json, pathlib, unicodedata
from urllib.parse import urlsplit

def nfc(v):
    return unicodedata.normalize("NFC", v) if isinstance(v, str) else ""

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def rows(path, max_rows=100000, max_line_bytes=1048576):
    """Stream bounded JSONL input instead of loading untrusted archives at once."""
    count = 0
    with path.open("rb") as handle:
        for number, line in enumerate(handle, 1):
            if len(line) > max_line_bytes:
                raise ValueError(f"jsonl_line_too_large:{path}:{number}")
            if not line.strip():
                continue
            count += 1
            if count > max_rows:
                raise ValueError(f"jsonl_row_limit:{path}:{number}")
            obj = json.loads(line.decode("utf-8"))
            if not isinstance(obj, dict):
                raise ValueError(f"non-object:{path}:{number}")
            yield obj

def key(row):
    fields = ("language", "set_id", "native_card_name",
              "printed_collector_number", "printed_denominator")
    values = []
    for field in fields:
        value = row.get(field)
        if not isinstance(value, str) or not value.strip():
            return None
        values.append(nfc(value))
    return tuple(values)

def valid_https_reference(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        return (parsed.scheme == "https" and bool(parsed.hostname)
                and not parsed.username and not parsed.password
                and not any(c.isspace() for c in value))
    except ValueError:
        return False

def reconcile(live_paths, source_path, output):
    if output.exists():
        raise FileExistsError("output_exists")
    if not live_paths or len(set(map(str, live_paths))) != len(live_paths):
        raise ValueError("duplicate_live_page_path")
    # Private snapshot is archived in six JSONL chunks, not 51 API-sized pages.
    # Bound each file while retaining the strict aggregate identity checks below.
    pages = [list(rows(path, max_rows=500)) for path in live_paths]
    if any(not page or len(page) > 500 for page in pages):
        raise ValueError("live_page_size_invalid")
    live = [x for page in pages for x in page]
    if len(live) != 1528 or collections.Counter(x.get("language") for x in live) != {"ko":1131,"zh-tw":397}:
        raise ValueError("live_snapshot_count_drift")
    ids = [x.get("canonical_printing_id") for x in live]
    if len(set(ids)) != len(ids) or any(not isinstance(x,str) or not x for x in ids):
        raise ValueError("duplicate_or_missing_live_id")
    if any(x.get("has_image_link") is not False for x in live):
        raise ValueError("live_record_not_missing")
    # Reject mixed or incomplete page snapshots before any matching.
    # Workbench snapshots are language-scoped, not one global KO/TW hash.
    snapshots_by_language = collections.defaultdict(set)
    for record in live:
        value = record.get("snapshot_sha256")
        if (not isinstance(value, str) or len(value) != 64 or
            any(c not in "0123456789abcdef" for c in value)):
            raise ValueError("missing_or_mixed_live_snapshot")
        snapshots_by_language[record["language"]].add(value)
    if any(len(values) != 1 for values in snapshots_by_language.values()):
        raise ValueError("missing_or_mixed_live_snapshot")
    by_key = collections.defaultdict(list)
    for x in live:
        k = key(x)
        if k is not None:
            by_key[k].append(x)
    source = list(rows(source_path))
    src_key = collections.defaultdict(list)
    for x in source:
        if x.get("language") in ("ko","zh-tw"):
            k = key(x)
            if k is not None:
                src_key[k].append(x)
    outcomes = collections.Counter()
    reviews = []
    exact_identity_matches = 0
    observed_image_urls = set()
    for target in live:
        k = key(target)
        matches = src_key.get(k, [])
        if k is None:
            state = "quarantine_missing_identity_fields"
        elif len(by_key[k]) != 1:
            state = "quarantine_multiple_live_printings"
        elif not matches:
            state = "no_exact_source"
        elif len(matches) != 1:
            state = "quarantine_multiple_source_printings"
        elif not target.get("finish") or not matches[0].get("finish"):
            state = "quarantine_finish_not_proven"
        elif target["finish"] != matches[0]["finish"]:
            state = "quarantine_finish_conflict"
        elif matches[0].get("artwork_commercial_rights_verified") is not True:
            state = "quarantine_artwork_rights"
        elif not valid_https_reference(matches[0].get("artwork_rights_evidence_url")):
            state = "quarantine_missing_rights_evidence"
        else:
            state = "review_unique_identity_and_rights_claim"
        # Count source-only evidence independently from artwork rights or live inserts.
        # Never manufacture image URLs from a provider base or an English image.
        unique = matches[0] if k is not None and len(by_key[k]) == 1 and len(matches) == 1 else None
        source_url = unique.get("image_url") if unique else None
        if unique:
            exact_identity_matches += 1
        # Malformed IPv6 hosts and whitespace must not crash a research run
        # or be counted as usable source artwork references.
        if not valid_https_reference(source_url):
            source_url = None
        if source_url:
            observed_image_urls.add(source_url)
        outcomes[state] += 1
        reviews.append({"canonical_printing_id":target["canonical_printing_id"],
                        "language":target["language"],"classification":state,
                        "source_matches":len(matches),
                        "source_image_url_reference":source_url,
                        "safe_for_vault_apply":False,
                        "public_artwork_allowed":False})
    result = {"classification":"PRIVATE_READ_ONLY_LIVE_IDENTITY_REVIEW",
              "live_rows":len(live),"source_rows":len(source),
              "exact_source_identity_matches":exact_identity_matches,
              "distinct_observed_source_image_urls":len(observed_image_urls),
              "new_live_image_links":0,"new_canonical_cards":0,
              "rights_granted":False,"production_writes":False,
              "input_sha256":{str(p):digest(p) for p in [*live_paths,source_path]},
              "outcomes":dict(sorted(outcomes.items())),"reviews":reviews}
    output.write_text(json.dumps(result,ensure_ascii=False,sort_keys=True,indent=2)+"\n",encoding="utf-8")
    return result

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--live",type=pathlib.Path,nargs="+",required=True)
    p.add_argument("--source",type=pathlib.Path,required=True)
    p.add_argument("--output",type=pathlib.Path,required=True)
    a=p.parse_args()
    r=reconcile(a.live,a.source,a.output)
    print(json.dumps({"live_rows":r["live_rows"],"outcomes":r["outcomes"]},sort_keys=True))

if __name__=="__main__":
    main()
