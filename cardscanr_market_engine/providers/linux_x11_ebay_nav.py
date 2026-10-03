"""Linux X11 eBay navigation via WSL :99 (search field + Sold click).

Navigation only — no Playwright clicks, no manufactured /sch URLs.
Playwright/CDP may attach afterward solely to READ the already-loaded page.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..live_navigation_attempt import attempts_dir_wsl
from ..navigation_runtime_context import (
    PRE_SUBMIT_QUERY_READY,
    load_navigation_runtime_context,
    pre_submit_only_requested,
)

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"
# Prefer 9444 so we do not collide with a Windows desktop Chrome on 9333.
DEFAULT_CDP_PORT = int(os.getenv("EBAY_BROWSER_CDP_PORT", "9444"))
WSL_DISTRO = os.getenv("CARDSCANR_WSL_DISTRO", "Ubuntu")
# Durable CardScanR-owned interpreter (NOT /tmp — ephemeral across WSL restarts).
DURABLE_X11_PYTHON = (
    os.getenv("CARDSCANR_X11_PYTHON")
    or "$HOME/.local/cardscanr-gui/venv/bin/python"
)


@dataclass
class LinuxNavResult:
    ok: bool
    query: str
    search_success: bool
    sold_click_success: bool
    sold_state_verified: bool
    url: str
    title: str
    sorry: bool
    challenge: bool
    sold_date_lines: int
    error: str | None = None
    diagnostics: dict[str, Any] | None = None


class X11NavigationRuntimeError(RuntimeError):
    """Local navigation infrastructure failure before any eBay interaction."""


def _wsl_python(
    script_args: list[str],
    *,
    timeout: int = 180,
    env_exports: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run a tools/*.py script inside WSL with the durable X11 interpreter."""
    py = " ".join(shlex_quote(a) for a in script_args)
    override = (os.getenv("CARDSCANR_X11_PYTHON") or "").strip()
    if override:
        py_assign = f"PY={shlex_quote(override)}"
    else:
        py_assign = 'PY="${CARDSCANR_GUI_PREFIX:-$HOME/.local/cardscanr-gui}/venv/bin/python"'
    extra_env = ""
    for key, value in (env_exports or {}).items():
        extra_env += f"export {key}={shlex_quote(str(value))}\n"
    bash = f"""
set -e
PREFIX="${{CARDSCANR_GUI_PREFIX:-$HOME/.local/cardscanr-gui}}"
{py_assign}
export PATH="$PREFIX/root/usr/bin:$HOME/.local/bin:$PATH"
export LD_LIBRARY_PATH="$PREFIX/root/usr/lib/x86_64-linux-gnu:$PREFIX/root/lib/x86_64-linux-gnu"
export DISPLAY="${{CARDSCANR_XVFB_DISPLAY:-:99}}"
unset WAYLAND_DISPLAY
{extra_env}
if [ ! -x "$PY" ]; then
  echo "X11_NAV_PYTHON_MISSING:$PY"
  exit 42
fi
"$PY" {py}
"""
    # Write to temp to avoid PowerShell quoting hell when nested — call via stdin file on D:
    tmp = Path(r"D:\DevCache\Temp") / f"wsl_nav_{int(time.time()*1000)}.sh"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(bash.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8"))
    wsl_path = "/mnt/d/DevCache/Temp/" + tmp.name
    proc = subprocess.run(
        ["wsl", "-d", WSL_DISTRO, "--", "bash", wsl_path],
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )
    out = proc.stdout or ""
    # Last JSON object in stdout
    text = out.strip()
    # Filter WSL noise lines
    lines = [ln for ln in text.splitlines() if not ln.startswith("wsl:") and "systemd" not in ln.lower()]
    blob = "\n".join(lines).strip()
    # Find outermost JSON
    start = blob.find("{")
    end = blob.rfind("}")
    combined = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 42 or "X11_NAV_PYTHON_MISSING" in combined:
        raise X11NavigationRuntimeError(
            "X11_NAV_PYTHON_MISSING: durable interpreter not found at "
            f"{DURABLE_X11_PYTHON}"
        )
    if start < 0 or end < 0:
        raise RuntimeError(f"wsl_nav_no_json exit={proc.returncode} stderr={proc.stderr[-500:]} stdout={blob[-800:]}")
    return json.loads(blob[start : end + 1])


def shlex_quote(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


def ensure_chrome_with_cdp(
    *,
    cdp_port: int = DEFAULT_CDP_PORT,
    start_url: str = "about:blank",
    runtime_mode: str | None = None,
    allow_existing_ebay_targets: bool = False,
) -> None:
    """Restart CardScanR Chrome on :99 with remote debugging if needed.

    Default start URL is ``about:blank`` so service bootstrap is NOT an eBay
    navigation and must not increment liveNavigationStarted. Callers that need
    an eBay landing page must navigate explicitly after the control-plane gate
    authorises live work.

    INTER_CARD / allow_existing_ebay_targets: if CDP is already up, leftover
    marketplace tabs are NOT treated as a cold-start failure.
    """
    from ..browser_lifecycle_policy import RUNTIME_INTER_CARD
    from ..navigation_runtime_context import load_navigation_runtime_context

    ctx = load_navigation_runtime_context()
    mode = (runtime_mode or ctx.runtime_mode or "").strip().upper()
    allow_ebay = bool(allow_existing_ebay_targets or mode == RUNTIME_INTER_CARD)
    safe_url = (start_url or "about:blank").strip() or "about:blank"
    if "ebay." in safe_url.lower():
        raise RuntimeError(
            "ensure_chrome_with_cdp refuses eBay start_url; bootstrap must use "
            "about:blank (or another non-eBay local target) so startup is not a live navigation"
        )
    allow_flag = "1" if allow_ebay else "0"
    check = f"""
PREFIX="$HOME/.local/cardscanr-gui"
export PATH="$PREFIX/root/usr/bin:$PATH"
export DISPLAY=:99
unset WAYLAND_DISPLAY
ALLOW_EBAY="{allow_flag}"
RUNTIME_MODE="{mode or "COLD_START"}"
# Service bootstrap is NOT a reliability/live-navigation attempt.
if curl -s -m 2 http://127.0.0.1:{cdp_port}/json/version >/dev/null 2>&1; then
  if [ "$ALLOW_EBAY" = "1" ] || [ "$RUNTIME_MODE" = "INTER_CARD" ]; then
    echo CDP_OK
    echo CDP_REUSED_EXISTING
    exit 0
  fi
  # COLD_START: already up — refuse if an eBay target is already present.
  if curl -s -m 2 http://127.0.0.1:{cdp_port}/json/list | grep -qi 'ebay\\.'; then
    echo CDP_HAS_EBAY_TARGET
    exit 4
  fi
  echo CDP_OK
  exit 0
fi
# stop existing chrome for this profile then start with CDP (blank/local target only)
"$PREFIX/bin/cardscanr_chrome_ctl.sh" stop || true
sleep 1
"$PREFIX/bin/cardscanr_chrome_ctl.sh" start \\
  --remote-debugging-port={cdp_port} \\
  --remote-debugging-address=127.0.0.1 \\
  --remote-allow-origins=* \\
  --disable-restore-session-state \\
  --window-position=50,50 --window-size=1100,700 \\
  {safe_url}
sleep 3
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  if curl -s -m 2 http://127.0.0.1:{cdp_port}/json/version >/dev/null 2>&1; then
    if curl -s -m 2 http://127.0.0.1:{cdp_port}/json/list | grep -qi 'ebay\\.'; then
      echo CDP_READY_BUT_EBAY_TARGET
      exit 4
    fi
    echo CDP_READY
    exit 0
  fi
  sleep 1
done
echo CDP_FAIL
tail -40 /tmp/chrome_cardscanr.log 2>/dev/null || true
exit 1
"""
    tmp = Path(r"D:\DevCache\Temp\wsl_chrome_cdp.sh")
    tmp.write_bytes(check.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8"))
    proc = subprocess.run(
        ["wsl", "-d", WSL_DISTRO, "--", "bash", "/mnt/d/DevCache/Temp/wsl_chrome_cdp.sh"],
        capture_output=True,
        text=True,
        timeout=90,
        encoding="utf-8",
        errors="replace",
    )
    if "CDP_OK" not in proc.stdout and "CDP_READY" not in proc.stdout:
        raise RuntimeError(
            "linux_chrome_cdp_failed: "
            f"mode={mode or 'COLD_START'} allowExistingEbay={allow_ebay} "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )


def navigate_query_to_sold(
    query: str,
    *,
    reset_homepage: bool = True,
    attempt_id: str | None = None,
    price_key_id: str | None = None,
    pre_submit_only: bool = False,
) -> LinuxNavResult:
    search_path = "/mnt/d/cardscanr-data/tools/linux_x11_ebay_search.py"
    sold_path = "/mnt/d/cardscanr-data/tools/linux_x11_ebay_sold.py"
    tag = f"nav_{int(time.time())}"
    attempt_id = (attempt_id or os.environ.get("CARDSCANR_LIVE_ATTEMPT_ID") or "").strip() or None
    price_key_id = (price_key_id or os.environ.get("CARDSCANR_PRICE_KEY_ID") or "").strip() or None
    ctx = load_navigation_runtime_context()
    go_home = bool(reset_homepage) and not ctx.is_inter_card()
    armed_pre_submit = pre_submit_only_requested(flag=pre_submit_only)
    env_exports: dict[str, str] = dict(ctx.env_exports())
    if attempt_id:
        env_exports["CARDSCANR_LIVE_ATTEMPT_ID"] = str(attempt_id)
    if price_key_id:
        env_exports["CARDSCANR_PRICE_KEY_ID"] = str(price_key_id)
    # Explicit Windows→WSL path for the ONE canonical attempt-event directory.
    # Do not rely on ambient Windows env inheritance inside WSL.
    env_exports["CARDSCANR_LIVE_NAV_ATTEMPTS_DIR"] = attempts_dir_wsl()
    env_exports["CARDSCANR_RUNTIME_MODE"] = ctx.runtime_mode
    try:
        args = [
            search_path,
            "--query",
            query,
            "--tag",
            tag,
            "--submit",
            "enter",
            "--runtime-mode",
            ctx.runtime_mode,
        ]
        if go_home:
            args.append("--home")
        if attempt_id:
            args.extend(["--attempt-id", str(attempt_id)])
        if price_key_id:
            args.extend(["--price-key-id", str(price_key_id)])
        if armed_pre_submit:
            args.append("--pre-submit-only")
        search = _wsl_python(args, timeout=120, env_exports=env_exports or None)
    except X11NavigationRuntimeError as exc:
        return LinuxNavResult(
            ok=False,
            query=query,
            search_success=False,
            sold_click_success=False,
            sold_state_verified=False,
            url="",
            title="",
            sorry=False,
            challenge=False,
            sold_date_lines=0,
            error=f"search_failed:{exc}",
            diagnostics={"localRuntimeFailure": True, "reason": "X11_NAV_PYTHON_MISSING"},
        )
    except Exception as exc:
        return LinuxNavResult(
            ok=False,
            query=query,
            search_success=False,
            sold_click_success=False,
            sold_state_verified=False,
            url="",
            title="",
            sorry=False,
            challenge=False,
            sold_date_lines=0,
            error=f"search_failed:{exc}",
        )

    if str(search.get("resultCode") or "") == PRE_SUBMIT_QUERY_READY or search.get("preSubmitQueryReady"):
        return LinuxNavResult(
            ok=True,
            query=query,
            search_success=True,
            sold_click_success=False,
            sold_state_verified=False,
            url=str(search.get("url") or ""),
            title=str(search.get("title") or ""),
            sorry=False,
            challenge=False,
            sold_date_lines=0,
            error=PRE_SUBMIT_QUERY_READY,
            diagnostics={
                "search": search,
                "resultCode": PRE_SUBMIT_QUERY_READY,
                "preSubmitQueryReady": True,
                "searchSubmissionStarted": False,
                "runtimeMode": ctx.runtime_mode,
            },
        )

    if search.get("challenge"):
        return LinuxNavResult(
            ok=False,
            query=query,
            search_success=False,
            sold_click_success=False,
            sold_state_verified=False,
            url=str(search.get("url") or ""),
            title=str(search.get("title") or ""),
            sorry=False,
            challenge=True,
            sold_date_lines=0,
            error="challenge_on_search",
            diagnostics={"search": search},
        )
    if search.get("sorry") or not search.get("ok"):
        err = str(search.get("error") or search.get("classification") or "search_not_ok")
        if search.get("aboutBlank"):
            err = "ABOUT_BLANK_ABORT"
        elif search.get("sorry") or str(search.get("classification") or "") == "TEMPORARY_EBAY_SERVER_FAILURE":
            err = "TEMPORARY_EBAY_SERVER_FAILURE"
        elif (
            search.get("ebayLive")
            or str(search.get("classification") or "")
            in {
                "EBAY_LIVE_RESULTS",
                "ALTERNATE_EBAY_SURFACE",
                "SOLD_UNAVAILABLE_ON_ALTERNATE_SURFACE",
                "LOCAL_SEARCH_SURFACE_STATE_LEAK",
                "LOCAL_SEARCH_SURFACE_RECOVERY_FAILED",
            }
            or "ebaylive/search" in str(search.get("url") or "").lower()
        ):
            err = str(search.get("classification") or "ALTERNATE_EBAY_SURFACE")
            if err not in {
                "EBAY_LIVE_RESULTS",
                "ALTERNATE_EBAY_SURFACE",
                "SOLD_UNAVAILABLE_ON_ALTERNATE_SURFACE",
                "LOCAL_SEARCH_SURFACE_STATE_LEAK",
                "LOCAL_SEARCH_SURFACE_RECOVERY_FAILED",
            }:
                err = "ALTERNATE_EBAY_SURFACE"
        # Local GUI already confirmed query + submit; do not rebrand as focus failure.
        elif (
            search.get("queryVisibleConfirmed")
            and search.get("submitted")
            and err == "SEARCH_INPUT_NOT_CONFIRMED"
        ):
            err = "search_results_not_confirmed"
        return LinuxNavResult(
            ok=False,
            query=query,
            search_success=bool(search.get("queryVisibleConfirmed") and search.get("submitted")),
            sold_click_success=False,
            sold_state_verified=False,
            url=str(search.get("url") or ""),
            title=str(search.get("title") or ""),
            sorry=bool(search.get("sorry")) or err == "TEMPORARY_EBAY_SERVER_FAILURE",
            challenge=False,
            sold_date_lines=0,
            error=err,
            diagnostics={"search": search},
        )

    time.sleep(0.35)
    try:
        sold = _wsl_python([sold_path, "--tag", tag], timeout=120)
    except Exception as exc:
        return LinuxNavResult(
            ok=False,
            query=query,
            search_success=True,
            sold_click_success=False,
            sold_state_verified=False,
            url=str(search.get("url") or ""),
            title=str(search.get("title") or ""),
            sorry=False,
            challenge=False,
            sold_date_lines=0,
            error=f"sold_failed:{exc}",
            diagnostics={"search": search},
        )

    body_path = ROOT / "reports" / "artifacts" / f"linux_sold_{tag}_body.txt"
    sold_dates = 0
    if body_path.is_file():
        for line in body_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().lower().startswith("sold "):
                sold_dates += 1

    verified = bool(sold.get("SOLD_STATE_VERIFIED"))
    # Merge T0–T10 stage marks from search + sold (parser/DB stages filled by provider).
    merged_marks: dict[str, float] = {}
    for blob in (search.get("guiAttemptTimings"), sold.get("guiAttemptTimings")):
        if isinstance(blob, dict) and isinstance(blob.get("marks"), dict):
            merged_marks.update({str(k): float(v) for k, v in blob["marks"].items() if v is not None})
    return LinuxNavResult(
        ok=verified and not sold.get("sorry") and not sold.get("challenge"),
        query=query,
        search_success=True,
        sold_click_success=bool(sold.get("soldClickSuccess") or sold.get("alreadySold")),
        sold_state_verified=verified,
        url=str(sold.get("url") or ""),
        title=str(sold.get("title") or ""),
        sorry=bool(sold.get("sorry")),
        challenge=bool(sold.get("challenge")),
        sold_date_lines=sold_dates,
        error=None if verified else str(sold.get("error") or "sold_not_verified"),
        diagnostics={"search": search, "sold": sold, "guiAttemptTimings": {"marks": merged_marks}},
    )
