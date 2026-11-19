"""Fail-closed candidate image controls and takedown audit tests."""
import json
import tempfile
import unittest
from pathlib import Path

from tools.private_artwork_takedown_controls import (
    apply_notice, atomic_save_json, default_policy, load_rows,
    resolve_candidate_images, validate_registry,
)


def candidate(cid, number="001"):
    url="https://assets.tcgdex.net/th/SV/SV7s/" + number
    return {
        "canonical_printing_id":cid,
        "language":"th",
        "provider":"tcgdex",
        "provider_set_id":"SV7s",
        "source_image_base_url":url,
        "provider_low_webp_url":url+"/low.webp",
        "identity_match":"exact_unique",
    }


class TakedownTest(unittest.TestCase):
    def test_default_global_kill_switch_blocks_even_opted_in_display(self):
        art, proof=resolve_candidate_images([candidate("p1"),candidate("p2","002")],default_policy(),explicitly_enabled=True)
        self.assertEqual(art,[])
        self.assertEqual(proof["blocked_candidate_count"],2)
        self.assertFalse(proof["copyright_permission_granted"])

    def test_specific_notice_blocks_only_matching_id_or_url(self):
        policy=default_policy()
        policy["global_kill_switch"]=False
        candidates=[candidate("p1"),candidate("p2","002"),candidate("p3","003")]
        shown,_=resolve_candidate_images(candidates,policy,explicitly_enabled=True)
        self.assertEqual(len(shown),3)
        revised=apply_notice(policy,case_id="NOTICE-001",
                             printing_ids=["p1"],
                             image_bases=[candidates[1]["source_image_base_url"]])
        shown,proof=resolve_candidate_images(candidates,revised,explicitly_enabled=True)
        self.assertEqual([r["canonical_printing_id"] for r in shown],["p3"])
        self.assertEqual(proof["blocked_candidate_count"],2)
        self.assertFalse(proof["cdn_purge_performed"])
        self.assertEqual(len(revised["notices"]),1)

    def test_notice_global_disable_takes_priority(self):
        p=default_policy();p["global_kill_switch"]=False
        p=apply_notice(p,case_id="STOP-ALL-2026",disable_everything=True)
        shown,_=resolve_candidate_images([candidate("p1")],p,explicitly_enabled=True)
        self.assertEqual(shown,[])
        self.assertTrue(p["global_kill_switch"])

    def test_duplicate_identity_and_bad_hosts_fail_closed(self):
        with self.assertRaisesRegex(ValueError,"duplicate_canonical"):
            validate_registry([candidate("p1"),candidate("p1","002")])
        bad=candidate("bad")
        bad["source_image_base_url"]="https://evil.example/th/SV/SV7s/001"
        with self.assertRaisesRegex(ValueError,"unsupported_image_source"):
            validate_registry([bad])
        bad=candidate("bad")
        bad["identity_match"]="fuzzy"
        with self.assertRaisesRegex(ValueError,"unverified_printing_identity"):
            validate_registry([bad])

    def test_invalid_notice_source_or_id_denied(self):
        with self.assertRaisesRegex(ValueError,"invalid_blocked_source"):
            apply_notice(default_policy(),case_id="VALID",image_bases=["http://evil.example/x"])
        with self.assertRaisesRegex(ValueError,"invalid_case_id"):
            apply_notice(default_policy(),case_id="../../pass",disable_everything=True)

    def test_atomic_policy_save_and_registry_are_readable(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            p=root/"policy.json"
            atomic_save_json(p,default_policy())
            self.assertTrue(json.loads(p.read_text())["global_kill_switch"])
            data=root/"candidates.jsonl"
            data.write_text(json.dumps(candidate("p1"))+"\n",encoding="utf-8")
            rows=load_rows(data)
            shown,_=resolve_candidate_images(rows,json.loads(p.read_text()),explicitly_enabled=False)
            self.assertEqual(shown,[])

    def test_no_notice_without_action(self):
        with self.assertRaisesRegex(ValueError,"notice_must_block_something"):
            apply_notice(default_policy(),case_id="EMPTY")


if __name__=="__main__":
    unittest.main()
