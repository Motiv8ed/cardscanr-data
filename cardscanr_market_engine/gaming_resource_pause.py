"""Host-side Fortnite-aware pricing pause (control plane only).

Detects Fortnite via normal Windows process enumeration. Does not touch the game,
anti-cheat, memory, windows, or input. Persists pause state so workers refuse NEW
pricing jobs while preserving the queue and last-good prices.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable

from .config import REPORTS_DIR

STATE_PATH = REPORTS_DIR / "runtime" / "gaming_resource_pause.json"
STATUS_PATH = REPORTS_DIR / "runtime" / "gaming_resource_pause_status.json"
EVENT_LOG_PATH = REPORTS_DIR / "runtime" / "gaming_resource_pause_events.jsonl"
INJECT_FLAG_PATH = REPORTS_DIR / "runtime" / "gaming_fortnite_inject.flag"

# Primary game client only — never Epic Games Launcher alone.
DEFAULT_FORTNITE_PROCESS_NAMES = (
    "FortniteClient-Win64-Shipping.exe",
)
# Observed on this host alongside Shipping; NOT sufficient alone.
RELATED_BUT_NOT_TRIGGER = (
    "FortniteClient-Win64-Shipping_EAC_EOS.exe",
    "EpicGamesLauncher.exe",
)

WORKER_RUNNING = "RUNNING"
WORKER_DRAINING = "DRAINING_CURRENT_JOB"
WORKER_PAUSED = "PAUSED_FOR_GAMING"

_lock = threading.Lock()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(value: datetime | None = None) -> str:
    current = value or _utc_now()
    return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def fortnite_process_names() -> tuple[str, ...]:
    raw = os.getenv("CARDSCANR_FORTNITE_PROCESS_NAMES", "").strip()
    if raw:
        names = tuple(item.strip() for item in raw.split(",") if item.strip())
        return names or DEFAULT_FORTNITE_PROCESS_NAMES
    return DEFAULT_FORTNITE_PROCESS_NAMES


def resume_delay_seconds() -> int:
    return _parse_positive_int("CARDSCANR_GAMING_RESUME_DELAY_SECONDS", 60)


def poll_interval_seconds() -> int:
    return _parse_positive_int("CARDSCANR_GAMING_POLL_INTERVAL_SECONDS", 20)


def save_vm_while_gaming_enabled() -> bool:
    return _parse_bool("CARDSCANR_SAVE_VM_WHILE_GAMING", False)


def pricing_vm_name() -> str:
    return os.getenv("CARDSCANR_PRICING_VM_NAME", "CardScanR-Pricing").strip() or "CardScanR-Pricing"


def manual_pause_requested() -> bool:
    return _parse_bool("PAUSE_PRICING_MANUALLY", False) or _parse_bool(
        "CARDSCANR_PAUSE_PRICING_MANUALLY", False
    )


def allow_test_inject() -> bool:
    return _parse_bool("CARDSCANR_GAMING_PAUSE_ALLOW_INJECT", False)


def append_event(event: str, **extra: Any) -> None:
    REPORTS_DIR.joinpath("runtime").mkdir(parents=True, exist_ok=True)
    payload = {"ts": _utc_iso(), "event": event, **extra}
    with EVENT_LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def list_matching_processes(names: tuple[str, ...]) -> list[dict[str, Any]]:
    """Enumerate processes by image name (read-only). No game interaction."""
    wanted = {n.lower() for n in names}
    found: list[dict[str, Any]] = []
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        TH32CS_SNAPPROCESS = 0x00000002

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == wintypes.HANDLE(-1).value:
            raise OSError("CreateToolhelp32Snapshot failed")
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                name = entry.szExeFile or ""
                if name.lower() in wanted:
                    found.append({"pid": int(entry.th32ProcessID), "name": name})
                ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snap)
    except Exception:
        # Fallback: PowerShell-free ctypes failed — try psutil if present, else empty.
        try:
            import psutil  # type: ignore

            for proc in psutil.process_iter(["pid", "name"]):
                name = str(proc.info.get("name") or "")
                if name.lower() in wanted:
                    found.append({"pid": int(proc.info["pid"]), "name": name})
        except Exception:
            pass
    return found


def fortnite_detected(*, allow_inject: bool | None = None) -> dict[str, Any]:
    """Return detection payload. Epic Launcher alone never counts as gaming."""
    inject_ok = allow_test_inject() if allow_inject is None else allow_inject
    if inject_ok and INJECT_FLAG_PATH.exists():
        return {
            "detected": True,
            "method": "test_inject_flag",
            "processes": [{"pid": 0, "name": "INJECT"}],
            "canonicalExecutable": DEFAULT_FORTNITE_PROCESS_NAMES[0],
            "antiCheatOrGameModification": "NONE",
        }
    matches = list_matching_processes(fortnite_process_names())
    return {
        "detected": bool(matches),
        "method": "CreateToolhelp32Snapshot_process_enumeration",
        "processes": matches,
        "canonicalExecutable": DEFAULT_FORTNITE_PROCESS_NAMES[0],
        "relatedNotTrigger": list(RELATED_BUT_NOT_TRIGGER),
        "antiCheatOrGameModification": "NONE",
    }


@dataclass
class GamingPauseState:
    gaming_resource_pause: bool = False
    fortnite_detected: bool = False
    manual_pause: bool = False
    worker_state: str = WORKER_RUNNING
    pause_started_at: str | None = None
    fortnite_exited_at: str | None = None
    resume_eligible_at: str | None = None
    current_card: str | None = None
    queued_jobs_hint: int | None = None
    vm_state: str = "unknown"
    save_vm_while_gaming: bool = False
    last_event: str | None = None
    updated_at: str | None = None
    transitions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_state() -> GamingPauseState:
    if not STATE_PATH.exists():
        return GamingPauseState(updated_at=_utc_iso())
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return GamingPauseState(updated_at=_utc_iso())
    if not isinstance(raw, dict):
        return GamingPauseState(updated_at=_utc_iso())
    return GamingPauseState(
        gaming_resource_pause=bool(raw.get("gaming_resource_pause")),
        fortnite_detected=bool(raw.get("fortnite_detected")),
        manual_pause=bool(raw.get("manual_pause")),
        worker_state=str(raw.get("worker_state") or WORKER_RUNNING),
        pause_started_at=raw.get("pause_started_at"),
        fortnite_exited_at=raw.get("fortnite_exited_at"),
        resume_eligible_at=raw.get("resume_eligible_at"),
        current_card=raw.get("current_card"),
        queued_jobs_hint=raw.get("queued_jobs_hint"),
        vm_state=str(raw.get("vm_state") or "unknown"),
        save_vm_while_gaming=bool(raw.get("save_vm_while_gaming")),
        last_event=raw.get("last_event"),
        updated_at=raw.get("updated_at"),
        transitions=list(raw.get("transitions") or [])[-40:],
    )


def save_state(state: GamingPauseState) -> None:
    REPORTS_DIR.joinpath("runtime").mkdir(parents=True, exist_ok=True)
    state.updated_at = _utc_iso()
    state.save_vm_while_gaming = save_vm_while_gaming_enabled()
    STATE_PATH.write_text(json.dumps(state.to_dict(), indent=2) + "\n", encoding="utf-8")
    STATUS_PATH.write_text(json.dumps(status_payload(state), indent=2) + "\n", encoding="utf-8")


def status_payload(state: GamingPauseState) -> dict[str, Any]:
    return {
        "FortniteDetected": "YES" if state.fortnite_detected else "NO",
        "gamingPauseActive": "YES" if state.gaming_resource_pause or state.manual_pause else "NO",
        "GAMING_RESOURCE_PAUSE": bool(state.gaming_resource_pause or state.manual_pause),
        "PAUSE_PRICING_MANUALLY": bool(state.manual_pause or manual_pause_requested()),
        "pricingWorkerState": state.worker_state,
        "queuedJobs": state.queued_jobs_hint,
        "currentCard": state.current_card,
        "pauseStarted": state.pause_started_at,
        "resumeEligibleAt": state.resume_eligible_at,
        "vmState": state.vm_state,
        "saveVmWhileGamingEnabled": save_vm_while_gaming_enabled(),
        "pollIntervalSeconds": poll_interval_seconds(),
        "resumeDelaySeconds": resume_delay_seconds(),
        "canonicalExecutable": DEFAULT_FORTNITE_PROCESS_NAMES[0],
        "detectionMethod": "Windows process enumeration only",
        "antiCheatOrGameModification": "NONE",
        "updatedAt": state.updated_at,
        "lastEvent": state.last_event,
    }


def _note_transition(state: GamingPauseState, event: str, **extra: Any) -> None:
    state.last_event = event
    state.transitions.append({"ts": _utc_iso(), "event": event, **extra})
    state.transitions = state.transitions[-40:]
    append_event(event, **extra)


def maybe_save_or_start_vm(*, pause: bool) -> str:
    """Optional deep gaming mode. Disabled by default. Never raises into worker."""
    if not save_vm_while_gaming_enabled():
        return "vm_idle_allowed_save_disabled"
    vm = pricing_vm_name()
    try:
        import subprocess

        if pause:
            subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f"if (Get-VM -Name '{vm}' -ErrorAction SilentlyContinue) {{ Save-VM -Name '{vm}' }}",
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            return f"save_vm_requested:{vm}"
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"if (Get-VM -Name '{vm}' -ErrorAction SilentlyContinue) {{ Start-VM -Name '{vm}' }}",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        return f"start_vm_requested:{vm}"
    except Exception as exc:
        return f"vm_action_error:{type(exc).__name__}"


class GamingResourcePauseController:
    """Tick detection and expose whether NEW pricing jobs may start."""

    def __init__(self, *, process_probe: Callable[[], dict[str, Any]] | None = None) -> None:
        self._probe = process_probe or fortnite_detected
        self.state = load_state()
        # Boot / process start: if Fortnite already running, begin paused (no brief batch).
        detection = self._probe()
        self.state.manual_pause = manual_pause_requested()
        if detection.get("detected") and not self.state.gaming_resource_pause:
            self._enter_pause(reason="startup_fortnite_already_running", detection=detection)
        elif self.state.manual_pause and self.state.worker_state != WORKER_PAUSED:
            self.state.worker_state = WORKER_PAUSED
            self.state.gaming_resource_pause = True
            if not self.state.pause_started_at:
                self.state.pause_started_at = _utc_iso()
            _note_transition(self.state, "OWNED_PRICING_PAUSE_REQUESTED", reason="manual_override_startup")
            save_state(self.state)
        else:
            self.state.fortnite_detected = bool(detection.get("detected"))
            save_state(self.state)

    def _enter_pause(self, *, reason: str, detection: dict[str, Any]) -> None:
        already = self.state.gaming_resource_pause
        self.state.fortnite_detected = True
        self.state.gaming_resource_pause = True
        if not already:
            self.state.pause_started_at = _utc_iso()
            self.state.fortnite_exited_at = None
            self.state.resume_eligible_at = None
            _note_transition(self.state, "FORTNITE_DETECTED", reason=reason, processes=detection.get("processes"))
            _note_transition(self.state, "OWNED_PRICING_PAUSE_REQUESTED", reason=reason)
            if self.state.worker_state == WORKER_RUNNING and not self.state.current_card:
                self.state.worker_state = WORKER_PAUSED
                _note_transition(self.state, "OWNED_PRICING_PAUSED", reason=reason)
            elif self.state.current_card:
                self.state.worker_state = WORKER_DRAINING
            else:
                self.state.worker_state = WORKER_PAUSED
                _note_transition(self.state, "OWNED_PRICING_PAUSED", reason=reason)
            vm_note = maybe_save_or_start_vm(pause=True)
            self.state.vm_state = "running_idle_worker_paused" if not save_vm_while_gaming_enabled() else vm_note
        save_state(self.state)

    def mark_job_started(self, card: str | None) -> None:
        with _lock:
            self.state.current_card = card
            if self.state.gaming_resource_pause or self.state.manual_pause:
                self.state.worker_state = WORKER_DRAINING
            else:
                self.state.worker_state = WORKER_RUNNING
            save_state(self.state)

    def mark_job_finished(self) -> None:
        with _lock:
            self.state.current_card = None
            if self.state.gaming_resource_pause or self.state.manual_pause or manual_pause_requested():
                self.state.worker_state = WORKER_PAUSED
                _note_transition(self.state, "OWNED_PRICING_PAUSED", reason="safe_card_boundary")
            else:
                self.state.worker_state = WORKER_RUNNING
            save_state(self.state)

    def set_queued_jobs_hint(self, count: int | None) -> None:
        with _lock:
            self.state.queued_jobs_hint = count
            save_state(self.state)

    def tick(self) -> GamingPauseState:
        with _lock:
            detection = self._probe()
            detected = bool(detection.get("detected"))
            self.state.manual_pause = manual_pause_requested()
            prev = self.state.fortnite_detected

            if detected and not prev:
                self._enter_pause(reason="transition_not_running_to_running", detection=detection)
                return self.state

            if detected:
                self.state.fortnite_detected = True
                self.state.gaming_resource_pause = True
                # Cancel any pending resume window while game is back.
                self.state.fortnite_exited_at = None
                self.state.resume_eligible_at = None
                if self.state.worker_state == WORKER_RUNNING and not self.state.current_card:
                    self.state.worker_state = WORKER_PAUSED
                save_state(self.state)
                return self.state

            # Fortnite absent
            self.state.fortnite_detected = False
            # Normal exit edge, or stuck pause where the edge was missed
            # (pause=true, fortnite_detected already false, resumeEligibleAt null).
            missed_exit = (
                self.state.gaming_resource_pause
                and not detected
                and not self.state.resume_eligible_at
                and not self.state.manual_pause
            )
            if (prev and not detected) or missed_exit:
                self.state.fortnite_exited_at = _utc_iso()
                delay = resume_delay_seconds()
                eligible = _utc_now().timestamp() + delay
                self.state.resume_eligible_at = _utc_iso(datetime.fromtimestamp(eligible, tz=timezone.utc))
                _note_transition(
                    self.state,
                    "FORTNITE_EXITED",
                    reason="missed_exit_recovery" if missed_exit and not prev else "transition_running_to_not_running",
                )
                _note_transition(self.state, "OWNED_PRICING_RESUME_DELAY", delaySeconds=delay)
                save_state(self.state)
                return self.state

            # Manual pause overrides automatic resume.
            if self.state.manual_pause:
                self.state.gaming_resource_pause = True
                self.state.worker_state = (
                    WORKER_DRAINING if self.state.current_card else WORKER_PAUSED
                )
                save_state(self.state)
                return self.state

            if self.state.gaming_resource_pause and self.state.resume_eligible_at:
                eligible = _parse_utc(self.state.resume_eligible_at)
                if eligible and _utc_now() >= eligible:
                    # Re-confirm still absent
                    recheck = self._probe()
                    if recheck.get("detected"):
                        self._enter_pause(reason="resume_aborted_fortnite_returned", detection=recheck)
                        return self.state
                    self.state.gaming_resource_pause = False
                    self.state.resume_eligible_at = None
                    self.state.fortnite_exited_at = None
                    self.state.worker_state = WORKER_RUNNING
                    self.state.vm_state = maybe_save_or_start_vm(pause=False) if save_vm_while_gaming_enabled() else "running_idle_or_ready"
                    _note_transition(self.state, "OWNED_PRICING_RESUMED")
                    save_state(self.state)
                    return self.state

            save_state(self.state)
            return self.state

    def should_block_new_jobs(self) -> bool:
        self.tick()
        if manual_pause_requested() or self.state.manual_pause:
            return True
        return bool(self.state.gaming_resource_pause)

    def block_reason(self) -> str | None:
        if manual_pause_requested() or self.state.manual_pause:
            return "PAUSE_PRICING_MANUALLY"
        if self.state.gaming_resource_pause:
            return "GAMING_RESOURCE_PAUSE"
        return None

    def status(self) -> dict[str, Any]:
        self.tick()
        return status_payload(self.state)


def sleep_while_paused(
    controller: GamingResourcePauseController,
    *,
    logger: Callable[[str], None] | None = None,
    max_wait_seconds: float | None = None,
) -> None:
    """Block the worker loop until pause clears (queue untouched)."""
    log = logger or (lambda _msg: None)
    started = time.monotonic()
    while controller.should_block_new_jobs():
        if max_wait_seconds is not None and (time.monotonic() - started) >= max_wait_seconds:
            return
        status = controller.status()
        log(
            f"[gaming-pause] blocked reason={controller.block_reason()} "
            f"worker={status.get('pricingWorkerState')} resumeEligible={status.get('resumeEligibleAt')}"
        )
        time.sleep(min(5, poll_interval_seconds()))
