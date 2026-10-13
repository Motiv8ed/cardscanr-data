#!/usr/bin/env python3
"""Audit KO / zh-Hans missing artwork gaps and lawful next sources.

Metadata / documentation only. Does NOT download from restricted hosts
(official Korea, official Asia, unlicensed mirrors).
"""

from __future__ import annotations

import json
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "reports" / "global_image_acquisition"
DEFAULT_LIVE = Path(r"D:\CardScanR_Data\cardscanr-data")
UA = "CardScanR-GapAudit/1.0 (metadata-only; no restricted downloads)"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def http_json(url: str) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.loads(resp.read().decode("utf-8"))


def catalogue_path(live: Path) -> Path | None:
    candidates = [
        live / "data" / "global" / "catalogue" / "cards.jsonl",
        live / "data" / "cards.jsonl",
    ]
    return next((p for p in candidates if p.exists()), None)


def has_image_candidate(row: dict) -> bool:
    for key in ("normalizedDisplayUrl", "normalizedThumbnailUrl", "originalDocumentedImageUrl"):
        url = row.get(key)
        if isinstance(url, str) and url.startswith("http"):
            return True
    # provider image maps sometimes nest under imageProvenance
    prov = row.get("imageProvenance")
    if isinstance(prov, dict):
        for key in ("displayUrl", "thumbnailUrl", "sourceUrl"):
            url = prov.get(key)
            if isinstance(url, str) and url.startswith("http"):
                return True
    return False


def summarize_stream(path: Path, languages: set[str]) -> dict:
    """Stream catalogue; match by language field or canonicalPrintingId prefix."""
    matched = 0
    with_url = 0
    hosts: Counter = Counter()
    lang_hits: Counter = Counter()
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            lang = str(row.get("language") or "")
            cid = str(row.get("canonicalPrintingId") or "")
            lang_cf = lang.casefold()
            prefix_hit = any(cid.startswith(f"pokemon|{p}|") for p in languages)
            if lang_cf not in languages and not prefix_hit:
                continue
            matched += 1
            lang_hits[lang or "?"] += 1
            if has_image_candidate(row):
                with_url += 1
                for key in (
                    "normalizedDisplayUrl",
                    "normalizedThumbnailUrl",
                    "originalDocumentedImageUrl",
                ):
                    url = row.get(key)
                    if isinstance(url, str) and url.startswith("http"):
                        hosts[url.split("/")[2].casefold()] += 1
                        break
    return {
        "printings": matched,
        "withAnyImageUrl": with_url,
        "withoutImageUrl": matched - with_url,
        "urlHosts": dict(hosts),
        "languageFieldValues": dict(lang_hits),
        "cataloguePath": str(path),
    }


def probe_tcgdex_lang(lang: str) -> dict:
    """Metadata probe only — counts cards with an image field, does not fetch binaries."""
    try:
        sets = http_json(f"https://api.tcgdex.net/v2/{lang}/sets")
    except Exception as exc:
        prior_key = "ko" if lang.startswith("ko") else "zh-cn"
        prior_path = REPORT / "ko_zhcn_tcgdex_card_presence.json"
        if prior_path.exists():
            prior = json.loads(prior_path.read_text(encoding="utf-8"))
            cached = dict(prior.get(prior_key) or {})
            cached["probeError"] = str(exc)[:200]
            cached["usedPriorProbe"] = True
            return cached
        return {
            "setsListed": 0,
            "nonemptySets": 0,
            "cardTotalSampled": 0,
            "imageCards": 0,
            "samples": [],
            "probeError": str(exc)[:200],
        }
    assert isinstance(sets, list)
    nonempty = 0
    image_cards = 0
    card_total = 0
    samples = []
    for s in sets:
        sid = s.get("id") if isinstance(s, dict) else None
        if not sid:
            continue
        try:
            detail = http_json(f"https://api.tcgdex.net/v2/{lang}/sets/{sid}")
        except Exception as exc:
            samples.append({"set": sid, "error": str(exc)[:120]})
            continue
        cards = detail.get("cards") if isinstance(detail, dict) else None
        if not isinstance(cards, list) or not cards:
            continue
        nonempty += 1
        card_total += len(cards)
        imgs = sum(1 for c in cards if isinstance(c, dict) and c.get("image"))
        image_cards += imgs
        if len(samples) < 5:
            samples.append({"set": sid, "cards": len(cards), "images": imgs})
    return {
        "setsListed": len(sets),
        "nonemptySets": nonempty,
        "cardTotalSampled": card_total,
        "imageCards": image_cards,
        "samples": samples,
    }


def main() -> int:
    live = Path(__import__("os").environ.get("CARDSCANR_DATA_LIVE_ROOT", str(DEFAULT_LIVE)))
    path = catalogue_path(live)
    if path is None:
        ko_summary = {"printings": 0, "error": "catalogue_missing"}
        zh_summary = {"printings": 0, "error": "catalogue_missing"}
        by_lang_totals = {}
    else:
        ko_summary = summarize_stream(path, {"ko", "ko-kr"})
        zh_summary = summarize_stream(path, {"zh-hans", "zh-cn", "zh-sg"})
        by_lang_totals = {
            "ko": ko_summary["printings"],
            "zh-Hans": zh_summary["printings"],
        }

    # Cross-check direct_images candidates (URL presence is usually here, not on cards.jsonl).
    direct = live / "data" / "global" / "catalogue" / "direct_images.jsonl"
    direct_by_lang: Counter = Counter()
    if direct.exists():
        with direct.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                lang = str(row.get("language") or "")
                if lang.casefold() in {"ko", "ko-kr", "zh-hans", "zh-cn", "zh-sg"}:
                    direct_by_lang[lang] += 1

    tcgdex_ko = probe_tcgdex_lang("ko")
    tcgdex_zh = probe_tcgdex_lang("zh-cn")

    lawful_sources = [
        {
            "source": "TCGdex metadata API (ko / zh-cn)",
            "yield": "Set/card metadata only; imageCards currently 0",
            "lawfulAction": "Continue metadata sync; do not invent artwork URLs",
            "downloadAllowed": False,
            "basis": "No image field on live cards; MIT covers DB not artwork",
        },
        {
            "source": "pokemoncard.co.kr (Pokémon Korea official)",
            "yield": "Likely dense KO checklist + official thumbs",
            "lawfulAction": "Seek prior written agreement before any reuse",
            "downloadAllowed": False,
            "basis": "Official ToS require prior agreement; discovery-only",
        },
        {
            "source": "Asia Pokémon Card Game Trainers Website / CN official channels",
            "yield": "zh-Hans checklists; possible official gallery pages",
            "lawfulAction": "Permission request; no bulk scrape",
            "downloadAllowed": False,
            "basis": "Official terms typically prohibit copying Data/images",
        },
        {
            "source": "Scrydex / commercial catalog APIs",
            "yield": "May add KO/CN if licensed in CardScanR plan",
            "lawfulAction": "Confirm language coverage + written CDN grant (EN/JA already)",
            "downloadAllowed": "pending_contract_scope",
            "basis": "Existing written auth is EN/JA CDN; do not assume KO/CN",
        },
        {
            "source": "Collector-contributed photos (CardScanR intake)",
            "yield": "Long-tail KO/zh-Hans printings with contributor licence",
            "lawfulAction": "Design intake + licence terms after promotion policy",
            "downloadAllowed": "after_contributor_licence",
            "basis": "Contributor grant separate from Pokémon IP; no public serve until rights clear",
        },
        {
            "source": "Community GitHub / Discord image dumps",
            "yield": "Often complete but provenance unclear",
            "lawfulAction": "Do not download; prefer origin hosts with permission",
            "downloadAllowed": False,
            "basis": "Usually no better rights than scraped origins",
        },
        {
            "source": "PokéWallet authenticated media",
            "yield": "Possible multilingual assets if licensed",
            "lawfulAction": "Clarification on bulk rehost; no spend this run",
            "downloadAllowed": False,
            "basis": "Discovery-only in staging allowlist",
        },
    ]

    payload = {
        "generatedAtUtc": utc_now(),
        "catalogueLanguageTotals": by_lang_totals,
        "catalogueGap": {
            "ko": ko_summary,
            "zh-Hans": zh_summary,
        },
        "directImageCandidatesByLanguage": dict(direct_by_lang),
        "tcgdexLiveProbe": {"ko": tcgdex_ko, "zh-cn": tcgdex_zh},
        "lawfulSourcesAndContributionPaths": lawful_sources,
        "blockedActionsThisRun": [
            "No downloads from pokemoncard.co.kr",
            "No downloads from asia.pokemon-card.com / official CN mirrors",
            "No community dump ingestion",
            "No production promotion",
        ],
        "recommendedNextHumanSteps": [
            "Send TCGdex clarification (draft) — does not unlock KO/zh-Hans artwork",
            "Draft permission letters to Pokémon Korea and CN/Asia official digital teams",
            "Ask Scrydex whether KO/zh-Hans images are in-scope of written CDN grant",
            "Plan collector-photo intake for residual gaps",
        ],
    }

    REPORT.mkdir(parents=True, exist_ok=True)
    (REPORT / "ko_zh_hans_gap_audit.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    md = [
        "# KO / zh-Hans image gap audit",
        "",
        f"Generated: {payload['generatedAtUtc']}",
        "",
        "## Catalogue",
        "",
        f"- KO printings: **{payload['catalogueGap']['ko']['printings']}** "
        f"(with URL: {payload['catalogueGap']['ko']['withAnyImageUrl']})",
        f"- zh-Hans printings: **{payload['catalogueGap']['zh-Hans']['printings']}** "
        f"(with URL: {payload['catalogueGap']['zh-Hans']['withAnyImageUrl']})",
        "",
        "## TCGdex live probe (metadata only)",
        "",
        f"- ko imageCards: **{tcgdex_ko['imageCards']}** / setsListed={tcgdex_ko['setsListed']}",
        f"- zh-cn imageCards: **{tcgdex_zh['imageCards']}** / setsListed={tcgdex_zh['setsListed']}",
        "",
        "Conclusion: TCGdex cannot close KO/zh-Hans artwork via URL acquisition.",
        "",
        "## Lawful paths (no restricted downloads this run)",
        "",
    ]
    for src in lawful_sources:
        md.append(
            f"- **{src['source']}** — yield: {src['yield']}; "
            f"action: {src['lawfulAction']}; downloadAllowed={src['downloadAllowed']}"
        )
    md.extend(["", "## Human next steps", ""])
    for step in payload["recommendedNextHumanSteps"]:
        md.append(f"- {step}")
    md.append("")
    (REPORT / "KO_ZH_HANS_GAP_AUDIT.md").write_text("\n".join(md), encoding="utf-8")
    print(json.dumps(payload, indent=2, ensure_ascii=False)[:2000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
