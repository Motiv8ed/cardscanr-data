from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cardscanr_market_engine import gaming_resource_pause as grp


class GamingResourcePauseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        root = Path(self._tmpdir.name)
        self.state_path = root / "gaming_resource_pause.json"
        self.status_path = root / "gaming_resource_pause_status.json"
        self.events_path = root / "gaming_resource_pause_events.jsonl"
        self.inject_path = root / "gaming_fortnite_inject.flag"
        self.patches = [
            mock.patch.object(grp, "STATE_PATH", self.state_path),
            mock.patch.object(grp, "STATUS_PATH", self.status_path),
            mock.patch.object(grp, "EVENT_LOG_PATH", self.events_path),
            mock.patch.object(grp, "INJECT_FLAG_PATH", self.inject_path),
            mock.patch.dict(
                os.environ,
                {
                    "CARDSCANR_GAMING_RESUME_DELAY_SECONDS": "1",
                    "CARDSCANR_GAMING_POLL_INTERVAL_SECONDS": "1",
                    "CARDSCANR_GAMING_PAUSE_ALLOW_INJECT": "false",
                    "PAUSE_PRICING_MANUALLY": "false",
                    "CARDSCANR_SAVE_VM_WHILE_GAMING": "false",
                },
                clear=False,
            ),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self._tmpdir.cleanup()

    def test_launcher_alone_does_not_count(self) -> None:
        def probe() -> dict:
            return {
                "detected": False,
                "method": "test",
                "processes": [],
                "canonicalExecutable": grp.DEFAULT_FORTNITE_PROCESS_NAMES[0],
                "antiCheatOrGameModification": "NONE",
            }

        ctrl = grp.GamingResourcePauseController(process_probe=probe)
        self.assertFalse(ctrl.should_block_new_jobs())

    def test_fortnite_shipping_pauses_and_resumes(self) -> None:
        detected = {"flag": False}

        def probe() -> dict:
            return {
                "detected": detected["flag"],
                "method": "test",
                "processes": [{"pid": 1, "name": "FortniteClient-Win64-Shipping.exe"}]
                if detected["flag"]
                else [],
                "canonicalExecutable": grp.DEFAULT_FORTNITE_PROCESS_NAMES[0],
                "antiCheatOrGameModification": "NONE",
            }

        ctrl = grp.GamingResourcePauseController(process_probe=probe)
        self.assertFalse(ctrl.should_block_new_jobs())
        detected["flag"] = True
        ctrl.tick()
        self.assertTrue(ctrl.should_block_new_jobs())
        self.assertEqual(ctrl.block_reason(), "GAMING_RESOURCE_PAUSE")
        detected["flag"] = False
        ctrl.tick()
        self.assertTrue(ctrl.should_block_new_jobs())  # settle delay
        # Force eligible now
        ctrl.state.resume_eligible_at = "2000-01-01T00:00:00Z"
        ctrl.tick()
        self.assertFalse(ctrl.should_block_new_jobs())
        events = self.events_path.read_text(encoding="utf-8").strip().splitlines()
        names = [json.loads(line)["event"] for line in events]
        self.assertIn("FORTNITE_DETECTED", names)
        self.assertIn("OWNED_PRICING_PAUSED", names)
        self.assertIn("FORTNITE_EXITED", names)
        self.assertIn("OWNED_PRICING_RESUMED", names)

    def test_manual_pause_blocks_resume(self) -> None:
        detected = {"flag": False}

        def probe() -> dict:
            return {
                "detected": detected["flag"],
                "method": "test",
                "processes": [],
                "canonicalExecutable": grp.DEFAULT_FORTNITE_PROCESS_NAMES[0],
                "antiCheatOrGameModification": "NONE",
            }

        with mock.patch.dict(os.environ, {"PAUSE_PRICING_MANUALLY": "true"}):
            ctrl = grp.GamingResourcePauseController(process_probe=probe)
            self.assertTrue(ctrl.should_block_new_jobs())
            self.assertEqual(ctrl.block_reason(), "PAUSE_PRICING_MANUALLY")


if __name__ == "__main__":
    unittest.main()
