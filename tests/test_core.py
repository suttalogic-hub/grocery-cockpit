import tempfile
import unittest
from pathlib import Path

import grocery_cockpit as g


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


if __name__ == "__main__":
    unittest.main()
