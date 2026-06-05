import json
import tempfile
import unittest
from pathlib import Path

import grocery_cockpit as g


FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


def load_bad_match_cases():
    with (FIXTURE_DIR / "bad_match_cases.json").open("r", encoding="utf-8") as handle:
        return json.load(handle)


class CorePricingTests(unittest.TestCase):
    def test_unit_price_normalizes_weight_and_volume(self):
        self.assertEqual(g.unit_price(29, 1, "kg"), 29)
        self.assertEqual(g.unit_price(38, 750, "ml"), 50.67)
        self.assertEqual(g.unit_price(90, 12, "pc"), 7.5)

    def test_pack_from_text_handles_common_grocery_shapes(self):
        self.assertEqual(g.pack_from_text("Fresh Potato, 1kg Rs 29"), (1.0, "kg"))
        self.assertEqual(g.pack_from_text("Coca-Cola Zero Sugar 750ml bottle"), (750.0, "ml"))
        self.assertEqual(g.pack_from_text("Value pack 2 x 500 g"), (1000.0, "g"))

    def test_amazon_product_urls_open_as_products(self):
        fallback = "https://www.amazon.in/s?k=potato&i=nowstore&almBrandId=ctnow&fpw=alm"
        product = "https://www.amazon.in/Fresh-Potato-1kg-Pack/dp/B07BG5GZP2/ref=sr_1_1"

        self.assertEqual(g.open_url_for_provider("amazon_fresh", product, fallback), (product, "product"))
        self.assertEqual(g.open_url_for_provider("amazon_fresh", "", fallback), (fallback, "search"))


class CoreMatchingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "grocery.sqlite"
        self.conn = g.open_db(self.db_path)
        self.config = g.default_config()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def add_item(self, item):
        item_id = g.add_item(self.conn, item)
        return self.conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()

    def test_same_size_item_rejects_wrong_pack_and_wrong_variant(self):
        item = self.add_item(
            g.ItemInput(
                name="Coca-Cola Zero Sugar Soft Drink",
                brand="",
                pack_value=750,
                pack_unit="ml",
                category="Beverages",
                match_mode="same_size",
            )
        )
        result = {
            "search_kind": "same_size",
            "links": [
                {
                    "text": "Coca-Cola Zero Sugar, No Calories Soft Drink Can, 300 Ml - Cola Rs 38 MRP Rs 40 ADD",
                    "href": "https://www.amazon.in/Coca-Cola-Zero-Soft-Drink-300ml/dp/example",
                },
                {
                    "text": "Coca-Cola 750ml Rs 35 MRP Rs 40 ADD",
                    "href": "https://www.amazon.in/Coca-Cola-750ml/dp/example",
                },
            ],
        }

        self.assertIsNone(g.best_probe_match(item, result))

    def test_generic_curd_rejects_ghee_that_mentions_curd_churned(self):
        item = self.add_item(
            g.ItemInput(
                name="Amul Curd Pouch",
                brand="",
                pack_value=700,
                pack_unit="g",
                category="Dairy",
                match_mode="category",
            )
        )
        result = {
            "search_kind": "generic_fallback",
            "links": [
                {
                    "text": "SONAI Desi Ghee 100 ML - Bilona Method, Curd Churned, Pure & Natural Rs 68 MRP Rs 100 ADD",
                    "href": "https://www.amazon.in/SONAI-Desi-Ghee-100/dp/example",
                }
            ],
        }

        self.assertIsNone(g.best_probe_match(item, result))

    def test_synthetic_bad_match_fixture_suite(self):
        cases = load_bad_match_cases()
        self.assertGreaterEqual(len(cases), 8)
        covered_modes = set()

        for case in cases:
            with self.subTest(case=case["id"]):
                self.assertTrue(case.get("reason"), "Each fixture must explain its matching risk.")
                covered_modes.add(case["mode"])
                item = self.add_item(g.ItemInput(**case["item"]))
                result = {
                    "search_kind": case.get("search_kind", case["item"].get("match_mode", "exact")),
                    "links": case["links"],
                }

                match = g.best_probe_match(item, result)
                if case["should_match"]:
                    self.assertIsNotNone(match)
                    if case.get("expected_price") is not None:
                        self.assertEqual(match["price"], case["expected_price"])
                    if case.get("expected_unit_price") is not None:
                        self.assertEqual(match["unit_price"], case["expected_unit_price"])
                    if case.get("expected_text_contains"):
                        self.assertIn(case["expected_text_contains"].lower(), match["text"].lower())
                else:
                    self.assertIsNone(match)

        self.assertEqual(covered_modes, {"exact", "category", "same_size", "unit"})


class DemoSeedTests(unittest.TestCase):
    def test_seed_demo_data_produces_visible_prices(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = g.open_db(Path(tmp) / "grocery.sqlite")
            try:
                config = g.default_config()
                g.seed_demo_data(conn, config)
                state = g.build_state(conn, config)
                self.assertEqual(state["item_count"], 6)
                self.assertTrue(any(card["best"] for card in state["items"]))
            finally:
                conn.close()


class WatchlistImportExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "grocery.sqlite"
        self.conn = g.open_db(self.db_path)
        self.config = g.default_config()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_export_watchlist_excludes_private_runtime_data(self):
        item_id = g.add_item(
            self.conn,
            g.ItemInput(
                name="Paneer",
                brand="Amul",
                pack_value=200,
                pack_unit="g",
                category="Dairy",
                target_price=80,
                match_mode="same_size",
            ),
        )
        g.add_observation(
            self.conn,
            g.ObservationInput(
                item_id=item_id,
                provider_id="blinkit",
                price=82,
                observed_at=g.utc_now_iso(),
                source="manual",
                url="https://example.test/private-product",
            ),
            self.config,
        )
        g.set_basket_item(self.conn, item_id, 2)

        payload = g.export_watchlist(self.conn)

        self.assertEqual(payload["schema"], g.WATCHLIST_SCHEMA)
        self.assertEqual(payload["item_count"], 1)
        self.assertEqual(payload["items"][0]["name"], "Paneer")
        self.assertEqual(payload["items"][0]["match_mode"], "same_size")
        encoded_items = json_dumps(payload["items"])
        for private_key in ["observations", "alerts", "basket_items", "created_at", "url"]:
            self.assertNotIn(private_key, encoded_items)
        for excluded in ["price_history", "alerts", "basket", "provider_sessions", "location", "access_key"]:
            self.assertIn(excluded, payload["excludes"])

    def test_import_watchlist_merges_and_ignores_price_history(self):
        payload = {
            "schema": g.WATCHLIST_SCHEMA,
            "schema_version": 1,
            "items": [
                {
                    "name": "Curry cut chicken",
                    "brand": "",
                    "pack_value": 500,
                    "pack_unit": "g",
                    "category": "Meat",
                    "target_price": 180,
                    "match_mode": "same_size",
                    "observations": [{"price": 1}],
                },
                {
                    "name": "Surf Excel",
                    "brand": "",
                    "pack_value": 1,
                    "pack_unit": "kg",
                    "category": "Household",
                    "match_mode": "exact",
                },
            ],
        }

        first = g.import_watchlist(self.conn, payload)
        second = g.import_watchlist(self.conn, payload)

        self.assertEqual(first["imported"], 2)
        self.assertEqual(second["existing"], 2)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS count FROM items WHERE active = 1").fetchone()["count"],
            2,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS count FROM observations").fetchone()["count"],
            0,
        )

    def test_import_watchlist_replace_deactivates_current_items(self):
        g.import_watchlist(self.conn, [{"name": "Old item", "category": "Demo"}])
        result = g.import_watchlist(self.conn, [{"name": "New item", "category": "Demo"}], replace=True)

        self.assertTrue(result["replaced"])
        active_names = [
            row["name"]
            for row in self.conn.execute("SELECT name FROM items WHERE active = 1 ORDER BY name").fetchall()
        ]
        self.assertEqual(active_names, ["New item"])


def json_dumps(payload):
    return json.dumps(payload, sort_keys=True)


if __name__ == "__main__":
    unittest.main()
