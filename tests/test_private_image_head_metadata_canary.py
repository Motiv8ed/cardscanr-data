import unittest

from tools.private_image_head_metadata_canary import (
    IMAGE_BASES, low_webp_url, sample_image_references,
)


class ImageHeadMetadataCanaryTests(unittest.TestCase):
    def test_exact_source_urls_only(self):
        self.assertEqual(len(IMAGE_BASES), 9)
        self.assertEqual(len(set(IMAGE_BASES)), 9)
        for url in IMAGE_BASES:
            self.assertEqual(low_webp_url(url), url + "/low.webp")

    def test_disallows_external_or_unverified_asset_origins(self):
        for url in (
            "http://assets.tcgdex.net/th/SV/SV7s/001",
            "https://evil.example/th/SV/SV7s/001",
            "https://assets.tcgdex.net/th/SV/SV7s/002?token=x",
            "https://assets.tcgdex.net/zh-cn/SV/SV7s/001",
            "https://assets.tcgdex.net/th/SV/SV7s/444",
            "https://assets.tcgdex.net/th/SV/SV7s/001/high.webp",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                low_webp_url(url)

    def test_success_does_not_mean_rights(self):
        calls = []
        def fake(url):
            calls.append(url)
            return {"classification": "confirmed_image_metadata", "http_status": 200,
                    "content_type": "image/webp", "declared_bytes": 65535}
        result=sample_image_references(pace_seconds=0,checker=fake)
        self.assertEqual(result["head_requests"], 9)
        self.assertEqual(result["counts"]["confirmed_image_metadata"], 9)
        self.assertEqual(len(calls),9)
        self.assertTrue(all(url.endswith("/low.webp") for url in calls))
        self.assertEqual(result["image_bodies_downloaded"],0)
        self.assertEqual(result["binary_files_saved"],0)
        self.assertFalse(result["artwork_rights_granted"])
        self.assertFalse(result["safe_to_publish"])

    def test_rate_limit_halts_no_retries(self):
        calls=[]
        def limited(url):
            calls.append(url)
            return {"classification": "rate_limited", "http_status":429,
                    "content_type":None,"declared_bytes":None}
        result=sample_image_references(pace_seconds=0,checker=limited)
        self.assertEqual(result["head_requests"],1)
        self.assertEqual(result["counts"]["rate_limited"],1)
        self.assertEqual(len(calls),1)

    def test_invalid_probe_result_fail_closed(self):
        with self.assertRaises(ValueError):
            sample_image_references(pace_seconds=0,checker=lambda url: {})

    def test_negative_pacing_denied(self):
        with self.assertRaises(ValueError):
            sample_image_references(pace_seconds=-1)


if __name__=="__main__":
    unittest.main()
