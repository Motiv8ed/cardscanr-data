#!/usr/bin/env python3
"""Focused offline tests for local Xvfb/Chrome/CDP runtime readiness."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.ebay_availability import EbayAvailabilitySnapshot, save_availability
from cardscanr_market_engine.ebay_browser_work_gate import evaluate_ebay_browser_work_gate
from cardscanr_market_engine.local_browser_runtime import (
    LocalBrowserRuntimeStatus,
    assert_safe_chrome_start_url,
    ensure_xvfb,
    probe_local_browser_runtime,
)
from cardscanr_market_engine.marketplace_ops_state import save_ops_state
from cardscanr_market_engine.control_plane_incidents import _save_ledger
from cardscanr_market_engine.providers.linux_x11_ebay_nav import ensure_chrome_with_cdp


class LocalRuntimeApiTests(unittest.TestCase):
    def test_01_probe_not_ready_when_xvfb_down(self) -> None:
        with mock.patch(
            "cardscanr_market_engine.local_browser_runtime._wsl_bash"
        ) as wsl:
            wsl.return_value = mock.Mock(
                returncode=0,
                stdout="WSL_OK\nXKBCOMP_PREFIX_OK\nXKBCOMP_SYSTEM_MISSING\nX11_UNIX_MODE=777\nLOCK_ABSENT\nXVFB_PROC_ABSENT\nXDPY_DOWN\n",
                stderr="",
            )
            st = probe_local_browser_runtime(cdp_port=19999)
        self.assertTrue(st.wsl_available)
        self.assertFalse(st.xvfb_ready)
        self.assertFalse(st.ready)

    def test_02_probe_ready_component_when_xdpy_ok(self) -> None:
        with mock.patch(
            "cardscanr_market_engine.local_browser_runtime._wsl_bash"
        ) as wsl:
            wsl.return_value = mock.Mock(
                returncode=0,
                stdout=(
                    "WSL_OK\nXKBCOMP_PREFIX_OK\nXKBCOMP_SYSTEM_MISSING\nX11_UNIX_MODE=777\n"
                    "LOCK_PRESENT\nXVFB_PROC_PRESENT\nXVFB_PROC 453 Xvfb :99 -screen 0\n"
                    "PIDFILE=453\nXDPY_OK\nXDPY dimensions:    1920x1080 pixels\n"
                ),
                stderr="",
            )
            with mock.patch(
                "cardscanr_market_engine.local_browser_runtime.urllib.request.urlopen",
                side_effect=OSError("cdp down"),
            ):
                st = probe_local_browser_runtime(cdp_port=19999)
        self.assertTrue(st.xvfb_ready)
        self.assertEqual(st.xvfb_pid, 453)
        self.assertFalse(st.cdp_ready)
        self.assertFalse(st.ready)  # CDP still required for full ready

    def test_03_stale_lock_handling_documented_in_ensure_script(self) -> None:
        script = (ROOT / "tools" / "cardscanr_ensure_xvfb.sh").read_text(encoding="utf-8")
        self.assertIn("CLEARED_STALE_LOCK", script)
        self.assertIn("LIVE_LOCK_KEPT", script)
        self.assertIn("X0", script)  # must mention never touching X0 / WSLg

    def test_04_missing_xkbcomp_explicit_reason(self) -> None:
        with mock.patch(
            "cardscanr_market_engine.local_browser_runtime.probe_local_browser_runtime"
        ) as probe:
            probe.side_effect = [
                LocalBrowserRuntimeStatus(wsl_available=True, xvfb_ready=False, display=":99"),
                LocalBrowserRuntimeStatus(wsl_available=True, xvfb_ready=False, display=":99"),
            ]
            with mock.patch("cardscanr_market_engine.local_browser_runtime.subprocess.run") as run:
                run.return_value = mock.Mock(
                    returncode=1,
                    stdout='{"ok": false, "reason": "xkbcomp_missing_at_usr_bin", "pid": null}',
                    stderr="",
                )
                result = ensure_xvfb(display=":99")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "xkbcomp_missing_at_usr_bin")

    def test_05_safe_start_url_accepts_about_blank(self) -> None:
        self.assertEqual(assert_safe_chrome_start_url("about:blank"), "about:blank")
        self.assertEqual(assert_safe_chrome_start_url(""), "about:blank")

    def test_06_safe_start_url_rejects_ebay(self) -> None:
        with self.assertRaises(RuntimeError):
            assert_safe_chrome_start_url("https://www.ebay.com.au/")
        with self.assertRaises(RuntimeError):
            ensure_chrome_with_cdp(start_url="https://www.ebay.com.au/")

    def test_07_bootstrap_does_not_touch_live_nav_counter(self) -> None:
        # Helpers must not increment reliability live-navigation accounting.
        src = (ROOT / "cardscanr_market_engine" / "local_browser_runtime.py").read_text(encoding="utf-8")
        sh = (ROOT / "tools" / "cardscanr_ensure_xvfb.sh").read_text(encoding="utf-8")
        for blob in (src, sh):
            self.assertNotIn("liveNavigationStartedCount", blob)
            self.assertNotIn("live_navigation_started", blob)
            self.assertNotRegex(blob, r"liveNavigationStarted\s*=")

    def test_08_ebay_target_means_not_safe_ready(self) -> None:
        st = LocalBrowserRuntimeStatus(
            xvfb_ready=True,
            cdp_ready=True,
            ebay_targets=["https://www.ebay.com.au/"],
        )
        st.ready = bool(st.xvfb_ready and st.cdp_ready and not st.ebay_targets)
        self.assertFalse(st.ready)

    def test_09_blank_only_target_acceptable(self) -> None:
        st = LocalBrowserRuntimeStatus(
            xvfb_ready=True,
            cdp_ready=True,
            ebay_targets=[],
        )
        st.ready = bool(st.xvfb_ready and st.cdp_ready and not st.ebay_targets)
        self.assertTrue(st.ready)

    def test_10_runtime_independent_of_marketplace_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            avail = root / "avail.json"
            ops = root / "ops.json"
            inc = root / "inc.json"
            save_availability(
                EbayAvailabilitySnapshot(state="HEALTHY", market="AU"),
                path=avail,
                force=True,
            )
            save_ops_state({"version": 1, "markets": {}}, path=ops, force=True)
            _save_ledger({"version": 1, "incidents": {}}, path=inc, force=True)
            market_ok = evaluate_ebay_browser_work_gate(
                market="AU",
                availability_path=avail,
                ops_path=ops,
                incidents_path=inc,
            )
            self.assertTrue(market_ok.allowed)
            market_need_rt = evaluate_ebay_browser_work_gate(
                market="AU",
                availability_path=avail,
                ops_path=ops,
                incidents_path=inc,
                require_local_runtime_ready=True,
                local_runtime_ready=False,
            )
            self.assertFalse(market_need_rt.allowed)
            self.assertIn("LOCAL_BROWSER_RUNTIME_NOT_READY", market_need_rt.reason_codes)

    def test_11_local_bootstrap_does_not_mutate_control_plane(self) -> None:
        # ensure_xvfb must not import/write availability/ops/incidents.
        src = (ROOT / "cardscanr_market_engine" / "local_browser_runtime.py").read_text(encoding="utf-8")
        self.assertNotIn("save_availability", src)
        self.assertNotIn("save_ops_state", src)
        self.assertNotIn("register_incident", src)
        self.assertNotIn("control_plane_incidents", src)


class ChromeControllerContractTests(unittest.TestCase):
    def test_chrome_ctl_has_session_restore_guards(self) -> None:
        ctl = (ROOT / "tools" / "cardscanr_chrome_ctl.sh").read_text(encoding="utf-8")
        self.assertIn("disable-restore-session-state", ctl)
        self.assertIn("suppress_session_restore_prefs", ctl)
        self.assertIn("restore_on_startup", ctl)


if __name__ == "__main__":
    unittest.main()
