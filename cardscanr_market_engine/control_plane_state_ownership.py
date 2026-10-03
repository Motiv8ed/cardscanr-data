#!/usr/bin/env python3
"""Document and assert the canonical control-plane state writer domain.

Production topology (CardScanR eBay AU linux_x11 probes):

- Windows host Python runs MarketPriceJobRunner, ebay_availability, marketplace_ops,
  and control_plane_incidents mutations against reports/runtime/*.json.
- WSL (Ubuntu) is invoked only for X11 Chrome/CDP navigation helpers
  (linux_x11_ebay_nav._wsl_python / tools/linux_x11_ebay_*.py). Those scripts do
  not call save_availability / save_ops_state / register_incident.
- Therefore same-OS Windows FileLock is sufficient for canonical JSON mutual
  exclusion. Shared cross-OS mutation of the same files is not part of the
  production writer set.

If a future design allows WSL Python to mutate the same JSON paths, FileLock
across Win32/WSL must be reassessed or ownership redesigned to a single writer.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Canonical runtime state files (relative to repo root / REPORTS_DIR).
CANONICAL_STATE_BASENAMES = (
    "marketplace_ops_state.json",
    "ebay_availability_state.json",
    "control_plane_incidents.json",
)

# Production writer domain for these files.
CANONICAL_STATE_WRITER_DOMAIN = "windows_host"

# WSL role relative to control-plane JSON.
WSL_ROLE = "x11_chrome_cdp_navigation_only"


def detect_process_domain() -> str:
    """Classify the current process OS domain for state-writing purposes."""
    if sys.platform.startswith("win"):
        return "windows_host"
    # Linux: treat /mnt/<drive>/ paths as WSL-mounted Windows filesystem.
    cwd = Path.cwd().as_posix().lower()
    if cwd.startswith("/mnt/") or "microsoft" in (os.uname().release.lower() if hasattr(os, "uname") else ""):
        return "wsl_linux"
    return "linux_native"


def assert_canonical_state_writer_domain_allowed() -> None:
    """Optional hard gate: refuse mutations when CARDSCANR_STATE_WRITER_DOMAIN is set.

    Default production does not set the env; Windows host writers proceed.
    Set CARDSCANR_STATE_WRITER_DOMAIN=windows_host to fail closed if a WSL
    process somehow imports mutation APIs against the shared mount.
    """
    required = os.getenv("CARDSCANR_STATE_WRITER_DOMAIN", "").strip()
    if not required:
        return
    current = detect_process_domain()
    if required == "windows_host" and current != "windows_host":
        raise RuntimeError(
            f"canonical_state_writer_refused:required={required} current={current} "
            f"(WSL must not mutate control-plane JSON)"
        )


def topology_summary() -> dict:
    return {
        "canonicalStateWriterDomain": CANONICAL_STATE_WRITER_DOMAIN,
        "wslRole": WSL_ROLE,
        "currentProcessDomain": detect_process_domain(),
        "canonicalStateBasenames": list(CANONICAL_STATE_BASENAMES),
        "fileLockValidity": (
            "same_os_windows_filelock_sufficient_when_windows_is_sole_canonical_writer"
        ),
        "sharedCrossOsMutation": False,
        "evidence": [
            "tools/linux_x11_single_probe_ready.py runs MarketPriceJobRunner on host Python",
            "cardscanr_market_engine/providers/linux_x11_ebay_nav.py shells to WSL for GUI only",
            "WSL tools/linux_x11_ebay_search.py and sold helpers do not call save_availability/save_ops_state",
        ],
    }


__all__ = [
    "CANONICAL_STATE_BASENAMES",
    "CANONICAL_STATE_WRITER_DOMAIN",
    "WSL_ROLE",
    "assert_canonical_state_writer_domain_allowed",
    "detect_process_domain",
    "topology_summary",
]
