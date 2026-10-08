#!/usr/bin/env python3
"""Multi-source EN/JA image independence gap-fill (ingestion only; resumable/idempotent).

Resolution order for unresolved canonical cards:
  owned local/R2 → catalogue permitted URL (incl. Scrydex under written auth)
  → current TCGdex API/set re-resolution → Pokémon TCG image host by mapped set
  → Scrydex CDN by known provider card id → validate → download once → hash
  → local master → R2/CDN → CardScanR canonical URL (+ provenance)

Does not bypass PokéWallet authentication. Does not run in the production app.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
CATALOGUE = ROOT / "public" / "v1" / "catalog" / "pokemon"
MASTER = ROOT / "data" / "images" / "master"
STATE = ROOT / "data" / "images" / "independence"
REPORT = ROOT / "reports" / "image_independence"
# Mirror key reports into the app repo for operator-facing FINAL docs.
REPORT_MIRROR = Path(r"D:\CardScanR\reports\image_independence")
CDN = "https://cardscanr-images.andygore149.workers.dev"
BUCKET = "cardscanr-card-images"
WORKER = Path(r"D:\CardScanR\card_scanner_app\scripts\card_image_cloud\worker-cardscanr-images")
UA = "CardScanR-ImageIndependence-Multisource/1.1"
SCRYDEX_RIGHTS = "scrydex_written_authorization_2026-10-07"

PERMITTED_HOSTS = {
    "images.pokemontcg.io": "pokemon_tcg_api",
    "assets.tcgdex.net": "tcgdex",
    "images.scrydex.com": "scrydex",
}
BLOCKED_HOSTS = {
    "api.pokewallet.io": "pokewallet_auth_gated_no_bypass",
}

# Common EN PokéWallet setName → TCGdex / pokemontcg set id (fail-closed if wrong).
EN_SET_NAME_ALIASES: dict[str, str] = {
    "expedition": "ecard1",
    "expedition base set": "ecard1",
    "aquapolis": "ecard2",
    "skyridge": "ecard3",
    "ruby and sapphire": "ex1",
    "sandstorm": "ex2",
    "dragon": "ex3",
    "team magma vs team aqua": "ex4",
    "hidden legends": "ex5",
    "firered leafgreen": "ex6",
    "fire red leaf green": "ex6",
    "team rocket returns": "ex7",
    "deoxys": "ex8",
    "emerald": "ex9",
    "unseen forces": "ex10",
    "delta species": "ex11",
    "legend maker": "ex12",
    "holon phantoms": "ex13",
    "crystal guardians": "ex14",
    "dragon frontiers": "ex15",
    "power keepers": "ex16",
    "diamond and pearl": "dp1",
    "mysterious treasures": "dp2",
    "secret wonders": "dp3",
    "great encounters": "dp4",
    "majestic dawn": "dp5",
    "legends awakened": "dp6",
    "stormfront": "dp7",
    "platinum": "pl1",
    "rising rivals": "pl2",
    "supreme victors": "pl3",
    "arceus": "pl4",
    "heartgold soulsilver": "hgss1",
    "heartgold & soulsilver": "hgss1",
    "unleashed": "hgss2",
    "undaunted": "hgss3",
    "triumphant": "hgss4",
    "call of legends": "col1",
    "black and white": "bw1",
    "black & white": "bw1",
    "emerging powers": "bw2",
    "noble victories": "bw3",
    "next destinies": "bw4",
    "dark explorers": "bw5",
    "dragons exalted": "bw6",
    "boundaries crossed": "bw7",
    "plasma storm": "bw8",
    "plasma freeze": "bw9",
    "plasma blast": "bw10",
    "legendary treasures": "bw11",
    "xy base set": "xy1",
    "xy": "xy1",
    "flashfire": "xy2",
    "xy - flashfire": "xy2",
    "furious fists": "xy3",
    "xy - furious fists": "xy3",
    "phantom forces": "xy4",
    "xy - phantom forces": "xy4",
    "primal clash": "xy5",
    "xy - primal clash": "xy5",
    "roaring skies": "xy6",
    "xy - roaring skies": "xy6",
    "ancient origins": "xy7",
    "xy - ancient origins": "xy7",
    "breakthrough": "xy8",
    "xy - breakthrough": "xy8",
    "breakpoint": "xy9",
    "xy - breakpoint": "xy9",
    "generations": "g1",
    "fates collide": "xy10",
    "xy - fates collide": "xy10",
    "steam siege": "xy11",
    "xy - steam siege": "xy11",
    "evolutions": "xy12",
    "xy - evolutions": "xy12",
    "sun and moon": "sm1",
    "sun & moon": "sm1",
    "guardians rising": "sm2",
    "burning shadows": "sm3",
    "crimson invasion": "sm4",
    "ultra prism": "sm5",
    "forbidden light": "sm6",
    "celestial storm": "sm7",
    "lost thunder": "sm8",
    "team up": "sm9",
    "unbroken bonds": "sm10",
    "unified minds": "sm11",
    "cosmic eclipse": "sm12",
    "sword and shield": "swsh1",
    "sword & shield": "swsh1",
    "rebel clash": "swsh2",
    "darkness ablaze": "swsh3",
    "vivid voltage": "swsh4",
    "battle styles": "swsh5",
    "chilling reign": "swsh6",
    "evolving skies": "swsh7",
    "fusion strike": "swsh8",
    "brilliant stars": "swsh9",
    "astral radiance": "swsh10",
    "lost origin": "swsh11",
    "silver tempest": "swsh12",
    "scarlet and violet": "sv1",
    "scarlet & violet": "sv1",
    "paldea evolved": "sv2",
    "obsidian flames": "sv3",
    "paradox rift": "sv4",
    "temporal forces": "sv5",
    "twilight masquerade": "sv6",
    "stellar crown": "sv7",
    "surging sparks": "sv8",
    "prismatic evolutions": "sv8pt5",
    "journey together": "sv9",
    "destined rivals": "sv10",
    "base set": "base1",
    "base set 2": "base2",
    "jungle": "base3",
    "fossil": "base4",
    "team rocket": "base5",
    "gym heroes": "gym1",
    "gym challenge": "gym2",
    "neo genesis": "neo1",
    "neo discovery": "neo2",
    "neo revelation": "neo3",
    "neo destiny": "neo4",
    "legendary collection": "base6",
    "wotc promo": "basep",
    "wotc promos": "basep",
    "nintendo promos": "np",
    "diamond and pearl promos": "dpp",
    "black and white promos": "bwp",
    "xy promos": "xyp",
    "sun and moon promos": "smp",
    "sword and shield promos": "swshp",
    "scarlet and violet promos": "svp",
    "mcdonald's promos 2011": "2011bw",
    "mcdonald's collection 2011": "2011bw",
    "mcdonald's promos 2012": "2012bw",
    "mcdonald's collection 2012": "2012bw",
    "mcdonald's promos 2014": "2014xy",
    "mcdonald's collection 2014": "2014xy",
    "mcdonald's promos 2015": "2015xy",
    "mcdonald's collection 2015": "2015xy",
    "mcdonald's promos 2016": "2016xy",
    "mcdonald's collection 2016": "2016xy",
    "mcdonald's promos 2017": "2017sm",
    "mcdonald's collection 2017": "2017sm",
    "mcdonald's promos 2018": "2018sm",
    "mcdonald's collection 2018": "2018sm",
    "mcdonald's promos 2019": "2019sm",
    "mcdonald's collection 2019": "2019sm",
    "mcdonald's promos 2021": "2021swsh",
    "mcdonald's collection 2021": "2021swsh",
    "mcdonald's promos 2022": "2022swsh",
    "mcdonald's collection 2022": "2022swsh",
    "mcdonald's promos 2023": "2023sv",
    "mcdonald's collection 2023": "2023sv",
    "mcdonald's promos 2024": "2024sv",
    "mcdonald's collection 2024": "2024sv",
    "sm promos": "smp",
    "sm black star promos": "smp",
    "swsh promos": "swshp",
    "swsh black star promos": "swshp",
    "sv promos": "svp",
    "xy promos": "xyp",
    "hgss promos": "hgssp",
    "hgss black star promos": "hgssp",
    "bw promos": "bwp",
    "dp promos": "dpp",
    "me promos": "mep",
    "me: mega evolution promo": "mep",
    "generations: radiant collection": "rc",
    "radiant collection": "rc",
    "legendary treasures: radiant collection": "bw11",
    "double crisis": "dc1",
    "dragon majesty": "sm75",
    "hidden fates": "sm115",
    "shining fates": "swsh45",
    "champion's path": "swsh35",
    "pokemon go": "swsh10.5",
    "crown zenith": "swsh12.5",
    "paldean fates": "sv3pt5",
    "shrouded fable": "sv6pt5",
    "sv: scarlet & violet promo cards": "svp",
    "scarlet & violet promo cards": "svp",
    "mcdonald's 25th anniversary promos": "2021swsh",
    "mcdonalds 25th anniversary promos": "2021swsh",
    "pps1": "pps1",
    "pps2": "pps2",
    "pps3": "pps3",
    "pps4": "pps4",
    "pps5": "pps5",
    "pps6": "pps6",
    "pps7": "pps7",
}

# English PokéWallet setName → TCGdex JA set id (authoritative historical aliases).
JA_SET_NAME_ALIASES: dict[str, str] = {
    "base expansion pack": "E1",
    "the town on no map": "E2",
    "wind from the sea": "E3",
    "split earth": "E4",
    "mysterious mountains": "E5",
    "mysterious mountain": "E5",
    "adv expansion pack": "ADV1",
    "miracle of the desert": "ADV2",
    "rulers of the heavens": "ADV3",
    "magma vs aqua: two ambitions": "ADV4",
    "undone seal": "ADV5",
    "flight of legends": "PCG1",
    "clash of the blue sky": "PCG2",
    "rocket gang strikes back": "PCG3",
    "golden sky, silvery ocean": "PCG4",
    "golden sky silvery ocean": "PCG4",
    "mirage forest": "PCG5",
    "holon research tower": "PCG6",
    "holon phantom": "PCG7",
    "miracle crystal": "PCG8",
    "offense and defense of the furthest ends": "PCG9",
}

# Scrydex CDN returns this fixed JPEG for unknown card ids (HTTP 200).
SCRYDEX_MISSING_IMAGE_SHA256 = (
    "fd7c3800f9b8ebadf4b31a735f569a180e66201741b00fafa17879967884ad2c"
)

# Image CDN set ids when they differ from TCGdex API set ids.
IMAGE_SET_ID_OVERRIDES: dict[str, str] = {
    "swsh10.5": "pgo",
    "2021swsh": "mcd21",
    "2019sm": "mcd19",
    "2018sm": "mcd18",
    "2017sm": "mcd17",
    "2016xy": "mcd16",
    "2015xy": "mcd15",
    "2014xy": "mcd14",
    "2012bw": "mcd12",
    "2011bw": "mcd11",
    "2022swsh": "mcd22",
    "2023sv": "mcd23",
    "2024sv": "mcd24",
}

# Prize-pack / stamped collector prefixes → TCGdex EN set ids.
STAMP_PREFIX_TO_TCGDEX: dict[str, str] = {
    "bst": "swsh5",
    "cre": "swsh6",
    "evs": "swsh7",
    "fst": "swsh8",
    "brs": "swsh9",
    "asr": "swsh10",
    "pgo": "swsh10.5",
    "lor": "swsh11",
    "sit": "swsh12",
    "crz": "swsh12.5",
    "svi": "sv01",
    "pal": "sv02",
    "obf": "sv03",
    "mew": "sv03.5",
    "par": "sv04",
    "paf": "sv04.5",
    "tef": "sv05",
    "twm": "sv06",
    "sfa": "sv06.5",
    "scr": "sv07",
    "ssp": "sv08",
    "pre": "sv08.5",
    "jtg": "sv09",
    "dri": "sv10",
    "cel": "cel25",
    "swsh": "swshp",
    "svp": "svp",
    "sm": "smp",
}

# Same prefixes → pokemontcg.io image folder ids.
STAMP_PREFIX_TO_POKEMONTCG: dict[str, str] = {
    "bst": "bst",
    "cre": "cre",
    "evs": "evs",
    "fst": "fst",
    "brs": "brs",
    "asr": "asr",
    "pgo": "pgo",
    "lor": "lor",
    "sit": "sit",
    "crz": "crz",
    "svi": "svi",
    "pal": "pal",
    "obf": "obf",
    "mew": "mew",
    "par": "par",
    "paf": "paf",
    "tef": "tef",
    "twm": "twm",
    "sfa": "sfa",
    "scr": "scr",
    "ssp": "ssp",
    "pre": "pre",
    "cel": "cel25",
    "swsh": "swshp",
    "svp": "svp",
    "sm": "smp",
}

# Collector-number promo prefixes → image/API set ids.
PROMO_COLLECTOR_PREFIXES: dict[str, str] = {
    "svp": "svp",
    "swsh": "swshp",
    "sm": "smp",
    "xy": "xyp",
    "bw": "bwp",
    "hgss": "hgssp",
    "dp": "dpp",
    "base": "basep",
}

LOCK = threading.Lock()
HTTP_SEM = threading.Semaphore(12)
progress_path = STATE / "multisource_progress.jsonl"
_set_card_cache: dict[str, dict[str, dict]] = {}
_sets_by_lang: dict[str, list[dict]] = {}
_sets_by_id: dict[str, dict[str, dict]] = {}
_set_map_cache: dict[str, Any] = {}
# Normalized alias keys (spaces/punctuation stripped) for reliable lookup.
EN_SET_NAME_ALIASES_NORM: dict[str, str] = {
    re.sub(r"[^a-z0-9]+", "", k.lower()): v for k, v in EN_SET_NAME_ALIASES.items()
}
JA_SET_NAME_ALIASES_NORM: dict[str, str] = {
    re.sub(r"[^a-z0-9]+", "", k.lower()): v for k, v in JA_SET_NAME_ALIASES.items()
}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def host_of(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ""


def magic_ok(data: bytes) -> bool:
    if len(data) < 12:
        return False
    if data[:3] == b"\xff\xd8\xff":
        return True
    if data[:4] == b"\x89PNG":
        return True
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    return False


def norm_text(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def safe(seg: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", seg)


def master_dir(lang: str, set_id: str, card_id: str) -> Path:
    return MASTER / lang / safe(set_id) / safe(card_id)


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def http_get(url: str, *, max_retries: int = 4) -> bytes:
    last: Exception | None = None
    for i in range(max_retries):
        try:
            with HTTP_SEM:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = resp.read()
            if not data:
                raise RuntimeError("empty")
            return data
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(min(2**i, 12))
    assert last is not None
    raise last


def http_get_json(url: str) -> Any:
    return json.loads(http_get(url).decode("utf-8"))


def to_webp(data: bytes) -> bytes:
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return data
    im = Image.open(BytesIO(data))
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGBA")
    buf = BytesIO()
    im.save(buf, format="WEBP", quality=82, method=4)
    out = buf.getvalue()
    if not magic_ok(out):
        raise RuntimeError("webp_failed")
    return out


def parse_promo_collector(collector: str) -> tuple[str | None, str | None]:
    """Parse 'SVP 175' / 'SWSH 001' / 'BST 006' style collectors → (set_key, number)."""
    raw = str(collector or "").strip()
    m = re.match(r"^([A-Za-z]{2,8})\s+(\d{1,4})$", raw)
    if not m:
        return None, None
    prefix = m.group(1).lower()
    number = m.group(2)
    set_key = PROMO_COLLECTOR_PREFIXES.get(prefix, prefix)
    return set_key, number


def parse_stamp_collector(collector: str) -> tuple[str | None, str | None]:
    """Prize-pack style 'BST 006' / 'EVS 007' → underlying expansion code + number."""
    raw = str(collector or "").strip()
    m = re.match(r"^([A-Za-z]{2,8})\s+(\d{1,4}[a-zA-Z]?)$", raw)
    if not m:
        return None, None
    return m.group(1).lower(), m.group(2)


def collector_candidates(collector: str) -> list[str]:
    raw = str(collector or "").strip()
    # Normalize PokéWallet float-like collectors ("1.0" → "1")
    if re.fullmatch(r"\d+\.0+", raw):
        raw = raw.split(".", 1)[0]
    out: list[str] = []
    if raw:
        out.append(raw)
    promo_set, promo_num = parse_promo_collector(raw)
    if promo_num:
        out.append(promo_num)
    # Prefer left side of "056/128"; never treat the denominator as a localId.
    left = raw.split("/", 1)[0].strip() if raw else ""
    if left and left not in out:
        out.append(left)
    # Digit forms only from left / promo number (not from "056/128" trailing denom)
    digit_seeds: list[str] = []
    if promo_num:
        digit_seeds.append(promo_num)
    if left and left != raw:
        digit_seeds.append(left)
    elif left and "/" not in left:
        digit_seeds.append(left)
    for seed in digit_seeds:
        compact = seed.replace(" ", "")
        m = re.search(r"(\d+[a-zA-Z]?)$", compact)
        if not m:
            continue
        digits = m.group(1)
        if digits not in out:
            out.append(digits)
        num_only = re.match(r"(\d+)", digits)
        if num_only:
            stripped = num_only.group(1).lstrip("0") or "0"
            if stripped not in out:
                out.append(stripped)
            z3 = stripped.zfill(3)
            if z3 not in out:
                out.append(z3)
    # question mark special
    if raw in {"?", "？"}:
        out.extend(["question", "?", "？"])
    # dedupe preserve order
    seen: set[str] = set()
    uniq: list[str] = []
    for item in out:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        uniq.append(item)
    return uniq


def _script_kind(text: str) -> str:
    if re.search(r"[\u3040-\u30ff\u3400-\u9fff]", text or ""):
        return "cjk"
    if re.search(r"[A-Za-z]", text or ""):
        return "latin"
    return "other"


def names_compatible(catalogue_name: str | None, provider_name: str | None) -> bool:
    """Secondary identity check. Empty on either side → pass. Ambiguous mismatch → fail."""
    if not (catalogue_name or "").strip() or not (provider_name or "").strip():
        return True
    # Cross-script names cannot be compared (EN PokéWallet vs JA TCGdex). Defer to
    # set+collector uniqueness from an authoritative mapping.
    if _script_kind(catalogue_name or "") != _script_kind(provider_name or "") and {
        _script_kind(catalogue_name or ""),
        _script_kind(provider_name or ""),
    } == {"cjk", "latin"}:
        return True
    a = norm_text(catalogue_name)
    b = norm_text(provider_name)
    if not a or not b:
        return True
    # PokéWallet often appends collector digits to the name ("Alakazam 1")
    a2 = re.sub(r"\d+$", "", a)
    b2 = re.sub(r"\d+$", "", b)
    if a == b or a2 == b2:
        return True
    if a.startswith(b) or b.startswith(a) or a2.startswith(b2) or b2.startswith(a2):
        return True
    # shared substantial prefix
    if len(a2) >= 4 and len(b2) >= 4 and (a2[:4] == b2[:4]):
        return True
    return False


def image_set_id(set_id: str) -> str:
    return IMAGE_SET_ID_OVERRIDES.get(set_id, set_id)


def load_tcgdex_sets() -> None:
    global _sets_by_lang, _sets_by_id
    cache = STATE / "tcgdex_sets_cache.json"
    if cache.exists():
        payload = json.loads(cache.read_text(encoding="utf-8"))
    else:
        payload = {
            "en": http_get_json("https://api.tcgdex.net/v2/en/sets"),
            "ja": http_get_json("https://api.tcgdex.net/v2/ja/sets"),
        }
        STATE.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    _sets_by_lang = {"en": payload["en"], "ja": payload["ja"]}
    _sets_by_id = {
        "en": {s["id"]: s for s in payload["en"]},
        "ja": {s["id"]: s for s in payload["ja"]},
    }


def tcgdex_set_cards(lang: str, set_id: str) -> dict[str, dict]:
    key = f"{lang}|{set_id}"
    if key in _set_card_cache:
        return _set_card_cache[key]
    cache_dir = STATE / "tcgdex_set_cards"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"{lang}_{safe(set_id)}.json"
    if cache_file.exists():
        cards = json.loads(cache_file.read_text(encoding="utf-8"))
    else:
        api_lang = "ja" if lang == "ja" else "en"
        detail = http_get_json(f"https://api.tcgdex.net/v2/{api_lang}/sets/{urllib.parse.quote(set_id)}")
        cards = detail.get("cards") or []
        cache_file.write_text(json.dumps(cards, ensure_ascii=False), encoding="utf-8")
    by_local: dict[str, dict] = {}
    for card in cards:
        lid = str(card.get("localId") or "")
        if lid:
            by_local[lid] = card
            by_local[lid.lower()] = card
            if lid.isdigit():
                by_local[str(int(lid))] = card
                by_local[str(int(lid)).zfill(3)] = card
    _set_card_cache[key] = by_local
    return by_local


def extract_set_code(set_name: str | None) -> str | None:
    text = (set_name or "").strip()
    if not text:
        return None
    # "SV1a: Triplet Beat", "SM-P: Sun & Moon Promos", "ME: Mega Evolution Promo"
    m = re.match(r"^([A-Za-z][A-Za-z0-9.-]*?)\s*[:：]", text)
    if m:
        return m.group(1)
    m = re.match(r"^([A-Za-z]+-?[A-Za-z0-9]*\d+[A-Za-z0-9-]*)\b", text)
    if m:
        return m.group(1)
    # Bare promo-style codes used as setName/setId (XYPR, S-P-CS, svM, si100)
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9.-]{1,12}", text):
        return text
    return None


def map_set_to_tcgdex(lang: str, set_id: str, set_name: str | None, collector: str) -> tuple[str | None, str]:
    """Return (tcgdex_set_id, reason). Fail closed on ambiguity."""
    cache_key = f"{lang}|{set_id}|{set_name}|{collector}"
    if cache_key in _set_map_cache:
        return _set_map_cache[cache_key]

    index = _sets_by_id.get(lang, {})
    sets = _sets_by_lang.get(lang, [])
    n = norm_text(set_name)
    promo_set, _promo_num = parse_promo_collector(collector)

    def _remember(result: tuple[str | None, str]) -> tuple[str | None, str]:
        _set_map_cache[cache_key] = result
        return result

    # 1) catalogue set id already a TCGdex id
    if set_id in index:
        return _remember((set_id, "direct_set_id"))
    # case-insensitive set id
    for sid in index:
        if sid.lower() == str(set_id or "").lower():
            return _remember((sid, "direct_set_id_ci"))

    # 1b) Bare promo / specialty set ids used as catalogue setId (pps1, svp, …)
    sid_raw = str(set_id or "").strip()
    if sid_raw and re.fullmatch(r"[A-Za-z][A-Za-z0-9.-]{1,12}", sid_raw):
        for sid in index:
            if sid.lower() == sid_raw.lower():
                return _remember((sid, "direct_set_id_ci"))
        # Not in TCGdex — still authoritative for Scrydex/pokemontcg CDN keys
        if re.fullmatch(r"(?i)pps\d+|svp|smp|swshp|xyp|bwp|hgssp|dpp|basep|mep", sid_raw):
            return _remember((sid_raw.lower() if sid_raw.lower().startswith("pps") else sid_raw, "promo_set_id_cdn"))

    # 2) Promo collector prefix (SVP 175) — authoritative before fuzzy name matching
    if promo_set:
        if promo_set in index:
            return _remember((promo_set, "promo_collector_prefix"))
        for sid in index:
            if sid.lower() == promo_set.lower():
                return _remember((sid, "promo_collector_prefix_ci"))
        # Promo sets may exist only on image CDNs (not TCGdex). Still return key
        # so Scrydex/pokemontcg candidates can be built.
        return _remember((promo_set, "promo_collector_prefix_cdn"))

    # 3) set code from name / id
    code = extract_set_code(set_name) or extract_set_code(set_id)
    if code and code in index:
        return _remember((code, "set_code_from_name"))
    if code:
        for sid in index:
            if sid.lower() == code.lower():
                return _remember((sid, "set_code_ci"))

    # 4) Authoritative language aliases (EN names → EN/JA TCGdex ids)
    if lang == "en" and n in EN_SET_NAME_ALIASES_NORM:
        alias = EN_SET_NAME_ALIASES_NORM[n]
        if alias in index or alias in IMAGE_SET_ID_OVERRIDES or alias in IMAGE_SET_ID_OVERRIDES.values():
            return _remember((alias, "en_alias_map"))
        for sid in index:
            if sid.lower() == alias.lower():
                return _remember((sid, "en_alias_map"))
    if lang == "ja" and n in JA_SET_NAME_ALIASES_NORM:
        alias = JA_SET_NAME_ALIASES_NORM[n]
        if alias in index:
            return _remember((alias, "ja_alias_map"))
        for sid in index:
            if sid.lower() == alias.lower():
                return _remember((sid, "ja_alias_map"))
        # Authoritative alias even if not yet in local index cache
        return _remember((alias, "ja_alias_map"))

    # 5) exact set name
    exact = [s for s in sets if norm_text(s.get("name")) == n]
    if len(exact) == 1:
        return _remember((exact[0]["id"], "exact_name"))
    if len(exact) > 1:
        return _remember((None, "ambiguous_exact_name"))

    # Deny fuzzy contains for promo-named sets (avoids SVP → sv01)
    if "promo" in n:
        return _remember((None, "no_set_mapping_promo_name"))

    # 6) denominator from collector (001/165 → 165) vs official count
    denom = None
    if "/" in str(collector or ""):
        right = str(collector).split("/", 1)[1].strip()
        if right.isdigit():
            denom = int(right)

    contains = []
    if n and len(n) >= 4:
        for s in sets:
            sn = norm_text(s.get("name"))
            if not sn:
                continue
            if n in sn or sn in n:
                contains.append(s)
    if denom is not None and contains:
        filtered = [
            s
            for s in contains
            if int((s.get("cardCount") or {}).get("official") or 0) == denom
            or int((s.get("cardCount") or {}).get("total") or 0) == denom
        ]
        if len(filtered) == 1:
            return _remember((filtered[0]["id"], "contains_name_cardcount"))
        if len(filtered) > 1:
            return _remember((None, "ambiguous_contains_cardcount"))
    if len(contains) == 1:
        return _remember((contains[0]["id"], "unique_contains_name"))
    if len(contains) > 1:
        return _remember((None, "ambiguous_set_name"))

    return _remember((None, "no_set_mapping"))


@dataclass
class Candidate:
    url: str
    provider: str
    match_basis: str
    provider_card_id: str | None = None
    provider_set_id: str | None = None
    provider_name: str | None = None
    rights_basis: str | None = None


@dataclass
class CardRow:
    canonical: str
    language: str
    folder: str
    set_id: str
    set_name: str | None
    collector: str
    name: str | None
    urls: list[str] = field(default_factory=list)
    external: dict[str, Any] = field(default_factory=dict)
    providers: dict[str, Any] = field(default_factory=dict)
    image_source: str = ""

    @property
    def candidate_ids(self) -> list[str]:
        out: list[str] = []
        for v in (
            self.external.get("pokemonTcgApiId"),
            self.external.get("tcgdexCardId"),
            self.providers.get("pokemonTcgApi"),
            self.providers.get("tcgdex"),
            f"{self.set_id}-{self.collector}",
        ):
            if v and str(v) not in out:
                out.append(str(v))
        return out


def load_cards() -> list[CardRow]:
    cards: list[CardRow] = []
    for folder, lang in (("en", "en"), ("jp", "ja")):
        for path in sorted((CATALOGUE / folder / "cards").glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            raw_cards = data.get("cards") if isinstance(data, dict) else data
            if not isinstance(raw_cards, list):
                continue
            for raw in raw_cards:
                if not isinstance(raw, dict):
                    continue
                set_id = str(raw.get("setId") or path.stem)
                collector = str(raw.get("collectorNumber") or "")
                external = raw.get("externalIds") if isinstance(raw.get("externalIds"), dict) else {}
                providers = raw.get("providerIds") if isinstance(raw.get("providerIds"), dict) else {}
                urls: list[str] = []
                for k in ("imageLarge", "imageUrlLarge", "imageUrl", "imageSmall", "imageUrlSmall"):
                    v = raw.get(k)
                    if isinstance(v, str) and v.strip() and v.strip() not in urls:
                        urls.append(v.strip())
                # preserve provenance original if already rewritten
                prov = raw.get("imageProvenance") if isinstance(raw.get("imageProvenance"), dict) else {}
                for k in ("originalLargeUrl", "originalUrl", "originalSmallUrl"):
                    v = prov.get(k)
                    if isinstance(v, str) and v.strip() and v.strip() not in urls:
                        urls.append(v.strip())
                cards.append(
                    CardRow(
                        canonical=str(raw.get("canonicalBaseId") or f"pokemon|{folder}|{set_id}|{collector}"),
                        language=lang,
                        folder=folder,
                        set_id=set_id,
                        set_name=raw.get("setName"),
                        collector=collector,
                        name=raw.get("name"),
                        urls=urls,
                        external=dict(external),
                        providers=dict(providers),
                        image_source=str(raw.get("imageSource") or ""),
                    )
                )
    return cards


def covered_canonicals() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not MASTER.exists():
        return out
    for sidecar in MASTER.rglob("asset.json"):
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        cid = meta.get("canonicalBaseId")
        display = sidecar.parent / "display.webp"
        if cid and display.exists():
            out[str(cid)] = meta
    return out


def load_progress() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not progress_path.exists():
        return out
    with progress_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            cid = row.get("id")
            if cid:
                out[str(cid)] = row
    return out


def append_progress(row: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    with LOCK:
        with progress_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


_oauth_token: str | None = None


def _load_oauth_token() -> str:
    global _oauth_token
    if _oauth_token:
        return _oauth_token
    toml = Path.home() / "AppData/Roaming/xdg.config/.wrangler/config/default.toml"
    text = toml.read_text(encoding="utf-8")
    m = re.search(r'oauth_token\s*=\s*"([^"]+)"', text)
    if not m:
        raise RuntimeError("wrangler oauth_token missing")
    _oauth_token = m.group(1)
    return _oauth_token


def wrangler_put(key: str, path: Path, content_type: str) -> bool:
    """Upload via Cloudflare R2 objects API (Wrangler OAuth). Falls back to wrangler CLI."""
    account = "bf8ce806dea1ac650343311ac77c35ee"
    try:
        token = _load_oauth_token()
        data = path.read_bytes()
        url = (
            f"https://api.cloudflare.com/client/v4/accounts/{account}/r2/buckets/"
            f"{BUCKET}/objects/{urllib.parse.quote(key, safe='')}"
        )
        req = urllib.request.Request(
            url,
            data=data,
            method="PUT",
            headers={"Authorization": f"Bearer {token}", "Content-Type": content_type},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return bool(body.get("success"))
    except Exception:
        cmd = [
            "npx",
            "wrangler",
            "r2",
            "object",
            "put",
            f"{BUCKET}/{key}",
            "--file",
            str(path),
            "--content-type",
            content_type,
            "--cache-control",
            "public, max-age=31536000, immutable",
            "--remote",
        ]
        r = subprocess.run(
            cmd,
            cwd=str(WORKER),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            shell=True,
        )
        return r.returncode == 0


def scrydex_urls_for_ids(ids: list[str]) -> list[Candidate]:
    out: list[Candidate] = []
    seen: set[str] = set()
    for pid in ids:
        if not pid:
            continue
        for variant in (pid, pid.lower()):
            for quality in ("large", "small"):
                url = f"https://images.scrydex.com/pokemon/{variant}/{quality}"
                if url in seen:
                    continue
                seen.add(url)
                out.append(
                    Candidate(
                        url=url,
                        provider="scrydex",
                        match_basis="provider_card_id_scrydex_cdn",
                        provider_card_id=pid,
                        rights_basis=SCRYDEX_RIGHTS,
                    )
                )
    return out


def resolve_candidates(card: CardRow) -> tuple[list[Candidate], list[str]]:
    """Build ordered candidates + attempt log. Never includes PokéWallet fetch URLs."""
    attempts: list[str] = []
    cands: list[Candidate] = []

    # 1) catalogue URLs on permitted hosts
    for url in card.urls:
        h = host_of(url)
        if h in BLOCKED_HOSTS:
            attempts.append(f"skip:{h}:{BLOCKED_HOSTS[h]}")
            continue
        if h in PERMITTED_HOSTS:
            provider = PERMITTED_HOSTS[h]
            rights = SCRYDEX_RIGHTS if provider == "scrydex" else None
            # Prefer large over small: already ordered in urls
            cands.append(
                Candidate(
                    url=url,
                    provider=provider,
                    match_basis="catalogue_url",
                    rights_basis=rights,
                )
            )
            attempts.append(f"catalogue:{h}")

    # 2) known provider IDs → TCGdex current API
    tcgdex_id = card.external.get("tcgdexCardId") or card.providers.get("tcgdex")
    ptcg_id = card.external.get("pokemonTcgApiId") or card.providers.get("pokemonTcgApi")
    provider_ids = [str(x) for x in (tcgdex_id, ptcg_id) if x]

    if tcgdex_id:
        api_lang = "ja" if card.language == "ja" else "en"
        api = f"https://api.tcgdex.net/v2/{api_lang}/cards/{urllib.parse.quote(str(tcgdex_id))}"
        attempts.append(f"tcgdex_api:{tcgdex_id}")
        try:
            payload = http_get_json(api)
            img = payload.get("image")
            pname = payload.get("name")
            if not names_compatible(card.name, pname):
                attempts.append("tcgdex_api:name_mismatch")
            elif img:
                for quality in ("high.webp", "high.png", "low.webp"):
                    cands.append(
                        Candidate(
                            url=f"{img}/{quality}",
                            provider="tcgdex",
                            match_basis="tcgdex_api_current",
                            provider_card_id=str(tcgdex_id),
                            provider_set_id=(payload.get("set") or {}).get("id"),
                            provider_name=pname,
                        )
                    )
            else:
                attempts.append("tcgdex_api:image_null")
        except Exception as exc:  # noqa: BLE001
            attempts.append(f"tcgdex_api:fail:{type(exc).__name__}")

    # 3) set mapping + localId match via TCGdex set cards
    mapped_set, map_reason = map_set_to_tcgdex(card.language, card.set_id, card.set_name, card.collector)
    attempts.append(f"set_map:{map_reason}:{mapped_set or '-'}")
    img_set = image_set_id(mapped_set) if mapped_set else None
    authoritative = map_reason in {
        "direct_set_id",
        "direct_set_id_ci",
        "promo_collector_prefix",
        "promo_collector_prefix_ci",
        "promo_collector_prefix_cdn",
        "promo_set_id_cdn",
        "set_code_from_name",
        "set_code_ci",
        "en_alias_map",
        "ja_alias_map",
        "exact_name",
        "contains_name_cardcount",
    }
    cdn_only_map = map_reason in {"promo_collector_prefix_cdn", "promo_set_id_cdn"}
    if mapped_set and not cdn_only_map:
        try:
            by_local = tcgdex_set_cards(card.language, mapped_set)
            matches: list[dict] = []
            for cand_num in collector_candidates(card.collector):
                hit = by_local.get(cand_num) or by_local.get(cand_num.lower())
                if hit and hit not in matches:
                    matches.append(hit)
            if len(matches) > 1:
                named = [m for m in matches if names_compatible(card.name, m.get("name"))]
                if len(named) == 1:
                    matches = named
                else:
                    attempts.append("tcgdex_set:ambiguous_localId")
                    matches = []
            if len(matches) == 1:
                m = matches[0]
                if not names_compatible(card.name, m.get("name")) and not (
                    authoritative
                    and {_script_kind(card.name or ""), _script_kind(str(m.get("name") or ""))}
                    == {"cjk", "latin"}
                ):
                    attempts.append("tcgdex_set:name_mismatch_fail_closed")
                else:
                    pid = str(m.get("id") or f"{mapped_set}-{m.get('localId')}")
                    provider_ids.append(pid)
                    img = m.get("image")
                    pname = m.get("name")
                    if not img:
                        api_lang = "ja" if card.language == "ja" else "en"
                        try:
                            full = http_get_json(
                                f"https://api.tcgdex.net/v2/{api_lang}/cards/{urllib.parse.quote(pid)}"
                            )
                            img = full.get("image")
                            pname = full.get("name") or pname
                            attempts.append(f"tcgdex_card_detail:{pid}")
                        except Exception as exc:  # noqa: BLE001
                            attempts.append(f"tcgdex_card_detail_fail:{type(exc).__name__}")
                    if img:
                        for quality in ("high.webp", "high.png", "low.webp"):
                            cands.append(
                                Candidate(
                                    url=f"{img}/{quality}",
                                    provider="tcgdex",
                                    match_basis=f"tcgdex_set_map:{map_reason}",
                                    provider_card_id=pid,
                                    provider_set_id=mapped_set,
                                    provider_name=pname,
                                )
                            )
                    if card.language == "en" and img_set:
                        local = str(m.get("localId") or "")
                        for num in collector_candidates(local or card.collector):
                            if "/" in num or " " in num or num in {"?", "？"}:
                                continue
                            if not re.fullmatch(r"\d+[a-zA-Z]?", num):
                                continue
                            for suffix in (f"{num}_hires.png", f"{num}.png"):
                                cands.append(
                                    Candidate(
                                        url=f"https://images.pokemontcg.io/{img_set}/{suffix}",
                                        provider="pokemon_tcg_api",
                                        match_basis=f"pokemontcg_from_map:{map_reason}",
                                        provider_card_id=f"{img_set}-{num}",
                                        provider_set_id=img_set,
                                        provider_name=pname,
                                    )
                                )
                    attempts.append(f"tcgdex_set:matched:{pid}")
            elif not matches:
                attempts.append("tcgdex_set:no_localId_match")
        except Exception as exc:  # noqa: BLE001
            attempts.append(f"tcgdex_set:fail:{type(exc).__name__}")

    # 3b2) Stamp/prize-pack collectors ("BST 006") → underlying EN expansion images
    stamp_prefix, stamp_num = parse_stamp_collector(card.collector)
    if card.language == "en" and stamp_prefix and stamp_num:
        tcgdex_stamp = STAMP_PREFIX_TO_TCGDEX.get(stamp_prefix, stamp_prefix)
        ptcg_stamp = STAMP_PREFIX_TO_POKEMONTCG.get(stamp_prefix, stamp_prefix)
        try:
            by_local = tcgdex_set_cards("en", tcgdex_stamp)
            hit = None
            for num in collector_candidates(stamp_num):
                hit = by_local.get(num) or by_local.get(num.lower())
                if hit:
                    break
            if hit and names_compatible(card.name, hit.get("name")):
                pid = str(hit.get("id") or f"{tcgdex_stamp}-{hit.get('localId')}")
                provider_ids.append(pid)
                img = hit.get("image")
                pname = hit.get("name")
                if not img:
                    try:
                        full = http_get_json(
                            f"https://api.tcgdex.net/v2/en/cards/{urllib.parse.quote(pid)}"
                        )
                        img = full.get("image")
                        pname = full.get("name") or pname
                    except Exception as exc:  # noqa: BLE001
                        attempts.append(f"stamp_tcgdex_detail_fail:{type(exc).__name__}")
                if img:
                    for quality in ("high.webp", "high.png", "low.webp"):
                        cands.append(
                            Candidate(
                                url=f"{img}/{quality}",
                                provider="tcgdex",
                                match_basis="stamp_collector_tcgdex",
                                provider_card_id=pid,
                                provider_set_id=tcgdex_stamp,
                                provider_name=pname,
                            )
                        )
                attempts.append(f"stamp_tcgdex:matched:{pid}")
            else:
                attempts.append("stamp_tcgdex:no_match")
        except Exception as exc:  # noqa: BLE001
            attempts.append(f"stamp_tcgdex:fail:{type(exc).__name__}")
        for num in collector_candidates(stamp_num):
            if not re.fullmatch(r"\d+[a-zA-Z]?", num):
                continue
            for suffix in (f"{num}_hires.png", f"{num}.png"):
                cands.append(
                    Candidate(
                        url=f"https://images.pokemontcg.io/{ptcg_stamp}/{suffix}",
                        provider="pokemon_tcg_api",
                        match_basis="stamp_collector_pokemontcg",
                        provider_card_id=f"{ptcg_stamp}-{num}",
                        provider_set_id=ptcg_stamp,
                    )
                )
            for c in scrydex_urls_for_ids(
                [
                    f"{ptcg_stamp}-{num}",
                    f"{ptcg_stamp.upper()}-{num}",
                    f"{tcgdex_stamp}-{num}",
                    f"{stamp_prefix.upper()}-{num}",
                ]
            ):
                c.match_basis = "stamp_collector_scrydex"
                cands.append(c)
            attempts.append(f"stamp_collector:{tcgdex_stamp}/{ptcg_stamp}-{num}")
            break

    # 3b) EN image CDN from authoritative mapping even without TCGdex list hit
    if card.language == "en" and img_set and authoritative:
        digit_nums: list[str] = []
        for num in collector_candidates(card.collector):
            if "/" in num or " " in num or num in {"?", "？"}:
                continue
            if not re.fullmatch(r"\d+[a-zA-Z]?", num):
                continue
            if num not in digit_nums:
                digit_nums.append(num)
        # Prefer unpadded forms first (pokemontcg.io often uses 1.png not 001.png)
        digit_nums.sort(key=lambda n: (len(re.sub(r"\D", "", n)), n))
        for num in digit_nums[:4]:
            for suffix in (f"{num}_hires.png", f"{num}.png"):
                cands.append(
                    Candidate(
                        url=f"https://images.pokemontcg.io/{img_set}/{suffix}",
                        provider="pokemon_tcg_api",
                        match_basis=f"pokemontcg_authoritative:{map_reason}",
                        provider_card_id=f"{img_set}-{num}",
                        provider_set_id=img_set,
                    )
                )
            attempts.append(f"pokemontcg_authoritative:{img_set}-{num}")

    # 4) Scrydex CDN by known / derived provider card ids
    uniq_ids: list[str] = []
    for pid in provider_ids:
        if pid and pid not in uniq_ids:
            uniq_ids.append(pid)
    if uniq_ids:
        cands.extend(scrydex_urls_for_ids(uniq_ids))
        attempts.append(f"scrydex_cdn_ids:{','.join(uniq_ids[:5])}")

    # 5) Scrydex CDN from mapped/extracted set code + collector when unique
    scrydex_set_keys: list[tuple[str, str]] = []
    if mapped_set and authoritative:
        scrydex_set_keys.append((img_set or mapped_set, map_reason))
        if img_set and img_set != mapped_set:
            scrydex_set_keys.append((mapped_set, f"{map_reason}_api_id"))
    code = extract_set_code(card.set_name) or extract_set_code(card.set_id)
    if code and all(code.lower() != k[0].lower() for k in scrydex_set_keys):
        scrydex_set_keys.append((code, "extracted_set_code"))
    if "/" in str(card.collector or ""):
        right = str(card.collector).split("/", 1)[1].strip()
        if right and re.fullmatch(r"[A-Za-z][A-Za-z0-9.-]{0,12}", right):
            if all(right.lower() != k[0].lower() for k in scrydex_set_keys):
                scrydex_set_keys.append((right, "collector_suffix_set_code"))

    for set_key, basis in scrydex_set_keys:
        for num in collector_candidates(card.collector):
            if "/" in num:
                num = num.split("/", 1)[0].strip()
            if " " in num:
                continue
            if not num or num in {"?", "？"}:
                continue
            ids = [f"{set_key}-{num}"]
            if num.isdigit():
                ids.append(f"{set_key}-{num.zfill(3)}")
                ids.append(f"{set_key.upper()}-{num}")
                ids.append(f"{set_key.upper()}-{num.zfill(3)}")
            for c in scrydex_urls_for_ids(ids):
                c.match_basis = f"scrydex_from_code:{basis}"
                cands.append(c)
            attempts.append(f"scrydex_from_code:{set_key}-{num}:{basis}")
            break

    # de-dupe by URL preserve order
    seen: set[str] = set()
    uniq: list[Candidate] = []
    for c in cands:
        if c.url in seen:
            continue
        seen.add(c.url)
        uniq.append(c)
    return uniq, attempts


def acquire(card: CardRow, upload: bool) -> dict:
    cands, attempts = resolve_candidates(card)
    if not cands:
        # classify
        hosts = sorted({host_of(u) for u in card.urls if host_of(u)})
        if hosts == ["api.pokewallet.io"] or (not hosts and card.image_source == "pokewallet"):
            reason = "auth_only_source_no_alternate"
        elif any(a.startswith("set_map:ambiguous") or "ambiguous" in a for a in attempts):
            reason = "ambiguous_identity"
        elif any("no_set_mapping" in a for a in attempts) and "api.pokewallet.io" in hosts:
            reason = "no_known_permitted_source"
        else:
            reason = "no_known_permitted_source"
        row = {
            "status": "unresolved",
            "id": card.canonical,
            "reason": reason,
            "attempts": attempts,
            "hosts": hosts,
            "at": utc_now(),
        }
        append_progress(row)
        return row

    last_err = None
    for cand in cands:
        if cand.provider_name and not names_compatible(card.name, cand.provider_name):
            last_err = "name_mismatch"
            continue
        try:
            raw = http_get(cand.url)
            if not magic_ok(raw):
                last_err = "invalid_payload"
                continue
            raw_digest = sha256(raw)
            if raw_digest == SCRYDEX_MISSING_IMAGE_SHA256:
                last_err = "scrydex_missing_placeholder"
                attempts.append(f"reject_placeholder:{cand.url}")
                continue
            card_id = cand.provider_card_id or (card.candidate_ids[0] if card.candidate_ids else f"{card.set_id}-{card.collector}")
            dest = master_dir(card.language, card.set_id, card_id)
            ext = ".png" if raw[:4] == b"\x89PNG" else (".webp" if raw[8:12] == b"WEBP" else ".bin")
            write_atomic(dest / f"original{ext}", raw)
            webp = to_webp(raw)
            display = dest / "display.webp"
            write_atomic(display, webp)
            digest = sha256(webp)
            object_key = (
                f"cards/{card.language}/{card.set_id.lower()}/{card_id.lower()}/{digest[:12]}/display.webp"
            )
            alias = f"cards/{card.language}/{card.set_id.lower()}/{card_id.lower()}/display.webp"
            uploaded = False
            public = None
            if upload:
                uploaded = wrangler_put(object_key, display, "image/webp")
                if uploaded:
                    pointer = dest / "display.webp.current.txt"
                    pointer.write_text(object_key, encoding="utf-8")
                    wrangler_put(f"{alias}.current", pointer, "text/plain")
                    wrangler_put(alias, display, "image/webp")
                    public = f"{CDN}/{alias}"
            meta = {
                "canonicalBaseId": card.canonical,
                "cardId": card_id,
                "language": card.language,
                "setId": card.set_id,
                "setName": card.set_name,
                "collectorNumber": card.collector,
                "sha256": digest,
                "byteSize": len(webp),
                "mimeType": "image/webp",
                "sourceProvider": cand.provider,
                "originalSourceUrl": cand.url,
                "matchBasis": cand.match_basis,
                "hostedObjectKey": object_key if uploaded else None,
                "publicUrl": public,
                "acquisition": "multisource_resolver",
                "acquiredAt": utc_now(),
                "provenanceConfidence": "CONFIRMED",
                "derivativeStatus": "display_webp",
                "uploaded": uploaded,
                "hostingStatus": "hosted" if uploaded else "local_only_pending_upload",
                "rightsBasis": cand.rights_basis,
                "attempts": attempts,
            }
            write_atomic(dest / "asset.json", json.dumps(meta, indent=2, ensure_ascii=False).encode("utf-8"))
            row = {
                "status": "ok",
                "id": card.canonical,
                "provider": cand.provider,
                "match_basis": cand.match_basis,
                "url": cand.url,
                "uploaded": uploaded,
                "bytes": len(webp),
                "at": utc_now(),
            }
            append_progress(row)
            return row
        except urllib.error.HTTPError as exc:
            last_err = f"http_{exc.code}"
            continue
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
            continue

    hosts = sorted({host_of(u) for u in card.urls if host_of(u)})
    reason = "corrupt_or_unreachable_across_sources"
    if last_err and "http_404" in str(last_err):
        reason = "provider_record_missing_or_dead_after_reresolution"
    if any("ambiguous" in a for a in attempts):
        reason = "ambiguous_identity"
    row = {
        "status": "unresolved",
        "id": card.canonical,
        "reason": reason,
        "last_error": last_err,
        "attempts": attempts,
        "hosts": hosts,
        "candidates_tried": len(cands),
        "at": utc_now(),
    }
    append_progress(row)
    return row


def rebuild_reports(cards: list[CardRow], covered: dict[str, dict], progress: dict[str, dict]) -> dict:
    REPORT.mkdir(parents=True, exist_ok=True)
    master_rows = []
    for sidecar in MASTER.rglob("asset.json"):
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        display = sidecar.parent / "display.webp"
        if not display.exists():
            continue
        public = meta.get("publicUrl") or ""
        if not public and meta.get("hostedObjectKey"):
            # prefer alias URL
            lang = meta.get("language")
            set_id = meta.get("setId")
            card_id = meta.get("cardId")
            if lang and set_id and card_id:
                public = f"{CDN}/cards/{lang}/{str(set_id).lower()}/{str(card_id).lower()}/display.webp"
        master_rows.append(
            {
                "canonical_card_id": meta.get("canonicalBaseId", ""),
                "language": meta.get("language", ""),
                "set_id": meta.get("setId", ""),
                "collector_number": meta.get("collectorNumber", ""),
                "card_id": meta.get("cardId", ""),
                "local_path": str(display),
                "sha256": meta.get("sha256", ""),
                "byte_size": meta.get("byteSize", ""),
                "mime_type": meta.get("mimeType", ""),
                "source_provider": meta.get("sourceProvider", ""),
                "original_source_url": meta.get("originalSourceUrl", ""),
                "hosted_object_key": meta.get("hostedObjectKey", ""),
                "public_url": public,
                "provenance_confidence": meta.get("provenanceConfidence", ""),
                "derivative_status": meta.get("derivativeStatus", ""),
                "acquisition": meta.get("acquisition", ""),
                "acquired_at": meta.get("acquiredAt", ""),
                "match_basis": meta.get("matchBasis", ""),
                "rights_basis": meta.get("rightsBasis", ""),
            }
        )
    by_canon = {r["canonical_card_id"]: r for r in master_rows if r["canonical_card_id"]}

    unresolved = []
    for card in cards:
        if card.canonical in by_canon:
            continue
        prog = progress.get(card.canonical) or {}
        reason = prog.get("reason") or "not_yet_resolved"
        attempts = prog.get("attempts") or []
        last_err = str(prog.get("last_error") or "")
        hosts = prog.get("hosts") or sorted({host_of(u) for u in card.urls if host_of(u)})
        attempt_blob = " ".join(str(a) for a in attempts)
        if "reject_placeholder" in attempt_blob or last_err == "scrydex_missing_placeholder":
            reason = "no_permitted_image_after_reresolution"
        elif last_err.startswith("http_") or "corrupt" in reason:
            reason = "corrupt_or_unreachable_across_sources"
        elif reason == "auth_only_source_no_alternate" and any(
            a.startswith("set_map:ja_alias_map")
            or a.startswith("set_map:en_alias_map")
            or a.startswith("stamp_")
            or a.startswith("scrydex_")
            or a.startswith("tcgdex_")
            for a in attempts
        ):
            # Alternates were attempted; still no usable image.
            reason = "no_permitted_image_after_reresolution"
        next_path = "manual_or_new_permitted_source"
        if reason == "auth_only_source_no_alternate":
            next_path = "obtain_pokewallet_written_rehost_then_auth_import"
        elif reason == "ambiguous_identity":
            next_path = "resolve_identity_with_printed_number_variant_evidence"
        elif "pokewallet" in ",".join(hosts):
            next_path = "collector_scan_or_pokewallet_written_rehost"
        unresolved.append(
            {
                "canonical_card_id": card.canonical,
                "language": card.language,
                "set_id": card.set_id,
                "collector_number": card.collector,
                "attempted_sources": ",".join(attempts[:20]) if attempts else ",".join(hosts),
                "failure_reason": reason,
                "category": reason,
                "next_recovery_path": next_path,
            }
        )

    fields = list(master_rows[0].keys()) if master_rows else []
    with (REPORT / "en_jp_image_master_manifest.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(master_rows)
    ufields = [
        "canonical_card_id",
        "language",
        "set_id",
        "collector_number",
        "attempted_sources",
        "failure_reason",
        "category",
        "next_recovery_path",
    ]
    with (REPORT / "en_jp_unresolved_images.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=ufields)
        w.writeheader()
        w.writerows(unresolved)

    def count_lang(lang: str) -> dict:
        total = sum(1 for c in cards if c.language == lang)
        local = sum(1 for c in cards if c.language == lang and c.canonical in by_canon)
        hosted = sum(
            1
            for c in cards
            if c.language == lang and c.canonical in by_canon and by_canon[c.canonical].get("public_url")
        )
        unres = sum(1 for u in unresolved if u["language"] == lang)
        return {"total": total, "localMaster": local, "hosted": hosted, "unresolved": unres}

    cats: dict[str, int] = {}
    for u in unresolved:
        cats[u["category"]] = cats.get(u["category"], 0) + 1

    # rescue stats from progress / master acquisition
    rescues = {
        "from_previous_pokewallet_only": 0,
        "from_tcgdex_reresolution": 0,
        "from_scrydex_written_authorization": 0,
        "from_other_sources": 0,
    }
    for meta in by_canon.values():
        acq = meta.get("acquisition") or ""
        if acq != "multisource_resolver":
            continue
        provider = meta.get("sourceProvider") or ""
        basis = meta.get("match_basis") or meta.get("matchBasis") or ""
        # approximate prior classification via URL host in original - use match basis
        if "pokewallet" in json.dumps(meta).lower() or "set_map" in basis or "from_map" in basis:
            # if originally PW-only path used alternate
            if "set_map" in basis or "from_map" in basis or "pokemontcg_from_map" in basis:
                rescues["from_previous_pokewallet_only"] += 1
        if "tcgdex" in provider and ("reresol" in basis or "api_current" in basis or "set_map" in basis):
            rescues["from_tcgdex_reresolution"] += 1
        if provider == "scrydex":
            rescues["from_scrydex_written_authorization"] += 1
        if provider not in {"scrydex", "tcgdex"} and "set_map" not in basis:
            rescues["from_other_sources"] += 1

    summary = {
        "generatedAtUtc": utc_now(),
        "en": count_lang("en"),
        "ja": count_lang("ja"),
        "masterRoot": str(MASTER.resolve()),
        "cdnBase": CDN,
        "unresolvedCategoryCounts": cats,
        "masterRows": len(master_rows),
        "rescues": rescues,
        "independentPct": round(
            100.0
            * (count_lang("en")["hosted"] + count_lang("ja")["hosted"])
            / max(1, count_lang("en")["total"] + count_lang("ja")["total"]),
            2,
        ),
    }
    (REPORT / "image_independence_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (STATE / "set_map_cache.json").write_text(json.dumps(_set_map_cache, indent=2), encoding="utf-8")
    # Mirror operator-facing artifacts into the app repo reports tree.
    try:
        REPORT_MIRROR.mkdir(parents=True, exist_ok=True)
        for name in (
            "en_jp_unresolved_images.csv",
            "en_jp_image_master_manifest.csv",
            "image_independence_summary.json",
        ):
            src = REPORT / name
            if src.exists():
                (REPORT_MIRROR / name).write_bytes(src.read_bytes())
    except OSError:
        pass
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--upload", action="store_true")
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--only-prior-category", choices=["scrydex", "dead", "pokewallet", "all"], default="all")
    parser.add_argument("--skip-hosted", action="store_true", default=True)
    args = parser.parse_args()

    STATE.mkdir(parents=True, exist_ok=True)
    load_tcgdex_sets()
    cards = load_cards()
    covered = covered_canonicals()
    progress = load_progress()

    if args.report_only:
        summary = rebuild_reports(cards, covered, progress)
        print(json.dumps(summary, indent=2), flush=True)
        return 0

    # prior unresolved CSV for filtering
    prior_cats: dict[str, str] = {}
    unresolved_csv = REPORT / "en_jp_unresolved_images.csv"
    if unresolved_csv.exists():
        with unresolved_csv.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                prior_cats[row["canonical_card_id"]] = row.get("category") or ""

    todo: list[CardRow] = []
    for card in cards:
        cat = prior_cats.get(card.canonical, "")
        if args.only_prior_category == "scrydex" and "scrydex" not in cat:
            if not any(host_of(u) == "images.scrydex.com" for u in card.urls):
                continue
        elif args.only_prior_category == "dead" and "broken_source" not in cat and "dead" not in cat:
            continue
        elif args.only_prior_category == "pokewallet" and "pokewallet" not in cat:
            if not any(host_of(u) == "api.pokewallet.io" for u in card.urls):
                continue

        meta = covered.get(card.canonical)
        if meta and (meta.get("publicUrl") or meta.get("hostedObjectKey")):
            continue
        if meta and not args.upload:
            continue
        prev = progress.get(card.canonical)
        if prev and prev.get("status") == "ok" and meta and not args.upload:
            continue
        if meta is None or (args.upload and meta and not (meta.get("publicUrl") or meta.get("hostedObjectKey"))):
            todo.append(card)

    if args.limit:
        todo = todo[: args.limit]

    print(
        json.dumps(
            {
                "todo": len(todo),
                "covered": len(covered),
                "progress_rows": len(progress),
                "category_filter": args.only_prior_category,
            }
        ),
        flush=True,
    )

    stats = {"ok": 0, "unresolved": 0, "uploaded": 0, "bytes": 0, "by_provider": {}}
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futs = {pool.submit(acquire, c, args.upload): c for c in todo}
        done = 0
        for fut in as_completed(futs):
            res = fut.result()
            done += 1
            if res.get("status") == "ok":
                stats["ok"] += 1
                stats["bytes"] += int(res.get("bytes") or 0)
                if res.get("uploaded"):
                    stats["uploaded"] += 1
                prov = res.get("provider") or "?"
                stats["by_provider"][prov] = stats["by_provider"].get(prov, 0) + 1
            else:
                stats["unresolved"] += 1
            if done % 25 == 0 or done == len(todo):
                print(f"progress {done}/{len(todo)} {stats}", flush=True)

    # refresh progress + covered for reports
    progress = load_progress()
    covered = covered_canonicals()
    summary = rebuild_reports(cards, covered, progress)
    (STATE / "multisource_stats.json").write_text(
        json.dumps({"stats": stats, "summary": summary}, indent=2), encoding="utf-8"
    )
    print("final_stats", json.dumps(stats), flush=True)
    print("summary", json.dumps(summary, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
