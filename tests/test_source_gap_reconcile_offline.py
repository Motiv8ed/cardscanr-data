import json, tempfile, unittest
from pathlib import Path
from tools.source_gap_reconcile_offline import reconcile

BASE={"canonical_id":"id-1","language":"id","set_code":"SV2a","collector_number":"001","variant":"standard"}
GOOD={**BASE,"source_kind":"official_card_detail","rights_status":"verified","source_url":"https://asia.pokemon-card.com/id/card-search/1/","image_url":"https://asia.pokemon-card.com/id/assets/1.png","source_sha256":"a"*64}

class ReconcileTests(unittest.TestCase):
    def check(self, evidence):
        with tempfile.TemporaryDirectory() as d:
            gaps=Path(d)/"gaps.jsonl";ev=Path(d)/"ev.jsonl"
            gaps.write_text(json.dumps(BASE)+"\n",encoding="utf-8")
            ev.write_text("".join(json.dumps(x)+"\n" for x in evidence),encoding="utf-8")
            return reconcile(gaps,ev)
    def test_valid_candidate_is_not_live_link(self):
        s,ok,q=self.check([GOOD]);self.assertEqual(len(ok),1);self.assertIsNone(s["new_live_links"])
    def test_spoofed_host(self):
        s,ok,q=self.check([{**GOOD,"image_url":"https://asia.pokemon-card.com.evil.test/a"}]);self.assertFalse(ok)
    def test_conflicting_second_receipt_quarantines_first(self):
        s,ok,q=self.check([GOOD,{**GOOD,"source_sha256":"bad"}]);self.assertFalse(ok);self.assertEqual(len(q),2)
    def test_replay_exclusion(self):
        s,ok,q=self.check([{**GOOD,"batch_id":"idremain20261122sv1v"}]);self.assertFalse(ok)
if __name__=="__main__":unittest.main()
