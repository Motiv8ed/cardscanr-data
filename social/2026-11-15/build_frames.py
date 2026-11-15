#!/usr/bin/env python3
"""Frame genuine public CardScanR captures for social. No generated or edited UI."""
from __future__ import annotations

import io
from pathlib import Path
from urllib.request import Request, urlopen
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "framed"
BG = "#07090d"
PANEL = "#10151c"
GOLD = "#d6a84b"
WHITE = "#f4f7fb"
MUTED = "#9aa8b8"

FEATURES = {
    "live-home-hero": ("Your collection. Organised.", "Browse, collect and build digital binders.", "Explore the free beta"),
    "live-scan-feature": ("Scan and identify cards.", "Real Android scanner shown on our live website.", "Discover CardScanR"),
    "live-collection-feature": ("Know what you own.", "Cards, quantities and collection progress together.", "Explore collections"),
    "live-binder-feature": ("Build better binders.", "Plan pages and keep missing pockets visible.", "Explore digital binders"),
    "live-missing-feature": ("See what's missing.", "Track set and binder progress as you collect.", "Explore CardScanR"),
    "live-android-web-feature": ("Android + Website Beta.", "Scan on Android. Organise on the website.", "Discover both"),
    "live-sharing-feature": ("Share on your terms.", "Unlisted read-only links, with private details hidden.", "Explore sharing"),
}

def font(size: int, bold: bool = False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for candidate in (
        Path("/usr/share/fonts/truetype/dejavu") / name,
        Path("/usr/share/fonts/truetype/liberation2") / ("LiberationSans-Bold.ttf" if bold else "LiberationSans-Regular.ttf"),
    ):
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size)
    return ImageFont.load_default()

def official_mark() -> Image.Image:
    url = "https://cardscanr.com/assets/images/cardscanr_mark.png"
    request = Request(url, headers={"User-Agent": "CardScanR-Social-Asset-Builder/1.0"})
    with urlopen(request, timeout=20) as res:
        if res.geturl() != url:
            raise RuntimeError("Unapproved brand mark redirect")
        data = res.read(400_001)
    if len(data) > 400_000 or not data.startswith(bytes.fromhex("89504e470d0a1a0a")):
        raise RuntimeError("Official live gold mark unavailable: no replacement logo permitted")
    image = Image.open(io.BytesIO(data)).convert("RGBA")
    if image.width < 100 or image.height < 100:
        raise RuntimeError("Brand mark too small")
    return image

def wrap(draw, text: str, f, max_width: int) -> list[str]:
    lines, current = [], ""
    for word in text.split():
        candidate = (current + " " + word).strip()
        if current and draw.textbbox((0,0), candidate, font=f)[2] > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines

def render(stem: str, words: tuple[str,str,str], mark: Image.Image, mode: str):
    width, height = (1080,1350) if mode == "feed" else (1080,1920)
    img = Image.new("RGB", (width,height), BG)
    dr = ImageDraw.Draw(img)
    dr.rounded_rectangle((24,24,width-24,height-24),radius=36,outline="#26313c",width=2)
    dr.line((76,170,1004,170),fill=GOLD,width=4)
    symbol=mark.copy()
    symbol.thumbnail((88,88),Image.Resampling.LANCZOS)
    img.paste(symbol,(75+(88-symbol.width)//2,63+(88-symbol.height)//2),symbol)
    dr.text((186,80),"CardScanR",font=font(59,True),fill=WHITE)
    dr.text((82,210),"LIVE WEBSITE CAPTURE",font=font(29,True),fill=GOLD)
    headline, subtitle, cta = words
    big=font(64 if mode=="feed" else 72,True)
    y=268 if mode=="feed" else 300
    for line in wrap(dr,headline,big,920):
        dr.text((80,y),line,font=big,fill=WHITE)
        y += 84
    sub=font(29 if mode=="feed" else 33)
    for line in wrap(dr,subtitle,sub,920):
        dr.text((80,y+8),line,font=sub,fill=MUTED)
        y += 43
    shot=Image.open(ROOT/(stem+".png")).convert("RGB")
    if shot.size != (1280,800):
        raise RuntimeError(f"{stem}: unexpected public browser screenshot size {shot.size}")
    # Preserve every source pixel's meaning; only uniform scaling, no UI rewriting.
    shot=shot.resize((928,580),Image.Resampling.LANCZOS)
    top=480 if mode=="feed" else 710
    dr.rounded_rectangle((59,top-17,1021,top+597),radius=24,fill=PANEL,outline=GOLD,width=3)
    img.paste(shot,(76,top))
    dr.text((78,top+611),"UNEDITED PRODUCT SHOWCASE  ·  15 NOV 2026",font=font(22),fill=MUTED)
    button_y=1150 if mode=="feed" else 1580
    dr.rounded_rectangle((76,button_y,1004,button_y+93),radius=42,fill=GOLD)
    cta_font=font(43,True)
    text_w=dr.textbbox((0,0),cta,font=cta_font)[2]
    dr.text(((1080-text_w)//2,button_y+19),cta,font=cta_font,fill=BG)
    foot_y=1271 if mode=="feed" else 1800
    dr.text((80,foot_y),"cardscanr.com",font=font(30,True),fill=WHITE)
    dr.text((642,foot_y+4),"SCAN. TRACK. COLLECT.",font=font(19,True),fill=GOLD)
    OUT.mkdir(parents=True,exist_ok=True)
    dest=OUT/(stem+"_"+mode+".png")
    img.save(dest,optimize=True)
    print(dest.relative_to(ROOT),dest.stat().st_size)

def main():
    mark=official_mark()
    for stem,words in FEATURES.items():
        if not (ROOT/(stem+".png")).exists():
            raise RuntimeError(f"Missing original public Bridge capture: {stem}")
        for mode in ("feed","story"):
            render(stem,words,mark,mode)

if __name__=="__main__":
    main()
