from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "tools" / "run_pokewallet_missing_price_worker.py"

spec = importlib.util.spec_from_file_location("missing_price_worker", MODULE_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError(f"Unable to load module from {MODULE_PATH}")

worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class MissingPriceWorkerValidationSmokeTests(unittest.TestCase):
    def test_command_return_code_keeps_zero_success(self) -> None:
        self.assertEqual(worker.command_return_code({"returnCode": 0}), 0)

    def test_first_failed_command_ignores_all_zero_results(self) -> None:
        results = [
            {
                "command": "python tools/validate_cache.py",
                "returnCode": 0,
                "stdoutTail": ["ok"],
                "stderrTail": [],
            }
        ]
        self.assertIsNone(worker.first_failed_command(results))

    def test_first_failed_command_reports_details(self) -> None:
        results = [
            {
                "command": "python tools/validate_cache.py",
                "returnCode": 0,
                "stdoutTail": ["ok"],
                "stderrTail": [],
            },
            {
                "command": "python tools/report_data_health.py",
                "returnCode": 2,
                "stdoutTail": ["line-a", "line-b"],
                "stderrTail": ["trace"],
            },
        ]
        failed = worker.first_failed_command(results)
        self.assertIsNotNone(failed)
        self.assertEqual(failed["command"], "python tools/report_data_health.py")
        self.assertEqual(failed["returnCode"], 2)
        self.assertEqual(failed["stdoutTail"], ["line-a", "line-b"])
        self.assertEqual(failed["stderrTail"], ["trace"])


if __name__ == "__main__":
    unittest.main()
