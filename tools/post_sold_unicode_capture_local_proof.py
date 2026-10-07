#!/usr/bin/env python3
"""Offline Unicode + large-HTML post-Sold capture proof (LOCAL Chrome only).

Starts:
  1) local HTTP server serving the Unicode fixture
  2) disposable Chrome with CDP + host-resolver MAP www.ebay.com.au -> 127.0.0.1
  3) production run_capture_worker_process against that local Sold-shaped URL

ZERO live eBay contact. Does not touch production Chrome profile.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.providers.post_sold_capture_process import (  # noqa: E402
    capture_process_result_to_sold_page,
    run_capture_worker_process,
)
from tools.generate_unicode_capture_fixture import (  # noqa: E402
    ACCENTED,
    EMOJI,
    JAPANESE,
    ZWNJ,
    build_html,
    character_counts,
)

OUT_DIR = ROOT / "reports" / "artifacts" / "post_sold_unicode_capture_closure"
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "unicode_capture"
CDP_PORT = 19557
HTTP_PORT = 19558
QUERY = "Ceruledge Phantasmal Flames 20"


def _free_port(preferred: int) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])


def _find_chrome() -> str:
    candidates = [
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    for path in candidates:
        if path.is_file():
            return str(path)
    raise FileNotFoundError("chrome.exe not found")


def _wait_cdp(port: int, timeout: float = 25.0) -> dict:
    deadline = time.monotonic() + timeout
    last_err = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1.5) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            last_err = f"{type(exc).__name__}:{exc}"
            time.sleep(0.25)
    raise TimeoutError(f"CDP not ready: {last_err}")


def _navigate(port: int, url: str) -> None:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/new?{urllib.request.quote(url, safe=':/?&=%')}", timeout=10) as resp:
        _ = resp.read()
    # Fallback: some Chrome builds want PUT
    time.sleep(0.8)


def _wait_target_url(port: int, needle: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    last: list = []
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2.0) as resp:
                targets = json.loads(resp.read().decode("utf-8"))
            last = targets if isinstance(targets, list) else []
            for t in last:
                url = str(t.get("url") or "")
                if needle.lower() in url.lower() and "lh_sold=1" in url.lower():
                    return t
        except Exception:
            pass
        time.sleep(0.35)
    raise TimeoutError(f"fixture target not ready; last={json.dumps(last)[:800]}")


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)

    # Clear leftover last-capture artifacts so proof cannot read stale HTML.
    last_dir = ROOT / "reports" / "artifacts" / "post_sold_capture_last"
    if last_dir.is_dir():
        for name in ("last_capture.html", "last_capture_body.txt", "last_capture_meta.json", "last_capture_candidates.json"):
            try:
                (last_dir / name).unlink(missing_ok=True)
            except OSError:
                pass

    html = build_html(min_bytes=5_500_000)
    fixture_path = FIXTURE_DIR / "unicode_sold_fixture.html"
    fixture_bytes = html.encode("utf-8")
    expected_sha = hashlib.sha256(fixture_bytes).hexdigest()
    fixture_path.write_bytes(fixture_bytes)
    expected_counts = character_counts(html)
    (FIXTURE_DIR / "unicode_sold_fixture.meta.json").write_text(
        json.dumps(
            {
                "sha256": expected_sha,
                "byteLength": len(fixture_bytes),
                "characterCounts": expected_counts,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    http_port = _free_port(HTTP_PORT)
    cdp_port = _free_port(CDP_PORT)

    # Serve fixture directory; map path /sch/i.html to the fixture file via a thin wrapper.
    serve_root = Path(tempfile.mkdtemp(prefix="cardscanr_unicode_http_"))
    sch_dir = serve_root / "sch"
    sch_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(fixture_path, sch_dir / "i.html")

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(serve_root), **kwargs)

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            return

        def handle_one_request(self) -> None:
            try:
                super().handle_one_request()
            except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
                return

    httpd = ThreadingHTTPServer(("127.0.0.1", http_port), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    profile = Path(tempfile.mkdtemp(prefix="cardscanr_unicode_chrome_"))
    chrome = _find_chrome()
    # MAP ebay host to local loopback — never contacts real eBay.
    target_url = (
        f"http://www.ebay.com.au:{http_port}/sch/i.html"
        f"?_nkw=Ceruledge+Phantasmal+Flames+20&LH_Sold=1&rt=nc"
    )
    chrome_cmd = [
        chrome,
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-default-apps",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-sync",
        f"--remote-debugging-port={cdp_port}",
        "--remote-debugging-address=127.0.0.1",
        "--remote-allow-origins=*",
        "--host-resolver-rules=MAP www.ebay.com.au 127.0.0.1",
        "--window-size=1200,900",
        "about:blank",
    ]
    chrome_proc = subprocess.Popen(
        chrome_cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    proof: dict = {
        "ebayNavigations": 0,
        "ebaySearchesSubmitted": 0,
        "soldInteractions": 0,
        "pricingAttempts": 0,
        "fixtureSha256": expected_sha,
        "fixtureByteLength": len(fixture_bytes),
        "targetUrl": target_url,
        "cdpPort": cdp_port,
        "httpPort": http_port,
    }
    try:
        version = _wait_cdp(cdp_port)
        proof["cdpVersion"] = version.get("Browser")
        # Navigate via CDP HTTP new-window API after Chrome is ready.
        nav_url = urllib.request.quote(target_url, safe="")
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{cdp_port}/json/new?{nav_url}",
                method="PUT",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                proof["navigateResponse"] = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            # Fallback GET form used by older Chrome builds.
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{cdp_port}/json/new?{nav_url}", timeout=15
                ) as resp:
                    proof["navigateResponse"] = json.loads(resp.read().decode("utf-8"))
            except Exception as exc2:
                proof["navigateError"] = f"{type(exc).__name__}:{exc}; fallback:{exc2}"

        selected = _wait_target_url(cdp_port, "ebay.com.au", timeout=35.0)
        proof["selectedTargetBeforeCapture"] = {
            "id": selected.get("id"),
            "url": selected.get("url"),
            "title": selected.get("title"),
        }
        time.sleep(1.0)  # allow large document parse

        os.environ["CARDSCANR_CAPTURE_ORIGIN"] = "LOCAL_FIXTURE"
        os.environ["CARDSCANR_JOB_ID"] = "local-unicode-fixture-proof"
        os.environ["CARDSCANR_LIVE_ATTEMPT_ID"] = "local-unicode-fixture-attempt"
        result = run_capture_worker_process(
            cdp_endpoint=f"http://127.0.0.1:{cdp_port}",
            expected_url=target_url,
            expected_query=QUERY,
            expected_origin="ebay.com.au",
            deadline_seconds=20.0,
            socket_timeout=8.0,
            max_results=60,
        )
        sold = capture_process_result_to_sold_page(result, x11_sold_state_verified=True)
        payload = result.payload if isinstance(result.payload, dict) else {}
        html_path = payload.get("html_path") or payload.get("htmlPath")
        captured_html = str(payload.get("html") or "")
        if (not captured_html) and html_path and Path(str(html_path)).is_file():
            captured_html = Path(str(html_path)).read_text(encoding="utf-8")
        captured_bytes = captured_html.encode("utf-8") if captured_html else b""
        captured_sha = hashlib.sha256(captured_bytes).hexdigest() if captured_bytes else ""
        counts = character_counts(captured_html) if captured_html else {}
        unexpected_fffd = int(counts.get("replacement_fffd") or 0)

        proof.update(
            {
                "workerStatus": result.status,
                "exitCode": result.exit_code,
                "orphanCountAfter": result.orphan_count_after,
                "elapsedMs": result.elapsed_ms,
                "capturePhase": sold.capture_phase,
                "failureClass": sold.failure_class,
                "targetId": payload.get("target_id") or sold.target_id,
                "targetUrlCaptured": payload.get("target_url") or sold.target_url,
                "targetTitle": payload.get("target_title") or sold.target_title,
                "bodyLength": payload.get("body_text_length") or len(str(payload.get("body_text") or "")),
                "htmlLengthChars": payload.get("html_length") or len(captured_html),
                "htmlLengthBytes": payload.get("html_length_bytes") or len(captured_bytes),
                "htmlPath": html_path,
                "htmlSha256Worker": payload.get("html_sha256"),
                "htmlSha256Recomputed": captured_sha,
                "characterCountsCaptured": counts,
                "characterCountsExpected": expected_counts,
                "u200cPreserved": (counts.get("u200c_zwnj") or 0) > 0,
                "japanesePreserved": (counts.get("japanese_marker") or 0) > 0,
                "emojiPreserved": (counts.get("emoji_marker") or 0) > 0,
                "unexpectedReplacementCharacters": unexpected_fffd,
                "stdoutContainsFullHtml": len(result.stdout_raw or "") > 1_000_000,
                "stdoutByteLength": len((result.stdout_raw or "").encode("utf-8")),
                "success": bool(
                    result.status == "SUCCESS"
                    and result.exit_code == 0
                    and result.orphan_count_after == 0
                    and sold.capture_phase == "POST_SOLD_CAPTURE_READY"
                    and (counts.get("u200c_zwnj") or 0) > 0
                    and (counts.get("japanese_marker") or 0) > 0
                    and (counts.get("emoji_marker") or 0) > 0
                    and unexpected_fffd == 0
                    and len(captured_bytes) >= 5_000_000
                ),
            }
        )
    except Exception as exc:
        proof["success"] = False
        proof["error"] = f"{type(exc).__name__}:{exc}"
    finally:
        try:
            chrome_proc.terminate()
            chrome_proc.wait(timeout=5)
        except Exception:
            try:
                chrome_proc.kill()
            except Exception:
                pass
        try:
            httpd.shutdown()
        except Exception:
            pass
        shutil.rmtree(profile, ignore_errors=True)
        shutil.rmtree(serve_root, ignore_errors=True)

    out_path = OUT_DIR / "unicode_local_capture_proof.json"
    out_path.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": proof.get("success"), "path": str(out_path), "status": proof.get("workerStatus")}, ensure_ascii=True))
    return 0 if proof.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
