"""Regression tests for official China product image URL provenance."""
import unittest

from cardscanr_worldwide.collectors.pokemon_china_products import parse_product


class ChinaImageHostTests(unittest.TestCase):
    def test_only_exact_https_official_host_and_upload_path(self):
        html = """<html><title>测试</title><main>
        <img src="https://image.pokemon.com.cn/uploads/card.png?token=private">
        <img src="https://image.pokemon.com.cn.evil.example/uploads/fake.png">
        <img src="https://evil.example/x/image.pokemon.com.cn/uploads/fake.png">
        <img src="http://image.pokemon.com.cn/uploads/insecure.png">
        <img src="https://image.pokemon.com.cn/other/uploads/invalid.png">
        </main></html>"""
        images = parse_product(html, "https://www.pokemon.cn/tcg/product/123.html")["images"]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["canonical_url"], "https://image.pokemon.com.cn/uploads/card.png")


if __name__ == "__main__":
    unittest.main()
