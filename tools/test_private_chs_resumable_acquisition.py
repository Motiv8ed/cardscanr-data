import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from tools import private_chs_resumable_acquisition as m

def fixture():
    sid="2"; slug="zh-hans/storming-emergence-verdant"
    cid="ptcg-chs-datasets:printing:2_10_abc:zh-cn:CN"
    pic="a"*32
    live=dict(canonical_printing_id=cid,set_id="ptcg-chs-datasets:set:2",language="zh-cn",has_image_link=False,native_card_name="皮卡丘",printed_collector_number="001")
    source=dict(id=slug,catalog="zh-hans",language="zh-Hans",code="CSM1bC",cards=[dict(number={"printed":"001/100"},variants=[dict(id="non-holo",picture={"front":{"image":pic}},finish="non_holo")])])
    archive=[dict(archived_canonical_printing_id=cid,archived_native_chinese_name="皮卡丘",printed_collector_number="001/100",source_picture_id=pic,normalized_source_set_code="CSM1BC",source_set_id=slug,independent_native_identity_corroborated=True)]
    scope=dict(id=sid,source_set=slug,archive_candidate_count=1,expected_tuple_sha256=m.sha(m.encoded(m.expected(archive,sid,slug))))
    return source,[live],archive,scope

class AcquisitionTests(unittest.TestCase):
    def test_exact_and_wrong_name(self):
        s,l,a,p=fixture()
        self.assertEqual(len(m.match(s,l,a,p)[0]),1)
        l[0]["native_card_name"]="喵喵"
        self.assertEqual(m.match(s,l,a,p)[1],{"archive_tuple_mismatch":1})
    def test_duplicate_number_and_multi_variant(self):
        s,l,a,p=fixture()
        s["cards"].append(copy.deepcopy(s["cards"][0]))
        self.assertEqual(m.match(s,l,a,p)[1],{"ambiguous_number":1})
        s,l,a,p=fixture()
        s["cards"][0]["variants"].append({"id":"holo"})
        self.assertEqual(m.match(s,l,a,p)[1],{"variant_not_unique":1})
    def test_plan_digest_and_live_scope_rejected(self):
        s,l,a,p=fixture()
        with self.assertRaises(ValueError): m.match(s,l,a,dict(p,expected_tuple_sha256="0"*64))
        l[0]["has_image_link"]=True
        with self.assertRaises(ValueError): m.match(s,l,a,p)
    def test_snapshot_integrity(self):
        s,l,a,p=fixture()
        h=m.sha(json.dumps(l,sort_keys=True,ensure_ascii=False,separators=(",",":")).encode())
        def read(secret,**kw): return dict(snapshot_sha256=h,missing_cards_in_snapshot=1,rows=l,next_cursor=None)
        self.assertEqual(m.live_get("dummy","2",read=read),(l,h))
        l[0]["native_card_name"]="wrong"
        with self.assertRaises(ValueError): m.live_get("dummy","2",read=read)
    def test_checkpoint_create_only(self):
        with tempfile.TemporaryDirectory() as td:
            f=Path(td)/"set_2.json"
            m.atomic_new(f,{"x":1});m.atomic_new(f,{"x":1})
            with self.assertRaises(ValueError):m.atomic_new(f,{"x":2})
            self.assertEqual(json.loads(f.read_bytes()),{"x":1})
            self.assertEqual(list(Path(td).iterdir()),[f])
    def test_timeout_then_next_source_and_resume(self):
        s,l,a,p=fixture()
        plan=dict(crosswalk_sha256=m.CROSSWALK,source_commit=m.REF,sets=[dict(p,id="30",source_set="zh-hans/dynamax-clash-flame"),p])
        def source_reader(slug):
            if slug!=p["source_set"]:raise TimeoutError()
            return s,m.encoded(s)
        def live_reader(secret,sid):return l,"a"*64
        with tempfile.TemporaryDirectory() as td,patch.object(m.time,"sleep"):
            out=Path(td)
            self.assertEqual(m.run(plan,a,out,"dummy",2,source_reader,live_reader)["verified_candidates"],1)
            self.assertFalse((out/"set_30.json").exists())
            self.assertEqual(m.run(plan,a,out,"dummy",2,source_reader,live_reader)["verified_candidates"],0)
            self.assertEqual(json.loads((out/"set_2.json").read_text(encoding="utf8"))["new_live_links"],0)

if __name__=="__main__":unittest.main()
