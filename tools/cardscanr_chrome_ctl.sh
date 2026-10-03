#!/bin/bash
# CardScanR Chrome on DISPLAY=:99 — stable start/stop helpers
set -euo pipefail
PREFIX="${HOME}/.local/cardscanr-gui"
export PATH="${HOME}/.local/bin:${PREFIX}/root/usr/bin:${PATH}"
export LD_LIBRARY_PATH="${PREFIX}/root/usr/lib/x86_64-linux-gnu:${PREFIX}/root/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export DISPLAY=:99
unset WAYLAND_DISPLAY
unset XDG_SESSION_TYPE
export GDK_BACKEND=x11
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
PROFILE="${HOME}/.config/cardscanr-chrome"
LOG="/tmp/chrome_cardscanr.log"
PIDFILE="/tmp/cardscanr_chrome.pid"
CHROME_BIN="$(command -v google-chrome-stable)"

ensure_stack() {
  if ! xdpyinfo >/dev/null 2>&1; then
    echo "ERROR: DISPLAY :99 not ready" >&2
    exit 10
  fi
  if ! ss -ltn | grep -q '127.0.0.1:5901'; then
    env -u WAYLAND_DISPLAY -u XDG_SESSION_TYPE \
      x11vnc -display :99 -rfbport 5901 -localhost -nopw -forever -shared -bg -o /tmp/x11vnc.log || true
  fi
}

chrome_pids() {
  pgrep -f "chrome.*--user-data-dir=${PROFILE}" 2>/dev/null || true
}

clear_stale_locks() {
  # Only when NO live Chrome owns this profile. Do not delete profile data.
  local live
  live="$(chrome_pids)"
  if [ -n "$live" ]; then
    echo "LOCKS_SKIP live_pids=${live//$'\n'/,}"
    return 0
  fi
  local removed=0
  for f in SingletonLock SingletonSocket SingletonCookie; do
    if [ -e "${PROFILE}/${f}" ] || [ -L "${PROFILE}/${f}" ]; then
      rm -f "${PROFILE}/${f}"
      removed=1
      echo "REMOVED_STALE_${f}"
    fi
  done
  # dangling socket dir from previous crash
  if [ -n "$(find /tmp -maxdepth 1 -type d -name 'com.google.Chrome.*' 2>/dev/null | head -1)" ]; then
    for d in /tmp/com.google.Chrome.*; do
      [ -d "$d" ] || continue
      # only remove if empty of live listeners; safe if chrome dead
      rm -rf "$d" 2>/dev/null || true
      echo "REMOVED_STALE_SOCKET_DIR $d"
    done
  fi
  if [ "$removed" = "0" ]; then
    echo "LOCKS_CLEAN"
  fi
}

mark_clean_exit_prefs() {
  # Soft-fix crash restore bounce without wiping profile.
  python3 - <<'PY'
import json, os
p=os.path.expanduser("~/.config/cardscanr-chrome/Default/Preferences")
if not os.path.exists(p):
    print("PREFS_MISSING")
    raise SystemExit(0)
with open(p,"r",encoding="utf-8") as f:
    d=json.load(f)
prof=d.setdefault("profile",{})
before=prof.get("exit_type")
prof["exit_type"]="Normal"
prof["exited_cleanly"]=True
# avoid session restore of crashed tabs interfering with controlled starts
sess=d.setdefault("session",{})
# keep user setting if explicit; otherwise leave
with open(p,"w",encoding="utf-8") as f:
    json.dump(d,f,separators=(",",":"))
print(f"PREFS_EXIT_TYPE {before}->Normal")
PY
}

# Soft-disable session restore in Preferences without wiping cookies/profile data.
# restore_on_startup: 5 = Open the New Tab page (Chromium enum).
suppress_session_restore_prefs() {
  python3 - <<'PY'
import json, os
p=os.path.expanduser("~/.config/cardscanr-chrome/Default/Preferences")
if not os.path.exists(p):
    print("PREFS_MISSING")
    raise SystemExit(0)
with open(p,"r",encoding="utf-8") as f:
    d=json.load(f)
sess=d.setdefault("session",{})
before=sess.get("restore_on_startup")
# 5 = NTP / blankish startup; do not continue where left off (1).
sess["restore_on_startup"]=5
# Clear startup URL list so no prior eBay URL is force-opened.
sess["startup_urls"]=[]
prof=d.setdefault("profile",{})
prof["exit_type"]="Normal"
prof["exited_cleanly"]=True
with open(p,"w",encoding="utf-8") as f:
    json.dump(d,f,separators=(",",":"))
print(f"PREFS_RESTORE_ON_STARTUP {before}->5")
PY
}

start_chrome() {
  ensure_stack
  clear_stale_locks
  mark_clean_exit_prefs
  suppress_session_restore_prefs
  mkdir -p "$PROFILE"
  # Launch detached from the calling shell so WSL script exit (SIGHUP) does not
  # kill Chrome — that was leaving Andrew with only the blue :99 root in noVNC.
  # SwiftShader so the window paints on Xvfb.
  # Session-restore hard flags + about:blank (or caller URL) prevent old eBay tabs
  # from auto-restoring during service bootstrap. Do not wipe cookies/profile.
  setsid -f "${CHROME_BIN}" \
    --user-data-dir="${PROFILE}" \
    --no-first-run \
    --disable-default-apps \
    --disable-session-crashed-bubble \
    --hide-crash-restore-bubble \
    --disable-restore-session-state \
    --disable-features=ChromeWhatsNewUI \
    --ozone-platform=x11 \
    --disable-dev-shm-usage \
    --use-gl=angle \
    --use-angle=swiftshader-webgl \
    --disable-gpu-compositing \
    --window-size=1200,720 \
    --window-position=40,40 \
    "$@" \
    >"${LOG}" 2>&1 < /dev/null
  sleep 0.4
  local cpid
  cpid="$(chrome_pids | head -1 || true)"
  if [ -z "$cpid" ]; then
    # fallback: launcher may still be forking
    sleep 0.6
    cpid="$(chrome_pids | head -1 || true)"
  fi
  echo "${cpid:-0}" >"$PIDFILE"
  echo "CHROME_MAIN_PID=${cpid:-unknown}"
  # Wait for a real Chrome window on :99
  local i line
  for i in $(seq 1 45); do
    if ! kill -0 "$cpid" 2>/dev/null; then
      # main launcher may exec; check profile procs
      if [ -z "$(chrome_pids)" ]; then
        echo "CHROME_DIED"
        tail -30 "$LOG" || true
        return 1
      fi
    fi
    line="$(xwininfo -root -tree 2>/dev/null | grep -F 'Google Chrome' | head -1 || true)"
    if [ -n "$line" ]; then
      echo "CHROME_WINDOW_OK $line"
      return 0
    fi
    sleep 0.5
  done
  echo "CHROME_WINDOW_TIMEOUT"
  return 1
}

stop_chrome() {
  local pids
  pids="$(chrome_pids)"
  if [ -z "$pids" ]; then
    echo "CHROME_ALREADY_STOPPED"
    clear_stale_locks
    return 0
  fi
  echo "CHROME_STOPPING pids=${pids//$'\n'/,}"
  # Prefer graceful: SIGTERM browser process tree for this profile
  pkill -TERM -f "chrome.*--user-data-dir=${PROFILE}" 2>/dev/null || true
  local i
  for i in $(seq 1 30); do
    if [ -z "$(chrome_pids)" ]; then
      echo "CHROME_STOPPED_CLEAN"
      clear_stale_locks
      mark_clean_exit_prefs
      return 0
    fi
    sleep 0.5
  done
  echo "CHROME_FORCE_KILL"
  pkill -KILL -f "chrome.*--user-data-dir=${PROFILE}" 2>/dev/null || true
  sleep 0.5
  clear_stale_locks
  mark_clean_exit_prefs
  if [ -z "$(chrome_pids)" ]; then
    echo "CHROME_STOPPED_FORCED"
    return 0
  fi
  echo "CHROME_STOP_FAIL"
  return 1
}

cmd="${1:-}"
shift || true
case "$cmd" in
  start) start_chrome "$@" ;;
  stop) stop_chrome ;;
  status)
    echo "DISPLAY=$DISPLAY"
    echo "PIDS=$(chrome_pids | tr '\n' ' ')"
    ls -la "$PROFILE"/SingletonLock 2>/dev/null || echo "NO_LOCK"
    xwininfo -root -tree 2>/dev/null | grep -F 'Google Chrome' | head -3 || echo "NO_WINDOW"
    ;;
  clear-stale) clear_stale_locks; mark_clean_exit_prefs ;;
  *) echo "usage: $0 start|stop|status|clear-stale [chrome args]"; exit 2 ;;
esac