#!/bin/bash
# Start CardScanR Xvfb on :99 without sudo / without touching eBay.
# Uses user+mount namespace overlay so bundled xkbcomp appears at /usr/bin/xkbcomp
# (Xvfb hardcodes that absolute path). Idempotent. Never kills WSLg :0.
set -euo pipefail

DISPLAY_NAME="${1:-:99}"
DISPLAY_NUM="${DISPLAY_NAME#:}"
PREFIX="${CARDSCANR_GUI_PREFIX:-$HOME/.local/cardscanr-gui}"
XVFB_BIN="$PREFIX/root/usr/bin/Xvfb"
XKBCOMP_SRC="$PREFIX/root/usr/bin/xkbcomp"
PIDFILE="/tmp/cardscanr_xvfb.pid"
UNSHARE_PIDFILE="/tmp/cardscanr_xvfb_unshare.pid"
STATUS_FILE="/tmp/cardscanr_xvfb_status"
LOG="/tmp/cardscanr_xvfb.log"
RESULT_JSON="/tmp/cardscanr_xvfb_result.json"

export PATH="$PREFIX/root/usr/bin:$HOME/.local/bin:$PATH"
export LD_LIBRARY_PATH="$PREFIX/root/usr/lib/x86_64-linux-gnu:$PREFIX/root/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export XKB_CONFIG_ROOT="$PREFIX/root/usr/share/X11/xkb"
unset WAYLAND_DISPLAY || true

write_result() {
  local ok="$1" reason="$2" pid="${3:-}"
  OK="$ok" REASON="$reason" PID="$pid" DISPLAY_NAME="$DISPLAY_NAME" \
  PIDFILE="$PIDFILE" UNSHARE_PIDFILE="$UNSHARE_PIDFILE" STATUS_FILE="$STATUS_FILE" LOG="$LOG" \
  RESULT_JSON="$RESULT_JSON" python3 <<'PY'
import json, os
from pathlib import Path
ok = os.environ["OK"] == "true"
pid_s = os.environ.get("PID") or ""
payload = {
    "ok": ok,
    "reason": os.environ["REASON"],
    "display": os.environ["DISPLAY_NAME"],
    "pid": int(pid_s) if pid_s.isdigit() else None,
    "unsharePid": None,
    "statusFile": Path(os.environ["STATUS_FILE"]).read_text(encoding="utf-8").strip() if Path(os.environ["STATUS_FILE"]).exists() else None,
    "logTail": Path(os.environ["LOG"]).read_text(encoding="utf-8", errors="replace")[-1200:] if Path(os.environ["LOG"]).exists() else "",
    "screen": "1920x1080x24",
}
up = Path(os.environ["UNSHARE_PIDFILE"])
if up.exists():
    try:
        payload["unsharePid"] = int(up.read_text(encoding="utf-8").strip())
    except Exception:
        pass
Path(os.environ["RESULT_JSON"]).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
print(json.dumps(payload, indent=2))
raise SystemExit(0 if ok else 1)
PY
}

already_pid=""
if xdpyinfo -display "$DISPLAY_NAME" >/dev/null 2>&1; then
  already_pid="$(pgrep -a Xvfb 2>/dev/null | grep -E "Xvfb[ ]+${DISPLAY_NAME}([ ]|$)" | awk '{print $1}' | head -1 || true)"
  if [ -n "$already_pid" ]; then
    echo "$already_pid" > "$PIDFILE"
    write_result true already_running "$already_pid"
    exit 0
  fi
fi

if [ ! -x "$XVFB_BIN" ]; then
  write_result false xvfb_binary_missing
  exit 2
fi
if [ ! -x "$XKBCOMP_SRC" ]; then
  write_result false xkbcomp_prefix_missing
  exit 3
fi

# Stale :99 lock/socket only — never touch X0
if [ -e "/tmp/.X${DISPLAY_NUM}-lock" ]; then
  if pgrep -a Xvfb 2>/dev/null | grep -qE "Xvfb[ ]+${DISPLAY_NAME}([ ]|$)"; then
    echo "LIVE_LOCK_KEPT" >&2
  else
    rm -f "/tmp/.X${DISPLAY_NUM}-lock"
    echo "CLEARED_STALE_LOCK" >&2
  fi
fi
if [ -e "/tmp/.X11-unix/X${DISPLAY_NUM}" ]; then
  if pgrep -a Xvfb 2>/dev/null | grep -qE "Xvfb[ ]+${DISPLAY_NAME}([ ]|$)"; then
    echo "LIVE_SOCKET_KEPT" >&2
  else
    rm -f "/tmp/.X11-unix/X${DISPLAY_NUM}" 2>/dev/null || echo "STALE_SOCKET_RM_BLOCKED" >&2
  fi
fi

mkdir -p "$HOME/.local/cardscanr-x11-unix"
chmod 1777 "$HOME/.local/cardscanr-x11-unix" 2>/dev/null || true

rm -f "$STATUS_FILE"
: > "$LOG"

nohup unshare --user --map-root-user --mount bash -c "
  set -e
  export PATH='$PREFIX/root/usr/bin:$HOME/.local/bin:'\"\$PATH\"
  export LD_LIBRARY_PATH='$PREFIX/root/usr/lib/x86_64-linux-gnu:$PREFIX/root/lib/x86_64-linux-gnu'
  export XKB_CONFIG_ROOT='$PREFIX/root/usr/share/X11/xkb'
  unset WAYLAND_DISPLAY
  mkdir -p /tmp/cardscanr-usrbin-upper /tmp/cardscanr-usrbin-work
  if ! mount -t overlay overlay -o lowerdir=/usr/bin,upperdir=/tmp/cardscanr-usrbin-upper,workdir=/tmp/cardscanr-usrbin-work /usr/bin; then
    echo overlay_mount_failed > '$STATUS_FILE'
    echo OVERLAY_MOUNT_FAILED >> '$LOG'
    exit 11
  fi
  cp -a '$XKBCOMP_SRC' /usr/bin/xkbcomp
  chmod 755 /usr/bin/xkbcomp
  chmod 1777 /tmp/.X11-unix 2>>'$LOG' || echo X11_UNIX_CHMOD_BLOCKED >> '$LOG'
  echo OVERLAY_XKBCOMP_READY >> '$LOG'
  '$XVFB_BIN' $DISPLAY_NAME -screen 0 1920x1080x24 -ac -nolisten tcp >>'$LOG' 2>&1 &
  echo \$! > '$PIDFILE'
  echo STARTED_PID=\$(cat '$PIDFILE') >> '$LOG'
  for i in \$(seq 1 30); do
    if xdpyinfo -display $DISPLAY_NAME >/dev/null 2>&1; then
      echo READY > '$STATUS_FILE'
      echo XDPY_READY >> '$LOG'
      wait \$(cat '$PIDFILE')
      exit 0
    fi
    if ! kill -0 \$(cat '$PIDFILE') 2>/dev/null; then
      echo DEAD > '$STATUS_FILE'
      echo XVFB_DIED >> '$LOG'
      exit 12
    fi
    sleep 0.5
  done
  echo TIMEOUT > '$STATUS_FILE'
  echo XDPY_TIMEOUT >> '$LOG'
  wait \$(cat '$PIDFILE')
" >/tmp/cardscanr_xvfb_unshare.out 2>/tmp/cardscanr_xvfb_unshare.err &
echo $! > "$UNSHARE_PIDFILE"

for _ in $(seq 1 40); do
  if [ -f "$STATUS_FILE" ]; then
    st="$(cat "$STATUS_FILE" || true)"
    case "$st" in
      READY|DEAD|TIMEOUT|overlay_mount_failed) break ;;
    esac
  fi
  sleep 0.25
done

PID="$(cat "$PIDFILE" 2>/dev/null || true)"
HOST_XDPY=0
ALIVE=0
if xdpyinfo -display "$DISPLAY_NAME" >/dev/null 2>&1; then HOST_XDPY=1; fi
if [ -n "${PID:-}" ] && kill -0 "$PID" 2>/dev/null; then ALIVE=1; fi
ST="$(cat "$STATUS_FILE" 2>/dev/null || true)"

if [ "$HOST_XDPY" = 1 ] && [ "$ALIVE" = 1 ]; then
  write_result true ok "$PID"
  exit 0
fi

REASON="xvfb_start_failed:${ST:-unknown}"
if [ -f "$LOG" ]; then
  if grep -qi 'xkbcomp.*not found' "$LOG"; then
    REASON="xkbcomp_missing_at_usr_bin"
  elif grep -qi 'failed to bind listener' "$LOG"; then
    REASON="x11_unix_socket_bind_failed"
  elif [ "$ST" = "overlay_mount_failed" ]; then
    REASON="overlay_mount_failed"
  fi
fi
write_result false "$REASON" "$PID"
exit 1
