#!/bin/bash
# Disposable Chrome+CDP proof on DISPLAY=:99 — NEVER uses production profile.
set -euo pipefail

DISPLAY_NAME="${1:-:99}"
CDP_PORT="${2:-19555}"
PREFIX="${CARDSCANR_GUI_PREFIX:-$HOME/.local/cardscanr-gui}"
export PATH="$PREFIX/root/usr/bin:$HOME/.local/bin:$PATH"
export LD_LIBRARY_PATH="$PREFIX/root/usr/lib/x86_64-linux-gnu:$PREFIX/root/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export DISPLAY="$DISPLAY_NAME"
unset WAYLAND_DISPLAY || true
unset XDG_SESSION_TYPE || true
export GDK_BACKEND=x11

OUT_DIR="/mnt/d/cardscanr-data/reports/artifacts/local_runtime_readiness"
mkdir -p "$OUT_DIR"
PROFILE="$(mktemp -d /tmp/cardscanr-chrome-disp-XXXXXX)"
LOG="/tmp/cardscanr_chrome_disp.log"
VERSION_FILE="/tmp/cardscanr_disp_version.json"
LIST_FILE="/tmp/cardscanr_disp_list.json"
RESULT="$OUT_DIR/disposable_cdp_proof.json"
CHROME_BIN="$(command -v google-chrome-stable)"

cleanup() {
  pkill -f "chrome.*--user-data-dir=${PROFILE}" 2>/dev/null || true
  sleep 0.4
  pkill -KILL -f "chrome.*--user-data-dir=${PROFILE}" 2>/dev/null || true
  rm -rf "$PROFILE" 2>/dev/null || true
}
trap cleanup EXIT

if ! xdpyinfo -display "$DISPLAY_NAME" >/dev/null 2>&1; then
  echo '{"ok":false,"reason":"xvfb_not_ready"}' | tee "$RESULT"
  exit 10
fi

: > "$LOG"
setsid -f "$CHROME_BIN" \
  --user-data-dir="$PROFILE" \
  --no-first-run \
  --no-default-browser-check \
  --disable-default-apps \
  --disable-extensions \
  --disable-background-networking \
  --disable-sync \
  --disable-translate \
  --disable-features=TranslateUI,ChromeWhatsNewUI \
  --disable-session-crashed-bubble \
  --hide-crash-restore-bubble \
  --disable-restore-session-state \
  --ozone-platform=x11 \
  --disable-dev-shm-usage \
  --use-gl=angle \
  --use-angle=swiftshader-webgl \
  --disable-gpu-compositing \
  --remote-debugging-port="$CDP_PORT" \
  --remote-debugging-address=127.0.0.1 \
  --window-size=1100,700 \
  --window-position=50,50 \
  about:blank \
  >"$LOG" 2>&1 < /dev/null

ok_version=0
for _ in $(seq 1 40); do
  if curl -s -m 2 "http://127.0.0.1:${CDP_PORT}/json/version" >"$VERSION_FILE" 2>/dev/null; then
    if grep -q Browser "$VERSION_FILE"; then
      ok_version=1
      break
    fi
  fi
  sleep 0.4
done

if [ "$ok_version" = 1 ]; then
  curl -s -m 2 "http://127.0.0.1:${CDP_PORT}/json/list" >"$LIST_FILE" 2>/dev/null || echo '[]' >"$LIST_FILE"
else
  echo '{}' >"$VERSION_FILE"
  echo '[]' >"$LIST_FILE"
fi

CHROME_PID="$(pgrep -f "chrome.*--user-data-dir=${PROFILE}" | head -1 || true)"
WIN_LINE="$(xwininfo -root -tree 2>/dev/null | grep -F 'Google Chrome' | head -1 || true)"

DISPLAY_NAME="$DISPLAY_NAME" CDP_PORT="$CDP_PORT" PROFILE="$PROFILE" \
CHROME_PID="$CHROME_PID" WIN_LINE="$WIN_LINE" LOG="$LOG" \
VERSION_FILE="$VERSION_FILE" LIST_FILE="$LIST_FILE" RESULT="$RESULT" \
OK_VERSION="$ok_version" python3 <<'PY'
import json, os
from pathlib import Path

ver = json.loads(Path(os.environ["VERSION_FILE"]).read_text(encoding="utf-8") or "{}")
targets = json.loads(Path(os.environ["LIST_FILE"]).read_text(encoding="utf-8") or "[]")
urls = [str((t or {}).get("url") or "") for t in targets] if isinstance(targets, list) else []
ebay = [u for u in urls if "ebay." in u.lower()]
blankish = [u for u in urls if u.startswith("about:") or u.startswith("chrome://") or u == ""]
pid_s = os.environ.get("CHROME_PID") or ""
payload = {
    "ok": os.environ.get("OK_VERSION") == "1" and not ebay,
    "profileType": "disposable_temp",
    "profileDir": os.environ["PROFILE"],
    "display": os.environ["DISPLAY_NAME"],
    "cdpPort": int(os.environ["CDP_PORT"]),
    "chromePid": int(pid_s) if pid_s.isdigit() else None,
    "jsonVersion": ver,
    "targetCount": len(targets) if isinstance(targets, list) else 0,
    "targetUrls": urls,
    "ebayTargets": ebay,
    "blankOrLocalTargets": blankish,
    "x11Window": os.environ.get("WIN_LINE") or "",
    "logTail": Path(os.environ["LOG"]).read_text(encoding="utf-8", errors="replace")[-800:],
}
Path(os.environ["RESULT"]).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
print(json.dumps(payload, indent=2))
raise SystemExit(0 if payload["ok"] else 1)
PY
