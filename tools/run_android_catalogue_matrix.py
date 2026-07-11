#!/usr/bin/env python3
"""Run the deterministic multilingual catalogue matrix on a QA Android app."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import time


LANGUAGES = (
    "de", "en", "es", "es-419", "fr", "id", "it", "ja", "ko",
    "pt-BR", "th", "zh-Hans", "zh-Hant",
)


def run(adb: Path, *args: str, capture: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(adb), *args],
        check=True,
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--adb", type=Path, required=True)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--package", default="com.example.card_scanner_app.qa")
    parser.add_argument("--activity", default="com.example.card_scanner_app.MainActivity")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument(
        "--languages",
        default=",".join(LANGUAGES),
        help="Comma-separated language subset in launch order",
    )
    args = parser.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)
    device_matrix = "/data/local/tmp/catalog_matrix.json"
    run(args.adb, "push", str(args.matrix), device_matrix)
    all_results: list[dict[str, object]] = []
    launches: list[dict[str, object]] = []

    languages = tuple(value.strip() for value in args.languages.split(",") if value.strip())
    unknown = set(languages).difference(LANGUAGES)
    if unknown:
        parser.error(f"unsupported languages: {sorted(unknown)}")

    for language in languages:
        run(args.adb, "logcat", "-c")
        run(args.adb, "shell", "am", "force-stop", args.package)
        started = time.monotonic()
        run(
            args.adb,
            "shell", "am", "start", "-n", f"{args.package}/{args.activity}",
            "--es", "cardscanrQaAction", "open_catalog_matrix",
            "--es", "cardscanrQaMatrixFile", device_matrix,
            "--es", "cardscanrQaMatrixLanguage", language,
            "--es", "cardscanrQaCaseId", f"matrix-{language}",
        )
        parsed: dict[str, dict[str, object]] = {}
        fatal = ""
        while time.monotonic() - started < args.timeout:
            log = run(args.adb, "logcat", "-d", "-v", "raw").stdout
            if "FATAL EXCEPTION" in log or "CatalogMatrixQa fatal=" in log:
                fatal = "fatal marker in logcat"
                break
            for match in re.finditer(r"CatalogMatrixQa (\{[^\r\n]+\})", log):
                row = json.loads(match.group(1))
                parsed[str(row["canonicalPrintingId"])] = row
            if len(parsed) >= 3:
                break
            time.sleep(3)

        elapsed_ms = round((time.monotonic() - started) * 1000)
        log_path = args.output / f"matrix_{language}.log"
        log_path.write_text(
            run(args.adb, "logcat", "-d", "-v", "time").stdout,
            encoding="utf-8",
        )
        screenshot = args.output / f"matrix_{language}.png"
        with screenshot.open("wb") as handle:
            subprocess.run(
                [str(args.adb), "exec-out", "screencap", "-p"],
                check=True,
                stdout=handle,
            )
        xml_device = f"/sdcard/matrix_{language}.xml"
        subprocess.run(
            [str(args.adb), "shell", "uiautomator", "dump", xml_device],
            check=False,
            capture_output=True,
        )
        xml_path = args.output / f"matrix_{language}.xml"
        xml = run(args.adb, "shell", "cat", xml_device).stdout
        xml_path.write_text(xml, encoding="utf-8")
        status_match = re.search(r"Matrix status ([^&\"]+)", xml)
        status = status_match.group(1) if status_match else "missing"
        rows = list(parsed.values())
        all_results.extend(rows)
        launches.append(
            {
                "language": language,
                "classification": "PASS"
                if len(rows) == 3 and all(bool(row.get("passed")) for row in rows)
                and "PASS" in status and not fatal
                else "FAIL",
                "sampleResults": len(rows),
                "elapsedMs": elapsed_ms,
                "renderedStatus": status,
                "fatal": fatal or None,
                "screenshot": str(screenshot),
                "uiTree": str(xml_path),
                "log": str(log_path),
            }
        )
        print(json.dumps(launches[-1], ensure_ascii=False), flush=True)

    report = {
        "classification": "PASS"
        if all(row["classification"] == "PASS" for row in launches)
        else "FAIL",
        "deviceSerial": run(args.adb, "get-serialno").stdout.strip(),
        "package": args.package,
        "languages": len(launches),
        "sampleResults": len(all_results),
        "launches": launches,
        "results": all_results,
    }
    (args.output / "android_multilingual_matrix.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0 if report["classification"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
