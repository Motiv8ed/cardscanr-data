#!/usr/bin/env python3
"""Read-only source-only TCGdex Korean Pocket per-set acquisition.
No guessed URLs, no artwork downloads, no production writes.
"""
import argparse, hashlib, json, pathlib, urllib.request, urllib.error, time

BASE = "https://api.tcgdex.net/v2/ko/sets/"
def fetch(set_id):
    req = urllib.request.Request(BASE + set_id, headers={"User-Agent":"CardScanR-source-research/1.0", "Accept":"application/json"})
    with urllib.request.urlopen(req, timeout=35) as response:
        if response.status != 200: raise RuntimeError(f"{set_id}: HTTP {response.status}")
        raw = response.read(8_000_001)
        if len(raw)>8_000_000: raise RuntimeError("Response too large")
    return json.loads(raw)

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--sets",nargs="+",default=["A1","A1a","A2","A2a","A2b","A3","A3a","A3b","A4","A4a","A4b","P-A"])
    p.add_argument("--out",default="source-research/ko-pocket")
    a=p.parse_args()
    out=pathlib.Path(a.out);out.mkdir(parents=True,exist_ok=True)
    rows=[];status=[]
    for sid in dict.fromkeys(a.sets):
        try:
            doc=fetch(sid)
            cards=doc.get("cards",[])
            if not isinstance(cards,list): raise ValueError("non-list cards")
            n=0
            for c in cards:
                if not isinstance(c,dict):continue
                # Do not synthesize image URLs. Keep only upstream-declared values.
                image=c.get("image")
                rows.append({"source":"tcgdex","language":"ko","source_set_requested":sid,
                    "source_set_id":doc.get("id"),"source_set_name":doc.get("name"),
                    "source_card_id":c.get("id"),"name":c.get("name"),"localId":c.get("localId"),
                    "image_url_base":image if isinstance(image,str) and image.startswith("https://") else None,
                    "source_endpoint":BASE+sid})
                n+=1
            status.append({"set":sid,"status":"ok","cards":n,"source_declared_images":sum(bool(x["image_url_base"]) for x in rows if x["source_set_requested"]==sid)})
        except (urllib.error.HTTPError,urllib.error.URLError,TimeoutError,ValueError,RuntimeError) as e:
            status.append({"set":sid,"status":"error","error":str(e)[:180]})
        time.sleep(0.25)
    rows.sort(key=lambda x:(x["source_set_requested"],str(x["source_card_id"])))
    payload="".join(json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(",",":"))+"\n" for x in rows).encode()
    (out/"cards.jsonl").write_bytes(payload)
    receipt={"scope":"source-only; no rights assertion; not live matched","requested_sets":a.sets,
        "source_rows":len(rows),"source_declared_image_bases":sum(bool(x["image_url_base"]) for x in rows),
        "cards_sha256":hashlib.sha256(payload).hexdigest(),"sets":status}
    (out/"receipt.json").write_text(json.dumps(receipt,ensure_ascii=False,indent=2,sort_keys=True)+"\n")
    print(json.dumps(receipt,ensure_ascii=False))
if __name__=="__main__":main()
