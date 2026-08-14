#!/usr/bin/env python3
"""CardScanR species-art ingestion pipeline.

LICENSE GATE
------------
PokeAPI / GitHub ``official-artwork`` sprites are Copyright The Pokémon Company.
The PokeAPI sprites repository CC0 covers the *collection effort*, not the
underlying Nintendo/TPC image copyrights (see LICENCE.txt in that repo and
TPC media usage guidance). CardScanR therefore MUST NOT mirror or republish
those upstream image bytes onto CardScanR CDN.

This tool generates **CardScanR-owned National Dex target placeholder artwork**
(original abstract type-motif plates keyed by Pokédex species_id), uploads them
into ``cardscanr-catalog`` R2 under ``v2/catalog/pokemon/species``, and
publishes a deterministic manifest.

Immutable policy
----------------
New generator versions publish under ``dex-v2/`` stable aliases and content-
addressed ``{sha12}/`` keys. Legacy ``{id}/display.webp`` aliases are never
overwritten with different bytes (rollback/fallback preserved).

Modes:
  --missing-only       generate/upload only species lacking local derivatives
  --verify             verify local hashes + optional remote HEAD
  --refresh-changed    regenerate when generator version changes
  --species N          force one species id
  --full-refresh       regenerate all local derivatives
  --upload / --no-upload
  --skip-existing-upload  skip R2 put when object already exists with size match

Examples:
  python tools/ingest_species_art.py --full-refresh --upload --include-content-addressed
  python tools/ingest_species_art.py --species 1 --species 4 --species 7 --upload
  python tools/ingest_species_art.py --verify
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

DATA_ROOT = Path(__file__).resolve().parents[1]
CARDSCANR_ROOT = Path(r"D:\CardScanR")
SPECIES_INDEX = CARDSCANR_ROOT / "shared" / "catalogue" / "pokemon_species_index.json"
SPECIES_TYPES = CARDSCANR_ROOT / "shared" / "catalogue" / "pokemon_species_types.json"
LOCAL_ROOT = DATA_ROOT / "data" / "species_art"
LOCAL_DERIV = LOCAL_ROOT / "derivatives"
LOCAL_ARCHIVE = LOCAL_ROOT / "archive"  # CardScanR-owned originals only
MANIFEST_DIR = CARDSCANR_ROOT / "shared" / "catalogue"
REPORT_DIR = CARDSCANR_ROOT / "artifacts" / "species_art_pipeline_20260814"

PUBLIC_BASE = os.environ.get(
    "CARDSCANR_R2_PUBLIC_BASE_URL", "https://assets.cardscanr.com"
).rstrip("/")
BUCKET = os.environ.get("CARDSCANR_R2_BUCKET", "cardscanr-catalog")
PREFIX = "v2/catalog/pokemon/species"
# Productized Dex-target plates (type motifs). Prior v1 aliases remain on CDN.
GENERATOR_VERSION = "cardscanr-species-dex-target-v2"
ASSET_VERSION = "20260814.2"
ALIAS_SEGMENT = "dex-v2"
CACHE_CONTROL_IMMUTABLE = "public, max-age=31536000, immutable"
CACHE_CONTROL_MANIFEST = "public, max-age=300"
UPSTREAM_PROVIDER = "none"
UPSTREAM_BLOCKED_PROVIDER = "pokeapi_sprites_official_artwork"
LICENSE_RESULT = "ASSET_SOURCE_LICENSE_BLOCKED"
LICENSE_NOTES = (
    "PokeAPI sprites LICENCE.txt: image contents Copyright The Pokémon Company; "
    "CC0 applies to repository collection effort only. TPC guidance asks third "
    "parties not to use Pokémon IP without authorization. CardScanR does not "
    "mirror official-artwork bytes; serves original CardScanR Dex-target plates. "
    "Type labels use local factual metadata (PokeAPI CSV CC0 data), not artwork."
)

# Abstract CardScanR type palettes — not Pokémon character art.
TYPE_PALETTE: dict[str, tuple[tuple[int, int, int], tuple[int, int, int]]] = {
    "normal": ((72, 76, 88), (150, 154, 166)),
    "fire": ((148, 52, 36), (232, 132, 54)),
    "water": ((32, 78, 148), (72, 158, 214)),
    "grass": ((34, 108, 68), (96, 176, 92)),
    "electric": ((156, 128, 28), (236, 204, 64)),
    "ice": ((72, 140, 164), (168, 220, 232)),
    "fighting": ((128, 44, 48), (196, 86, 68)),
    "poison": ((96, 52, 128), (164, 96, 186)),
    "ground": ((128, 100, 52), (196, 156, 88)),
    "flying": ((78, 112, 168), (158, 182, 214)),
    "psychic": ((156, 56, 112), (220, 112, 164)),
    "bug": ((100, 124, 36), (164, 186, 72)),
    "rock": ((108, 92, 56), (164, 144, 96)),
    "ghost": ((64, 52, 108), (118, 96, 168)),
    "dragon": ((64, 52, 132), (118, 88, 196)),
    "dark": ((42, 44, 54), (88, 90, 104)),
    "steel": ((92, 104, 116), (164, 174, 186)),
    "fairy": ((148, 88, 128), (220, 152, 184)),
}
NEUTRAL_PALETTE = ((28, 34, 44), (96, 108, 128))
BRAND_GOLD = (212, 175, 95)


@dataclass
class SpeciesRecord:
    species_id: int
    canonical_name: str
    source_provider: str
    source_url: str | None
    source_license_provenance: str
    source_etag: str | None
    source_last_modified: str | None
    source_sha256: str | None
    output_sha256: str
    mime_type: str
    width: int
    height: int
    bytes: int
    thumb_sha256: str
    thumb_bytes: int
    fetched_at: str
    asset_version: str
    generator_version: str
    cdn_display_url: str
    cdn_thumb_url: str
    cdn_display_key: str
    cdn_thumb_key: str
    status: str


def utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_species_index() -> list[dict[str, Any]]:
    if not SPECIES_INDEX.exists():
        raise SystemExit(
            f"missing species index {SPECIES_INDEX}; run export_species_index.py first"
        )
    payload = json.loads(SPECIES_INDEX.read_text(encoding="utf-8"))
    entries = payload["entries"]
    if len(entries) != 1025:
        raise SystemExit(f"expected 1025 entries, got {len(entries)}")
    return entries


_TYPES_CACHE: dict[int, list[str]] | None = None


def load_species_types() -> dict[int, list[str]]:
    global _TYPES_CACHE
    if _TYPES_CACHE is not None:
        return _TYPES_CACHE
    if not SPECIES_TYPES.exists():
        print(
            f"WARN: missing {SPECIES_TYPES}; falling back to neutral motifs",
            file=sys.stderr,
        )
        _TYPES_CACHE = {}
        return _TYPES_CACHE
    payload = json.loads(SPECIES_TYPES.read_text(encoding="utf-8"))
    out: dict[int, list[str]] = {}
    for key, value in payload.get("species", {}).items():
        types = [str(t).lower() for t in value.get("types", []) if t]
        out[int(key)] = types[:2]
    _TYPES_CACHE = out
    return out


def species_dir(species_id: int) -> Path:
    return LOCAL_DERIV / f"{species_id:04d}"


def pad_id(species_id: int) -> str:
    return f"{species_id:04d}"


def _font(size: int) -> ImageFont.ImageFont:
    for candidate in (
        r"C:\Windows\Fonts\segoeuib.ttf",
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\arial.ttf",
    ):
        path = Path(candidate)
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def _lerp(a: int, b: int, t: float) -> int:
    return int(a + (b - a) * t)


def _mix(
    c0: tuple[int, int, int], c1: tuple[int, int, int], t: float
) -> tuple[int, int, int]:
    return (_lerp(c0[0], c1[0], t), _lerp(c0[1], c1[1], t), _lerp(c0[2], c1[2], t))


def _palette_for(types: list[str]) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    if not types:
        return NEUTRAL_PALETTE
    primary = TYPE_PALETTE.get(types[0], NEUTRAL_PALETTE)
    if len(types) < 2:
        return primary
    secondary = TYPE_PALETTE.get(types[1], NEUTRAL_PALETTE)
    return (primary[0], secondary[1])


def _draw_brand_frame(draw: ImageDraw.ImageDraw, size: int) -> None:
    m = max(8, size // 32)
    # Gold corner geometry — CardScanR brand mark, not character art.
    for (x0, y0, x1, y1) in (
        (m, m, m + size // 7, m),
        (m, m, m, m + size // 7),
        (size - m - size // 7, m, size - m, m),
        (size - m, m, size - m, m + size // 7),
        (m, size - m, m + size // 7, size - m),
        (m, size - m - size // 7, m, size - m),
        (size - m - size // 7, size - m, size - m, size - m),
        (size - m, size - m - size // 7, size - m, size - m),
    ):
        draw.line([(x0, y0), (x1, y1)], fill=(*BRAND_GOLD, 200), width=max(2, size // 128))


def _draw_motif_grass(
    draw: ImageDraw.ImageDraw, cx: float, cy: float, r: float, color: tuple[int, int, int]
) -> None:
    # Botanical chevrons / leaf diamonds — abstract geometry only.
    for i, scale in enumerate((1.0, 0.72, 0.48)):
        rr = r * scale
        leaf = [
            (cx, cy - rr),
            (cx + rr * 0.55, cy - rr * 0.15),
            (cx, cy + rr * 0.35),
            (cx - rr * 0.55, cy - rr * 0.15),
        ]
        alpha = 180 - i * 35
        draw.polygon(leaf, fill=(*color, alpha))
    stem_y0 = cy + r * 0.1
    stem_y1 = cy + r * 0.85
    draw.line([(cx, stem_y0), (cx, stem_y1)], fill=(*color, 200), width=max(3, int(r // 18)))
    for dx in (-0.28, 0.28):
        draw.line(
            [(cx, stem_y0 + r * 0.15), (cx + r * dx, stem_y0 + r * 0.45)],
            fill=(*color, 170),
            width=max(2, int(r // 24)),
        )


def _draw_motif_fire(
    draw: ImageDraw.ImageDraw, cx: float, cy: float, r: float, color: tuple[int, int, int]
) -> None:
    # Angular heat wedges — not a creature silhouette.
    for i, (ox, scale) in enumerate(((-0.22, 0.9), (0.0, 1.1), (0.22, 0.85))):
        tip = cy - r * scale
        base = cy + r * 0.55
        half = r * (0.22 + 0.05 * i)
        pts = [
            (cx + ox * r, tip),
            (cx + ox * r + half, base),
            (cx + ox * r - half * 0.55, base - r * 0.12),
            (cx + ox * r - half, base),
        ]
        draw.polygon(pts, fill=(*color, 170 - i * 20))


def _draw_motif_water(
    draw: ImageDraw.ImageDraw, cx: float, cy: float, r: float, color: tuple[int, int, int]
) -> None:
    for i in range(5):
        rr = r * (0.35 + i * 0.14)
        y = cy + r * 0.05 * (i - 2)
        draw.arc(
            [cx - rr, y - rr * 0.45, cx + rr, y + rr * 0.45],
            start=200,
            end=340,
            fill=(*color, 200 - i * 20),
            width=max(2, int(r // 20)),
        )
    draw.ellipse(
        [cx - r * 0.22, cy - r * 0.22, cx + r * 0.22, cy + r * 0.22],
        outline=(*color, 210),
        width=max(2, int(r // 22)),
    )


def _draw_motif_electric(
    draw: ImageDraw.ImageDraw, cx: float, cy: float, r: float, color: tuple[int, int, int]
) -> None:
    bolt = [
        (cx + r * 0.05, cy - r * 0.95),
        (cx - r * 0.15, cy - r * 0.08),
        (cx + r * 0.08, cy - r * 0.08),
        (cx - r * 0.05, cy + r * 0.95),
        (cx + r * 0.2, cy + r * 0.12),
        (cx - r * 0.02, cy + r * 0.12),
    ]
    draw.polygon(bolt, fill=(*color, 210))
    for ang in (-35, 35, 90):
        rad = math.radians(ang)
        x1 = cx + math.cos(rad) * r * 0.35
        y1 = cy + math.sin(rad) * r * 0.35
        x2 = cx + math.cos(rad) * r * 0.95
        y2 = cy + math.sin(rad) * r * 0.95
        draw.line([(x1, y1), (x2, y2)], fill=(*color, 160), width=max(2, int(r // 28)))


def _draw_motif_psychic(
    draw: ImageDraw.ImageDraw, cx: float, cy: float, r: float, color: tuple[int, int, int]
) -> None:
    for i in range(6):
        rr = r * (0.25 + i * 0.12)
        draw.ellipse(
            [cx - rr, cy - rr, cx + rr, cy + rr],
            outline=(*color, 200 - i * 18),
            width=max(2, int(r // 26)),
        )
    draw.ellipse(
        [cx - r * 0.12, cy - r * 0.12, cx + r * 0.12, cy + r * 0.12],
        fill=(*color, 210),
    )


def _draw_motif_neutral(
    draw: ImageDraw.ImageDraw, cx: float, cy: float, r: float, color: tuple[int, int, int]
) -> None:
    # Hex + diamond brand geometry (legacy-safe abstract mark).
    hex_pts = []
    for i in range(6):
        ang = math.radians(60 * i - 30)
        hex_pts.append((cx + math.cos(ang) * r * 0.72, cy + math.sin(ang) * r * 0.72))
    draw.polygon(hex_pts, outline=(*color, 200), width=max(2, int(r // 20)))
    diamond = r * 0.34
    draw.polygon(
        [
            (cx, cy - diamond),
            (cx + diamond * 0.72, cy),
            (cx, cy + diamond),
            (cx - diamond * 0.72, cy),
        ],
        fill=(*color, 160),
        outline=(*BRAND_GOLD, 180),
    )


def _draw_motif_for_type(
    draw: ImageDraw.ImageDraw,
    primary: str | None,
    cx: float,
    cy: float,
    r: float,
    color: tuple[int, int, int],
) -> None:
    mapping = {
        "grass": _draw_motif_grass,
        "bug": _draw_motif_grass,
        "fire": _draw_motif_fire,
        "fighting": _draw_motif_fire,
        "dragon": _draw_motif_fire,
        "water": _draw_motif_water,
        "ice": _draw_motif_water,
        "electric": _draw_motif_electric,
        "steel": _draw_motif_electric,
        "psychic": _draw_motif_psychic,
        "fairy": _draw_motif_psychic,
        "ghost": _draw_motif_psychic,
        "poison": _draw_motif_psychic,
    }
    fn = mapping.get(primary or "", _draw_motif_neutral)
    fn(draw, cx, cy, r, color)


def render_placeholder(
    species_id: int,
    size: int,
    *,
    name: str | None = None,
    types: list[str] | None = None,
) -> Image.Image:
    """CardScanR Dex-target plate — abstract type motif, never character art."""
    types = [t.lower() for t in (types or [])][:2]
    low, high = _palette_for(types)
    img = Image.new("RGBA", (size, size), (10, 12, 16, 255))
    draw = ImageDraw.Draw(img)

    # Vertical type wash
    for y in range(size):
        t = y / max(1, size - 1)
        c = _mix(low, high, t * 0.85)
        draw.line([(0, y), (size, y)], fill=(*c, 255))

    # Soft vignette plate
    cx = cy = size / 2
    radius = size * 0.46
    overlay = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    for i in range(int(radius), 0, -2):
        t = i / radius
        alpha = int(120 * (1.0 - t))
        odraw.ellipse(
            [cx - i, cy - i, cx + i, cy + i],
            fill=(8, 10, 14, alpha),
        )
    img = Image.alpha_composite(img, overlay)
    draw = ImageDraw.Draw(img)

    ring = radius * 0.82
    draw.ellipse(
        [cx - ring, cy - ring, cx + ring, cy + ring],
        outline=(*BRAND_GOLD, 170),
        width=max(2, size // 110),
    )
    _draw_brand_frame(draw, size)

    motif_color = _mix(high, (240, 244, 248), 0.35)
    _draw_motif_for_type(
        draw,
        types[0] if types else None,
        cx,
        cy - size * 0.02,
        radius * 0.55,
        motif_color,
    )

    # Monogram
    display_name = (name or "SPECIES").strip() or "SPECIES"
    initial = display_name[0].upper()
    mono_font = _font(max(28, size // 5))
    mb = draw.textbbox((0, 0), initial, font=mono_font)
    mw, mh = mb[2] - mb[0], mb[3] - mb[1]
    draw.text(
        ((size - mw) / 2, cy - mh * 0.35),
        initial,
        font=mono_font,
        fill=(245, 248, 252, 40),
    )

    # Labels
    if species_id > 0:
        dex = f"#{species_id:03d}" if species_id < 1000 else f"#{species_id}"
    else:
        dex = "#---"
    title = display_name.upper()
    if types:
        type_line = " / ".join(t.upper() for t in types)
    else:
        type_line = "CARDSCANR DEX"

    dex_font = _font(max(16, size // 14))
    title_font = _font(max(14, size // 16))
    type_font = _font(max(12, size // 20))

    def _centered(text: str, font: ImageFont.ImageFont, y: float, fill: tuple) -> None:
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        draw.text(((size - tw) / 2, y), text, font=font, fill=fill)

    _centered(dex, dex_font, size * 0.08, (236, 240, 245, 235))
    _centered(title[:18], title_font, size * 0.78, (245, 248, 252, 235))
    _centered(type_line[:22], type_font, size * 0.88, (*BRAND_GOLD, 220))
    return img


def encode_webp(image: Image.Image, path: Path) -> tuple[bytes, str, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="WEBP", quality=85, method=4, lossless=False)
    data = path.read_bytes()
    return data, sha256_bytes(data), len(data)


def record_from_meta(meta: dict[str, Any]) -> SpeciesRecord:
    fields = SpeciesRecord.__dataclass_fields__
    return SpeciesRecord(**{k: meta[k] for k in fields})


def build_derivatives(species_id: int, name: str, *, force: bool) -> tuple[SpeciesRecord, bool]:
    """Returns (record, unchanged)."""
    out_dir = species_dir(species_id)
    display_path = out_dir / "display.webp"
    thumb_path = out_dir / "thumb.webp"
    meta_path = out_dir / "meta.json"
    types = load_species_types().get(species_id, [])

    if (
        not force
        and display_path.exists()
        and thumb_path.exists()
        and meta_path.exists()
    ):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("generator_version") == GENERATOR_VERSION:
            return record_from_meta(meta), True

    display = render_placeholder(species_id, 512, name=name, types=types)
    thumb = display.resize((256, 256), Image.Resampling.BILINEAR)
    _display_bytes, display_sha, display_len = encode_webp(display, display_path)
    _thumb_bytes, thumb_sha, thumb_len = encode_webp(thumb, thumb_path)

    if os.environ.get("CARDSCANR_SPECIES_ARCHIVE_PNG", "").strip() == "1":
        archive_path = LOCAL_ARCHIVE / f"{species_id:04d}.png"
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        display.save(archive_path, format="PNG")

    sha12 = display_sha[:12]
    sid = pad_id(species_id)
    display_key = f"{PREFIX}/{sid}/{sha12}/display.webp"
    thumb_key = f"{PREFIX}/{sid}/{sha12}/thumb.webp"
    # New immutable alias segment — do not overwrite legacy /display.webp bytes.
    alias_display_key = f"{PREFIX}/{sid}/{ALIAS_SEGMENT}/display.webp"
    alias_thumb_key = f"{PREFIX}/{sid}/{ALIAS_SEGMENT}/thumb.webp"

    record = SpeciesRecord(
        species_id=species_id,
        canonical_name=name,
        source_provider="cardscanr_generated_dex_target",
        source_url=None,
        source_license_provenance=(
            f"{LICENSE_RESULT}; upstream={UPSTREAM_BLOCKED_PROVIDER}; "
            f"served={GENERATOR_VERSION}; types=local_pokeapi_csv_cc0"
        ),
        source_etag=None,
        source_last_modified=None,
        source_sha256=None,
        output_sha256=display_sha,
        mime_type="image/webp",
        width=512,
        height=512,
        bytes=display_len,
        thumb_sha256=thumb_sha,
        thumb_bytes=thumb_len,
        fetched_at=utc_iso(),
        asset_version=ASSET_VERSION,
        generator_version=GENERATOR_VERSION,
        cdn_display_url=f"{PUBLIC_BASE}/{alias_display_key}",
        cdn_thumb_url=f"{PUBLIC_BASE}/{alias_thumb_key}",
        cdn_display_key=display_key,
        cdn_thumb_key=thumb_key,
        status="available",
    )
    extra = asdict(record)
    extra["alias_display_key"] = alias_display_key
    extra["alias_thumb_key"] = alias_thumb_key
    extra["content_display_key"] = display_key
    extra["content_thumb_key"] = thumb_key
    extra["types"] = types
    extra["legacy_alias_preserved"] = f"{PREFIX}/{sid}/display.webp"
    meta_path.write_text(json.dumps(extra, indent=2) + "\n", encoding="utf-8")
    return record, False


def wrangler_put(
    *,
    local_path: Path,
    object_key: str,
    content_type: str,
    cache_control: str,
    dry_run: bool,
) -> None:
    npx = "npx.cmd" if os.name == "nt" else "npx"
    cmd = [
        npx,
        "--yes",
        "wrangler",
        "r2",
        "object",
        "put",
        f"{BUCKET}/{object_key}",
        "--file",
        str(local_path),
        "--content-type",
        content_type,
        "--cache-control",
        cache_control,
        "--remote",
    ]
    if dry_run:
        print("DRY", " ".join(cmd))
        return
    completed = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"wrangler put failed for {object_key}: {completed.stderr or completed.stdout}"
        )


def http_head(url: str) -> tuple[int, dict[str, str]]:
    import urllib.request

    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; CardScanRSpeciesIngest/2.0; "
                "+https://cardscanr.com)"
            ),
            "Range": "bytes=0-0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            headers = {k.lower(): v for k, v in resp.headers.items()}
            return int(resp.status), headers
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": str(exc)}


def upload_species(
    record: SpeciesRecord,
    *,
    skip_existing: bool,
    dry_run: bool,
    aliases_only: bool = False,
) -> list[str]:
    out_dir = species_dir(record.species_id)
    meta = json.loads((out_dir / "meta.json").read_text(encoding="utf-8"))
    uploaded: list[str] = []
    pairs = [
        (
            out_dir / "display.webp",
            meta["alias_display_key"],
            CACHE_CONTROL_IMMUTABLE,
        ),
        (
            out_dir / "thumb.webp",
            meta["alias_thumb_key"],
            CACHE_CONTROL_IMMUTABLE,
        ),
    ]
    if not aliases_only:
        pairs.extend(
            [
                (
                    out_dir / "display.webp",
                    meta["content_display_key"],
                    CACHE_CONTROL_IMMUTABLE,
                ),
                (
                    out_dir / "thumb.webp",
                    meta["content_thumb_key"],
                    CACHE_CONTROL_IMMUTABLE,
                ),
            ]
        )
    for local_path, key, cache in pairs:
        if skip_existing:
            status, _headers = http_head(f"{PUBLIC_BASE}/{key}")
            if status in (200, 206):
                continue
        wrangler_put(
            local_path=local_path,
            object_key=key,
            content_type="image/webp",
            cache_control=cache,
            dry_run=dry_run,
        )
        uploaded.append(key)
    return uploaded


def build_generic_placeholder() -> Path:
    path = LOCAL_DERIV / "placeholder" / "dex-v2" / "generic.webp"
    path.parent.mkdir(parents=True, exist_ok=True)
    base = render_placeholder(0, 512, name="Species", types=[])
    base.save(path, format="WEBP", quality=90, method=6)
    return path


def write_manifest(records: list[SpeciesRecord]) -> tuple[Path, Path, str]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    species_map = {
        f"{r.species_id:04d}": {
            "species_id": r.species_id,
            "canonical_name": r.canonical_name,
            "source_provider": r.source_provider,
            "source_url": r.source_url,
            "source_license_provenance": r.source_license_provenance,
            "source_etag": r.source_etag,
            "source_last_modified": r.source_last_modified,
            "source_sha256": r.source_sha256,
            "output_sha256": r.output_sha256,
            "mime_type": r.mime_type,
            "width": r.width,
            "height": r.height,
            "bytes": r.bytes,
            "thumb_sha256": r.thumb_sha256,
            "thumb_bytes": r.thumb_bytes,
            "fetched_at": r.fetched_at,
            "asset_version": r.asset_version,
            "cdn_display_url": r.cdn_display_url,
            "cdn_thumb_url": r.cdn_thumb_url,
            "cdn_display_key": r.cdn_display_key,
            "cdn_thumb_key": r.cdn_thumb_key,
            "status": r.status,
            "types": load_species_types().get(r.species_id, []),
            "legacy_cdn_display_url": (
                f"{PUBLIC_BASE}/{PREFIX}/{r.species_id:04d}/display.webp"
            ),
        }
        for r in records
    }
    payload = {
        "schema": "cardscanr.species_art.manifest.v1",
        "generated_at": utc_iso(),
        "asset_version": ASSET_VERSION,
        "generator_version": GENERATOR_VERSION,
        "alias_segment": ALIAS_SEGMENT,
        "public_base_url": PUBLIC_BASE,
        "storage_prefix": PREFIX,
        "type_metadata_source": "pokeapi_csv_types_data_cc0",
        "license": {
            "result": LICENSE_RESULT,
            "blocked_upstream": UPSTREAM_BLOCKED_PROVIDER,
            "notes": LICENSE_NOTES,
            "served_assets": "cardscanr_generated_dex_target",
        },
        "counts": {
            "expected": 1025,
            "available": sum(1 for r in records if r.status == "available"),
            "failed": sum(1 for r in records if r.status != "available"),
        },
        "generic_placeholder_url": (
            f"{PUBLIC_BASE}/{PREFIX}/placeholder/{ALIAS_SEGMENT}/generic.webp"
        ),
        "legacy_generic_placeholder_url": (
            f"{PUBLIC_BASE}/{PREFIX}/placeholder/generic.webp"
        ),
        "species": species_map,
    }
    body = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    digest = sha256_bytes(body.encode("utf-8"))
    versioned = MANIFEST_DIR / f"pokemon_species_art_manifest.{digest[:16]}.json"
    active = MANIFEST_DIR / "pokemon_species_art_manifest.json"
    versioned.write_text(body, encoding="utf-8")
    active.write_text(body, encoding="utf-8")
    staged = REPORT_DIR / "pokemon_species_art_manifest.staged.json"
    staged.write_text(body, encoding="utf-8")
    return active, versioned, digest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--missing-only", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--refresh-changed", action="store_true")
    parser.add_argument("--full-refresh", action="store_true")
    parser.add_argument("--species", type=int, action="append", default=[])
    parser.add_argument("--upload", action="store_true", default=False)
    parser.add_argument("--no-upload", action="store_true")
    parser.add_argument(
        "--skip-existing-upload",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--aliases-only", action="store_true", default=True)
    parser.add_argument("--include-content-addressed", action="store_true")
    parser.add_argument("--upload-concurrency", type=int, default=4)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    do_upload = args.upload and not args.no_upload

    entries = load_species_index()
    if args.species:
        wanted = set(args.species)
        entries = [e for e in entries if e["species_id"] in wanted]
    if args.limit > 0:
        entries = entries[: args.limit]

    force = bool(args.full_refresh) or bool(args.refresh_changed)
    records: list[SpeciesRecord] = []
    failed: list[dict[str, Any]] = []
    skipped_unchanged = 0

    def work(entry: dict[str, Any]) -> tuple[SpeciesRecord, bool]:
        sid = int(entry["species_id"])
        name = str(entry["canonical_name"])
        return build_derivatives(sid, name, force=force)

    LOCAL_DERIV.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futures = {pool.submit(work, entry): entry for entry in entries}
        for fut in as_completed(futures):
            entry = futures[fut]
            try:
                record, unchanged = fut.result()
                records.append(record)
                if unchanged:
                    skipped_unchanged += 1
            except Exception as exc:  # noqa: BLE001
                failed.append(
                    {
                        "species_id": entry["species_id"],
                        "error": str(exc),
                        "status": "download_failed",
                    }
                )

    records.sort(key=lambda r: r.species_id)
    generic_path = build_generic_placeholder()

    uploaded_keys: list[str] = []
    if do_upload:
        aliases_only = bool(args.aliases_only) and not bool(
            args.include_content_addressed
        )
        generic_key = f"{PREFIX}/placeholder/{ALIAS_SEGMENT}/generic.webp"
        wrangler_put(
            local_path=generic_path,
            object_key=generic_key,
            content_type="image/webp",
            cache_control=CACHE_CONTROL_IMMUTABLE,
            dry_run=args.dry_run,
        )
        uploaded_keys.append(generic_key)

        def upload_one(record: SpeciesRecord) -> list[str]:
            return upload_species(
                record,
                skip_existing=args.skip_existing_upload,
                dry_run=args.dry_run,
                aliases_only=aliases_only,
            )

        with ThreadPoolExecutor(max_workers=max(1, args.upload_concurrency)) as pool:
            futures = [pool.submit(upload_one, record) for record in records]
            for fut in as_completed(futures):
                try:
                    uploaded_keys.extend(fut.result())
                except Exception as exc:  # noqa: BLE001
                    failed.append(
                        {
                            "species_id": "upload",
                            "error": str(exc),
                            "status": "upload_failed",
                        }
                    )

    # Never publish a partial species map as the active 1025 manifest.
    if len(records) != 1025:
        print(
            f"WARN: generated {len(records)} species; skipping active manifest rewrite",
            file=sys.stderr,
        )
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        sample_manifest = REPORT_DIR / "pokemon_species_art_manifest.partial.json"
        sample_manifest.write_text(
            json.dumps(
                {
                    "asset_version": ASSET_VERSION,
                    "generator_version": GENERATOR_VERSION,
                    "species": {
                        f"{r.species_id:04d}": {
                            "cdn_display_url": r.cdn_display_url,
                            "output_sha256": r.output_sha256,
                        }
                        for r in records
                    },
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        active = MANIFEST_DIR / "pokemon_species_art_manifest.json"
        versioned = active
        digest = "partial"
    else:
        active, versioned, digest = write_manifest(records)

    if do_upload and len(records) == 1025:
        wrangler_put(
            local_path=versioned,
            object_key=f"{PREFIX}/manifest.{digest[:16]}.json",
            content_type="application/json",
            cache_control=CACHE_CONTROL_IMMUTABLE,
            dry_run=args.dry_run,
        )
        wrangler_put(
            local_path=active,
            object_key=f"{PREFIX}/manifest.json",
            content_type="application/json",
            cache_control=CACHE_CONTROL_MANIFEST,
            dry_run=args.dry_run,
        )
        uploaded_keys.append(f"{PREFIX}/manifest.json")

    sample_ids = [1, 4, 7, 25, 122, 1025]
    sample_validation: dict[str, Any] = {}
    if args.verify or do_upload:
        for sid in sample_ids:
            url = f"{PUBLIC_BASE}/{PREFIX}/{sid:04d}/{ALIAS_SEGMENT}/display.webp"
            status, headers = http_head(url)
            sample_validation[str(sid)] = {
                "url": url,
                "http_status": status,
                "content_type": headers.get("content-type"),
                "content_length": headers.get("content-length"),
                "cache_control": headers.get("cache-control"),
            }

    total_bytes = sum(r.bytes + r.thumb_bytes for r in records)
    report = {
        "generated_at": utc_iso(),
        "license_provenance_result": LICENSE_RESULT,
        "license_notes": LICENSE_NOTES,
        "species_source": "cardscanr_generated_dex_target",
        "blocked_upstream": UPSTREAM_BLOCKED_PROVIDER,
        "asset_version": ASSET_VERSION,
        "generator_version": GENERATOR_VERSION,
        "type_metadata_source": "pokeapi_csv_types_data_cc0",
        "species_expected": 1025,
        "species_available": len(records),
        "species_failed": len(failed),
        "species_skipped_unchanged": skipped_unchanged,
        "objects_uploaded": len(uploaded_keys),
        "total_storage_bytes": total_bytes,
        "cardscanr_storage_prefix": PREFIX,
        "alias_segment": ALIAS_SEGMENT,
        "public_base_url": PUBLIC_BASE,
        "manifest_path": str(active),
        "manifest_versioned_path": str(versioned),
        "manifest_sha256": digest,
        "sample_cdn_validation": sample_validation,
        "failed": failed[:50],
        "upload_enabled": do_upload,
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORT_DIR / "INGEST_REPORT.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 1 if failed and do_upload else 0


if __name__ == "__main__":
    sys.exit(main())
