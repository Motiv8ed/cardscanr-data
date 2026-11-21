#!/usr/bin/env python3
"""Archive-only Chinese native identity evidence from a SHA-pinned bulk source.

Source: zsjy947/PTCG-card-data, CC0 for compilation/format only.
Upstream text, card art, names, and other third-party rights are NOT licensed
by this script. No image URLs, binaries, live vault writes, or publication.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path

PINNED = "7f03f38f4538f01d8ee03121aa91aad3b0ae1149"

def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def examine(root: Path) -> tuple[list[dict], dict]:
    manifest_bytes = (root / "data/manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest.get("schemaVersion") != 1:
        raise ValueError("schema_drift")
    sets = manifest.get("sets")
    if not isinstance(sets, dict) or not sets:
        raise ValueError("missing_set_manifest")
    seen = defaultdict(list)
    rows = []
    md5_receipts = {}
    for set_id in sorted(sets):
        spec = sets[set_id]
        if not isinstance(set_id, str) or not set_id or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.-_" for c in set_id):
            raise ValueError("unsafe_set_id")
        file = root / "data/cards" / (set_id + ".json")
        if file.is_symlink() or not file.is_file() or file.stat().st_size > 4_000_000:
            raise ValueError("unsafe_set_file")
        raw = file.read_bytes()
        actual = hashlib.md5(raw).hexdigest()
        if actual != spec["md5"]:
            raise ValueError("source_md5_drift:" + set_id)
        cards = json.loads(raw)
        if not isinstance(cards, list) or len(cards) != spec["count"]:
            raise ValueError("source_card_count_drift:" + set_id)
        md5_receipts[set_id] = actual
        # Source README documents the split 151C waves with upstream
        # card setCode 151C; retain the file's wave as a distinct identity.
        is_151_wave = set_id in {"151C-LV", "151C-WANG", "151C-JING", "151C-JU"}
        expected_card_code = "151C" if is_151_wave else set_id
        for card in cards:
            if not isinstance(card, dict):
                raise ValueError("invalid_source_card_record:" + set_id)
            # The published CS1DC file contains eight CSAC records. This is
            # genuine upstream data drift. Quarantine mismatches in source-
            # only evidence instead of silently relabelling or importing.
            code_matches = card.get("setCode") == expected_card_code
            number = card.get("cardIndex")
            name = card.get("cardName")
            if not all(isinstance(x, str) and 0 < len(x) <= 300 for x in (number, name)):
                raise ValueError("incomplete_native_card_identity")
            if not isinstance(card.get("rarity"), str):
                raise ValueError("missing_rarity")
            key = (set_id.casefold(), number.lstrip("0") or "0", name.strip())
            if code_matches:
                seen[key].append(card)
            rows.append({
                "language": "zh-cn", "source_set_code": set_id,
                "source_observed_set_code": card.get("setCode"),
                "source_file_set_code_verified": code_matches,
                "source_collector_number": number, "native_chinese_name": name,
                "source_rarity": card["rarity"],
                "source_english_name": card.get("nameEn") if isinstance(card.get("nameEn"), str) else None,
                "source_english_set_code": card.get("setCodeEn") if isinstance(card.get("setCodeEn"), str) else None,
                "source_english_collector_number": card.get("cardIndexEn") if isinstance(card.get("cardIndexEn"), str) else None,
                "classification": ("NATIVE_IDENTITY_CORROBORATION_ONLY"
                                   if code_matches else "UPSTREAM_SET_DRIFT_QUARANTINED"),
                "canonical_printing_id": None,
                "image_url": None, "image_rights_granted": False,
                "live_vault_match_verified": False,
                "safe_for_vault_apply": False,
            })
    duplicates = {k for k, values in seen.items() if len(values) > 1}
    # Fail closed on duplicates: quarantine all repeated native identities.
    for row in rows:
        key = (row["source_set_code"].casefold(), row["source_collector_number"].lstrip("0") or "0", row["native_chinese_name"].strip())
        row["source_identity_unique"] = (
            row["source_file_set_code_verified"] and key not in duplicates
        )
    rows.sort(key=lambda r: (r["source_set_code"], r["source_collector_number"], r["native_chinese_name"], r["source_rarity"]))
    receipt = {
        "classification": "PRIVATE_NATIVE_CHINESE_IDENTITY_EVIDENCE_ONLY",
        "source_repository": "zsjy947/PTCG-card-data",
        "source_commit": PINNED,
        "source_manifest_sha256": sha256(manifest_bytes),
        "source_manifest_generated": manifest.get("generated"),
        "source_set_count": len(sets), "source_card_rows": len(rows),
        "unique_native_identity_rows": sum(r["source_identity_unique"] for r in rows),
        "quarantined_duplicate_identity_rows": sum(
            r["source_file_set_code_verified"] and not r["source_identity_unique"]
            for r in rows),
        "quarantined_source_set_mismatch_rows": sum(
            not r["source_file_set_code_verified"] for r in rows),
        "quarantined_identity_rows": sum(not r["source_identity_unique"] for r in rows),
        "source_set_md5": md5_receipts,
        "rights": "CC0 compilation/format only; upstream content/artwork rights NOT granted",
        "artwork_url_count": 0, "image_binaries_downloaded": 0,
        "canonical_cards_added": 0, "live_vault_writes": 0,
        "verified_live_printing_matches": 0, "safe_for_publication": False,
        "note": "Independent native name+set+number+rarity evidence; exact live printing/finish and rights require separate verification.",
    }
    return rows, receipt

def write(root: Path, output: Path) -> dict:
    if output.exists() or output.is_symlink():
        raise FileExistsError("output_must_be_new")
    rows, receipt = examine(root)
    output.mkdir(parents=True)
    raw = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows).encode()
    (output / "native_identity.jsonl").write_bytes(raw)
    receipt["native_identity_jsonl_sha256"] = sha256(raw)
    (output / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf8")
    return receipt

def self_test():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        folder = root / "data/cards"
        folder.mkdir(parents=True)
        cards = [
            {"setCode": "CSV1C", "cardIndex": "001", "cardName": "皮卡丘", "rarity": "C", "nameEn": "Pikachu"},
            {"setCode": "CSV1C", "cardIndex": "001", "cardName": "皮卡丘", "rarity": "R", "nameEn": "Pikachu"},
            {"setCode": "CSV1C", "cardIndex": "002", "cardName": "喵喵", "rarity": "C"},
        ]
        raw = json.dumps(cards).encode()
        (folder / "CSV1C.json").write_bytes(raw)
        manifest = {"schemaVersion": 1, "generated": "test", "sets": {"CSV1C": {"count": 3, "md5": hashlib.md5(raw).hexdigest()}}}
        (root / "data/manifest.json").write_text(json.dumps(manifest))
        rows, receipt = examine(root)
        assert receipt["source_card_rows"] == 3
        assert receipt["quarantined_duplicate_identity_rows"] == 2
        assert receipt["unique_native_identity_rows"] == 1
        assert all(r["safe_for_vault_apply"] is False and r["image_url"] is None for r in rows)
        # Strictly document and verify only the four known 151C wave aliases.
        wave = [{"setCode": "151C", "cardIndex": "001", "cardName": "妙蛙种子", "rarity": "C"}]
        wave_bytes = json.dumps(wave).encode()
        (folder / "151C-JING.json").write_bytes(wave_bytes)
        manifest["sets"]["151C-JING"] = {"count": 1, "md5": hashlib.md5(wave_bytes).hexdigest()}
        (root / "data/manifest.json").write_text(json.dumps(manifest))
        wave_rows, wave_receipt = examine(root)
        assert wave_receipt["source_card_rows"] == 4
        assert any(r["source_set_code"] == "151C-JING" for r in wave_rows)
        wave[0]["setCode"] = "NOT151C"
        tampered = json.dumps(wave).encode()
        (folder / "151C-JING.json").write_bytes(tampered)
        manifest["sets"]["151C-JING"]["md5"] = hashlib.md5(tampered).hexdigest()
        (root / "data/manifest.json").write_text(json.dumps(manifest))
        drift_rows, drift_receipt = examine(root)
        assert drift_receipt["quarantined_source_set_mismatch_rows"] == 1
        assert any(r["source_set_code"] == "151C-JING" and
                   r["classification"] == "UPSTREAM_SET_DRIFT_QUARANTINED" and
                   not r["source_identity_unique"] for r in drift_rows)
        (folder / "151C-JING.json").write_bytes(wave_bytes)
        manifest["sets"]["151C-JING"]["md5"] = hashlib.md5(wave_bytes).hexdigest()
        (root / "data/manifest.json").write_text(json.dumps(manifest))
        # Non-wave wrong source set code is quarantined, never treated as
        # validated source identity or eligible for a live import.
        mixed = [
            {"setCode": "CS1DC", "cardIndex": "001", "cardName": "正常", "rarity": "C"},
            {"setCode": "CSAC", "cardIndex": "002", "cardName": "异源", "rarity": "C"},
        ]
        mixed_bytes = json.dumps(mixed).encode()
        (folder / "CS1DC.json").write_bytes(mixed_bytes)
        manifest["sets"]["CS1DC"] = {"count": 2, "md5": hashlib.md5(mixed_bytes).hexdigest()}
        (root / "data/manifest.json").write_text(json.dumps(manifest))
        mixed_rows, mixed_receipt = examine(root)
        assert mixed_receipt["quarantined_source_set_mismatch_rows"] == 1
        assert mixed_receipt["quarantined_identity_rows"] == 3
        assert len([r for r in mixed_rows if r["classification"] == "UPSTREAM_SET_DRIFT_QUARANTINED"]) == 1
        assert all(not r["safe_for_vault_apply"] for r in mixed_rows)
        manifest["sets"]["CSV1C"]["md5"] = "0" * 32
        (root / "data/manifest.json").write_text(json.dumps(manifest))
        try:
            examine(root)
            raise AssertionError("tampered source accepted")
        except ValueError as e:
            assert str(e).startswith("source_md5_drift")
    print("PASS: SHA receipt, duplicate quarantine, rights hold, tamper fail-closed")

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--source", type=Path)
    p.add_argument("--output", type=Path)
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args()
    if a.self_test:
        self_test()
    else:
        if not a.source or not a.output:
            p.error("--source and --output required")
        r = write(a.source, a.output)
        print(json.dumps({k: r[k] for k in ("source_set_count", "source_card_rows", "unique_native_identity_rows", "quarantined_duplicate_identity_rows", "verified_live_printing_matches", "live_vault_writes")}, sort_keys=True))
