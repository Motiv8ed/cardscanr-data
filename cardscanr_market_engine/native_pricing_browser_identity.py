"""Native Windows Chrome identity checks; pure, no process or network actions.

A caller must obtain process, session and socket evidence from the OS, not
from an untrusted CDP response. This module never authorizes marketplace work.
"""
from __future__ import annotations

import ntpath
from ipaddress import ip_address


def _norm(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(path))


def _one_flag(argv: list[str], name: str) -> str | None:
    values = []
    for index, arg in enumerate(argv):
        if arg == name:
            if index + 1 >= len(argv) or argv[index + 1].startswith("--"):
                return None
            values.append(argv[index + 1])
        elif arg.startswith(name + "="):
            values.append(arg[len(name) + 1:])
    return values[0] if len(values) == 1 else None


def verify_chrome_identity(*, listeners: list[dict], processes: list[dict],
                           profile: str, chrome_executable: str,
                           session_id: int, port: int = 9232) -> dict:
    """Check one exact same-session Chrome listener and profile.

    The fixed native candidate uses port 9232. The current WSL 9444 and
    Bridge/Studio 9231/9230 must never be adopted by this candidate.
    """
    def denied(reason):
        return {"verified": False, "reason": reason,
                "marketplace_work_authorized": False}
    if type(port) is not int or port != 9232:
        return denied("WRONG_PORT")
    if type(session_id) is not int or session_id <= 0:
        return denied("NO_INTERACTIVE_SESSION")
    if not isinstance(profile, str) or not ntpath.isabs(profile):
        return denied("PROFILE_NOT_ABSOLUTE")
    if not isinstance(chrome_executable, str) or not ntpath.isabs(chrome_executable):
        return denied("EXECUTABLE_NOT_ABSOLUTE")
    if not isinstance(listeners, list) or not listeners:
        return denied("NO_LISTENER")
    if any(not isinstance(row, dict) or row.get("port") != port for row in listeners):
        return denied("INVALID_LISTENER_INVENTORY")
    try:
        if not all(ip_address(row["address"]).is_loopback for row in listeners):
            return denied("NON_LOOPBACK_LISTENER")
    except (KeyError, TypeError, ValueError):
        return denied("INVALID_LISTENER_ADDRESS")
    if any(type(row.get("pid")) is not int or row["pid"] <= 0 for row in listeners):
        return denied("INVALID_LISTENER_PID")
    pids = {row["pid"] for row in listeners}
    if len(pids) != 1:
        return denied("MULTIPLE_LISTENER_OWNERS")
    pid = next(iter(pids))
    candidates = [row for row in processes if isinstance(row, dict) and row.get("pid") == pid]
    if len(candidates) != 1:
        return denied("UNKNOWN_PROCESS")
    row = candidates[0]
    if row.get("session") != session_id:
        return denied("WRONG_WINDOWS_SESSION")
    if not isinstance(row.get("executable"), str) or _norm(row["executable"]) != _norm(chrome_executable):
        return denied("WRONG_EXECUTABLE")
    argv = row.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) for arg in argv):
        return denied("UNKNOWN_PROCESS_ARGUMENTS")
    if any(arg.startswith("--remote-allow-origins") or arg.startswith("--headless")
           for arg in argv):
        return denied("UNAPPROVED_BROWSER_FLAGS")
    actual_profile = _one_flag(argv, "--user-data-dir")
    if not actual_profile or _norm(actual_profile) != _norm(profile):
        return denied("WRONG_PROFILE")
    if _one_flag(argv, "--remote-debugging-port") != str(port):
        return denied("WRONG_DEBUG_PORT")
    if _one_flag(argv, "--remote-debugging-address") != "127.0.0.1":
        return denied("WRONG_DEBUG_ADDRESS")
    return {"verified": True, "pid": pid, "reason": "EXACT_OWNER",
            "marketplace_work_authorized": False}
