"""Offline regression tests for PR91's fail-closed Indonesian source pagination.

No network access, image downloads, vault writes, or external source activation.
"""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "tools" / "private_official_indonesian_sv4a_next_urls.py"
spec = importlib.util.spec_from_file_location("sv4a_diagnostics", SOURCE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class FakeCollector:
    requests = []
    def __init__(self, *args, **kwargs):
        pass
    def fetch(self, url):
        self.requests.append(url)
        return SimpleNamespace(status=200, content=b"<html></html>")
    def close(self):
        pass


class PaginationGuardTests(unittest.TestCase):
    def setUp(self):
        FakeCollector.requests = []

    def check_count(self, count, accepted):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "not-created"
            ids = [str(i) for i in range(1, count + 1)]
            with patch.object(mod, "Collector", FakeCollector), patch.object(mod, "parse_card_list", return_value=(ids, None)), patch.object(mod, "parse_card_detail", return_value={"local_name":"Nama","collector_number":"001","image_url":None}):
                if accepted:
                    result = mod.run(out, limit=1, page=19)
                    self.assertEqual(result["source_cards"], 1)
                    self.assertEqual(len(FakeCollector.requests), 2)
                    self.assertTrue((out / "receipt.json").exists())
                else:
                    with self.assertRaisesRegex(ValueError, rf"unexpected_source_list_count: page=19 parsed={count}$"):
                        mod.run(out, limit=1, page=19)
                    self.assertEqual(len(FakeCollector.requests), 1, "no detail requests on invalid list count")
                    self.assertFalse(out.exists(), "no artifact on invalid list count")

    def test_zero_rejected(self):
        self.check_count(0, False)

    def test_fifty_one_rejected(self):
        self.check_count(51, False)

    def test_one_accepted(self):
        self.check_count(1, True)

    def test_fifty_accepted(self):
        self.check_count(50, True)


if __name__ == "__main__":
    unittest.main()
