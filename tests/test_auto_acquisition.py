from __future__ import annotations
import hashlib
import importlib.util
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE=Path(__file__).resolve().parents[1]/'cardscanr_worldwide'/'auto_acquisition.py'
spec=importlib.util.spec_from_file_location('test_auto_acquisition_module', MODULE)
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class AutoAcquisitionTests(unittest.TestCase):
    def make_feed(self, path):
        data={"sources": {"tw-official": {"id":"tw-official", "region":"TW","language":"zh-Hant","enabled":True,
                        "rights_status":"metadata_only","seeds":[{"kind":"index","url":"https://official.invalid/tw/card-search/list/"}]},
                        "ko-official":{"id":"ko-official","region":"KR","language":"ko","enabled":False,
                        "rights_status":"metadata_only","seeds":[{"kind":"index","url":"https://official.invalid/cards"}]}}}
        fake=types.ModuleType('cardscanr_worldwide.source_feed')
        fake.load_feed=lambda _:({},data['sources'])
        return patch.dict(sys.modules,{'cardscanr_worldwide.source_feed':fake})

    def test_autodiscovery_plan_prioritises_korean_and_finds_seeds(self):
        with self.make_feed(None):
            plan=module.plan(Path('feed.json'))
        self.assertEqual([s['source_id'] for s in plan['sources']],['ko-official','tw-official'])
        self.assertTrue(plan['sources'][1]['has_autodiscovery_seed'])
        self.assertEqual(plan['sources'][0]['status'],'disabled_needs_source_review')

    def test_bounded_auto_cycle_needs_no_user_url(self):
        calls=[]
        def fake_run(feed,sid,archive,**kwargs):
            calls.append((sid,kwargs['max_items']))
            return {'new_images':2}
        with self.make_feed(None):
            out=module.run_cycle(Path('feed.json'),Path('archive'),per_source_limit=8,total_limit=8,run_batch_fn=fake_run)
        self.assertEqual(calls,[('tw-official',8)])
        self.assertEqual(out['verified_downloads'],2)
        self.assertEqual(out['attempt_budget_reserved'],8)

    def make_archive(self, directory, *, rights='metadata_only', corrupt=False):
        arch=directory/'archive'
        arch.mkdir()
        db=sqlite3.connect(arch/'source-feed-checkpoint.sqlite')
        db.execute('CREATE TABLE artifacts (source_id TEXT,kind TEXT,url TEXT,sha256 TEXT,byte_size INTEGER,relative_path TEXT,metadata_json TEXT,fetched_at TEXT)')
        card_url='https://official.test/kr/cards/123'
        image_url='https://official.test/images/123.png'
        data=[('card',card_url,b'<html>pika</html>', {'region':'KR','language':'ko','rights_status':rights}),
              ('image',image_url,b'\x89PNG\r\n\x1a\nfilebytes', {'region':'KR','language':'ko','rights_status':rights,'parent_url':card_url})]
        for kind,url,content,meta in data:
            relative=f'sources/kr-official/{kind}/test.{"png" if kind=="image" else "html"}'
            target=arch/relative
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(content + (b'corrupted' if corrupt and kind=='image' else b''))
            db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?)',('kr-official',kind,url,
                       hashlib.sha256(content).hexdigest(),len(content),relative,json.dumps(meta),'2026-11-15'))
        db.commit();db.close()
        return arch,card_url

    def test_stage_keeps_unmatched_and_rights_blocked_out_of_app(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            arch,_=self.make_archive(root)
            summary=module.stage_app_candidates(arch,root/'output')
            self.assertEqual(summary['card_records'],1)
            self.assertEqual(summary['image_files_checked'],1)
            self.assertEqual(summary['app_image_import_candidates'],0)
            rows=(root/'output'/'card_candidates.jsonl').read_text().splitlines()
            record=json.loads(rows[0])
            self.assertIn('no_verified_exact_identity_crosswalk',record['blocked_reasons'])
            self.assertIn('image_publication_rights_not_cleared',record['blocked_reasons'])
            self.assertFalse(summary['production_pack_published'])

    def test_bad_hash_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            arch,_=self.make_archive(root,corrupt=True)
            summary=module.stage_app_candidates(arch,root/'output')
            self.assertEqual(len(summary['files_with_missing_or_bad_hashes']),1)
            self.assertEqual(summary['app_image_import_candidates'],0)

    def test_existing_staged_identity_and_cleared_rights_yield_import_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            arch,url=self.make_archive(root,rights='approved_for_mirror')
            mapping=root/'mapping.jsonl'
            mapping.write_text(json.dumps({'source_id':'kr-official','source_card_url':url,'match_status':'verified',
                'canonical_printing_id':'cid1','card_variant_id':'vid1'})+'\n')
            staging=root/'world.sqlite'
            conn=sqlite3.connect(staging)
            conn.executescript('CREATE TABLE card_printing (id TEXT PRIMARY KEY); CREATE TABLE card_variant (id TEXT PRIMARY KEY,card_printing_id TEXT); INSERT INTO card_printing VALUES ("cid1"); INSERT INTO card_variant VALUES ("vid1","cid1");')
            conn.close()
            summary=module.stage_app_candidates(arch,root/'output',crosswalk=mapping,worldwide_db=staging)
            self.assertEqual(summary['app_image_import_candidates'],1)
            manifest=json.loads((root/'output'/'app_image_import_candidates.jsonl').read_text().strip())
            self.assertFalse(manifest['publicationApproved'])
            self.assertEqual(manifest['cardVariantId'],'vid1')

    def test_worldwide_gap_query_counts_distinct_printings(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'world.sqlite'; c=sqlite3.connect(db)
            c.executescript('''CREATE TABLE set_release (id TEXT PRIMARY KEY,region_code TEXT,language_code TEXT);
            CREATE TABLE card_printing (id TEXT PRIMARY KEY,set_release_id TEXT);
            CREATE TABLE card_variant (id TEXT PRIMARY KEY,card_printing_id TEXT);
            CREATE TABLE card_image_candidate (id TEXT PRIMARY KEY,card_variant_id TEXT);
            CREATE TABLE image_validation_result (id TEXT PRIMARY KEY,card_image_candidate_id TEXT,status TEXT);
            INSERT INTO set_release VALUES('s1','KR','ko'); INSERT INTO card_printing VALUES('cp1','s1'); INSERT INTO card_printing VALUES('cp2','s1');
            INSERT INTO card_variant VALUES('v1','cp1'); INSERT INTO card_image_candidate VALUES('i1','v1');
            INSERT INTO image_validation_result VALUES('r1','i1','pass');''')
            c.close()
            result=module.world_gaps(db)
            self.assertEqual(result[('KR','ko')]['without_verified_image_candidates'],1)
            self.assertEqual(result[('KR','ko')]['with_verified_image_candidates'],1)

if __name__=='__main__':unittest.main()