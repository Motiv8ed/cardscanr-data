"""Fixture-only tests for archived pack manifest presence and safe HEAD probes."""
import json
import unittest
from tools.audit_archived_language_packs import RELEASES, audit, inspect, head


class ArchivedPackTests(unittest.TestCase):
    @staticmethod
    def fixture(_):
        doc = {"catalogueReleaseId": "canary-fixture",
               "packs": [
                  {"kind": "language", "packId": "ko", "languages": ["ko"], "recordCount": 239,
                   "compressedDatabaseUrl": "https://assets.cardscanr.com/v2/catalog/pokemon/packs/test/ko.sqlite.gz"},
                  {"kind": "language", "packId": "en", "languages": ["en"], "recordCount": 20000,
                   "compressedDatabaseUrl": "https://evil.example.com/private"},
                  {"kind": "core", "packId": "core"}]}
        return doc, json.dumps(doc).encode()

    def test_fixed_endpoints_and_safe_status(self):
        checked = []
        def fake_head(url):
            checked.append(url)
            return {"state": "http_response", "status": 200}
        report = audit(get=self.fixture, check_head=fake_head)
        self.assertEqual(report["classification"], "PASS")
        self.assertEqual(len(report["releases"]), len(RELEASES))
        self.assertEqual(report["production_writes"], 0)
        self.assertFalse(report["release_eligibility_verified"])
        self.assertEqual(len(checked), 4)
        self.assertEqual(report["releases"]["august_production"]["language_packs"][0]["records"], 239)

    def test_external_head_url_rejected_without_network(self):
        rec = head("https://evil.example.com/private")
        self.assertEqual(rec["state"], "rejected_url_outside_allowlist")
        self.assertEqual(head("http://assets.cardscanr.com/thing")["state"],
                         "rejected_url_outside_allowlist")

    def test_missing_one_release_is_partial(self):
        def missing(url):
            if url == RELEASES["august_canary"]:
                raise OSError("missing")
            return self.fixture(url)
        report = audit(get=missing, check_head=lambda url: {"state":"http_response","status":200})
        self.assertEqual(report["classification"], "PARTIAL")
        self.assertEqual(report["failures"][0]["name"], "august_canary")

    def test_invalid_manifest_rejected(self):
        with self.assertRaises(ValueError):
            inspect("bad", {"packs": None}, b"{}")


if __name__ == "__main__":
    unittest.main()
