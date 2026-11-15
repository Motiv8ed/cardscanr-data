#!/usr/bin/env python3
"""Create genuine CardScanR social images. Source screenshots are never modified."""
from __future__ import annotations
from pathlib import Path
from urllib.request import Request, urlopen
import hashlib, json, textwrap
from PIL import Image, ImageDraw, ImageFont, ImageOps
from io import BytesIO

ROOT=Path(__file__).resolve().parents[1]
ASSETS=ROOT/"social"/"2026-11-15"
OUTPUT=ASSETS/"derived"
GOLD="#d6a84b"
BG="#07090d"
PANEL="#161d27"
WHITE="#f4f7fb"
MUTED="#9aa8b8"
MARK_URL="https://cardscanr.com/assets/images/cardscanr_mark.png"
ITEMS={
  "home-hero":("Your collection, organised","Start collecting with CardScanR"),
  "collection-feature":("See what you own","Cards, collections and binder progress"),
  "binder-feature":("Organise your binders","Keep empty pockets and set progress visible"),
  "missing-feature":("See what's missing","Plan your next cards in one place"),
  "scan-feature":("Scan on Android","Identify your cards and confirm each match"),
  "sharing-feature":("Share when you choose","Unlisted links. Read-only collection views"),
  "android-web-feature":("Two ways to collect","Scan on Android. Organise on the web"),
}
def font(size:int,bold:bool=False):
    paths=(["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf","/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"] if bold else ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf","/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"])
    for p in paths:
        if Path(p).exists():return ImageFont.truetype(p,size)
    raise RuntimeError("Missing a standard font")
def load_mark():
    req=Request(MARK_URL,headers={"User-Agent":"CardScanR/1.0 SocialAssetBuilder"})
    with urlopen(req,timeout=15) as r:
        if r.geturl().split("/")[2].lower()!="cardscanr.com":raise RuntimeError("Unexpected logo redirect")
        content=r.read(1_000_001)
    if len(content)>1_000_000 or not content.startswith(b"\\x89PNG\\r\\n\\x1a\\n"):
        raise RuntimeError("Official CardScanR logo PNG unavailable")
    return Image.open(BytesIO(content)).convert("RGBA")
def rtext(draw,txt,x,y,max_width,size,color=WHITE,bold=False,space=1.19):
    f=font(size,bold)
    words=txt.split();lines=[];line=""
    for w in words:
        trial=(line+" "+w).strip()
        if line and draw.textbbox((0,0),trial,font=f)[2]>max_width:
            lines.append(line);line=w
        else:line=trial
    if line:lines.append(line)
    line_h=int(size*space)
    for i,line in enumerate(lines):draw.text((x,y+i*line_h),line,font=f,fill=color,stroke_width=0)
    return y+len(lines)*line_h
def frame(name,src,mark,fmt):
    is_story=(fmt=="story")
    W,H=(1080,1920) if is_story else (1080,1350)
    im=Image.new("RGB",(W,H),BG)
    d=ImageDraw.Draw(im)
    left=66;right=W-left
    d.rounded_rectangle((44,38,W-44,H-42),radius=32,outline="#263341",width=2)
    mark_size=78 if is_story else 65
    mk=ImageOps.contain(mark,(mark_size,mark_size),Image.Resampling.LANCZOS)
    im.paste(mk,(left,76 if is_story else 59),mk)
    brand_y=95 if is_story else 76
    d.text((left+mark_size+19,brand_y),"CardScan",font=font(41 if is_story else 39,True),fill=WHITE)
    tx=left+mark_size+19+d.textbbox((0,0),"CardScan",font=font(41 if is_story else 39,True))[2]
    d.text((tx,brand_y),"R",font=font(41 if is_story else 39,True),fill=GOLD)
    y=200 if is_story else 161
    d.line((left,y,right,y),fill=GOLD,width=3)
    heading,subheading=ITEMS[name]
    y=rtext(d,heading,left,y+56,right-left,76 if is_story else 67,WHITE,True,1.12)
    y=rtext(d,subheading,left,y+28,right-left,32 if is_story else 29,MUTED,False,1.3)
    # A pixel-faithful source screenshot, only resized; no overlays, fake controls or altered UI.
    shot_top=max(y+64,575 if is_story else 430)
    maxshot_h=780 if is_story else 635
    dims=ImageOps.contain(src,(W-132,maxshot_h),Image.Resampling.LANCZOS)
    shot_x=(W-dims.width)//2
    d.rounded_rectangle((shot_x-11,shot_top-11,shot_x+dims.width+11,shot_top+dims.height+11),radius=17,fill=PANEL,outline="#344357",width=3)
    im.paste(dims,(shot_x,shot_top))
    footer_y=max(shot_top+dims.height+87,1430 if is_story else 1112)
    d.rounded_rectangle((left,footer_y,right,footer_y+108 if is_story else footer_y+86),radius=46,fill=GOLD)
    d.text((left+46,footer_y+(30 if is_story else 19)),"Explore CardScanR",font=font(39 if is_story else 35,True),fill=BG)
    line_y=footer_y+(171 if is_story else 143)
    d.text((left,line_y),"cardscanr.com",font=font(34 if is_story else 30,True),fill=WHITE)
    d.text((left,line_y+63),"SCAN.  TRACK.  COLLECT.",font=font(19,False),fill=GOLD)
    return im
def main():
    mark=load_mark()
    OUTPUT.mkdir(parents=True,exist_ok=True)
    manifest={"source":"Fresh screenshots of live CardScanR pages, original PNGs in sibling directory","official_logo":MARK_URL,"rules":"Source UI is not altered; no AI or mockup." ,"items":[]}
    for slug in ITEMS:
        source=ASSETS/("live-"+slug+".png")
        if not source.is_file():raise FileNotFoundError(str(source))
        data=source.read_bytes()
        with Image.open(BytesIO(data)) as ii:src=ii.convert("RGB")
        if src.size!=(1280,800):raise ValueError(f"Unexpected screenshot size {slug}: {src.size}")
        item={"source_file":str(source.relative_to(ROOT)),"source_sha256":hashlib.sha256(data).hexdigest(),"outputs":{}}
        for fmt in ("story","feed"):
            file=OUTPUT/(fmt+"-"+slug+".png")
            img=frame(slug,src,mark,fmt)
            img.save(file,format="PNG",optimize=True)
            item["outputs"][fmt]=str(file.relative_to(ROOT))
        manifest["items"].append(item)
    (OUTPUT/"manifest.json").write_text(json.dumps(manifest,indent=2)+"\\n",encoding="utf8")
    print("Generated "+str(len(ITEMS)*2)+" first-party branded social PNGs")
if __name__=="__main__":main()
