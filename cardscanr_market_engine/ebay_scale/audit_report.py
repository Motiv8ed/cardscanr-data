from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .audit_paths import audit_jsonl_path, audit_reports_dir


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def load_audit_rows(*, since: datetime | None, jsonl_path: Path) -> list[dict[str, Any]]:
    if not jsonl_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if since is not None:
            ts = _parse_ts(str(row.get("recordedAt") or ""))
            if ts is None or ts < since:
                continue
        rows.append(row)
    return rows


def _within_pct(row: dict[str, Any], pct: float) -> bool:
    diff = row.get("pctDiff")
    if diff is None:
        return False
    try:
        return float(diff) <= pct
    except (TypeError, ValueError):
        return False


def summarise_sweeps(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_sweep: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_sweep[str(row.get("sweepId") or "unknown")].append(row)

    per_sweep: list[dict[str, Any]] = []
    mismatch_reasons: Counter[str] = Counter()

    for sweep_id, sweep_rows in sorted(by_sweep.items()):
        priced = [r for r in sweep_rows if r.get("setSweepMedian") is not None]
        comparable = [r for r in priced if r.get("perCardPrice") is not None]
        agree_15 = sum(1 for r in comparable if _within_pct(r, 15))
        agree_30 = sum(1 for r in comparable if _within_pct(r, 30))
        diffs = sorted(
            comparable,
            key=lambda r: float(r.get("absDiffAud") or 0),
            reverse=True,
        )[:20]
        top_diffs = [
            {
                "cardId": r.get("catalogueCardId"),
                "absDiffAud": r.get("absDiffAud"),
                "pctDiff": r.get("pctDiff"),
                "sweepMedian": r.get("setSweepMedian"),
                "perCardPrice": r.get("perCardPrice"),
                "listingTitles": [m.get("title") for m in (r.get("matchedListings") or [])[:5]],
            }
            for r in diffs
        ]
        for r in sweep_rows:
            if r.get("suspectedMismatch"):
                for reason in r.get("suspectedMismatchReasons") or []:
                    mismatch_reasons[str(reason)] += 1
        per_sweep.append(
            {
                "sweepId": sweep_id,
                "setCode": sweep_rows[0].get("setCode") if sweep_rows else "",
                "cardsPriced": len(priced),
                "cardsPerSweep": len(priced),
                "comparableToPerCard": len(comparable),
                "agreementWithin15Pct": agree_15,
                "agreementWithin30Pct": agree_30,
                "agreementRate15Pct": round(agree_15 / len(comparable), 4) if comparable else None,
                "agreementRate30Pct": round(agree_30 / len(comparable), 4) if comparable else None,
                "topDifferences": top_diffs,
                "suspectedMismatchCount": sum(1 for r in sweep_rows if r.get("suspectedMismatch")),
            }
        )

    return {
        "totalRows": len(rows),
        "sweepCount": len(by_sweep),
        "perSweep": per_sweep,
        "suspectedMismatchReasons": dict(mismatch_reasons),
    }


def render_markdown(summary: dict[str, Any], *, since_label: str) -> str:
    lines = [
        "# eBay set-sweep shadow audit report",
        "",
        f"Since: **{since_label}**",
        f"Total audit rows: **{summary.get('totalRows', 0)}**",
        f"Sweeps: **{summary.get('sweepCount', 0)}**",
        "",
        "## Per sweep",
        "",
    ]
    for block in summary.get("perSweep") or []:
        lines.append(f"### `{block.get('sweepId')}` ({block.get('setCode')})")
        lines.append(f"- Cards priced: **{block.get('cardsPriced')}**")
        ar15 = block.get("agreementRate15Pct")
        ar30 = block.get("agreementRate30Pct")
        if ar15 is not None:
            lines.append(f"- Agreement ±15% vs per-card: **{ar15:.1%}** ({block.get('agreementWithin15Pct')}/{block.get('comparableToPerCard')})")
            lines.append(f"- Agreement ±30% vs per-card: **{ar30:.1%}** ({block.get('agreementWithin30Pct')}/{block.get('comparableToPerCard')})")
        lines.append(f"- Suspected mismatches: **{block.get('suspectedMismatchCount')}**")
        lines.append("")
        lines.append("#### Top differences")
        for item in block.get("topDifferences") or []:
            titles = "; ".join(str(t) for t in (item.get("listingTitles") or [])[:3])
            lines.append(
                f"- `{item.get('cardId')}`: Δ AUD {item.get('absDiffAud')} "
                f"({item.get('pctDiff')}%) sweep={item.get('sweepMedian')} per-card={item.get('perCardPrice')} — {titles}"
            )
        lines.append("")

    reasons = summary.get("suspectedMismatchReasons") or {}
    if reasons:
        lines.append("## Suspected mismatch reasons")
        lines.append("")
        for reason, count in sorted(reasons.items(), key=lambda x: (-x[1], x[0])):
            lines.append(f"- `{reason}`: **{count}**")
        lines.append("")

    lines.append("_Generated from local append-only audit JSONL (no Supabase)._")
    return "\n".join(lines)


def write_report(*, since: datetime | None, jsonl_path: Path | None = None) -> Path:
    path = jsonl_path or audit_jsonl_path()
    rows = load_audit_rows(since=since, jsonl_path=path)
    summary = summarise_sweeps(rows)
    since_label = since.isoformat().replace("+00:00", "Z") if since else "all"
    md = render_markdown(summary, since_label=since_label)
    out_dir = audit_reports_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"set_sweep_audit_report_{stamp}.md"
    out_path.write_text(md, encoding="utf-8")
    # Also write JSON summary alongside markdown for tooling.
    json_path = out_dir / f"set_sweep_audit_report_{stamp}.json"
    json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return out_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarise eBay set-sweep shadow audit logs")
    parser.add_argument(
        "--since",
        default="",
        help="ISO timestamp — include rows recorded at or after this time (UTC)",
    )
    parser.add_argument(
        "--jsonl",
        default="",
        help="Optional path to audit JSONL (default: reports/runtime/ebay_scale_audit/set_sweep_audit.jsonl)",
    )
    args = parser.parse_args(argv)
    since = _parse_ts(args.since) if args.since else None
    jsonl = Path(args.jsonl).expanduser() if args.jsonl else None
    out = write_report(since=since, jsonl_path=jsonl)
    print(str(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())