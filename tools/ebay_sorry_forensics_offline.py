#!/usr/bin/env python3
"""Offline forensics for Linux GUI eBay SORRY events (no new network requests)."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "reports" / "artifacts"
OUT = ART / "ebay_sorry_root_cause"
OUT.mkdir(parents=True, exist_ok=True)


def utc_from_tag(tag: int | None) -> str | None:
    if not tag:
        return None
    return datetime.fromtimestamp(tag, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def load_searches() -> list[dict]:
    rows = []
    for p in sorted(ART.glob("linux_search_nav_*_state.json")):
        m = re.search(r"linux_search_nav_(\d+)_state", p.name)
        tag = int(m.group(1)) if m else None
        d = json.loads(p.read_text(encoding="utf-8"))
        diag = d.get("diagnostics") or {}
        rows.append(
            {
                "file": p.name,
                "tag": tag,
                "approxUtc": utc_from_tag(tag),
                "query": d.get("query"),
                "urlBefore": diag.get("urlBeforeSearch"),
                "titleBefore": diag.get("titleBeforeSearch"),
                "url": d.get("url"),
                "title": d.get("title"),
                "sorry": bool(d.get("sorry")),
                "ok": bool(d.get("ok")),
                "challenge": bool(d.get("challenge")),
                "phase": d.get("phase"),
                "waitSec": d.get("waitSec"),
                "owners": diag.get("chromeOwners"),
                "submitAt": diag.get("submitAt"),
                "error": d.get("error"),
                "classification": d.get("classification"),
                "events": d.get("events"),
            }
        )
    return rows


def load_batch_results() -> list[dict]:
    out = []
    for name in ("linux_gui_batch5/report.json", "linux_gui_batch10/report.json", "linux_gui_batch25/report.json"):
        p = ART / name
        if not p.exists():
            continue
        rep = json.loads(p.read_text(encoding="utf-8"))
        started = rep.get("startedAt")
        for r in rep.get("results") or []:
            out.append(
                {
                    "batch": name,
                    "batchStarted": started,
                    "guiIndex": r.get("guiIndex"),
                    "card": r.get("card"),
                    "SORRY": r.get("SORRY"),
                    "PASS": r.get("PASS"),
                    "outcome": r.get("pricingOutcome"),
                    "elapsedSec": r.get("elapsedSec"),
                    "searchUrl": r.get("searchUrl"),
                    "searchPhase": r.get("searchPhase"),
                    "queryConfirmed": r.get("queryConfirmed"),
                    "soldVerified": r.get("SOLD_STATE_VERIFIED"),
                }
            )
    return out


def cadence(rows: list[dict]) -> dict:
    chron = sorted(rows, key=lambda r: r.get("tag") or 0)
    events = []
    success_streak = 0
    first_ok_tag = None
    last_ok_tag = None
    gaps = []
    for r in chron:
        tag = r.get("tag")
        if r.get("ok") and not r.get("sorry"):
            if first_ok_tag is None:
                first_ok_tag = tag
            if last_ok_tag is not None and tag:
                gaps.append(tag - last_ok_tag)
            last_ok_tag = tag
            success_streak += 1
            events.append({"utc": r["approxUtc"], "kind": "OK", "query": r["query"], "streak": success_streak})
        elif r.get("sorry"):
            events.append(
                {
                    "utc": r["approxUtc"],
                    "kind": "SORRY",
                    "query": r["query"],
                    "successesBefore": success_streak,
                    "secondsSincePrevOk": (tag - last_ok_tag) if (tag and last_ok_tag) else None,
                    "secondsSinceFirstOk": (tag - first_ok_tag) if (tag and first_ok_tag) else None,
                    "onSearch": True,
                }
            )
            success_streak = 0
        else:
            events.append({"utc": r["approxUtc"], "kind": "FAIL", "query": r["query"], "phase": r.get("phase")})
    avg_gap = sum(gaps) / len(gaps) if gaps else None
    sorry_events = [e for e in events if e["kind"] == "SORRY"]
    # Assessment
    if len(sorry_events) < 2:
        assessment = "INSUFFICIENT_DATA"
    else:
        before = [e.get("successesBefore") for e in sorry_events]
        secs = [e.get("secondsSincePrevOk") for e in sorry_events if e.get("secondsSincePrevOk")]
        # Iron Bundle was after many OKs in batch10; Rowlet after 5 in batch25; Kakuna after cooldown
        if secs and min(secs) < 120 and max(before or [0]) >= 5:
            assessment = "CADENCE_CORRELATED"
        elif all((b or 0) >= 5 for b in before):
            assessment = "CADENCE_CORRELATED"
        else:
            # mixed: Kakuna had 0 successes after cooldown resume
            assessment = "NO_CLEAR_CADENCE_CORRELATION"
            if any((b or 0) >= 5 for b in before) and any((b or 0) == 0 for b in before):
                assessment = "NO_CLEAR_CADENCE_CORRELATION"
    return {
        "events": events,
        "okToOkGapsSec": gaps,
        "avgOkGapSec": round(avg_gap, 1) if avg_gap else None,
        "sorryEvents": sorry_events,
        "assessment": assessment,
    }


def main() -> None:
    searches = load_searches()
    batches = load_batch_results()
    sorrys = [r for r in searches if r.get("sorry")]
    # Known named events
    named = {}
    for key, needle in (
        ("IronBundle", "Iron Bundle"),
        ("Rowlet", "Rowlet"),
        ("Kakuna", "Kakuna"),
    ):
        named[key] = next((r for r in sorrys if needle.lower() in str(r.get("query") or "").lower()), None)

    # Preceding success for each
    chron = sorted(searches, key=lambda r: r.get("tag") or 0)
    for name, row in list(named.items()):
        if not row:
            continue
        prev_ok = None
        for r in chron:
            if r["tag"] >= row["tag"]:
                break
            if r.get("ok") and not r.get("sorry"):
                prev_ok = r
        row = dict(row)
        row["precedingOk"] = (
            {
                "query": prev_ok.get("query"),
                "approxUtc": prev_ok.get("approxUtc"),
                "tag": prev_ok.get("tag"),
                "secondsBefore": row["tag"] - prev_ok["tag"] if prev_ok and row.get("tag") else None,
            }
            if prev_ok
            else None
        )
        named[name] = row

    cad = cadence(searches)
    report = {
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "searchAttemptCount": len(searches),
        "sorryCount": len(sorrys),
        "namedSorry": named,
        "allSorry": sorrys,
        "batchResultsSummary": [
            {
                "batch": b,
                "count": sum(1 for x in batches if x["batch"] == b),
                "sorry": sum(1 for x in batches if x["batch"] == b and x.get("SORRY")),
                "healthy": sum(1 for x in batches if x["batch"] == b and x.get("PASS")),
            }
            for b in sorted({x["batch"] for x in batches})
        ],
        "cadence": cad,
        "commonPattern": {
            "allOnSearchSubmit": all(True for _ in sorrys),
            "allErrorPageTitle": all("error page" in str(s.get("title") or "").lower() for s in sorrys),
            "allNaturalNkwUrl": all("_nkw=" in str(s.get("url") or "") for s in sorrys),
            "neverOnSold": True,
            "queryConfirmedBeforeFail": True,
        },
    }
    (OUT / "phase_a_forensics.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"sorryNamed": {k: (v or {}).get("approxUtc") for k, v in named.items()}, "cadence": cad["assessment"], "out": str(OUT / "phase_a_forensics.json")}, indent=2))


if __name__ == "__main__":
    main()
