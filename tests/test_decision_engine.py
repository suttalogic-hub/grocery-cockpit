import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import grocery_cockpit as g


def hours_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


def days_ago(days: float) -> str:
    return hours_ago(days * 24)


class DecisionEngineTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = g.open_db(Path(self.tmp.name) / "grocery.sqlite")
        self.config = g.default_config()
        self.config["providers"] = [
            provider
            for provider in self.config["providers"]
            if provider["id"] in {"zepto", "blinkit"}
        ]

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def add_item(
        self,
        name: str,
        *,
        pack_value: float = 1,
        pack_unit: str = "pc",
        match_mode: str = "exact",
    ) -> int:
        return g.add_item(
            self.conn,
            g.ItemInput(
                name=name,
                pack_value=pack_value,
                pack_unit=pack_unit,
                category="Test groceries",
                match_mode=match_mode,
            ),
        )

    def add_price(
        self,
        item_id: int,
        provider_id: str,
        price: float,
        *,
        observed_at: str | None = None,
        mrp: float | None = None,
        source: str = "manual",
        title: str = "",
        pack_value: float | None = None,
        pack_unit: str = "",
        delivery_fee: float = 0,
        handling_fee: float = 0,
    ) -> None:
        g.add_observation(
            self.conn,
            g.ObservationInput(
                item_id=item_id,
                provider_id=provider_id,
                price=price,
                observed_at=observed_at or g.utc_now_iso(),
                mrp=mrp,
                source=source,
                title=title,
                pack_value=pack_value,
                pack_unit=pack_unit,
                delivery_fee=delivery_fee,
                handling_fee=handling_fee,
            ),
            self.config,
        )

    def add_alert_history(self, item_id: int, provider_id: str = "zepto", price: float = 100) -> None:
        for days in (6, 5, 4):
            self.add_price(item_id, provider_id, price, observed_at=days_ago(days))


class BasketOptimizationTests(DecisionEngineTestCase):
    def test_recommends_one_app_when_extra_cost_is_within_convenience_gap(self):
        paneer = self.add_item("Paneer")
        milk = self.add_item("Milk")
        for item_id in (paneer, milk):
            g.set_basket_item(self.conn, item_id, 1)

        self.add_price(paneer, "zepto", 100)
        self.add_price(milk, "zepto", 100)
        self.add_price(paneer, "blinkit", 80)
        self.add_price(milk, "blinkit", 130)

        basket = g.build_basket(self.conn, self.config)

        self.assertEqual(basket["split"]["total"], 180)
        self.assertEqual(basket["best_single"]["provider_id"], "zepto")
        self.assertEqual(basket["best_single"]["total"], 200)
        self.assertEqual(basket["recommendation"]["mode"], "single_app")
        self.assertEqual(basket["recommendation"]["extra_cost"], 20)

    def test_recommends_split_when_saving_exceeds_convenience_gap(self):
        paneer = self.add_item("Paneer")
        milk = self.add_item("Milk")
        for item_id in (paneer, milk):
            g.set_basket_item(self.conn, item_id, 1)

        self.add_price(paneer, "zepto", 100)
        self.add_price(milk, "zepto", 300)
        self.add_price(paneer, "blinkit", 300)
        self.add_price(milk, "blinkit", 100)

        basket = g.build_basket(self.conn, self.config)

        self.assertEqual(basket["split"]["total"], 200)
        self.assertEqual(basket["split"]["app_count"], 2)
        self.assertEqual(basket["split"]["saving_vs_best_single"], 200)
        self.assertEqual(basket["recommendation"]["mode"], "split")
        self.assertEqual(basket["recommendation"]["saving"], 200)

    def test_large_basket_uses_percentage_convenience_gap(self):
        rice = self.add_item("Rice")
        oil = self.add_item("Oil")
        for item_id in (rice, oil):
            g.set_basket_item(self.conn, item_id, 1)

        self.add_price(rice, "zepto", 1000)
        self.add_price(oil, "zepto", 1080)
        self.add_price(rice, "blinkit", 900)
        self.add_price(oil, "blinkit", 1100)

        basket = g.build_basket(self.conn, self.config)

        self.assertEqual(basket["split"]["total"], 1980)
        self.assertEqual(basket["convenience_gap"], 99)
        self.assertEqual(basket["best_single"]["provider_id"], "blinkit")
        self.assertEqual(basket["recommendation"]["mode"], "single_app")
        self.assertEqual(basket["recommendation"]["extra_cost"], 20)

    def test_recommends_split_when_no_single_app_covers_every_item(self):
        paneer = self.add_item("Paneer")
        milk = self.add_item("Milk")
        for item_id in (paneer, milk):
            g.set_basket_item(self.conn, item_id, 1)

        self.add_price(paneer, "zepto", 90)
        self.add_price(milk, "blinkit", 60)

        basket = g.build_basket(self.conn, self.config)

        self.assertIsNone(basket["best_single"])
        self.assertEqual(basket["recommendation"]["mode"], "split")
        self.assertEqual(basket["recommendation"]["reason"], "No single app has every basket item yet.")
        self.assertEqual(basket["split"]["app_count"], 2)

    def test_quantity_changes_line_totals_and_recommendation_totals(self):
        paneer = self.add_item("Paneer")
        g.set_basket_item(self.conn, paneer, 2.5)
        self.add_price(paneer, "zepto", 80)
        self.add_price(paneer, "blinkit", 90)

        basket = g.build_basket(self.conn, self.config)

        self.assertEqual(basket["split"]["total"], 200)
        self.assertEqual(basket["split"]["lines"][0]["quantity"], 2.5)
        self.assertEqual(basket["split"]["lines"][0]["line_total"], 200)

    def test_unit_price_mode_chooses_better_value_pack_and_avoids_raw_total_comparison(self):
        potatoes = self.add_item("Potatoes", pack_value=1, pack_unit="kg", match_mode="unit")
        g.set_basket_item(self.conn, potatoes, 1)
        self.add_price(potatoes, "zepto", 90, pack_value=1, pack_unit="kg")
        self.add_price(potatoes, "blinkit", 60, pack_value=500, pack_unit="g")

        basket = g.build_basket(self.conn, self.config)

        self.assertEqual(basket["split"]["lines"][0]["provider_id"], "zepto")
        self.assertEqual(basket["split"]["rank_mode"], "unit_value")
        self.assertEqual(basket["recommendation"]["mode"], "split")
        self.assertIsNone(basket["split"]["saving_vs_best_single"])

    def test_suspicious_low_price_is_not_used_for_basket(self):
        paneer = self.add_item("Paneer")
        g.set_basket_item(self.conn, paneer, 1)
        self.add_price(paneer, "zepto", 80, observed_at=hours_ago(1))
        self.add_price(paneer, "zepto", 1)
        self.add_price(paneer, "blinkit", 75)

        basket = g.build_basket(self.conn, self.config)

        self.assertEqual(basket["split"]["lines"][0]["provider_id"], "blinkit")
        self.assertEqual(basket["split"]["total"], 75)


class AlertDecisionTests(DecisionEngineTestCase):
    def alert_rows(self):
        return self.conn.execute("SELECT * FROM alerts ORDER BY reference_window, id").fetchall()

    def test_creates_alert_at_exact_ten_day_threshold(self):
        paneer = self.add_item("Paneer")
        self.add_alert_history(paneer)

        self.add_price(paneer, "zepto", 80)

        alerts = self.alert_rows()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["reference_window"], "10d_avg")
        self.assertEqual(alerts[0]["reference_price"], 100)
        self.assertEqual(alerts[0]["drop_percent"], 20)
        self.assertEqual(alerts[0]["current_price"], 80)

    def test_creates_thirty_day_alert_from_older_history(self):
        paneer = self.add_item("Paneer")
        for days in (25, 20, 15):
            self.add_price(paneer, "zepto", 100, observed_at=days_ago(days))

        self.add_price(paneer, "zepto", 75)

        alerts = self.alert_rows()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["reference_window"], "30d_avg")
        self.assertEqual(alerts[0]["drop_percent"], 25)

    def test_does_not_alert_below_threshold_or_without_enough_history(self):
        paneer = self.add_item("Paneer")
        self.add_price(paneer, "zepto", 100, observed_at=days_ago(3))
        self.add_price(paneer, "zepto", 100, observed_at=days_ago(2))
        self.add_price(paneer, "zepto", 79)
        self.assertEqual(self.alert_rows(), [])

        milk = self.add_item("Milk")
        self.add_alert_history(milk)
        self.add_price(milk, "zepto", 81)
        self.assertEqual(self.alert_rows(), [])

    def test_same_observation_does_not_create_duplicate_alerts(self):
        paneer = self.add_item("Paneer")
        self.add_alert_history(paneer)
        self.add_price(paneer, "zepto", 80)

        g.evaluate_and_record_alerts(self.conn, paneer, "zepto", self.config)
        g.evaluate_and_record_alerts(self.conn, paneer, "zepto", self.config)

        self.assertEqual(len(self.alert_rows()), 1)

    def test_delivery_and_handling_fees_are_part_of_alert_price(self):
        paneer = self.add_item("Paneer")
        self.add_alert_history(paneer)

        self.add_price(paneer, "zepto", 75, delivery_fee=3, handling_fee=2)

        alerts = self.alert_rows()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["current_price"], 80)
        self.assertEqual(alerts[0]["drop_percent"], 20)

    def test_suspicious_browser_price_does_not_create_alert(self):
        paneer = self.add_item("Paneer", pack_value=200, pack_unit="g")
        self.add_alert_history(paneer)

        self.add_price(
            paneer,
            "zepto",
            1,
            mrp=100,
            source="browser-probe",
            title="Paneer 200g Rs 1 MRP Rs 100 ADD",
            pack_value=200,
            pack_unit="g",
        )

        self.assertEqual(self.alert_rows(), [])

    def test_prune_removes_expired_alerts(self):
        paneer = self.add_item("Paneer")
        self.add_alert_history(paneer)
        self.add_price(paneer, "zepto", 80, observed_at=hours_ago(3))
        self.assertEqual(len(self.alert_rows()), 1)

        g.prune_alerts(self.conn, self.config)

        self.assertEqual(self.alert_rows(), [])


if __name__ == "__main__":
    unittest.main()
