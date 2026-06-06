import unittest

import provider_adapters as adapters


EXPECTED_PROVIDER_IDS = {
    "zepto",
    "blinkit",
    "swiggy_instamart",
    "amazon_fresh",
    "jiomart",
    "dmart",
    "bigbasket",
}


class ProviderAdapterContractTests(unittest.TestCase):
    def test_catalog_exposes_every_supported_provider(self):
        self.assertEqual(set(adapters.provider_ids()), EXPECTED_PROVIDER_IDS)
        self.assertEqual(set(adapters.configured_provider_map()), EXPECTED_PROVIDER_IDS)

        for provider in adapters.provider_catalog():
            with self.subTest(provider=provider["id"]):
                self.assertTrue(provider["name"])
                self.assertTrue(provider["kind"])
                self.assertTrue(provider["status"])
                self.assertIn("{query}", provider["search_url"])

    def test_search_urls_are_built_without_browser_sessions(self):
        providers = adapters.configured_provider_map()
        expected_hosts = {
            "zepto": "www.zepto.com",
            "blinkit": "blinkit.com",
            "swiggy_instamart": "www.swiggy.com",
            "amazon_fresh": "www.amazon.in",
            "jiomart": "www.jiomart.com",
            "dmart": "www.dmart.in",
            "bigbasket": "www.bigbasket.com",
        }

        for provider_id, expected_host in expected_hosts.items():
            with self.subTest(provider=provider_id):
                self.assertIn(expected_host, adapters.build_search_url(providers[provider_id], "paneer 200g"))

        self.assertEqual(
            adapters.build_search_url(providers["jiomart"], "black sesame seeds"),
            "https://www.jiomart.com/search/black%20sesame%20seeds",
        )
        self.assertEqual(
            adapters.build_search_url(providers["amazon_fresh"], "curry cut chicken"),
            "https://www.amazon.in/s?k=curry+cut+chicken&i=nowstore&almBrandId=ctnow&fpw=alm",
        )
        self.assertEqual(
            adapters.build_search_url(providers["blinkit"], "Coke Zero 750ml"),
            "https://blinkit.com/s/?q=Coke+Zero+750ml",
        )

    def test_open_strategies_preserve_provider_behavior(self):
        fallback = "https://example.test/search"
        product = "https://example.test/product"
        amazon_product = "https://www.amazon.in/example/dp/B012345678"

        self.assertEqual(adapters.choose_open_url("jiomart", product, fallback), (fallback, "search"))
        self.assertEqual(
            adapters.choose_open_url("amazon_fresh", amazon_product, fallback),
            (amazon_product, "product"),
        )
        self.assertEqual(adapters.choose_open_url("amazon_fresh", product, fallback), (fallback, "search"))
        for provider_id in {"zepto", "blinkit", "swiggy_instamart", "dmart", "bigbasket"}:
            with self.subTest(provider=provider_id):
                self.assertEqual(adapters.choose_open_url(provider_id, product, fallback), (product, "product"))

    def test_scan_modes_and_timeouts_are_adapter_policy(self):
        self.assertEqual(adapters.scan_mode({"status": "browser-ready"}), "ready")
        self.assertEqual(adapters.scan_mode({"status": "needs-access"}), "setup")
        self.assertEqual(adapters.scan_mode({"status": "manual-link"}), "manual")
        self.assertEqual(adapters.scan_mode({"status": "future"}), "planned")

        self.assertEqual(adapters.scan_timeout("zepto", 60), 120)
        self.assertEqual(adapters.scan_timeout("amazon_fresh", 120), 360)
        self.assertEqual(adapters.scan_timeout("bigbasket", 120), 420)
        self.assertEqual(adapters.scan_timeout("bigbasket", 120, focused=True), 480)
        self.assertEqual(adapters.scan_timeout("blinkit", 900), 900)

    def test_unknown_provider_has_generic_compatibility_fallback(self):
        with self.assertRaisesRegex(ValueError, "Unknown provider adapter"):
            adapters.adapter_for("unknown")
        self.assertEqual(
            adapters.choose_open_url("unknown", "https://example.test/product", "https://example.test/search"),
            ("https://example.test/product", "product"),
        )
        self.assertEqual(adapters.scan_timeout("unknown", 75), 75)


if __name__ == "__main__":
    unittest.main()
