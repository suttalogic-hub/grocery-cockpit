#!/usr/bin/env python3
"""Personal grocery price cockpit."""

from __future__ import annotations

import argparse
import dataclasses
import gzip
import html
import http.cookies
import http.server
import json
import os
import random
import re
import secrets
import shutil
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


APP_NAME = "Groceries"
APP_VERSION = "0.14.9"
DEFAULT_PORT = 8877
ACCESS_COOKIE = "grocery_cockpit_access"
STATE_CACHE_SECONDS = 30
AUTO_SCAN_PROVIDER_IDS = ["zepto", "blinkit", "swiggy_instamart", "amazon_fresh", "jiomart", "dmart", "bigbasket"]
WATCHLIST_SCHEMA = "grocery-cockpit.watchlist"
WATCHLIST_SCHEMA_VERSION = 1
WATCHLIST_IMPORT_LIMIT = 1000


PROVIDERS = [
    {
        "id": "zepto",
        "name": "Zepto",
        "kind": "quick-commerce",
        "status": "browser-ready",
        "search_url": "https://www.zepto.com/search?query={query}",
    },
    {
        "id": "blinkit",
        "name": "Blinkit",
        "kind": "quick-commerce",
        "status": "browser-ready",
        "search_url": "https://blinkit.com/s/?q={query}",
    },
    {
        "id": "swiggy_instamart",
        "name": "Swiggy Instamart",
        "kind": "quick-commerce",
        "status": "browser-ready",
        "search_url": "https://www.swiggy.com/instamart/search?query={query}",
    },
    {
        "id": "amazon_fresh",
        "name": "Amazon Now",
        "kind": "marketplace-grocery",
        "status": "browser-ready",
        "search_url": "https://www.amazon.in/s?k={query}&i=nowstore&almBrandId=ctnow&fpw=alm",
    },
    {
        "id": "jiomart",
        "name": "JioMart",
        "kind": "grocery",
        "status": "browser-ready",
        "search_url": "https://www.jiomart.com/search/{query}",
    },
    {
        "id": "dmart",
        "name": "DMart Ready",
        "kind": "grocery",
        "status": "browser-ready",
        "search_url": "https://www.dmart.in/search?searchTerm={query}",
    },
    {
        "id": "bigbasket",
        "name": "BigBasket",
        "kind": "grocery",
        "status": "browser-ready",
        "search_url": "https://www.bigbasket.com/ps/?q={query}",
    },
]


@dataclasses.dataclass
class ItemInput:
    name: str
    brand: str = ""
    pack_value: float | None = None
    pack_unit: str = ""
    category: str = ""
    target_price: float | None = None
    notes: str = ""
    match_mode: str = "exact"


FLEXIBILITY_MODES = {
    "exact": {
        "label": "Exact item",
        "short_label": "Exact",
        "description": "Only this product should count.",
    },
    "category": {
        "label": "Any close match",
        "short_label": "Any close match",
        "description": "Any acceptable brand or promoted option can count.",
    },
    "same_size": {
        "label": "Same size",
        "short_label": "Same size",
        "description": "Any acceptable brand can count, but the pack size must match.",
    },
    "unit": {
        "label": "Best unit price",
        "short_label": "Unit price",
        "description": "Any acceptable size can count; compare by unit price when available.",
    },
}


def normalize_match_mode(value: Any) -> str:
    mode = str(value or "exact").strip().lower().replace("-", "_")
    aliases = {
        "flexible": "unit",
        "generic": "category",
        "same_category": "category",
        "any_brand": "category",
        "same_pack": "same_size",
        "unit_price": "unit",
        "cheapest_unit": "unit",
    }
    mode = aliases.get(mode, mode)
    return mode if mode in FLEXIBILITY_MODES else "exact"


@dataclasses.dataclass
class ObservationInput:
    item_id: int
    provider_id: str
    price: float
    observed_at: str
    mrp: float | None = None
    delivery_fee: float = 0.0
    handling_fee: float = 0.0
    in_stock: bool = True
    source: str = "manual"
    url: str = ""
    title: str = ""
    pack_value: float | None = None
    pack_unit: str = ""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def normalize_pack_unit(unit: str) -> str:
    unit = (unit or "").strip().lower()
    aliases = {
        "grams": "g",
        "gram": "g",
        "gm": "g",
        "gms": "g",
        "kgs": "kg",
        "kilogram": "kg",
        "kilograms": "kg",
        "ltr": "l",
        "ltrs": "l",
        "litre": "l",
        "litres": "l",
        "liter": "l",
        "liters": "l",
        "ml.": "ml",
        "pcs": "pc",
        "piece": "pc",
        "pieces": "pc",
    }
    return aliases.get(unit, unit)


def pack_to_base(value: float | None, unit: str) -> tuple[float | None, str]:
    if value is None:
        return None, ""
    unit = normalize_pack_unit(unit)
    if unit == "kg":
        return value * 1000, "g"
    if unit == "l":
        return value * 1000, "ml"
    return value, unit


def unit_price(price: float, pack_value: float | None, pack_unit: str) -> float | None:
    base_value, base_unit = pack_to_base(pack_value, pack_unit)
    if not base_value or not base_unit:
        return None
    if base_unit == "g":
        return round(price / base_value * 1000, 2)
    if base_unit == "ml":
        return round(price / base_value * 1000, 2)
    if base_unit == "pc":
        return round(price / base_value, 2)
    return round(price / base_value, 2)


def unit_price_label(pack_unit: str) -> str:
    unit = pack_to_base(1, pack_unit)[1]
    if unit == "g":
        return "Rs/kg"
    if unit == "ml":
        return "Rs/L"
    if unit == "pc":
        return "Rs/pc"
    return f"Rs/{unit or 'unit'}"


def pack_from_text(value: str) -> tuple[float | None, str]:
    text = re.sub(r"\s+", " ", value or "")
    text = re.sub(r"([A-Za-z])([0-9])", r"\1 \2", text)
    unit_pattern = r"kg|kgs|kilogram|kilograms|g|gm|gms|gram|grams|ml|l|ltr|ltrs|litre|litres|liter|liters|pc|pcs|piece|pieces"
    patterns = [
        ("quantity_first", rf"\b([0-9]+)\s*x\s*([0-9]+(?:\.[0-9]+)?)\s*({unit_pattern})(?=$|[^a-z0-9]|[A-Z])"),
        ("value_first", rf"(?<![A-Za-z0-9])([0-9]+(?:\.[0-9]+)?)\s*({unit_pattern})(?=$|[^a-z0-9]|[A-Z])\s*x\s*([0-9]+)"),
        ("single", rf"(?<![A-Za-z0-9])([0-9]+(?:\.[0-9]+)?)\s*({unit_pattern})(?=$|[^a-z0-9]|[A-Z])"),
    ]
    matches: list[tuple[float, str, int]] = []
    for kind, pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            if kind == "quantity_first":
                multiplier = float(match.group(1))
                value = float(match.group(2)) * multiplier
                unit = match.group(3)
            elif kind == "value_first":
                multiplier = float(match.group(3))
                value = float(match.group(1)) * multiplier
                unit = match.group(2)
            else:
                value = float(match.group(1))
                unit = match.group(2)
            unit = normalize_pack_unit(unit)
            if unit in {"g", "ml"} and value > 100000:
                continue
            if unit in {"kg", "l"} and value > 100:
                continue
            if unit == "pc" and value > 1000:
                continue
            matches.append((value, unit, match.start()))
    if not matches:
        return None, ""
    matches.sort(key=lambda entry: (entry[1] == "pc", entry[2]))
    value, unit, _ = matches[0]
    return value, unit


def canonical_item_name(name: str, brand: str = "") -> str:
    text = " ".join(part for part in [brand, name.split("|", 1)[0]] if part)
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def packs_equivalent(
    left_value: float | None,
    left_unit: str,
    right_value: float | None,
    right_unit: str,
) -> bool:
    left_base_value, left_base_unit = pack_to_base(left_value, left_unit)
    right_base_value, right_base_unit = pack_to_base(right_value, right_unit)
    if left_base_value is None or right_base_value is None:
        return False
    return left_base_unit == right_base_unit and abs(float(left_base_value) - float(right_base_value)) < 0.001


def existing_similar_item_id(conn: sqlite3.Connection, item: ItemInput) -> int | None:
    target_name = canonical_item_name(item.name, item.brand)
    if not target_name or item.pack_value is None:
        return None
    rows = conn.execute(
        """
        SELECT id, name, brand, pack_value, pack_unit
        FROM items
        WHERE active = 1
        """
    ).fetchall()
    for row in rows:
        if canonical_item_name(row["name"], row["brand"]) != target_name:
            continue
        if packs_equivalent(item.pack_value, item.pack_unit, row["pack_value"], row["pack_unit"]):
            return int(row["id"])
    return None


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def compact_text(value: Any, limit: int = 360) -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]+", " ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}..."


def default_config() -> dict[str, Any]:
    return {
        "location": {
            "label": "Home",
            "pincode": "",
            "city": "",
        },
        "settings": {
            "refresh_interval_minutes": 60,
            "min_10d_avg_drop_percent": 20,
            "min_30d_avg_drop_percent": 25,
            "min_history_points": 3,
            "include_delivery_fees": True,
            "basket_alert_min_saving": 50,
            "single_app_convenience_threshold_rupees": 50,
            "single_app_convenience_threshold_percent": 5,
            "alert_expiry_hours": 2,
        },
        "access": {
            "enabled": True,
            "key": "",
        },
        "providers": PROVIDERS,
    }


def load_config(path: Path) -> dict[str, Any]:
    config = default_config()
    existing = read_json(path)
    deep_update(config, existing)
    changed = not path.exists()
    if config.get("access", {}).get("enabled") and not config.get("access", {}).get("key"):
        config["access"]["key"] = secrets.token_urlsafe(24)
        changed = True
    if changed:
        write_json(path, config)
    return config


def deep_update(base: dict[str, Any], override: dict[str, Any]) -> None:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_update(base[key], value)
        else:
            base[key] = value


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            brand TEXT NOT NULL DEFAULT '',
            pack_value REAL,
            pack_unit TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT '',
            target_price REAL,
            notes TEXT NOT NULL DEFAULT '',
            match_mode TEXT NOT NULL DEFAULT 'exact',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL,
            provider_id TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            price REAL NOT NULL,
            mrp REAL,
            delivery_fee REAL NOT NULL DEFAULT 0,
            handling_fee REAL NOT NULL DEFAULT 0,
            effective_price REAL NOT NULL,
            unit_price REAL,
            in_stock INTEGER NOT NULL DEFAULT 1,
            source TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL DEFAULT '',
            pack_value REAL,
            pack_unit TEXT NOT NULL DEFAULT '',
            raw_json TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_observations_item_provider_time
            ON observations(item_id, provider_id, observed_at DESC);

        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL,
            provider_id TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            current_price REAL NOT NULL,
            reference_price REAL,
            reference_window TEXT NOT NULL,
            drop_percent REAL,
            reason TEXT NOT NULL,
            alert_key TEXT NOT NULL UNIQUE,
            expires_at TEXT NOT NULL DEFAULT '',
            dismissed_at TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS basket_items (
            item_id INTEGER PRIMARY KEY,
            quantity REAL NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS scan_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            requested_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            source TEXT NOT NULL,
            provider_count INTEGER NOT NULL DEFAULT 0,
            item_count INTEGER NOT NULL DEFAULT 0,
            target_count INTEGER NOT NULL DEFAULT 0,
            observation_count INTEGER NOT NULL DEFAULT 0,
            message TEXT NOT NULL DEFAULT '',
            plan_path TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS probe_imports (
            provider_id TEXT NOT NULL,
            item_id INTEGER NOT NULL,
            observed_at TEXT NOT NULL,
            price REAL NOT NULL,
            url TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            PRIMARY KEY (provider_id, item_id, observed_at, price, url)
        );
        """
    )
    item_columns = {row["name"] for row in conn.execute("PRAGMA table_info(items)").fetchall()}
    if "match_mode" not in item_columns:
        conn.execute("ALTER TABLE items ADD COLUMN match_mode TEXT NOT NULL DEFAULT 'exact'")
    alert_columns = {row["name"] for row in conn.execute("PRAGMA table_info(alerts)").fetchall()}
    if "expires_at" not in alert_columns:
        conn.execute("ALTER TABLE alerts ADD COLUMN expires_at TEXT NOT NULL DEFAULT ''")
    if "dismissed_at" not in alert_columns:
        conn.execute("ALTER TABLE alerts ADD COLUMN dismissed_at TEXT NOT NULL DEFAULT ''")
    migrate_item_match_modes(conn)
    conn.commit()


def migrate_item_match_modes(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT id, name, brand, category, match_mode FROM items").fetchall()
    for row in rows:
        raw_mode = str(row["match_mode"] or "exact").strip().lower()
        if raw_mode in FLEXIBILITY_MODES:
            continue
        text = " ".join([row["brand"] or "", row["name"] or "", row["category"] or ""]).lower()
        if raw_mode == "flexible":
            next_mode = "exact" if ("surf excel" in text or "boroplus" in text or "boro plus" in text) else "unit"
        else:
            next_mode = normalize_match_mode(raw_mode)
        conn.execute("UPDATE items SET match_mode = ? WHERE id = ?", (next_mode, row["id"]))

    rows = conn.execute(
        """
        SELECT id, name, brand, category, match_mode
        FROM items
        WHERE match_mode = 'exact'
        """
    ).fetchall()
    for row in rows:
        text = " ".join([row["brand"] or "", row["name"] or "", row["category"] or ""]).lower()
        tokens = set(text_tokens(text))
        next_mode = ""
        for profile in GENERIC_FALLBACK_PROFILES:
            required = set(profile.get("required", set()))
            if str(profile["key"]) in tokens or required <= tokens:
                next_mode = "same_size" if profile.get("strict_pack", True) else "unit"
                break
        if next_mode:
            conn.execute("UPDATE items SET match_mode = ? WHERE id = ?", (next_mode, row["id"]))


def add_item(conn: sqlite3.Connection, item: ItemInput) -> int:
    match_mode = normalize_match_mode(item.match_mode)
    existing_id = existing_similar_item_id(conn, item)
    if existing_id is not None:
        return existing_id
    cursor = conn.execute(
        """
        INSERT INTO items (
            name, brand, pack_value, pack_unit, category, target_price, notes, match_mode, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item.name.strip(),
            item.brand.strip(),
            item.pack_value,
            normalize_pack_unit(item.pack_unit),
            item.category.strip(),
            item.target_price,
            item.notes.strip(),
            match_mode,
            utc_now_iso(),
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def watchlist_item_payload(item: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    pack_value = item_value(item, "pack_value", None)
    target_price = item_value(item, "target_price", None)
    return {
        "name": compact_text(item_value(item, "name", ""), 160),
        "brand": compact_text(item_value(item, "brand", ""), 120),
        "pack_value": float(pack_value) if pack_value is not None else None,
        "pack_unit": normalize_pack_unit(str(item_value(item, "pack_unit", "") or "")),
        "category": compact_text(item_value(item, "category", ""), 120),
        "target_price": float(target_price) if target_price is not None else None,
        "notes": compact_text(item_value(item, "notes", ""), 500),
        "match_mode": normalize_match_mode(item_value(item, "match_mode", "exact")),
    }


def export_watchlist(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT name, brand, pack_value, pack_unit, category, target_price, notes, match_mode
        FROM items
        WHERE active = 1
        ORDER BY category COLLATE NOCASE, brand COLLATE NOCASE, name COLLATE NOCASE, id
        """
    ).fetchall()
    items = [watchlist_item_payload(row) for row in rows]
    return {
        "schema": WATCHLIST_SCHEMA,
        "schema_version": WATCHLIST_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "exported_at": utc_now_iso(),
        "item_count": len(items),
        "items": items,
        "excludes": ["price_history", "alerts", "basket", "provider_sessions", "location", "access_key"],
    }


def optional_float(value: Any, field_name: str) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a number") from exc


def watchlist_item_input(entry: Any) -> ItemInput:
    if not isinstance(entry, dict):
        raise ValueError("item must be an object")
    name = compact_text(entry.get("name", ""), 160)
    if not name:
        raise ValueError("item name is required")
    return ItemInput(
        name=name,
        brand=compact_text(entry.get("brand", ""), 120),
        pack_value=optional_float(entry.get("pack_value"), "pack_value"),
        pack_unit=normalize_pack_unit(str(entry.get("pack_unit", "") or "")),
        category=compact_text(entry.get("category", ""), 120),
        target_price=optional_float(entry.get("target_price"), "target_price"),
        notes=compact_text(entry.get("notes", ""), 500),
        match_mode=normalize_match_mode(entry.get("match_mode", "exact")),
    )


def existing_watchlist_item_id(conn: sqlite3.Connection, item: ItemInput) -> int | None:
    target_name = canonical_item_name(item.name, item.brand)
    if not target_name:
        return None
    rows = conn.execute(
        """
        SELECT id, name, brand, pack_value, pack_unit
        FROM items
        WHERE active = 1
        """
    ).fetchall()
    for row in rows:
        if canonical_item_name(row["name"], row["brand"]) != target_name:
            continue
        if item.pack_value is None and row["pack_value"] is None:
            return int(row["id"])
        if packs_equivalent(item.pack_value, item.pack_unit, row["pack_value"], row["pack_unit"]):
            return int(row["id"])
    return None


def import_watchlist(conn: sqlite3.Connection, payload: Any, replace: bool = False) -> dict[str, Any]:
    source = payload
    if isinstance(payload, dict) and "watchlist" in payload:
        source = payload.get("watchlist")
    if isinstance(source, dict):
        items = source.get("items")
    else:
        items = source
    if not isinstance(items, list):
        raise ValueError("watchlist must contain an items array")
    if len(items) > WATCHLIST_IMPORT_LIMIT:
        raise ValueError(f"watchlist is too large; max {WATCHLIST_IMPORT_LIMIT} items")

    if replace:
        conn.execute("UPDATE items SET active = 0 WHERE active = 1")
        conn.execute("DELETE FROM basket_items")
        conn.execute("DELETE FROM alerts")
        conn.commit()

    imported = 0
    existing = 0
    skipped = 0
    errors: list[str] = []
    for index, entry in enumerate(items, start=1):
        try:
            item = watchlist_item_input(entry)
        except ValueError as exc:
            skipped += 1
            errors.append(f"item {index}: {exc}")
            continue
        if existing_watchlist_item_id(conn, item) is not None:
            existing += 1
            continue
        add_item(conn, item)
        imported += 1

    return {
        "imported": imported,
        "existing": existing,
        "skipped": skipped,
        "errors": errors[:20],
        "total": len(items),
        "replaced": bool(replace),
    }


def add_observation(conn: sqlite3.Connection, obs: ObservationInput, config: dict[str, Any]) -> None:
    pack_value = obs.pack_value
    pack_unit = normalize_pack_unit(obs.pack_unit)
    if pack_value is None:
        item = conn.execute("SELECT pack_value, pack_unit FROM items WHERE id = ?", (obs.item_id,)).fetchone()
        if item:
            pack_value = item["pack_value"]
            pack_unit = item["pack_unit"]
    effective_price = obs.price
    if config["settings"].get("include_delivery_fees", True):
        effective_price += obs.delivery_fee + obs.handling_fee
    u_price = unit_price(effective_price, pack_value, pack_unit)
    payload = dataclasses.asdict(obs)
    conn.execute(
        """
        INSERT INTO observations (
            item_id, provider_id, observed_at, price, mrp, delivery_fee, handling_fee,
            effective_price, unit_price, in_stock, source, title, url, pack_value,
            pack_unit, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            obs.item_id,
            obs.provider_id,
            obs.observed_at,
            obs.price,
            obs.mrp,
            obs.delivery_fee,
            obs.handling_fee,
            effective_price,
            u_price,
            1 if obs.in_stock else 0,
            obs.source,
            obs.title,
            obs.url,
            pack_value,
            pack_unit,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        ),
    )
    conn.commit()
    evaluate_and_record_alerts(conn, obs.item_id, obs.provider_id, config)


def observations_for_window(
    conn: sqlite3.Connection,
    item_id: int,
    provider_id: str,
    days: int,
) -> list[sqlite3.Row]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    return conn.execute(
        """
        SELECT *
        FROM observations
        WHERE item_id = ? AND provider_id = ? AND observed_at >= ?
        ORDER BY observed_at DESC
        """,
        (item_id, provider_id, cutoff),
    ).fetchall()


def latest_observation(
    conn: sqlite3.Connection,
    item_id: int,
    provider_id: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM observations
        WHERE item_id = ? AND provider_id = ?
        ORDER BY observed_at DESC
        LIMIT 1
        """,
        (item_id, provider_id),
    ).fetchone()


def latest_available_observation(
    conn: sqlite3.Connection,
    item_id: int,
    provider_id: str,
) -> sqlite3.Row | None:
    item = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        return conn.execute(
            """
            SELECT *
            FROM observations
            WHERE item_id = ?
              AND provider_id = ?
              AND in_stock = 1
            ORDER BY observed_at DESC
            LIMIT 1
            """,
            (item_id, provider_id),
        ).fetchone()
    rows = conn.execute(
        """
        SELECT *
        FROM observations
        WHERE item_id = ?
          AND provider_id = ?
          AND in_stock = 1
        ORDER BY observed_at DESC
        LIMIT 30
        """,
        (item_id, provider_id),
    ).fetchall()
    for row in rows:
        if observation_pricing_review(item, row)["trusted"]:
            return row
    return None


def alert_expiry_hours(config: dict[str, Any]) -> float:
    try:
        return max(0.25, float(config.get("settings", {}).get("alert_expiry_hours", 2)))
    except (TypeError, ValueError):
        return 2


def alert_expires_at(observed_at: str, config: dict[str, Any]) -> str:
    try:
        observed = parse_iso(observed_at)
    except ValueError:
        observed = datetime.now(timezone.utc)
    return (observed + timedelta(hours=alert_expiry_hours(config))).isoformat(timespec="seconds")


def prune_alerts(conn: sqlite3.Connection, config: dict[str, Any]) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=alert_expiry_hours(config))).isoformat(timespec="seconds")
    rows = conn.execute("SELECT id, observed_at FROM alerts WHERE expires_at = ''").fetchall()
    for row in rows:
        conn.execute(
            "UPDATE alerts SET expires_at = ? WHERE id = ?",
            (alert_expires_at(row["observed_at"], config), row["id"]),
        )
    conn.execute(
        """
        DELETE FROM alerts
        WHERE dismissed_at != ''
           OR (expires_at != '' AND expires_at <= ?)
           OR (expires_at = '' AND observed_at <= ?)
        """,
        (now, cutoff),
    )
    conn.commit()


def evaluate_and_record_alerts(
    conn: sqlite3.Connection,
    item_id: int,
    provider_id: str,
    config: dict[str, Any],
) -> None:
    item = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        return
    latest = latest_available_observation(conn, item_id, provider_id)
    if not latest or not latest["in_stock"]:
        return
    settings = config["settings"]
    min_history = int(settings["min_history_points"])
    windows = [
        ("10d_avg", 10, float(settings["min_10d_avg_drop_percent"])),
        ("30d_avg", 30, float(settings["min_30d_avg_drop_percent"])),
    ]
    for label, days, min_drop in windows:
        rows = observations_for_window(conn, item_id, provider_id, days)
        previous = [
            float(row["effective_price"])
            for row in rows
            if row["id"] != latest["id"] and observation_pricing_review(item, row)["trusted"]
        ]
        if len(previous) < min_history:
            continue
        reference = statistics.fmean(previous)
        if reference <= latest["effective_price"]:
            continue
        drop = round((reference - latest["effective_price"]) * 100 / reference, 2)
        if drop < min_drop:
            continue
        bucket = parse_iso(latest["observed_at"]).strftime("%Y-%m-%d")
        alert_key = f"{item_id}:{provider_id}:{label}:{bucket}:{latest['effective_price']:.2f}"
        reason = f"{drop:.0f}% below {label.replace('_', ' ')}"
        expires_at = alert_expires_at(latest["observed_at"], config)
        try:
            conn.execute(
                """
                INSERT INTO alerts (
                    item_id, provider_id, observed_at, current_price, reference_price,
                    reference_window, drop_percent, reason, alert_key, expires_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item_id,
                    provider_id,
                    latest["observed_at"],
                    latest["effective_price"],
                    reference,
                    label,
                    drop,
                    reason,
                    alert_key,
                    expires_at,
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            pass


def provider_by_id(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {provider["id"]: provider for provider in config.get("providers", PROVIDERS)}


def public_config(config: dict[str, Any]) -> dict[str, Any]:
    public = json.loads(json.dumps(config))
    if isinstance(public.get("access"), dict):
        public["access"] = {"enabled": bool(public["access"].get("enabled", True))}
    return public


def public_observation(
    row: sqlite3.Row | dict[str, Any] | None,
    include_details: bool = True,
) -> dict[str, Any] | None:
    if row is None:
        return None
    payload = dict(row)
    fields = [
        "observed_at",
        "effective_price",
        "unit_price",
        "in_stock",
        "pack_value",
        "pack_unit",
        "source",
    ]
    if include_details:
        fields.extend(
            [
                "price",
                "mrp",
                "delivery_fee",
                "handling_fee",
                "source",
                "title",
                "url",
            ]
        )
    compact = {field: payload.get(field) for field in fields if field in payload}
    if compact.get("title"):
        compact["title"] = compact_text(compact["title"], limit=180)
    compact["is_generic_alternative"] = compact.get("source") == "browser-probe-generic"
    if include_details and compact.get("unit_price") is None and compact.get("title"):
        pack_value, pack_unit = pack_from_text(str(compact["title"]))
        computed_unit = unit_price(float(compact["effective_price"]), pack_value, pack_unit)
        if computed_unit is not None:
            compact["pack_value"] = pack_value
            compact["pack_unit"] = pack_unit
            compact["unit_price"] = computed_unit
    return compact


def item_value(item: sqlite3.Row | dict[str, Any], key: str, default: Any = "") -> Any:
    if isinstance(item, sqlite3.Row):
        try:
            return item[key]
        except (IndexError, KeyError):
            return default
    return item.get(key, default)


def item_query_text(item: sqlite3.Row | dict[str, Any]) -> str:
    query = " ".join(
        part
        for part in [
            item_value(item, "brand"),
            item_value(item, "name"),
            format_pack(item_value(item, "pack_value", None), item_value(item, "pack_unit")),
        ]
        if part
    )
    return query.strip()


def search_url(
    provider: dict[str, Any],
    item: sqlite3.Row | dict[str, Any],
    query_override: str | None = None,
) -> str:
    query = (query_override or item_query_text(item)).strip()
    if provider.get("id") == "jiomart":
        return f"https://www.jiomart.com/search/{urllib.parse.quote(query)}"
    if provider.get("id") == "amazon_fresh":
        return f"https://www.amazon.in/s?k={urllib.parse.quote_plus(query)}&i=nowstore&almBrandId=ctnow&fpw=alm"
    return provider["search_url"].format(query=urllib.parse.quote_plus(query))


def amazon_now_open_url() -> str:
    return "/amazon-now"


def is_amazon_product_url(url: str | None) -> bool:
    if not url:
        return False
    try:
        parsed = urllib.parse.urlparse(str(url))
    except ValueError:
        return False
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    return host.endswith("amazon.in") and ("/dp/" in path or "/gp/product/" in path)


def open_url_for_provider(provider_id: str, product_url: str | None, fallback_search_url: str) -> tuple[str, str]:
    if provider_id == "jiomart":
        return fallback_search_url, "search"
    if provider_id == "amazon_fresh":
        if is_amazon_product_url(product_url):
            return str(product_url), "product"
        return fallback_search_url, "search"
    if product_url:
        return product_url, "product"
    return fallback_search_url, "search"


def format_pack(value: float | None, unit: str) -> str:
    if value is None or not unit:
        return ""
    if float(value).is_integer():
        return f"{int(value)}{unit}"
    return f"{value:g}{unit}"


def item_display_name(item: sqlite3.Row) -> str:
    parts = [item["brand"], item["name"], format_pack(item["pack_value"], item["pack_unit"])]
    return " ".join(part for part in parts if part).strip()


def public_item(item: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    payload = dict(item)
    mode = item_match_mode(item)
    payload["match_mode"] = mode
    payload["match_label"] = FLEXIBILITY_MODES[mode]["short_label"]
    payload["match_description"] = FLEXIBILITY_MODES[mode]["description"]
    return payload


def latest_prices_for_item(
    conn: sqlite3.Connection,
    item: sqlite3.Row,
    config: dict[str, Any],
    include_details: bool = True,
) -> list[dict[str, Any]]:
    providers = provider_by_id(config)
    rows = []
    for provider_id, provider in providers.items():
        latest = latest_available_observation(conn, int(item["id"]), provider_id)
        public_latest = public_observation(latest, include_details=include_details)
        generic_search = bool(public_latest and public_latest.get("is_generic_alternative"))
        row_search_url = search_url(
            provider,
            item,
            generic_search_query_for_item(item) if generic_search else None,
        )
        product_url = str(latest["url"] or "") if latest else ""
        row_open_url, row_open_kind = open_url_for_provider(provider_id, product_url, row_search_url)
        label_unit = (
            public_latest.get("pack_unit")
            if public_latest and public_latest.get("pack_unit")
            else item["pack_unit"]
        )
        row: dict[str, Any] = {
            "provider_id": provider_id,
            "provider_name": provider["name"],
            "status": provider.get("status", ""),
            "latest": public_latest,
            "unit_label": unit_price_label(label_unit),
            "open_url": row_open_url,
            "open_kind": row_open_kind,
        }
        if generic_search or not product_url:
            row["search_url"] = row_search_url
        rows.append(row)
    priced = [row for row in rows if row["latest"]]
    if priced:
        best_choice = min(price_choice_key(item, row["latest"]) for row in priced)
        for row in rows:
            row["is_best"] = bool(row["latest"] and price_choice_key(item, row["latest"]) == best_choice)
    return rows


def set_basket_item(conn: sqlite3.Connection, item_id: int, quantity: float) -> None:
    if quantity <= 0:
        conn.execute("DELETE FROM basket_items WHERE item_id = ?", (item_id,))
    else:
        conn.execute(
            """
            INSERT INTO basket_items (item_id, quantity, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(item_id) DO UPDATE SET
                quantity = excluded.quantity,
                updated_at = excluded.updated_at
            """,
            (item_id, quantity, utc_now_iso()),
        )
    conn.commit()


def clear_basket(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM basket_items")
    conn.commit()


def basket_rank_value(item: sqlite3.Row, latest: dict[str, Any]) -> tuple[float, str]:
    if item_ranks_by_unit_price(item) and latest.get("unit_price") is not None:
        return float(latest["unit_price"]), "unit_price"
    return float(latest["effective_price"]), "price"


def price_choice_key(item: sqlite3.Row, latest: dict[str, Any]) -> tuple[float, int, float]:
    rank_value, _ = basket_rank_value(item, latest)
    if latest.get("is_generic_alternative"):
        rank_value *= 1.05
    return (
        rank_value,
        1 if latest.get("is_generic_alternative") else 0,
        float(latest["effective_price"]),
    )


def build_basket(conn: sqlite3.Connection, config: dict[str, Any]) -> dict[str, Any]:
    providers = provider_by_id(config)
    rows = conn.execute(
        """
        SELECT b.quantity, i.*
        FROM basket_items b
        JOIN items i ON i.id = b.item_id
        WHERE i.active = 1
        ORDER BY i.category, i.name
        """
    ).fetchall()
    split_lines: list[dict[str, Any]] = []
    missing_split: list[str] = []
    basket_items_detail: list[dict[str, Any]] = []
    split_total = 0.0
    provider_options = {
        provider_id: {
            "provider_id": provider_id,
            "provider_name": provider["name"],
            "total": 0.0,
            "covered": 0,
            "missing": [],
            "lines": [],
            "complete": False,
            "coverage_percent": 0,
            "extra_vs_split": None,
            "within_convenience": False,
        }
        for provider_id, provider in providers.items()
    }

    for item in rows:
        quantity = float(item["quantity"])
        display_name = item_display_name(item)
        prices = latest_prices_for_item(conn, item, config)
        available = [
            price
            for price in prices
            if price["latest"] and int(price["latest"]["in_stock"]) == 1
        ]
        best = min(
            available,
            key=lambda price: price_choice_key(item, price["latest"]),
            default=None,
        )
        item_options: list[dict[str, Any]] = []
        if best:
            rank_value, rank_mode = basket_rank_value(item, best["latest"])
            line_total = float(best["latest"]["effective_price"]) * quantity
            best_product_url = best["latest"].get("url", "")
            best_search_url = best.get("search_url") or search_url(providers[best["provider_id"]], item)
            best_open_url, best_open_kind = open_url_for_provider(
                best["provider_id"],
                best_product_url,
                best_search_url,
            )
            split_total += line_total
            split_lines.append(
                {
                    "item_id": item["id"],
                    "display_name": display_name,
                    "quantity": quantity,
                    "provider_id": best["provider_id"],
                    "provider_name": best["provider_name"],
                    "unit_price": float(best["latest"]["effective_price"]),
                    "line_total": round(line_total, 2),
                    "rank_value": round(rank_value, 2),
                    "rank_mode": rank_mode,
                    "unit_label": best.get("unit_label", ""),
                    "matched_unit_price": best["latest"].get("unit_price"),
                    "observed_at": best["latest"].get("observed_at"),
                    "title": best["latest"].get("title", ""),
                    "url": best_product_url,
                    "search_url": best_search_url,
                    "open_url": best_open_url,
                    "open_kind": best_open_kind,
                }
            )
        else:
            missing_split.append(display_name)

        for price in prices:
            option = provider_options[price["provider_id"]]
            if price["latest"] and int(price["latest"]["in_stock"]) == 1:
                unit_effective = float(price["latest"]["effective_price"])
                rank_value, rank_mode = basket_rank_value(item, price["latest"])
                line_total = unit_effective * quantity
                product_url = price["latest"].get("url", "")
                price_search_url = price.get("search_url") or search_url(providers[price["provider_id"]], item)
                open_url, open_kind = open_url_for_provider(
                    price["provider_id"],
                    product_url,
                    price_search_url,
                )
                option["total"] += line_total
                option["covered"] += 1
                option["lines"].append(
                    {
                        "item_id": item["id"],
                        "display_name": display_name,
                        "quantity": quantity,
                        "unit_price": unit_effective,
                        "line_total": round(line_total, 2),
                        "rank_value": round(rank_value, 2),
                        "rank_mode": rank_mode,
                        "unit_label": price.get("unit_label", ""),
                        "matched_unit_price": price["latest"].get("unit_price"),
                        "observed_at": price["latest"].get("observed_at"),
                        "title": price["latest"].get("title", ""),
                        "url": product_url,
                        "search_url": price_search_url,
                        "open_url": open_url,
                        "open_kind": open_kind,
                    }
                )
                item_options.append(
                    {
                        "provider_id": price["provider_id"],
                        "provider_name": price["provider_name"],
                        "available": True,
                        "is_best": bool(best and price["provider_id"] == best["provider_id"]),
                        "unit_price": unit_effective,
                        "line_total": round(line_total, 2),
                        "rank_value": round(rank_value, 2),
                        "rank_mode": rank_mode,
                        "mrp": price["latest"].get("mrp"),
                        "unit_label": price.get("unit_label", ""),
                        "matched_unit_price": price["latest"].get("unit_price"),
                        "observed_at": price["latest"].get("observed_at"),
                        "title": price["latest"].get("title", ""),
                        "url": product_url,
                        "search_url": price_search_url,
                        "open_url": open_url,
                        "open_kind": open_kind,
                    }
                )
            else:
                price_search_url = search_url(providers[price["provider_id"]], item)
                fallback_open_url, fallback_open_kind = open_url_for_provider(
                    price["provider_id"],
                    price["latest"].get("url", "") if price["latest"] else "",
                    price_search_url,
                )
                option["missing"].append(display_name)
                item_options.append(
                    {
                        "provider_id": price["provider_id"],
                        "provider_name": price["provider_name"],
                        "available": False,
                        "is_best": False,
                        "unit_price": None,
                        "line_total": None,
                        "rank_value": None,
                        "rank_mode": "price",
                        "mrp": None,
                        "unit_label": price.get("unit_label", ""),
                        "matched_unit_price": None,
                        "observed_at": price["latest"].get("observed_at") if price["latest"] else None,
                        "title": price["latest"].get("title", "") if price["latest"] else "",
                        "url": price["latest"].get("url", "") if price["latest"] else price_search_url,
                        "search_url": price_search_url,
                        "open_url": fallback_open_url,
                        "open_kind": fallback_open_kind,
                    }
                )

        item_options.sort(
            key=lambda option: (
                not option["available"],
                not option["is_best"],
                0 if option["rank_value"] is None else float(option["rank_value"]),
                option["provider_name"],
            )
        )
        basket_items_detail.append(
            {
                "item_id": item["id"],
                "display_name": display_name,
                "quantity": quantity,
                "best": item_options[0] if item_options and item_options[0]["available"] else None,
                "options": item_options,
            }
        )

    for option in provider_options.values():
        option["total"] = round(option["total"], 2)
        option["missing_count"] = len(option["missing"])
        option["complete"] = bool(rows and option["missing_count"] == 0)
        option["coverage_percent"] = round((option["covered"] * 100 / len(rows)) if rows else 0)

    complete_options = [
        option
        for option in provider_options.values()
        if rows and option["missing_count"] == 0
    ]
    complete_options.sort(key=lambda option: option["total"])
    all_options = sorted(
        provider_options.values(),
        key=lambda option: (option["missing_count"], option["total"]),
    )
    best_single = complete_options[0] if complete_options else None
    split_rank_mode = "unit_value" if any(line.get("rank_mode") == "unit_price" for line in split_lines) else "price"
    threshold_rupees = float(config["settings"].get("single_app_convenience_threshold_rupees", 50))
    threshold_percent = float(config["settings"].get("single_app_convenience_threshold_percent", 5))
    convenience_gap = max(threshold_rupees, split_total * threshold_percent / 100)
    for option in provider_options.values():
        if option["complete"] and split_total and split_rank_mode != "unit_value":
            option["extra_vs_split"] = round(option["total"] - split_total, 2)
            option["within_convenience"] = option["extra_vs_split"] <= convenience_gap

    close_single_options = [
        option
        for option in complete_options
        if option.get("within_convenience")
    ][:3]
    if not close_single_options:
        close_single_options = complete_options[:3]

    provider_groups_map: dict[str, dict[str, Any]] = {}
    for line in split_lines:
        group = provider_groups_map.setdefault(
            line["provider_id"],
            {
                "provider_id": line["provider_id"],
                "provider_name": line["provider_name"],
                "total": 0.0,
                "item_count": 0,
                "lines": [],
            },
        )
        group["total"] += float(line["line_total"])
        group["item_count"] += 1
        group["lines"].append(line)
    provider_groups = sorted(
        [
            {
                **group,
                "total": round(float(group["total"]), 2),
                "open_urls": [
                    line["open_url"]
                    for line in group["lines"]
                    if line.get("open_url")
                ],
                "checklist": "\n".join(
                    [
                        f"{group['provider_name']} - {len(group['lines'])} item(s) - Rs {float(group['total']):.0f}",
                        *[
                            f"- {line['display_name']} x{line['quantity']:g} - Rs {float(line['line_total']):.0f}"
                            for line in group["lines"]
                        ],
                    ]
                ),
            }
            for group in provider_groups_map.values()
        ],
        key=lambda group: (-float(group["total"]), group["provider_name"]),
    )
    saving_vs_best_single = (
        round(best_single["total"] - split_total, 2)
        if best_single and split_total and split_rank_mode != "unit_value"
        else None
    )
    if split_rank_mode == "unit_value" and split_lines:
        recommendation = {
            "mode": "split",
            "provider_id": None,
            "provider_name": "Split across apps",
            "reason": "Best-value mode compares pack-normalized unit prices; one-app raw totals may use smaller packs.",
            "extra_cost": 0,
            "saving": 0,
        }
    elif best_single and split_total and best_single["total"] <= split_total + convenience_gap:
        recommendation = {
            "mode": "single_app",
            "provider_id": best_single["provider_id"],
            "provider_name": best_single["provider_name"],
            "reason": "One app is close enough to the cheapest split basket.",
            "extra_cost": round(best_single["total"] - split_total, 2),
            "saving": 0,
        }
    elif split_lines:
        if best_single and saving_vs_best_single is not None:
            reason = f"Split saves Rs {saving_vs_best_single:.0f} compared with the best one-app order."
        else:
            reason = "No single app has every basket item yet."
        recommendation = {
            "mode": "split",
            "provider_id": None,
            "provider_name": "Split across apps",
            "reason": reason,
            "extra_cost": 0,
            "saving": max(0, saving_vs_best_single or 0),
        }
    else:
        recommendation = {
            "mode": "empty",
            "provider_id": None,
            "provider_name": "",
            "reason": "Add items to the basket to compare apps.",
            "extra_cost": 0,
            "saving": 0,
        }

    return {
        "items": basket_items_detail,
        "split": {
            "total": round(split_total, 2),
            "lines": split_lines,
            "missing": missing_split,
            "provider_groups": provider_groups,
            "app_count": len(provider_groups),
            "saving_vs_best_single": saving_vs_best_single,
            "rank_mode": split_rank_mode,
            "label": "Best-value split" if split_rank_mode == "unit_value" else "Cheapest split",
        },
        "single_app_options": all_options,
        "close_single_options": close_single_options,
        "best_single": best_single,
        "recommendation": recommendation,
        "convenience_gap": round(convenience_gap, 2),
    }


TOKEN_STOPWORDS = {
    "and",
    "beverage",
    "buy",
    "calorie",
    "carbonated",
    "drink",
    "for",
    "low",
    "off",
    "pack",
    "pc",
    "pcs",
    "piece",
    "pieces",
    "profile",
    "refreshment",
    "results",
    "showing",
    "soft",
    "the",
    "with",
}

GENERIC_IDENTITY_TOKENS = {
    "bread",
    "brown",
    "browny",
    "fresh",
    "maida",
    "multigrain",
    "pure",
    "wheat",
    "whole",
}

REQUIRED_MATCH_TOKENS = {
    "activated",
    "atta",
    "badami",
    "bharta",
    "charcoal",
    "curd",
    "deal",
    "ghee",
    "hybrid",
    "jaggery",
    "kesar",
    "kimchi",
    "local",
    "malai",
    "masala",
    "moti",
    "multigrain",
    "ooty",
    "paneer",
    "protein",
    "safeda",
    "sattu",
    "toned",
    "zero",
}

FLEXIBLE_OPTIONAL_TOKENS = {
    "cut",
    "gel",
    "pack",
    "seed",
    "seeds",
}

GENERIC_FALLBACK_PROFILES = [
    {
        "key": "paneer",
        "query": "paneer",
        "required": {"paneer"},
        "reject": {"paratha", "masala", "spice", "tikka", "tofu", "cheese", "khoa", "cream"},
        "strict_pack": True,
    },
    {
        "key": "chicken",
        "query": "curry cut chicken",
        "required": {"chicken"},
        "reject": {"masala", "sausage", "salami", "nuggets", "kebab"},
        "strict_pack": False,
    },
    {
        "key": "potato",
        "query": "potato",
        "required": {"potato"},
        "reject": {"sweet", "paratha", "chips", "wafer", "masala"},
        "strict_pack": False,
    },
    {
        "key": "onion",
        "query": "onion",
        "required": {"onion"},
        "reject": {"powder", "rings"},
        "strict_pack": False,
    },
    {
        "key": "tomato",
        "query": "tomato",
        "required": {"tomato"},
        "reject": {"ketchup", "sauce", "puree", "powder"},
        "strict_pack": False,
    },
    {
        "key": "banana",
        "query": "banana",
        "required": {"banana"},
        "reject": {"chips", "wafer"},
        "strict_pack": False,
    },
    {
        "key": "cucumber",
        "query": "cucumber",
        "required": {"cucumber"},
        "reject": {"pickle"},
        "strict_pack": False,
    },
    {
        "key": "mushroom",
        "query": "mushroom",
        "required": {"mushroom"},
        "reject": {"soup", "sauce"},
        "strict_pack": True,
    },
    {
        "key": "milk",
        "query": "toned milk",
        "required": {"milk"},
        "reject": {"shake", "powder", "chocolate", "badam"},
        "strict_pack": True,
    },
    {
        "key": "curd",
        "query": "curd",
        "required": {"curd"},
        "reject": {"rice", "starter", "ghee", "butter"},
        "strict_pack": True,
    },
    {
        "key": "ghee",
        "query": "ghee",
        "required": {"ghee"},
        "reject": {"diya", "lamp"},
        "strict_pack": True,
    },
    {
        "key": "bread",
        "query": "whole wheat bread",
        "required": {"bread"},
        "reject": {"crumbs", "sticks", "toast", "rusk"},
        "strict_pack": True,
    },
    {
        "key": "sesame",
        "query": "black sesame seeds",
        "required": {"sesame"},
        "reject": {"oil", "bar", "laddu"},
        "strict_pack": False,
    },
]


def item_field(item: sqlite3.Row | dict[str, Any], key: str, default: Any = "") -> Any:
    try:
        value = item[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def item_match_mode(item: sqlite3.Row | dict[str, Any]) -> str:
    return normalize_match_mode(item_field(item, "match_mode", "exact"))


def is_flexible_item(item: sqlite3.Row | dict[str, Any]) -> bool:
    return item_match_mode(item) != "exact"


def item_allows_generic_fallback(item: sqlite3.Row | dict[str, Any]) -> bool:
    return item_match_mode(item) in {"category", "same_size", "unit"}


def item_requires_pack_match(item: sqlite3.Row | dict[str, Any]) -> bool:
    return item_match_mode(item) in {"exact", "same_size"}


def item_ranks_by_unit_price(item: sqlite3.Row | dict[str, Any]) -> bool:
    return item_match_mode(item) == "unit"


def text_tokens(value: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9]+", (value or "").lower())
        if token and token not in TOKEN_STOPWORDS and not any(char.isdigit() for char in token)
    ]


def generic_fallback_profile(item: sqlite3.Row | dict[str, Any]) -> dict[str, Any] | None:
    if not item_allows_generic_fallback(item):
        return None
    text = " ".join(
        str(item_field(item, key, "") or "")
        for key in ("brand", "name", "category")
    ).lower()
    tokens = set(text_tokens(text))
    for profile in GENERIC_FALLBACK_PROFILES:
        key = str(profile["key"])
        required = set(profile.get("required", set()))
        if key in tokens or required <= tokens:
            return profile
    return None


def generic_match_item(
    item: sqlite3.Row | dict[str, Any],
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    profile = profile or generic_fallback_profile(item) or {}
    mode = item_match_mode(item)
    return {
        "id": item_field(item, "id", 0),
        "name": profile.get("query") or item_field(item, "name", ""),
        "brand": "",
        "pack_value": item_field(item, "pack_value", None),
        "pack_unit": item_field(item, "pack_unit", ""),
        "category": item_field(item, "category", ""),
        "match_mode": "same_size" if mode == "same_size" else "category",
    }


def generic_search_query_for_item(item: sqlite3.Row | dict[str, Any]) -> str:
    profile = generic_fallback_profile(item)
    if not profile:
        return item_query_text(item)
    return str(profile["query"])


def identity_tokens_for_item(item: sqlite3.Row) -> set[str]:
    if is_flexible_item(item):
        return set()
    brand_tokens = set(text_tokens(item["brand"]))
    if brand_tokens:
        return brand_tokens
    name_tokens = text_tokens(item["name"])
    return {token for token in name_tokens[:3] if token not in GENERIC_IDENTITY_TOKENS}


def required_match_tokens_for_item(item: sqlite3.Row) -> set[str]:
    if is_flexible_item(item):
        tokens = text_tokens(item_display_name(item))
        strong_tokens = [token for token in tokens if token not in FLEXIBLE_OPTIONAL_TOKENS]
        return set(strong_tokens[:4] if len(strong_tokens) >= 2 else tokens[:3])
    return set(text_tokens(item_display_name(item))) & REQUIRED_MATCH_TOKENS


def price_amounts_from_text(value: str) -> list[float]:
    amounts: list[float] = []
    seen: set[str] = set()

    def add_amount(raw: str) -> None:
        amount = float(raw.replace(",", ""))
        if not 0 < amount < 100000:
            return
        key = f"{amount:.2f}"
        if key in seen:
            return
        seen.add(key)
        amounts.append(amount)

    patterns = [
        "(?:\u20b9|\u00e2\u201a\u00b9|Rs\\.?|INR)\\s*([0-9][0-9,]*(?:\\.[0-9]{1,2})?)",
        "([0-9][0-9,]*(?:\\.[0-9]{1,2})?)\\s*(?:\u20b9|\u00e2\u201a\u00b9|rupees?)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, value or "", flags=re.IGNORECASE):
            add_amount(match.group(1))

    for match in re.finditer(
        r"\b[0-9]{1,2}%\s*OFF\s+([0-9][0-9,]*(?:\.[0-9]{1,2})?)\b(?!\s*(?:MINS?|MINUTES?)\b)(?:\s+([0-9][0-9,]*(?:\.[0-9]{1,2})?)\b)?",
        value or "",
        flags=re.IGNORECASE,
    ):
        add_amount(match.group(1))
        if match.group(2):
            add_amount(match.group(2))
    for match in re.finditer(
        r"\b(?:ml|g|kg|ltr|litre|liter|combo)\b(?:\s*x\s*\d+)?(?:\s+[A-Za-z][A-Za-z+*,.'-]*){0,8}\s+([1-9][0-9]{1,4})(?=\s|$)",
        value or "",
        flags=re.IGNORECASE,
    ):
        add_amount(match.group(1))
    return amounts


def price_amount_spans_from_text(value: str) -> list[tuple[int, float]]:
    spans: list[tuple[int, float]] = []
    seen: set[tuple[int, str]] = set()

    def add_span(start: int, raw: str) -> None:
        amount = float(raw.replace(",", ""))
        if not 0 < amount < 100000:
            return
        key = (start, f"{amount:.2f}")
        if key in seen:
            return
        seen.add(key)
        spans.append((start, amount))

    patterns = [
        "(?:\u20b9|\u00e2\u201a\u00b9|Rs\\.?|INR)\\s*([0-9][0-9,]*(?:\\.[0-9]{1,2})?)",
        "([0-9][0-9,]*(?:\\.[0-9]{1,2})?)\\s*(?:\u20b9|\u00e2\u201a\u00b9|rupees?)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, value or "", flags=re.IGNORECASE):
            add_span(match.start(1), match.group(1))
    for match in re.finditer(
        r"\b[0-9]{1,2}%\s*OFF\s+([0-9][0-9,]*(?:\.[0-9]{1,2})?)\b(?!\s*(?:MINS?|MINUTES?)\b)(?:\s+([0-9][0-9,]*(?:\.[0-9]{1,2})?)\b)?",
        value or "",
        flags=re.IGNORECASE,
    ):
        add_span(match.start(1), match.group(1))
        if match.group(2):
            add_span(match.start(2), match.group(2))
    for match in re.finditer(
        r"\b(?:ml|g|kg|ltr|litre|liter|combo)\b(?:\s*x\s*\d+)?(?:\s+[A-Za-z][A-Za-z+*,.'-]*){0,8}\s+([1-9][0-9]{1,4})(?=\s|$)",
        value or "",
        flags=re.IGNORECASE,
    ):
        add_span(match.start(1), match.group(1))
    return sorted(spans, key=lambda item: item[0])


def probe_text_chunks(text: str) -> list[str]:
    if re.search(r"\b\d{1,2}\s+MINS\b", text, flags=re.IGNORECASE):
        return [
            chunk.strip()
            for chunk in re.split(r"\b\d{1,2}\s+MINS\b", text, flags=re.IGNORECASE)
            if len(chunk.strip()) >= 24
        ]
    return [
        chunk.strip()
        for chunk in re.split(r"\bADD\b", text, flags=re.IGNORECASE)
        if len(chunk.strip()) >= 24
    ]


def pack_variants(value: float | None, unit: str) -> set[str]:
    if value is None:
        return set()
    unit = normalize_pack_unit(unit)
    variants = set()
    as_int = int(value) if float(value).is_integer() else value
    variants.add(f"{as_int}{unit}".lower())
    variants.add(f"{as_int} {unit}".lower())
    base_value, base_unit = pack_to_base(value, unit)
    if base_value and base_unit and (base_value, base_unit) != (value, unit):
        base_int = int(base_value) if float(base_value).is_integer() else base_value
        variants.add(f"{base_int}{base_unit}".lower())
        variants.add(f"{base_int} {base_unit}".lower())
    return variants


def pack_matches_text(item: sqlite3.Row, value: str) -> bool:
    variants = pack_variants(item["pack_value"], item["pack_unit"])
    if not variants:
        return True
    text = re.sub(r"\s+", " ", (value or "").lower())
    compact = text.replace(" ", "")
    return any(variant in text or variant.replace(" ", "") in compact for variant in variants)


def candidate_pack_matches_item(item: sqlite3.Row, candidate: dict[str, Any]) -> bool:
    candidate_value = candidate.get("pack_value")
    candidate_unit = candidate.get("pack_unit", "")
    if candidate_value is None or not candidate_unit:
        return True
    return packs_equivalent(item["pack_value"], item["pack_unit"], float(candidate_value), str(candidate_unit))


def probe_link_candidates(result: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = []
    for link in result.get("links") or []:
        text = re.sub(r"\s+", " ", str(link.get("text") or "")).strip()
        if not text or "ADD" not in text.upper():
            continue
        for chunk in probe_text_chunks(text):
            amounts = price_amounts_from_text(chunk)
            if not amounts:
                continue
            price = amounts[0]
            mrp = None
            for amount in amounts[1:]:
                if amount >= price and amount <= price * 5:
                    mrp = amount
                    break
            pack_value, pack_unit = pack_from_text(chunk)
            candidates.append(
                {
                    "price": price,
                    "mrp": mrp,
                    "text": chunk,
                    "url": str(link.get("href") or result.get("final_url") or result.get("search_url") or ""),
                    "pack_value": pack_value,
                    "pack_unit": pack_unit,
                }
            )
    return candidates


def probe_text_candidates(result: dict[str, Any]) -> list[dict[str, Any]]:
    text = re.sub(r"\s+", " ", str(result.get("text_excerpt") or "")).strip()
    if not text:
        return []
    candidates = []
    for chunk in probe_text_chunks(text):
        amounts = price_amounts_from_text(chunk)
        if not amounts:
            continue
        price = amounts[0]
        mrp = None
        for amount in amounts[1:]:
            if amount >= price and amount <= price * 5:
                mrp = amount
                break
        pack_value, pack_unit = pack_from_text(chunk)
        candidates.append(
            {
                "price": price,
                "mrp": mrp,
                "text": chunk[:420],
                "url": str(result.get("final_url") or result.get("search_url") or ""),
                "pack_value": pack_value,
                "pack_unit": pack_unit,
            }
        )
    return candidates


def probe_card_candidates(result: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = []
    seen = set()
    for candidate in [*probe_link_candidates(result), *probe_text_candidates(result)]:
        key = (candidate["price"], candidate["mrp"], candidate["text"][:160])
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)
    return candidates


def candidate_unavailable(text: str) -> bool:
    clean = re.sub(r"\s+", " ", text or "").lower()
    return bool(re.search(r"\b(sold out|notify me|out of stock|currently unavailable)\b", clean))


def candidate_noise(text: str) -> bool:
    clean = re.sub(r"\s+", " ", text or "").lower()
    return "minimum value" in clean and "maximum value" in clean and "showing results for" in clean


def candidate_match_text(text: str) -> str:
    clean = re.sub(r"\s+", " ", text or "").strip()
    matches = list(re.finditer(r'showing results for\s+"[^"]+"\s*', clean, flags=re.IGNORECASE))
    if not matches:
        return clean
    tail = clean[matches[-1].end() :].strip()
    return tail if len(tail) >= 20 else clean


def result_with_shared_links(result: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    if result.get("links") or not payload.get("shared_links"):
        return result
    next_result = dict(result)
    next_result["links"] = payload.get("shared_links") or []
    return next_result


def candidate_matches_exact_item(
    item: sqlite3.Row | dict[str, Any],
    match_text: str,
    candidate_tokens: set[str],
) -> bool:
    target_tokens = set(text_tokens(item_display_name(item)))
    if len(target_tokens) < 2:
        return False
    required_tokens = required_match_tokens_for_item(item)
    if required_tokens and not required_tokens <= candidate_tokens:
        return False
    identity_tokens = identity_tokens_for_item(item)
    if identity_tokens and not (identity_tokens & candidate_tokens):
        return False
    if pack_variants(item_field(item, "pack_value", None), str(item_field(item, "pack_unit", ""))) and not pack_matches_text(item, match_text):
        return False
    hits = target_tokens & candidate_tokens
    return len(hits) >= min(3, len(target_tokens))


def best_probe_match(item: sqlite3.Row, result: dict[str, Any]) -> dict[str, Any] | None:
    generic_mode = result.get("search_kind") == "generic_fallback"
    generic_profile = generic_fallback_profile(item) if generic_mode else None
    match_item = generic_match_item(item, generic_profile) if generic_profile else item
    target_tokens = set(text_tokens(item_display_name(match_item)))
    if len(target_tokens) < 2:
        target_tokens = set(generic_profile.get("required", set())) if generic_profile else target_tokens
    if not target_tokens:
        return None
    flexible = is_flexible_item(item)
    best: dict[str, Any] | None = None
    requires_pack_match = bool(pack_variants(item["pack_value"], item["pack_unit"])) and item_requires_pack_match(item)
    identity_tokens = set() if (generic_profile or flexible) else identity_tokens_for_item(item)
    required_tokens = set(generic_profile.get("required", set())) if generic_profile else required_match_tokens_for_item(item)
    reject_tokens = set(generic_profile.get("reject", set())) if generic_profile else set()
    for candidate in probe_card_candidates(result):
        if candidate_unavailable(candidate["text"]) or candidate_noise(candidate["text"]):
            continue
        match_text = candidate_match_text(candidate["text"])
        candidate_tokens = set(text_tokens(match_text))
        if reject_tokens & candidate_tokens:
            continue
        if required_tokens and not required_tokens <= candidate_tokens:
            continue
        hits = target_tokens & candidate_tokens
        token_score = len(hits) / len(target_tokens)
        pack_match = pack_matches_text(item, match_text)
        if requires_pack_match and not pack_match:
            continue
        if requires_pack_match and not candidate_pack_matches_item(item, candidate):
            continue
        identity_hits = identity_tokens & candidate_tokens
        if identity_tokens and not identity_hits:
            continue
        score = token_score + (0.18 if pack_match else 0)
        candidate["score"] = round(score, 3)
        candidate["token_hits"] = sorted(hits)
        candidate["identity_hits"] = sorted(identity_hits)
        candidate["required_hits"] = sorted(required_tokens & candidate_tokens)
        candidate["pack_match"] = pack_match
        exact_item_match = bool(generic_profile and candidate_matches_exact_item(item, match_text, candidate_tokens))
        candidate["match_kind"] = "generic_fallback" if generic_profile and not exact_item_match else "exact"
        candidate["generic_query"] = generic_profile.get("query", "") if generic_profile else ""
        candidate["match_text"] = match_text[:420]
        candidate["unit_price"] = unit_price(
            float(candidate["price"]),
            candidate.get("pack_value"),
            candidate.get("pack_unit", ""),
        )
        if best is None:
            best = candidate
            continue
        best_unit = best.get("unit_price")
        candidate_unit = candidate.get("unit_price")
        best_has_unit = best_unit is not None
        candidate_has_unit = candidate_unit is not None
        if item_ranks_by_unit_price(item):
            best_value = float(best_unit) if best_unit is not None else float(best["price"])
            candidate_value = float(candidate_unit) if candidate_unit is not None else float(candidate["price"])
            if (
                candidate_has_unit,
                -candidate_value,
                -float(candidate["price"]),
                candidate["score"],
            ) > (
                best_has_unit,
                -best_value,
                -float(best["price"]),
                best["score"],
            ):
                best = candidate
        elif generic_profile:
            if (
                -float(candidate["price"]),
                candidate["score"],
                candidate_has_unit,
            ) > (
                -float(best["price"]),
                best["score"],
                best_has_unit,
            ):
                best = candidate
        elif (
            candidate["score"],
            candidate_has_unit,
            -(float(candidate_unit) if candidate_unit is not None else float(candidate["price"])),
            -float(candidate["price"]),
        ) > (
            best["score"],
            best_has_unit,
            -(float(best_unit) if best_unit is not None else float(best["price"])),
            -float(best["price"]),
        ):
            best = candidate
    if not best:
        return None
    if generic_profile:
        min_hits = min(1, len(target_tokens))
        if best["score"] < 0.55 or len(best["token_hits"]) < min_hits:
            return None
    elif flexible:
        min_hits = min(2, len(target_tokens))
        if best["score"] < 0.6 or len(best["token_hits"]) < min_hits:
            return None
    else:
        min_hits = min(3, len(target_tokens))
        if best["score"] < 0.78 or len(best["token_hits"]) < min_hits or not best["pack_match"]:
            return None
    return best


def review_source_label(source: str) -> str:
    labels = {
        "browser-probe": "found by app",
        "browser-probe-generic": "alternative found by app",
        "zepto-order-history": "order history",
        "amazon-now-order-history": "order history",
        "blinkit-order-history": "order history",
        "manual": "manual",
    }
    return labels.get(source or "", source or "unknown")


def observation_match_review(item: dict[str, Any], observation: sqlite3.Row) -> dict[str, Any]:
    title = re.sub(r"\s+", " ", str(observation["title"] or "")).strip()
    target_name = item_display_name(item)
    target_tokens = set(text_tokens(target_name))
    observed_tokens = set(text_tokens(title))
    hits = sorted(target_tokens & observed_tokens)
    required_tokens = required_match_tokens_for_item(item)
    identity_tokens = identity_tokens_for_item(item)
    flexible = is_flexible_item(item)
    missing_required = sorted(required_tokens - observed_tokens)
    identity_hits = sorted(identity_tokens & observed_tokens)
    token_score = round(len(hits) / len(target_tokens), 2) if target_tokens else 1.0
    pack_match = pack_matches_text(item, title) if title else False
    has_pack = bool(pack_variants(item["pack_value"], item["pack_unit"]))
    price = float(observation["price"])
    mrp = float(observation["mrp"]) if observation["mrp"] is not None else None
    notes: list[str] = []

    if not title:
        notes.append("missing product title")
    if has_pack and not pack_match and not flexible:
        notes.append("pack not visible")
    if identity_tokens and not identity_hits:
        notes.append("brand/name identity weak")
    if missing_required:
        notes.append(f"missing key word: {', '.join(missing_required[:3])}")
    low_threshold = 0.45 if flexible else 0.55
    watch_threshold = 0.65 if flexible else 0.75
    exact_threshold = 0.6 if flexible else 0.78
    if token_score < low_threshold:
        notes.append("low title overlap")
    elif token_score < watch_threshold:
        notes.append("partial title overlap")
    if mrp is not None and price > mrp * 1.05:
        notes.append("price above MRP")

    if flexible and not notes and token_score >= exact_threshold:
        status = "exact"
        label = "Good alternative match"
    elif not notes and token_score >= 0.78 and pack_match:
        status = "exact"
        label = "Looks right"
    elif any(note in notes for note in {"pack not visible", "brand/name identity weak", "low title overlap", "price above MRP"}) or missing_required:
        status = "review"
        label = "Needs checking"
    else:
        status = "watch"
        label = "Likely match"

    discount_percent = None
    if mrp and mrp > price:
        discount_percent = round((mrp - price) * 100 / mrp, 1)

    return {
        "status": status,
        "label": label,
        "notes": notes[:4],
        "token_score": token_score,
        "token_hits": hits[:8],
        "identity_hits": identity_hits[:6],
        "pack_match": pack_match,
        "has_pack": has_pack,
        "discount_percent": discount_percent,
        "source_label": review_source_label(str(observation["source"] or "")),
    }


def observation_pricing_review(
    item: sqlite3.Row | dict[str, Any],
    observation: sqlite3.Row | dict[str, Any],
) -> dict[str, Any]:
    source = str(item_field(observation, "source", "") or "")
    generic_source = source == "browser-probe-generic"
    generic_profile = generic_fallback_profile(item) if generic_source else None
    review_item = generic_match_item(item, generic_profile) if generic_profile else item
    review = observation_match_review(review_item, observation)
    title = re.sub(r"\s+", " ", str(item_field(observation, "title", "") or "")).strip()
    title_lower = title.lower()
    reasons: list[str] = []
    try:
        price = float(item_field(observation, "effective_price", item_field(observation, "price", 0)) or 0)
    except (TypeError, ValueError):
        price = 0
    try:
        mrp_value = item_field(observation, "mrp", None)
        mrp = float(mrp_value) if mrp_value is not None else None
    except (TypeError, ValueError):
        mrp = None

    if price <= 0:
        reasons.append("price missing")
    elif price <= 1:
        reasons.append("price is too low to trust")
    if mrp is not None and mrp > 0 and price > mrp * 1.08:
        reasons.append("price above MRP")
    if mrp is not None and mrp >= 50 and 0 < price <= mrp * 0.04:
        reasons.append("discount is too extreme to trust automatically")

    if source in {"seed", "seed-alert"}:
        reasons.append("demo price")
    if "order-history" in source:
        try:
            observed_at = parse_iso(str(item_field(observation, "observed_at", "")))
            if datetime.now(timezone.utc) - observed_at > timedelta(days=3):
                reasons.append("old order-history price")
        except (TypeError, ValueError):
            reasons.append("order-history date missing")
    if source.startswith("browser-probe"):
        if review["status"] == "review":
            reasons.append("weak product match")
        if generic_profile:
            title_tokens = set(text_tokens(title))
            required = set(generic_profile.get("required", set()))
            reject = set(generic_profile.get("reject", set()))
            if required and not required <= title_tokens:
                reasons.append("generic item missing")
            if reject & title_tokens:
                reasons.append("generic item looks wrong")
        name_tokens = text_tokens(str(item_field(item, "name", "")))
        leading_required: list[str] = []
        if len(name_tokens) >= 3:
            for token in name_tokens[:3]:
                if token in GENERIC_IDENTITY_TOKENS:
                    break
                leading_required.append(token)
                if len(leading_required) == 2:
                    break
        if (
            not generic_profile
            and not is_flexible_item(item)
            and not str(item_field(item, "brand", "") or "").strip()
            and leading_required
        ):
            if len(leading_required) == 1:
                missing_lead = not re.search(rf"\b{re.escape(leading_required[0])}\b", title_lower)
            else:
                missing_lead = not re.search(
                    rf"\b{re.escape(leading_required[0])}\s+{re.escape(leading_required[1])}\b",
                    title_lower,
                )
            if missing_lead:
                reasons.append("brand/name token missing")
        if re.search(r"\b(these items will be back soon|sold out|out of stock|notify me|currently unavailable)\b", title_lower):
            reasons.append("unavailable or suggestion page")
        try:
            raw_price = float(item_field(observation, "price", price) or price)
        except (TypeError, ValueError):
            raw_price = price
        amount_spans = price_amount_spans_from_text(title)
        price_pos = next((pos for pos, amount in amount_spans if abs(amount - raw_price) < 0.01), None)
        key_tokens = (
            set(generic_profile.get("required", set())) if generic_profile else set()
        ) or (
            identity_tokens_for_item(item)
            or required_match_tokens_for_item(item)
            or set(text_tokens(item_display_name(item)))
        )
        token_positions = [
            match.start()
            for token in key_tokens
            for match in re.finditer(rf"\b{re.escape(token)}\b", title_lower)
        ]
        target_pos = min(token_positions) if token_positions else None
        if price_pos is not None and target_pos is not None and price_pos < target_pos:
            intervening_prices = [amount for pos, amount in amount_spans if price_pos < pos < target_pos]
            if len(intervening_prices) > 2:
                reasons.append("price belongs to another nearby product")
        price_context = title
        if price_pos is not None:
            if target_pos is not None and target_pos > price_pos:
                later_prices = [pos for pos, _ in amount_spans if pos > target_pos]
                context_end = later_prices[0] if later_prices else min(len(title), target_pos + 180)
                price_context = title[max(0, price_pos - 8) : context_end]
            else:
                previous_prices = [pos for pos, _ in amount_spans if pos < price_pos]
                context_start = max(0, previous_prices[-1] + 1) if previous_prices else 0
                price_context = title[context_start : min(len(title), price_pos + 8)]
            local_tokens = set(text_tokens(price_context))
            if generic_profile:
                critical_tokens = set(generic_profile.get("required", set()))
            else:
                critical_tokens = (
                    set(leading_required)
                    | identity_tokens_for_item(item)
                    | required_match_tokens_for_item(item)
                )
            if len(critical_tokens) >= 2 and len(critical_tokens & local_tokens) < 2:
                reasons.append("price text does not match item")
        if (
            price_pos is not None
            and item_requires_pack_match(item)
            and pack_variants(item_field(item, "pack_value", None), str(item_field(item, "pack_unit", "")))
        ):
            if not pack_matches_text(review_item, price_context):
                reasons.append("pack not near price")

    return {
        "trusted": not reasons,
        "reasons": reasons[:5],
        "review": review,
    }


def recent_match_review(conn: sqlite3.Connection, config: dict[str, Any], limit: int = 100) -> dict[str, Any]:
    providers = provider_by_id(config)
    rows = conn.execute(
        """
        SELECT
            o.id AS observation_id,
            o.item_id,
            o.provider_id,
            o.observed_at,
            o.price,
            o.mrp,
            o.effective_price,
            o.unit_price,
            o.source,
            o.title,
            o.url,
            i.name,
            i.brand,
            i.pack_value,
            i.pack_unit,
            i.category,
            i.match_mode
        FROM observations o
        JOIN items i ON i.id = o.item_id
        WHERE i.active = 1
          AND o.source NOT IN ('seed', 'seed-alert')
        ORDER BY o.observed_at DESC, o.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    entries = []
    summary = {
        "total": 0,
        "exact": 0,
        "watch": 0,
        "review": 0,
        "ignored": 0,
        "scanner": 0,
        "order_history": 0,
    }
    for row in rows:
        item = {
            "id": row["item_id"],
            "name": row["name"],
            "brand": row["brand"],
            "pack_value": row["pack_value"],
            "pack_unit": row["pack_unit"],
            "category": row["category"],
            "match_mode": row["match_mode"],
        }
        pricing_review = observation_pricing_review(item, row)
        review = pricing_review["review"]
        provider = providers.get(row["provider_id"], {})
        row_search_url = search_url(provider, item) if provider else str(row["url"] or "")
        row_open_url, row_open_kind = open_url_for_provider(
            str(row["provider_id"]),
            str(row["url"] or ""),
            row_search_url,
        )
        summary["total"] += 1
        summary[review["status"]] += 1
        if not pricing_review["trusted"]:
            summary["ignored"] += 1
        if str(row["source"] or "").startswith("browser-probe"):
            summary["scanner"] += 1
        elif "history" in str(row["source"]):
            summary["order_history"] += 1
        entries.append(
            {
                "observation_id": int(row["observation_id"]),
                "item_id": int(row["item_id"]),
                "display_name": item_display_name(item),
                "category": row["category"],
                "provider_id": row["provider_id"],
                "provider_name": provider.get("name", row["provider_id"]),
                "observed_at": row["observed_at"],
                "price": float(row["price"]),
                "mrp": float(row["mrp"]) if row["mrp"] is not None else None,
                "effective_price": float(row["effective_price"]),
                "unit_price": float(row["unit_price"]) if row["unit_price"] is not None else None,
                "unit_label": unit_price_label(row["pack_unit"]),
                "source": row["source"],
                "title": row["title"],
                "url": row["url"],
                "search_url": row_search_url,
                "open_url": row_open_url,
                "open_kind": row_open_kind,
                "pack_label": format_pack(row["pack_value"], row["pack_unit"]),
                "review": review,
                "pricing_trusted": pricing_review["trusted"],
                "pricing_reasons": pricing_review["reasons"],
            }
        )
    return {
        "summary": summary,
        "items": entries,
    }


def normalize_probe_match_statuses(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    provider_id: str,
    path: Path,
) -> dict[str, Any]:
    if payload.get("match_status_mode") != "best_probe_match":
        return payload
    changed = False
    matched = 0
    for result in payload.get("results") or []:
        if not isinstance(result, dict):
            continue
        if result.get("status") not in {"price_candidates", "no_price_found"}:
            continue
        item_id = int(result.get("item_id") or 0)
        item = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        match = best_probe_match(item, result_with_shared_links(result, payload)) if item else None
        if match:
            matched += 1
            next_candidates = [float(match["price"])]
            if match.get("mrp"):
                next_candidates.append(float(match["mrp"]))
            updates = {
                "status": "price_candidates",
                "price_candidates": next_candidates,
                "text_excerpt": match.get("match_text") or match["text"],
                "final_url": match.get("url", ""),
            }
        else:
            updates = {
                "status": "no_price_found",
                "price_candidates": [],
                "text_excerpt": "",
                "final_url": "",
            }
        for key, value in updates.items():
            if result.get(key) != value:
                result[key] = value
                changed = True
    if payload.get("matched_count") != matched:
        payload["matched_count"] = matched
        changed = True
    if changed:
        write_json(path, payload)
    return payload


def import_probe_results_for_provider(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    data_dir: Path,
    provider_id: str,
) -> int:
    path = data_dir / f"{provider_id}_probe_results.json"
    if not path.exists():
        return 0
    try:
        payload = read_json(path)
    except (OSError, json.JSONDecodeError):
        return 0
    payload = normalize_probe_match_statuses(conn, payload, provider_id, path)
    imported = 0
    for result in payload.get("results") or []:
        if not isinstance(result, dict):
            continue
        item_id = int(result.get("item_id") or 0)
        item = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not item:
            continue
        match = best_probe_match(item, result_with_shared_links(result, payload))
        if not match:
            if result.get("status") in {"price_candidates", "no_price_found"}:
                conn.execute(
                    """
                    UPDATE observations
                    SET in_stock = 0
                    WHERE id = (
                        SELECT id
                        FROM observations
                        WHERE item_id = ?
                          AND provider_id = ?
                          AND source LIKE 'browser-probe%'
                        ORDER BY observed_at DESC, id DESC
                        LIMIT 1
                    )
                    """,
                    (item_id, provider_id),
                )
                conn.commit()
            continue
        observed_at = str(result.get("observed_at") or payload.get("created_at") or utc_now_iso())
        url = match.get("url") or str(result.get("final_url") or result.get("search_url") or "")
        observation_source = "browser-probe-generic" if match.get("match_kind") == "generic_fallback" else "browser-probe"
        existing_observation = conn.execute(
            """
            SELECT id
            FROM observations
            WHERE item_id = ?
              AND provider_id = ?
              AND observed_at = ?
              AND price = ?
              AND source = ?
            LIMIT 1
            """,
            (item_id, provider_id, observed_at, float(match["price"]), observation_source),
        ).fetchone()
        if existing_observation:
            continue
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO probe_imports (
                provider_id, item_id, observed_at, price, url, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (provider_id, item_id, observed_at, float(match["price"]), url, utc_now_iso()),
        )
        add_observation(
            conn,
            ObservationInput(
                item_id=item_id,
                provider_id=provider_id,
                observed_at=observed_at,
                price=float(match["price"]),
                mrp=float(match["mrp"]) if match.get("mrp") else None,
                source=observation_source,
                url=url,
                title=(match.get("match_text") or match["text"])[:240],
                pack_value=float(match["pack_value"]) if match.get("pack_value") else item["pack_value"],
                pack_unit=match.get("pack_unit") or item["pack_unit"],
            ),
            config,
        )
        imported += 1
    return imported


def probe_result_signature(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    stat = path.stat()
    return {
        "mtime_ns": stat.st_mtime_ns,
        "size": stat.st_size,
    }


def mark_probe_results_imported(data_dir: Path, provider_id: str, imported: int = 0) -> None:
    path = data_dir / f"{provider_id}_probe_results.json"
    signature = probe_result_signature(path)
    if not signature:
        return
    cache_path = data_dir / "probe_import_cache.json"
    try:
        cache = read_json(cache_path)
    except (OSError, json.JSONDecodeError):
        cache = {}
    if not isinstance(cache, dict):
        cache = {}
    next_cache = dict(cache)
    next_cache[provider_id] = {
        "app_version": APP_VERSION,
        "signature": signature,
        "imported": int(imported),
        "checked_at": utc_now_iso(),
    }
    if next_cache != cache:
        write_json(cache_path, next_cache)


def import_probe_results(conn: sqlite3.Connection, config: dict[str, Any], data_dir: Path) -> int:
    total = 0
    cache_path = data_dir / "probe_import_cache.json"
    try:
        cache = read_json(cache_path)
    except (OSError, json.JSONDecodeError):
        cache = {}
    next_cache: dict[str, Any] = dict(cache) if isinstance(cache, dict) else {}
    for provider in config.get("providers", PROVIDERS):
        provider_id = provider["id"]
        path = data_dir / f"{provider_id}_probe_results.json"
        signature = probe_result_signature(path)
        if not signature:
            continue
        cached = next_cache.get(provider_id, {})
        if (
            cached.get("app_version") == APP_VERSION
            and cached.get("signature") == signature
        ):
            continue
        imported = import_probe_results_for_provider(conn, config, data_dir, provider_id)
        total += imported
        mark_probe_results_imported(data_dir, provider_id, imported=imported)
        next_cache[provider_id] = read_json(cache_path).get(provider_id, {})
    if next_cache != cache:
        write_json(cache_path, next_cache)
    return total


def probe_status_label(status: str) -> str:
    labels = {
        "not_run": "Not checked",
        "blocked": "Blocked by site",
        "rate_limited": "Rate-limited",
        "profile_in_use": "Setup browser open",
        "needs_setup": "Needs login/location",
        "price_candidates": "Found price text",
        "no_price_found": "No price found",
        "navigation_error": "Navigation issue",
        "error": "Check error",
    }
    return labels.get(status or "", status or "Unknown")


def probe_status_message(status: str) -> str:
    messages = {
        "not_run": "No browser price check has run for this app yet.",
        "blocked": "The site blocked this browser profile. A visible logged-in profile is likely needed.",
        "rate_limited": "The site slowed or rejected the request. The next check should use a visible profile and slower pacing.",
        "profile_in_use": "The setup browser is still using this app profile. Close it, then check prices again.",
        "needs_setup": "Open the dedicated setup browser, log in, and set the delivery address.",
        "price_candidates": "The app page showed price text. Confident matches are saved into price history automatically.",
        "no_price_found": "The page opened, but no useful price text was detected.",
        "navigation_error": "The page did not load cleanly.",
        "error": "The price check hit an unexpected error.",
    }
    return messages.get(status or "", "Latest price check status is available.")


def screenshot_url_from_path(data_dir: Path, value: str) -> str:
    if not value:
        return ""
    try:
        path = Path(value).resolve()
        screenshot_root = (data_dir / "probe-screenshots").resolve()
        if screenshot_root in path.parents and path.is_file():
            return "/probe-screenshots/" + urllib.parse.quote(path.name)
    except (OSError, RuntimeError, ValueError):
        return ""
    return ""


def latest_probe_status(payload: dict[str, Any]) -> str:
    results = [row for row in payload.get("results", []) if isinstance(row, dict)]
    if not results:
        return "not_run"
    counts: dict[str, int] = {}
    for result in results:
        status = str(result.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    priority = [
        "price_candidates",
        "profile_in_use",
        "blocked",
        "rate_limited",
        "needs_setup",
        "navigation_error",
        "error",
        "no_price_found",
    ]
    for status in priority:
        if status in counts:
            return status
    return next(iter(counts.keys()))


def probe_import_count(
    conn: sqlite3.Connection,
    provider_id: str,
    results: list[dict[str, Any]],
) -> int:
    imported = 0
    for result in results:
        item_id = int(result.get("item_id") or 0)
        observed_at = str(result.get("observed_at") or "")
        if not item_id or not observed_at:
            continue
        row = conn.execute(
            """
            SELECT id
            FROM observations
            WHERE item_id = ?
              AND provider_id = ?
              AND observed_at = ?
              AND source = 'browser-probe'
            LIMIT 1
            """,
            (item_id, provider_id, observed_at),
        ).fetchone()
        if row:
            imported += 1
    return imported


def build_scanner_status(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    data_dir: Path,
) -> dict[str, Any]:
    providers = []
    latest_created_at = ""
    for provider in config.get("providers", PROVIDERS):
        provider_id = provider["id"]
        path = data_dir / f"{provider_id}_probe_results.json"
        if not path.exists():
            providers.append(
                {
                    "provider_id": provider_id,
                    "provider_name": provider["name"],
                    "provider_status": provider.get("status", ""),
                    "status": "not_run",
                    "label": probe_status_label("not_run"),
                    "message": probe_status_message("not_run"),
                    "checked_at": "",
                    "target_count": 0,
                    "result_count": 0,
                    "imported_count": 0,
                    "counts": {},
                    "sample": None,
                    "action": read_worker_action(data_dir, provider_id),
                }
            )
            continue
        try:
            payload = read_json(path)
        except (OSError, json.JSONDecodeError):
            payload = {
                "created_at": "",
                "target_count": 0,
                "results": [
                    {
                        "status": "error",
                        "error": "Could not read probe results.",
                    }
                ],
            }
        results = [row for row in payload.get("results", []) if isinstance(row, dict)]
        counts: dict[str, int] = {}
        for result in results:
            status = str(result.get("status") or "unknown")
            counts[status] = counts.get(status, 0) + 1
        status = latest_probe_status(payload)
        checked_at = str(payload.get("created_at") or "")
        if checked_at and checked_at > latest_created_at:
            latest_created_at = checked_at
        sample = next((row for row in results if str(row.get("status") or "") == status), results[0] if results else None)
        providers.append(
            {
                "provider_id": provider_id,
                "provider_name": provider["name"],
                "provider_status": provider.get("status", ""),
                "status": status,
                "label": probe_status_label(status),
                "message": probe_status_message(status),
                "checked_at": checked_at,
                "target_count": int(payload.get("target_count") or len(results)),
                "result_count": len(results),
                "imported_count": probe_import_count(conn, provider_id, results),
                "counts": counts,
                "action": read_worker_action(data_dir, provider_id),
                "sample": {
                    "display_name": sample.get("display_name", "") if sample else "",
                    "excerpt": compact_text(sample.get("text_excerpt") or sample.get("error") or "") if sample else "",
                    "price_candidates": sample.get("price_candidates", [])[:6] if sample else [],
                    "screenshot_url": screenshot_url_from_path(data_dir, sample.get("screenshot_path", "")) if sample else "",
                }
                if sample
                else None,
            }
        )
    checked = [provider for provider in providers if provider["status"] != "not_run"]
    return {
        "updated_at": latest_created_at,
        "checked_count": len(checked),
        "providers": providers,
    }


def read_worker_action(data_dir: Path, provider_id: str) -> dict[str, Any] | None:
    path = data_dir / f"{provider_id}_worker_action.json"
    if not path.exists():
        return None
    try:
        payload = read_json(path)
    except (OSError, json.JSONDecodeError):
        return None
    status = str(payload.get("status", ""))
    pid = payload.get("pid", "")
    if status == "started" and pid:
        status = "running" if pid_is_running(pid) else "finished"
    return {
        "action": payload.get("action", ""),
        "status": status,
        "started_at": payload.get("started_at", ""),
        "pid": pid,
        "message": payload.get("message", ""),
    }


def pid_is_running(pid: Any) -> bool:
    try:
        process_id = int(pid)
    except (TypeError, ValueError):
        return False
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {process_id}", "/NH"],
                capture_output=True,
                text=True,
                timeout=2,
                creationflags=creationflags,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return str(process_id) in result.stdout
    try:
        os.kill(process_id, 0)
        return True
    except OSError:
        return False


def default_headless_scan() -> bool:
    value = os.environ.get("GROCERY_SCAN_HEADLESS", "").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return os.name != "nt" and not os.environ.get("DISPLAY")


def ps_single_quoted(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def close_provider_profile_browsers(data_dir: Path, provider_id: str) -> int:
    if os.name != "nt":
        return 0
    profile_dir = (data_dir / "browser-profiles" / re.sub(r"[^A-Za-z0-9_-]", "_", provider_id).lower()).resolve()
    if not profile_dir.exists():
        return 0
    script = (
        f"$needle = {ps_single_quoted(str(profile_dir))}; "
        "Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | "
        "Where-Object { $_.CommandLine -like \"*$needle*\" } | "
        "Select-Object -ExpandProperty ProcessId"
    )
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0
    pids = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    killed = 0
    for pid in sorted(set(pids)):
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=creationflags,
        )
        killed += 1
    if killed:
        time.sleep(2)
    return killed


def launch_worker_action(
    config: dict[str, Any],
    data_dir: Path,
    action: str,
    provider_id: str,
    minutes: float = 30,
    limit: int = 3,
    headless: bool = False,
) -> dict[str, Any]:
    providers = provider_by_id(config)
    if provider_id not in providers:
        raise ValueError("Unknown provider")
    if action not in {"setup", "probe"}:
        raise ValueError("Unknown worker action")

    root = Path(__file__).resolve().parent
    worker_path = root / "browser_scan_worker.mjs"
    node_path = shutil.which("node") or "node"
    command = [node_path, str(worker_path), action, provider_id]
    if action == "setup":
        command += ["--minutes", str(max(1, min(float(minutes), 120)))]
        message = "Setup browser opened."
    else:
        closed_count = close_provider_profile_browsers(data_dir, provider_id)
        command += ["--limit", str(max(1, min(int(limit), 120)))]
        if headless or default_headless_scan():
            command.append("--headless")
        message = "Price refresh started."
        if closed_count:
            message = f"Closed {closed_count} setup browser process(es), then started the price check."

    data_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = data_dir / f"{provider_id}_{action}.log"
    stderr_path = data_dir / f"{provider_id}_{action}.err.log"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    with stdout_path.open("a", encoding="utf-8") as stdout, stderr_path.open("a", encoding="utf-8") as stderr:
        stdout.write(f"\n--- {utc_now_iso()} {' '.join(command)} ---\n")
        stdout.flush()
        process = subprocess.Popen(
            command,
            cwd=str(root),
            stdout=stdout,
            stderr=stderr,
            creationflags=creationflags,
        )
    payload = {
        "action": action,
        "provider_id": provider_id,
        "provider_name": providers[provider_id]["name"],
        "status": "started",
        "started_at": utc_now_iso(),
        "pid": process.pid,
        "message": message,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
    }
    write_json(data_dir / f"{provider_id}_worker_action.json", payload)
    return payload


def auto_scan_status(data_dir: Path) -> dict[str, Any]:
    path = data_dir / "auto_scan_status.json"
    if not path.exists():
        return {
            "enabled": False,
            "state": "stopped",
            "pid": "",
            "providers": AUTO_SCAN_PROVIDER_IDS,
            "limit": 20,
            "interval_minutes": 60,
            "cycles": 0,
            "last_runs": [],
            "last_error": "",
        }
    try:
        status = read_json(path)
    except (OSError, json.JSONDecodeError):
        status = {}
    pid = status.get("pid", "")
    running = pid_is_running(pid) if pid else False
    if status.get("enabled") and not running:
        status["enabled"] = False
        status["state"] = "stopped"
    status["running"] = running
    return status


def launch_auto_scan(data_dir: Path, limit: int = 20, interval_minutes: float = 60) -> dict[str, Any]:
    existing = auto_scan_status(data_dir)
    if existing.get("running"):
        return existing
    root = Path(__file__).resolve().parent
    worker_path = root / "auto_scan_worker.py"
    command = [
        sys.executable,
        str(worker_path),
        "--providers",
        ",".join(AUTO_SCAN_PROVIDER_IDS),
        "--limit",
        str(max(1, min(int(limit), 120))),
        "--interval-minutes",
        str(max(5, min(float(interval_minutes), 240))),
    ]
    data_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = data_dir / "auto_scan.log"
    stderr_path = data_dir / "auto_scan.err.log"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    with stdout_path.open("a", encoding="utf-8") as stdout, stderr_path.open("a", encoding="utf-8") as stderr:
        stdout.write(f"\n--- {utc_now_iso()} {' '.join(command)} ---\n")
        stdout.flush()
        process = subprocess.Popen(
            command,
            cwd=str(root),
            stdout=stdout,
            stderr=stderr,
            creationflags=creationflags,
        )
    payload = {
        "enabled": True,
        "running": True,
        "state": "starting",
        "pid": process.pid,
        "providers": AUTO_SCAN_PROVIDER_IDS,
        "limit": max(1, min(int(limit), 120)),
        "interval_minutes": max(5, min(float(interval_minutes), 240)),
        "started_at": utc_now_iso(),
        "cycles": int(existing.get("cycles") or 0),
        "last_runs": existing.get("last_runs", []),
        "last_error": "",
        "rotation": True,
        "cycle_batch_number": existing.get("cycle_batch_number") or int(existing.get("cycles") or 0) + 1,
        "next_batch": existing.get("next_batch", {}),
        "message": "Background checking started.",
    }
    write_json(data_dir / "auto_scan_status.json", payload)
    return payload


def basket_scan_status(data_dir: Path) -> dict[str, Any]:
    path = data_dir / "basket_scan_status.json"
    if not path.exists():
        return {
            "running": False,
            "state": "idle",
            "pid": "",
            "providers": AUTO_SCAN_PROVIDER_IDS,
            "last_runs": [],
            "last_error": "",
        }
    try:
        status = read_json(path)
    except (OSError, json.JSONDecodeError):
        status = {}
    pid = status.get("pid", "")
    running = pid_is_running(pid) if pid else False
    if not running and status.get("state") in {"starting", "scanning"}:
        status["state"] = "finished"
    status["running"] = running
    return status


def launch_basket_scan(
    data_dir: Path,
    item_count: int,
    scan_kind: str = "basket",
    focus_item_id: int | None = None,
    focus_label: str = "",
    resume_auto: bool = False,
) -> dict[str, Any]:
    existing = basket_scan_status(data_dir)
    if existing.get("running"):
        return existing
    root = Path(__file__).resolve().parent
    worker_path = root / "basket_scan_worker.py"
    command = [
        sys.executable,
        str(worker_path),
        "--limit",
        str(max(1, min(int(item_count), 120))),
    ]
    if resume_auto:
        command.append("--resume-auto")
    if default_headless_scan():
        command.append("--headless")
    data_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = data_dir / "basket_scan.log"
    stderr_path = data_dir / "basket_scan.err.log"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    with stdout_path.open("a", encoding="utf-8") as stdout, stderr_path.open("a", encoding="utf-8") as stderr:
        stdout.write(f"\n--- {utc_now_iso()} {' '.join(command)} ---\n")
        stdout.flush()
        process = subprocess.Popen(
            command,
            cwd=str(root),
            stdout=stdout,
            stderr=stderr,
            creationflags=creationflags,
        )
    payload = {
        "running": True,
        "state": "starting",
        "pid": process.pid,
        "providers": AUTO_SCAN_PROVIDER_IDS,
        "limit": max(1, min(int(item_count), 120)),
        "scan_kind": scan_kind,
        "focus_item_id": focus_item_id,
        "focus_label": focus_label,
        "resume_auto": resume_auto,
        "started_at": utc_now_iso(),
        "last_runs": existing.get("last_runs", []),
        "last_error": "",
        "message": f"{focus_label} price refresh started." if scan_kind == "item" and focus_label else "Basket price refresh started.",
    }
    write_json(data_dir / "basket_scan_status.json", payload)
    return payload


def probe_imports_busy(config: dict[str, Any], data_dir: Path) -> bool:
    active_states = {"starting", "scanning"}
    auto_status = auto_scan_status(data_dir)
    if auto_status.get("running") and auto_status.get("state") in active_states:
        return True
    basket_status = basket_scan_status(data_dir)
    if basket_status.get("running") and basket_status.get("state") in active_states:
        return True
    for provider in config.get("providers", PROVIDERS):
        action = read_worker_action(data_dir, provider["id"])
        if (
            action
            and action.get("action") == "probe"
            and action.get("status") in {"started", "running"}
        ):
            return True
    return False


def stop_auto_scan(data_dir: Path) -> dict[str, Any]:
    status = auto_scan_status(data_dir)
    pid = status.get("pid")
    if pid and pid_is_running(pid):
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=creationflags,
            )
        else:
            os.kill(int(pid), 15)
    status.update(
        {
            "enabled": False,
            "running": False,
            "state": "stopped",
            "next_run_at": "",
            "updated_at": utc_now_iso(),
            "message": "Background checking paused.",
        }
    )
    write_json(data_dir / "auto_scan_status.json", status)
    return status


def build_state(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    data_dir: Path | None = None,
) -> dict[str, Any]:
    providers = provider_by_id(config)
    prune_alerts(conn, config)
    items = conn.execute("SELECT * FROM items WHERE active = 1 ORDER BY category, name").fetchall()
    alerts = conn.execute(
        """
        SELECT a.*, i.name, i.brand, i.pack_value, i.pack_unit
        FROM alerts a
        JOIN items i ON i.id = a.item_id
        WHERE a.dismissed_at = ''
          AND (a.expires_at = '' OR a.expires_at > ?)
        ORDER BY a.observed_at DESC, a.id DESC
        LIMIT 50
        """
        ,
        (utc_now_iso(),),
    ).fetchall()
    alert_cards: list[dict[str, Any]] = []
    for row in alerts:
        alert = dict(row)
        provider = providers.get(alert["provider_id"], {})
        if provider:
            alert["provider_name"] = provider.get("name", alert["provider_id"])
            alert["search_url"] = search_url(provider, alert)
            alert["open_url"], alert["open_kind"] = open_url_for_provider(
                alert["provider_id"],
                "",
                alert["search_url"],
            )
        alert_cards.append(alert)
    item_cards = []
    for item in items:
        prices = latest_prices_for_item(conn, item, config, include_details=False)
        priced = [row for row in prices if row["latest"] and row["latest"]["in_stock"]]
        best = None
        if priced:
            best = min(priced, key=lambda row: price_choice_key(item, row["latest"]))
        item_cards.append(
            {
                "item": public_item(item),
                "display_name": item_display_name(item),
                "prices": prices,
                "best": best,
            }
        )
    return {
        "app": APP_NAME,
        "version": APP_VERSION,
        "now": utc_now_iso(),
        "config": public_config(config),
        "flexibility_modes": FLEXIBILITY_MODES,
        "providers": config.get("providers", PROVIDERS),
        "item_count": len(item_cards),
        "items": item_cards,
        "alerts": alert_cards,
        "basket": build_basket(conn, config),
        "match_review": recent_match_review(conn, config, limit=12),
        "scan": latest_scan_run(conn),
        "scanner": build_scanner_status(conn, config, data_dir) if data_dir else None,
        "auto_scan": auto_scan_status(data_dir) if data_dir else None,
        "basket_scan": basket_scan_status(data_dir) if data_dir else None,
    }


def seed_demo_data(conn: sqlite3.Connection, config: dict[str, Any]) -> None:
    existing = conn.execute("SELECT COUNT(*) AS count FROM items").fetchone()["count"]
    if existing:
        return
    items = [
        ItemInput("Butter", "Amul", 500, "g", "Dairy", 250),
        ItemInput("Toned Milk", "Amul", 1, "l", "Dairy", 70),
        ItemInput("Sunflower Oil", "Fortune", 1, "l", "Staples", 125),
        ItemInput("Atta", "Aashirvaad", 5, "kg", "Staples", 240),
        ItemInput("Basmati Rice", "Daawat", 5, "kg", "Staples", 550),
        ItemInput("Eggs", "", 12, "pc", "Protein", 90),
    ]
    item_ids = [add_item(conn, item) for item in items]
    providers = [provider["id"] for provider in PROVIDERS]
    base_prices = {
        "Butter": 285,
        "Toned Milk": 74,
        "Sunflower Oil": 145,
        "Atta": 270,
        "Basmati Rice": 680,
        "Eggs": 115,
    }
    now = datetime.now(timezone.utc)
    random.seed(42)
    for item_id in item_ids:
        item = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        base = base_prices[item["name"]]
        for provider_id in providers:
            provider_offset = random.uniform(-0.07, 0.08)
            for days_ago in (28, 21, 14, 9, 5, 2):
                noise = random.uniform(-0.04, 0.05)
                price = round(base * (1 + provider_offset + noise))
                add_observation(
                    conn,
                    ObservationInput(
                        item_id=item_id,
                        provider_id=provider_id,
                        price=price,
                        mrp=round(base * 1.18),
                        observed_at=(now - timedelta(days=days_ago)).isoformat(timespec="seconds"),
                        source="demo",
                        title=item_display_name(item),
                    ),
                    config,
                )
        if item["name"] in {"Sunflower Oil", "Atta"}:
            add_observation(
                conn,
                ObservationInput(
                    item_id=item_id,
                    provider_id="blinkit" if item["name"] == "Sunflower Oil" else "jiomart",
                    price=round(base * 0.74),
                    mrp=round(base * 1.18),
                    observed_at=now.isoformat(timespec="seconds"),
                    source="demo-alert",
                    title=item_display_name(item),
                ),
                config,
            )


def clear_all_data(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM alerts")
    conn.execute("DELETE FROM observations")
    conn.execute("DELETE FROM items")
    conn.commit()


def infer_category(title: str) -> str:
    text = title.lower()
    buckets = [
        ("Dairy", ["milk", "curd", "paneer", "cheese", "butter", "ghee"]),
        ("Produce", ["banana", "onion", "cucumber", "mushroom", "tomato", "potato", "fruit"]),
        ("Staples", ["atta", "rice", "dal", "oil", "sattu", "masala", "powder", "paste", "asafoetida"]),
        ("Beverages", ["coca-cola", "drink", "juice", "ors", "electrolyte"]),
        ("Bread", ["bread"]),
        ("Personal Care", ["toothpaste", "charcoal", "skin", "shampoo"]),
        ("Household", ["cleaner", "descaler", "washing"]),
    ]
    for category, needles in buckets:
        if any(needle in text for needle in needles):
            return category
    return "Zepto history"


def import_zepto_orders(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    extract_path: Path,
    replace: bool = False,
) -> dict[str, int]:
    payload = read_json(extract_path)
    if replace:
        clear_all_data(conn)
    item_map: dict[str, int] = {}
    imported_observations = 0
    skipped_freebies = 0
    for order in payload.get("orders", []):
        observed_at = order.get("order_date") or utc_now_iso()
        for product in order.get("products", []):
            price = product.get("price")
            if price is None or float(price) <= 0:
                skipped_freebies += 1
                continue
            title = product.get("title", "").strip()
            if not title:
                continue
            pack_text = product.get("pack_text", "")
            key = f"{title}||{pack_text}".lower()
            if key not in item_map:
                item_id = add_item(
                    conn,
                    ItemInput(
                        name=title,
                        pack_value=product.get("pack_value"),
                        pack_unit=product.get("pack_unit") or "",
                        category=infer_category(title),
                        target_price=product.get("min_price"),
                        notes=f"Imported from Zepto order history; pack: {pack_text}",
                    ),
                )
                item_map[key] = item_id
            add_observation(
                conn,
                ObservationInput(
                    item_id=item_map[key],
                    provider_id="zepto",
                    price=float(price),
                    mrp=float(product["mrp"]) if product.get("mrp") is not None else None,
                    observed_at=observed_at,
                    source="zepto-order-history",
                    title=title,
                    url=product.get("url", ""),
                    pack_value=product.get("pack_value"),
                    pack_unit=product.get("pack_unit") or "",
                ),
                config,
            )
            imported_observations += 1
    return {
        "items": len(item_map),
        "observations": imported_observations,
        "skipped_freebies": skipped_freebies,
        "orders": int(payload.get("order_count") or len(payload.get("orders", []))),
    }


class GroceryHandler(http.server.BaseHTTPRequestHandler):
    config_path: Path
    db_path: Path
    _state_cache: dict[str, Any] | None = None
    _state_cache_at: float = 0.0
    _state_cache_lock = threading.Lock()
    _state_refresh_lock = threading.Lock()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/manifest.webmanifest":
            self.send_text(
                json.dumps(render_manifest(), ensure_ascii=False),
                "application/manifest+json; charset=utf-8",
                headers={"Cache-Control": "no-cache"},
            )
            return
        if parsed.path == "/service-worker.js":
            self.send_text(
                render_service_worker(),
                "text/javascript; charset=utf-8",
                headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
            )
            return
        if parsed.path.startswith("/icons/"):
            self.serve_icon(parsed.path)
            return
        config = load_config(self.config_path)
        authorized, set_cookie = self.check_access(config, parsed)
        if parsed.path == "/" or parsed.path == "/index.html":
            if not authorized:
                self.send_html(render_locked_page(), status=403)
                return
            headers = self.access_cookie_header(config) if set_cookie else {}
            headers["Cache-Control"] = "no-store"
            self.send_html(render_app(), headers=headers)
            return
        if parsed.path == "/amazon-now":
            if not authorized:
                self.send_html(render_locked_page(), status=403)
                return
            headers = self.access_cookie_header(config) if set_cookie else {}
            headers["Cache-Control"] = "no-store"
            self.send_html(render_amazon_now_handoff(), headers=headers)
            return
        if parsed.path.startswith("/api/state"):
            if not authorized:
                self.send_json({"ok": False, "error": "Private access key required"}, status=403)
                return
            self.send_json(self.get_state(full_ok=self.client_accepts_gzip()))
            return
        if parsed.path == "/api/watchlist/export":
            if not authorized:
                self.send_json({"ok": False, "error": "Private access key required"}, status=403)
                return
            self.handle_export_watchlist()
            return
        if parsed.path.startswith("/probe-screenshots/"):
            if not authorized:
                self.send_json({"ok": False, "error": "Private access key required"}, status=403)
                return
            self.serve_probe_screenshot(parsed.path)
            return
        self.send_error(404)

    def do_POST(self) -> None:
        try:
            parsed = urllib.parse.urlparse(self.path)
            config = load_config(self.config_path)
            authorized, _ = self.check_access(config, parsed)
            if not authorized:
                self.send_json({"ok": False, "error": "Private access key required"}, status=403)
                return
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            if parsed.path == "/api/items":
                self.handle_add_item(payload)
                return
            if parsed.path == "/api/watchlist/import":
                self.handle_import_watchlist(payload)
                return
            if parsed.path == "/api/item/update":
                self.handle_update_item(payload)
                return
            if parsed.path == "/api/observations":
                self.handle_add_observation(payload)
                return
            if parsed.path == "/api/alerts/dismiss":
                self.handle_dismiss_alert(payload)
                return
            if parsed.path == "/api/alerts/clear":
                self.handle_clear_alerts()
                return
            if parsed.path == "/api/basket":
                self.handle_set_basket(payload)
                return
            if parsed.path == "/api/basket/clear":
                self.handle_clear_basket()
                return
            if parsed.path == "/api/basket/scan":
                self.handle_basket_scan()
                return
            if parsed.path == "/api/item/scan":
                self.handle_item_scan(payload)
                return
            if parsed.path == "/api/scan":
                self.handle_scan()
                return
            if parsed.path == "/api/provider/setup":
                self.handle_provider_setup(payload)
                return
            if parsed.path == "/api/provider/probe":
                self.handle_provider_probe(payload)
                return
            if parsed.path == "/api/auto-scan/start":
                self.handle_auto_scan_start(payload)
                return
            if parsed.path == "/api/auto-scan/stop":
                self.handle_auto_scan_stop()
                return
            self.send_error(404)
        except Exception as exc:
            self.send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=500)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def client_accepts_gzip(self) -> bool:
        return "gzip" in self.headers.get("Accept-Encoding", "").lower()

    def get_state(self, full_ok: bool = False) -> dict[str, Any]:
        cls = type(self)
        config = load_config(self.config_path)
        scan_busy = probe_imports_busy(config, self.db_path.parent)
        current_basket_scan = basket_scan_status(self.db_path.parent)
        with cls._state_cache_lock:
            cached = cls._state_cache
            cache_age = time.monotonic() - cls._state_cache_at
        if cached is not None:
            cached_basket_scan = cached.get("basket_scan") or {}
            scan_finished_changed = bool(
                not scan_busy
                and current_basket_scan.get("finished_at")
                and current_basket_scan.get("finished_at") != cached_basket_scan.get("finished_at")
            )
            if scan_finished_changed:
                state = self.build_fresh_state(config=config)
                return self.remember_state(state)
            if cache_age >= STATE_CACHE_SECONDS and not scan_busy:
                self.refresh_state_cache_later(config)
            if scan_busy:
                return self.with_live_scan_status(cached, compact=False)
            return cached

        with cls._state_refresh_lock:
            with cls._state_cache_lock:
                cached = cls._state_cache
            if cached is not None:
                if scan_busy:
                    return self.with_live_scan_status(cached, compact=False)
                return cached
            state = self.build_fresh_state(config=config)
            state = self.remember_state(state)
            return self.with_live_scan_status(state, compact=False) if scan_busy else state

    def build_fresh_state(self, config: dict[str, Any] | None = None) -> dict[str, Any]:
        if config is None:
            config = load_config(self.config_path)
        conn = open_db(self.db_path)
        try:
            if not probe_imports_busy(config, self.db_path.parent):
                import_probe_results(conn, config, self.db_path.parent)
            return build_state(conn, config, self.db_path.parent)
        finally:
            conn.close()

    def remember_state(self, state: dict[str, Any]) -> dict[str, Any]:
        cls = type(self)
        with cls._state_cache_lock:
            cls._state_cache = state
            cls._state_cache_at = time.monotonic()
        return state

    def refresh_state_cache_later(self, config: dict[str, Any]) -> None:
        cls = type(self)
        if not cls._state_refresh_lock.acquire(blocking=False):
            return
        config_path = self.config_path
        db_path = self.db_path

        def refresh() -> None:
            try:
                fresh_config = load_config(config_path)
                conn = open_db(db_path)
                try:
                    if not probe_imports_busy(fresh_config, db_path.parent):
                        import_probe_results(conn, fresh_config, db_path.parent)
                    state = build_state(conn, fresh_config, db_path.parent)
                finally:
                    conn.close()
                with cls._state_cache_lock:
                    cls._state_cache = state
                    cls._state_cache_at = time.monotonic()
            finally:
                cls._state_refresh_lock.release()

        threading.Thread(target=refresh, name="grocery-state-refresh", daemon=True).start()

    def with_live_scan_status(self, state: dict[str, Any], compact: bool = True) -> dict[str, Any]:
        live = dict(state)
        live["auto_scan"] = auto_scan_status(self.db_path.parent)
        live["basket_scan"] = basket_scan_status(self.db_path.parent)
        live["item_count"] = state.get("item_count", len(state.get("items", [])))
        if not compact:
            return live
        live["items"] = []
        basket = dict(state.get("basket") or {})
        basket["items"] = [
            {key: value for key, value in item.items() if key != "options"}
            for item in basket.get("items", [])
        ]
        basket["single_app_options"] = []
        basket["close_single_options"] = []
        live["basket"] = basket
        review = state.get("match_review") or {}
        live["match_review"] = {
            "summary": review.get("summary", {}),
            "items": [],
        }
        return live

    def check_access(self, config: dict[str, Any], parsed: urllib.parse.ParseResult) -> tuple[bool, bool]:
        access = config.get("access", {})
        if not access.get("enabled", True):
            return True, False
        expected = str(access.get("key") or "")
        if not expected:
            return True, False
        query_key = urllib.parse.parse_qs(parsed.query).get("key", [""])[0]
        if query_key == expected:
            return True, True
        cookie_header = self.headers.get("Cookie", "")
        if cookie_header:
            try:
                cookie = http.cookies.SimpleCookie()
                cookie.load(cookie_header)
                if cookie.get(ACCESS_COOKIE) and cookie[ACCESS_COOKIE].value == expected:
                    return True, False
            except http.cookies.CookieError:
                return False, False
        return False, False

    def access_cookie_header(self, config: dict[str, Any]) -> dict[str, str]:
        token = str(config.get("access", {}).get("key") or "")
        if not token:
            return {}
        return {
            "Set-Cookie": (
                f"{ACCESS_COOKIE}={token}; Path=/; Max-Age=31536000; "
                "SameSite=Lax"
            )
        }

    def handle_export_watchlist(self) -> None:
        conn = open_db(self.db_path)
        try:
            payload = export_watchlist(conn)
        finally:
            conn.close()
        filename = f"grocery-cockpit-watchlist-{datetime.now().strftime('%Y%m%d')}.json"
        self.send_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            "application/json; charset=utf-8",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )

    def handle_import_watchlist(self, payload: Any) -> None:
        config = load_config(self.config_path)
        replace = bool(payload.get("replace")) if isinstance(payload, dict) else False
        conn = open_db(self.db_path)
        try:
            result = import_watchlist(conn, payload, replace=replace)
            state = self.remember_state(build_state(conn, config, self.db_path.parent))
            self.send_json({"ok": True, "result": result, "state": state})
        finally:
            conn.close()

    def handle_add_item(self, payload: dict[str, Any]) -> None:
        config = load_config(self.config_path)
        conn = open_db(self.db_path)
        try:
            item_id = add_item(
                conn,
                ItemInput(
                    name=payload["name"],
                    brand=payload.get("brand", ""),
                    pack_value=float(payload["pack_value"]) if payload.get("pack_value") else None,
                    pack_unit=payload.get("pack_unit", ""),
                    category=payload.get("category", ""),
                    target_price=float(payload["target_price"]) if payload.get("target_price") else None,
                    notes=payload.get("notes", ""),
                    match_mode=payload.get("match_mode", "exact"),
                ),
            )
            if payload.get("add_to_basket"):
                set_basket_item(conn, item_id, float(payload.get("quantity") or 1))
            basket_scan = None
            scan_error = ""
            paused_auto = False
            if payload.get("scan_now"):
                existing_scan = basket_scan_status(self.db_path.parent)
                if existing_scan.get("running"):
                    scan_error = "Another focused refresh is already running."
                else:
                    item = conn.execute(
                        "SELECT * FROM items WHERE id = ? AND active = 1",
                        (item_id,),
                    ).fetchone()
                    if item is None:
                        scan_error = "Item not found."
                    else:
                        focus_label = item_display_name(item)
                        plan = create_scan_plan(
                            conn,
                            config,
                            self.db_path.parent,
                            source="quick_need",
                            item_ids=[item_id],
                            plan_filename="basket_scan_plan.json",
                        )
                        auto_status = auto_scan_status(self.db_path.parent)
                        paused_auto = bool(auto_status.get("enabled") or auto_status.get("running"))
                        if auto_status.get("running"):
                            stop_auto_scan(self.db_path.parent)
                        basket_scan = launch_basket_scan(
                            self.db_path.parent,
                            item_count=1,
                            scan_kind="item",
                            focus_item_id=item_id,
                            focus_label=focus_label,
                            resume_auto=paused_auto,
                        )
            state = self.remember_state(build_state(conn, config, self.db_path.parent))
            self.send_json(
                {
                    "ok": True,
                    "item_id": item_id,
                    "state": state,
                    "basket_scan": basket_scan,
                    "scan_started": bool(basket_scan),
                    "scan_error": scan_error,
                    "paused_auto": paused_auto,
                }
            )
        finally:
            conn.close()

    def handle_update_item(self, payload: dict[str, Any]) -> None:
        raw_item_id = payload.get("item_id")
        try:
            item_id = int(raw_item_id)
        except (TypeError, ValueError):
            self.send_json({"ok": False, "error": "Invalid item"}, status=400)
            return
        updates: dict[str, Any] = {}
        if "match_mode" in payload:
            updates["match_mode"] = normalize_match_mode(payload.get("match_mode"))
        if not updates:
            self.send_json({"ok": False, "error": "Nothing to update"}, status=400)
            return
        config = load_config(self.config_path)
        conn = open_db(self.db_path)
        try:
            item = conn.execute("SELECT id FROM items WHERE id = ? AND active = 1", (item_id,)).fetchone()
            if item is None:
                self.send_json({"ok": False, "error": "Item not found"}, status=404)
                return
            for key, value in updates.items():
                conn.execute(f"UPDATE items SET {key} = ? WHERE id = ?", (value, item_id))
            conn.commit()
            plan = create_scan_plan(conn, config, self.db_path.parent, source="item_settings")
            state = self.remember_state(build_state(conn, config, self.db_path.parent))
            self.send_json({"ok": True, "state": state, "plan": plan["summary"], "item_id": item_id})
        finally:
            conn.close()

    def handle_add_observation(self, payload: dict[str, Any]) -> None:
        config = load_config(self.config_path)
        conn = open_db(self.db_path)
        try:
            add_observation(
                conn,
                ObservationInput(
                    item_id=int(payload["item_id"]),
                    provider_id=payload["provider_id"],
                    price=float(payload["price"]),
                    mrp=float(payload["mrp"]) if payload.get("mrp") else None,
                    observed_at=payload.get("observed_at") or utc_now_iso(),
                    delivery_fee=float(payload.get("delivery_fee") or 0),
                    handling_fee=float(payload.get("handling_fee") or 0),
                    in_stock=bool(payload.get("in_stock", True)),
                    source=payload.get("source", "manual"),
                    url=payload.get("url", ""),
                    title=payload.get("title", ""),
                    pack_value=float(payload["pack_value"]) if payload.get("pack_value") else None,
                    pack_unit=payload.get("pack_unit", ""),
                ),
                config,
            )
            self.send_json({"ok": True, "state": self.remember_state(build_state(conn, config, self.db_path.parent))})
        finally:
            conn.close()

    def handle_dismiss_alert(self, payload: dict[str, Any]) -> None:
        raw_alert_id = payload.get("alert_id")
        try:
            alert_id = int(raw_alert_id)
        except (TypeError, ValueError):
            self.send_json({"ok": False, "error": "Invalid alert"}, status=400)
            return
        config = load_config(self.config_path)
        conn = open_db(self.db_path)
        try:
            conn.execute("DELETE FROM alerts WHERE id = ?", (alert_id,))
            conn.commit()
            self.send_json({"ok": True, "state": self.remember_state(build_state(conn, config, self.db_path.parent))})
        finally:
            conn.close()

    def handle_clear_alerts(self) -> None:
        config = load_config(self.config_path)
        conn = open_db(self.db_path)
        try:
            conn.execute("DELETE FROM alerts")
            conn.commit()
            self.send_json({"ok": True, "state": self.remember_state(build_state(conn, config, self.db_path.parent))})
        finally:
            conn.close()

    def handle_set_basket(self, payload: dict[str, Any]) -> None:
        raw_item_id = payload.get("item_id")
        if raw_item_id in (None, ""):
            self.send_json({"ok": False, "error": "Missing item_id"}, status=400)
            return
        try:
            item_id = int(raw_item_id)
            quantity = float(payload.get("quantity", 1))
        except (TypeError, ValueError):
            self.send_json({"ok": False, "error": "Invalid basket item or quantity"}, status=400)
            return
        config = load_config(self.config_path)
        conn = open_db(self.db_path)
        try:
            set_basket_item(conn, item_id, quantity)
            self.send_json({"ok": True, "state": self.remember_state(build_state(conn, config, self.db_path.parent))})
        finally:
            conn.close()

    def handle_clear_basket(self) -> None:
        config = load_config(self.config_path)
        conn = open_db(self.db_path)
        try:
            clear_basket(conn)
            self.send_json({"ok": True, "state": self.remember_state(build_state(conn, config, self.db_path.parent))})
        finally:
            conn.close()

    def handle_basket_scan(self) -> None:
        config = load_config(self.config_path)
        conn = open_db(self.db_path)
        try:
            existing_scan = basket_scan_status(self.db_path.parent)
            if existing_scan.get("running"):
                self.send_json({"ok": False, "error": "A focused refresh is already running."}, status=409)
                return
            rows = conn.execute(
                """
                SELECT b.item_id
                FROM basket_items b
                JOIN items i ON i.id = b.item_id
                WHERE i.active = 1
                ORDER BY i.category, i.name
                """
            ).fetchall()
            item_ids = [int(row["item_id"]) for row in rows]
            if not item_ids:
                self.send_json({"ok": False, "error": "Add items to the basket first."}, status=400)
                return
            plan = create_scan_plan(
                conn,
                config,
                self.db_path.parent,
                source="basket",
                item_ids=item_ids,
                plan_filename="basket_scan_plan.json",
            )
            auto_status = auto_scan_status(self.db_path.parent)
            resume_auto = bool(auto_status.get("enabled") or auto_status.get("running"))
            if auto_status.get("running"):
                stop_auto_scan(self.db_path.parent)
            status = launch_basket_scan(
                self.db_path.parent,
                item_count=len(item_ids),
                scan_kind="basket",
                resume_auto=resume_auto,
            )
            self.send_json(
                {
                    "ok": True,
                    "plan": plan["summary"],
                    "run_id": plan["run_id"],
                    "basket_scan": status,
                }
            )
        finally:
            conn.close()

    def handle_item_scan(self, payload: dict[str, Any]) -> None:
        raw_item_id = payload.get("item_id")
        if raw_item_id in (None, ""):
            self.send_json({"ok": False, "error": "Missing item_id"}, status=400)
            return
        try:
            item_id = int(raw_item_id)
        except (TypeError, ValueError):
            self.send_json({"ok": False, "error": "Invalid item_id"}, status=400)
            return
        existing_scan = basket_scan_status(self.db_path.parent)
        if existing_scan.get("running"):
            self.send_json({"ok": False, "error": "Another focused refresh is already running."}, status=409)
            return
        config = load_config(self.config_path)
        conn = open_db(self.db_path)
        try:
            item = conn.execute(
                "SELECT * FROM items WHERE id = ? AND active = 1",
                (item_id,),
            ).fetchone()
            if item is None:
                self.send_json({"ok": False, "error": "Item not found"}, status=404)
                return
            focus_label = item_display_name(item)
            plan = create_scan_plan(
                conn,
                config,
                self.db_path.parent,
                source="item",
                item_ids=[item_id],
                plan_filename="basket_scan_plan.json",
            )
            auto_status = auto_scan_status(self.db_path.parent)
            resume_auto = bool(auto_status.get("enabled") or auto_status.get("running"))
            if auto_status.get("running"):
                stop_auto_scan(self.db_path.parent)
            status = launch_basket_scan(
                self.db_path.parent,
                item_count=1,
                scan_kind="item",
                focus_item_id=item_id,
                focus_label=focus_label,
                resume_auto=resume_auto,
            )
            self.send_json(
                {
                    "ok": True,
                    "plan": plan["summary"],
                    "run_id": plan["run_id"],
                    "basket_scan": status,
                    "item_id": item_id,
                    "display_name": focus_label,
                    "paused_auto": resume_auto,
                }
            )
        finally:
            conn.close()

    def handle_scan(self) -> None:
        config = load_config(self.config_path)
        conn = open_db(self.db_path)
        try:
            plan = create_scan_plan(conn, config, self.db_path.parent, source="phone")
            self.send_json({"ok": True, "plan": plan["summary"], "run_id": plan["run_id"]})
        finally:
            conn.close()

    def handle_provider_setup(self, payload: dict[str, Any]) -> None:
        config = load_config(self.config_path)
        provider_id = str(payload.get("provider_id") or "")
        minutes = float(payload.get("minutes") or 30)
        action = launch_worker_action(config, self.db_path.parent, "setup", provider_id, minutes=minutes)
        self.send_json({"ok": True, "action": action})

    def handle_provider_probe(self, payload: dict[str, Any]) -> None:
        config = load_config(self.config_path)
        provider_id = str(payload.get("provider_id") or "")
        limit = int(payload.get("limit") or 3)
        headless = bool(payload.get("headless", False))
        action = launch_worker_action(
            config,
            self.db_path.parent,
            "probe",
            provider_id,
            limit=limit,
            headless=headless,
        )
        self.send_json({"ok": True, "action": action})

    def handle_auto_scan_start(self, payload: dict[str, Any]) -> None:
        limit = int(payload.get("limit") or 20)
        interval_minutes = float(payload.get("interval_minutes") or 60)
        status = launch_auto_scan(self.db_path.parent, limit=limit, interval_minutes=interval_minutes)
        self.send_json({"ok": True, "auto_scan": status})

    def handle_auto_scan_stop(self) -> None:
        status = stop_auto_scan(self.db_path.parent)
        self.send_json({"ok": True, "auto_scan": status})

    def serve_icon(self, request_path: str) -> None:
        icon_path = Path(request_path).name
        if not re.fullmatch(r"icon-(180|192|512)\.png", icon_path):
            self.send_error(404)
            return
        path = Path(__file__).resolve().parent / "static" / icon_path
        if not path.exists():
            self.send_error(404)
            return
        payload = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "public, max-age=31536000")
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()

    def serve_probe_screenshot(self, request_path: str) -> None:
        filename = Path(urllib.parse.unquote(request_path)).name
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.png", filename):
            self.send_error(404)
            return
        path = self.db_path.parent / "probe-screenshots" / filename
        if not path.exists():
            self.send_error(404)
            return
        payload = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "private, max-age=60")
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()

    def send_html(self, text: str, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self.send_text(text, "text/html; charset=utf-8", status=status, headers=headers)

    def write_response_body(self, payload: bytes) -> None:
        # Large uncompressed JSON responses can be fragile on mobile browsers if
        # they are written in one shot.
        chunk_size = 64 * 1024
        for offset in range(0, len(payload), chunk_size):
            self.wfile.write(payload[offset : offset + chunk_size])
        self.wfile.flush()

    def send_text(
        self,
        text: str,
        content_type: str,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        payload = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.write_response_body(payload)

    def send_json(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        accepts_gzip = "gzip" in self.headers.get("Accept-Encoding", "").lower()
        if accepts_gzip and len(data) > 1024:
            body = gzip.compress(data)
            content_encoding = "gzip"
        else:
            body = data
            content_encoding = ""
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            if content_encoding:
                self.send_header("Content-Encoding", content_encoding)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.write_response_body(body)
        except (BrokenPipeError, ConnectionResetError):
            return


def write_last_run(path: Path, payload: dict[str, Any]) -> None:
    write_json(path, payload)


def provider_scan_mode(provider: dict[str, Any]) -> str:
    status = provider.get("status", "")
    if status in {"browser-ready", "official-api-ready", "order-history-ready"}:
        return "ready"
    if status in {"browser-profile-needed", "needs-access"}:
        return "setup"
    if status in {"manual-link", "official-api-needed", "chrome-assisted"}:
        return "manual"
    return "planned"


def latest_scan_run(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT *
        FROM scan_runs
        ORDER BY requested_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    return dict(row) if row else None


def create_scan_plan(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    data_dir: Path,
    source: str = "ui",
    item_ids: list[int] | None = None,
    plan_filename: str = "latest_scan_plan.json",
) -> dict[str, Any]:
    requested_at = utc_now_iso()
    providers = list(provider_by_id(config).values())
    if item_ids:
        placeholders = ",".join("?" for _ in item_ids)
        items = conn.execute(
            f"""
            SELECT *
            FROM items
            WHERE active = 1 AND id IN ({placeholders})
            ORDER BY category, name
            """,
            tuple(item_ids),
        ).fetchall()
    else:
        items = conn.execute(
            """
            SELECT *
            FROM items
            WHERE active = 1
            ORDER BY category, name
            """
        ).fetchall()
    targets: list[dict[str, Any]] = []
    for item in items:
        fallback_profile = generic_fallback_profile(item)
        mode = item_match_mode(item)
        search_query = generic_search_query_for_item(item) if fallback_profile else item_query_text(item)
        search_kind = "generic_fallback" if fallback_profile else mode
        for provider in providers:
            targets.append(
                {
                    "item_id": int(item["id"]),
                    "display_name": item_display_name(item),
                    "search_display_name": search_query,
                    "search_kind": search_kind,
                    "generic_key": fallback_profile.get("key", "") if fallback_profile else "",
                    "brand": item["brand"],
                    "name": item["name"],
                    "pack_value": item["pack_value"],
                    "pack_unit": item["pack_unit"],
                    "category": item["category"],
                    "match_mode": mode,
                    "provider_id": provider["id"],
                    "provider_name": provider["name"],
                    "provider_status": provider.get("status", ""),
                    "scan_mode": provider_scan_mode(provider),
                    "search_url": search_url(provider, item, search_query),
                }
            )
    plan = {
        "app": APP_NAME,
        "version": APP_VERSION,
        "requested_at": requested_at,
        "source": source,
        "location": config.get("location", {}),
        "summary": {
            "providers": len(providers),
            "items": len(items),
            "targets": len(targets),
            "ready_targets": sum(1 for target in targets if target["scan_mode"] == "ready"),
            "setup_targets": sum(1 for target in targets if target["scan_mode"] == "setup"),
            "manual_targets": sum(1 for target in targets if target["scan_mode"] == "manual"),
        },
        "targets": targets,
    }
    data_dir.mkdir(parents=True, exist_ok=True)
    plan_path = data_dir / plan_filename
    write_json(plan_path, plan)
    cursor = conn.execute(
        """
        INSERT INTO scan_runs (
            requested_at, status, source, provider_count, item_count, target_count, message, plan_path
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            requested_at,
            "queued",
            source,
            len(providers),
            len(items),
            len(targets),
            "Scan plan created. Browser worker is the next connection point.",
            str(plan_path),
        ),
    )
    run_id = int(cursor.lastrowid)
    conn.commit()
    plan["run_id"] = run_id
    write_json(plan_path, plan)
    write_last_run(
        data_dir / "last_run.json",
        {
            "run_id": run_id,
            "status": "queued",
            "observed_at": requested_at,
            "message": "Scan plan created. Browser worker is the next connection point.",
            "plan_path": str(plan_path),
            "summary": plan["summary"],
        },
    )
    return plan


def render_manifest() -> dict[str, Any]:
    return {
        "name": "Groceries",
        "short_name": "Groceries",
        "description": "Personal grocery price cockpit.",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#f6f7fb",
        "theme_color": "#e23744",
        "orientation": "portrait-primary",
        "icons": [
            {
                "src": "/icons/icon-180.png",
                "sizes": "180x180",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": "/icons/icon-192.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any maskable",
            },
            {
                "src": "/icons/icon-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any maskable",
            },
        ],
    }


def render_service_worker() -> str:
    return f"""const CACHE_NAME = 'grocery-cockpit-{APP_VERSION}';
const APP_SHELL = [
  '/manifest.webmanifest',
  '/icons/icon-180.png',
  '/icons/icon-192.png',
  '/icons/icon-512.png'
];

self.addEventListener('install', event => {{
  event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(APP_SHELL)));
  self.skipWaiting();
}});

self.addEventListener('activate', event => {{
  event.waitUntil(
    caches.keys().then(keys => Promise.all(keys
      .filter(key => key !== CACHE_NAME)
      .map(key => caches.delete(key))))
  );
  self.clients.claim();
}});

self.addEventListener('fetch', event => {{
  const url = new URL(event.request.url);
  if (url.origin !== location.origin) return;
  if (url.pathname.startsWith('/api/')) {{
    event.respondWith(fetch(event.request));
    return;
  }}
  if (event.request.mode === 'navigate' || url.pathname === '/' || url.pathname === '/index.html') {{
    event.respondWith(
      fetch(event.request).catch(() => new Response('<!doctype html><title>Grocery Cockpit</title><main style="font-family:system-ui;padding:20px">Grocery Cockpit is offline. Reopen it when the server is reachable.</main>', {{
        headers: {{'Content-Type': 'text/html; charset=utf-8'}}
      }}))
    );
    return;
  }}
  event.respondWith(
    caches.match(event.request).then(cached => cached || fetch(event.request).then(response => {{
      const copy = response.clone();
      caches.open(CACHE_NAME).then(cache => cache.put(event.request, copy));
      return response;
    }}))
  );
}});

self.addEventListener('notificationclick', event => {{
  event.notification.close();
  const url = event.notification.data?.url || '/';
  event.waitUntil(clients.openWindow(url));
}});
"""


def render_locked_page() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="theme-color" content="#e23744">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-title" content="Groceries">
  <meta name="apple-mobile-web-app-status-bar-style" content="default">
  <link rel="manifest" href="/manifest.webmanifest">
  <link rel="apple-touch-icon" href="/icons/icon-180.png">
  <link rel="icon" sizes="192x192" href="/icons/icon-192.png">
  <title>Groceries</title>
  <style>
    body {
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 24px;
      background: #f6f7fb;
      color: #191919;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
    }
    main {
      width: min(420px, 100%);
      background: #fff;
      border: 1px solid #f0d8c9;
      border-radius: 8px;
      padding: 20px;
    }
    h1 { margin: 0 0 8px; font-size: 22px; }
    p { margin: 0; color: #676b70; line-height: 1.45; }
  </style>
</head>
<body>
  <main>
    <h1>Groceries</h1>
    <p>Private link required. Open the private phone link for your grocery app.</p>
  </main>
</body>
</html>"""


def render_amazon_now_handoff() -> str:
    app_url = "com.amazon.mobile.shopping://"
    alt_app_url = "com.amazon.mobile.shopping.web://www.amazon.in/"
    web_url = "https://www.amazon.in/"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="theme-color" content="#e23744">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-title" content="Groceries">
  <meta name="apple-mobile-web-app-status-bar-style" content="default">
  <title>Open Amazon Now</title>
  <style>
    :root {{
      --accent: #e23744;
      --ink: #17110f;
      --muted: #667085;
      --line: #e8e1dc;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 18px;
      font: 15px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--ink);
      background: #fff7f3;
    }}
    main {{
      width: min(420px, 100%);
      display: grid;
      gap: 12px;
      padding: 18px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      box-shadow: 0 18px 50px rgba(24, 31, 46, .12);
    }}
    h1 {{ margin: 0; font-size: 24px; line-height: 1.15; }}
    p {{ margin: 0; color: var(--muted); }}
    a, button {{
      width: 100%;
      min-height: 44px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 10px 12px;
      background: #fff;
      color: var(--ink);
      font: inherit;
      font-weight: 700;
      text-decoration: none;
    }}
    .primary {{
      border-color: var(--accent);
      background: var(--accent);
      color: #fff;
    }}
    .tiny {{ font-size: 12px; }}
  </style>
</head>
<body>
  <main>
    <h1>Open Amazon Now</h1>
    <p id="status">Amazon's old Now web route opens the wrong page, so this opens Amazon directly. Tap the lightning Now icon inside Amazon.</p>
    <button class="primary" id="openApp" type="button">Open Amazon app</button>
    <a href="{html.escape(web_url)}" id="webFallback">Open Amazon website</a>
    <a href="/" class="tiny">Back to Groceries</a>
  </main>
  <script>
    const appUrl = {json.dumps(app_url)};
    const altAppUrl = {json.dumps(alt_app_url)};
    const statusNode = document.getElementById('status');

    function tryUrl(url) {{
      const frame = document.createElement('iframe');
      frame.style.display = 'none';
      frame.src = url;
      document.body.appendChild(frame);
      window.setTimeout(() => frame.remove(), 2500);
    }}

    function openAmazonNow() {{
      statusNode.textContent = 'Opening Amazon. If iOS asks, choose Open, then tap Now inside Amazon.';
      tryUrl(appUrl);
      window.setTimeout(() => {{
        if (!document.hidden) tryUrl(altAppUrl);
      }}, 900);
      window.setTimeout(() => {{
        if (!document.hidden) {{
          statusNode.textContent = 'The app did not take over. Use the Amazon website button below, then open Now inside Amazon.';
        }}
      }}, 1800);
    }}

    document.getElementById('openApp').addEventListener('click', openAmazonNow);
  </script>
</body>
</html>"""


def render_app() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="theme-color" content="#e23744">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-title" content="Groceries">
  <meta name="apple-mobile-web-app-status-bar-style" content="default">
  <link rel="manifest" href="/manifest.webmanifest">
  <link rel="apple-touch-icon" href="/icons/icon-180.png">
  <link rel="icon" sizes="192x192" href="/icons/icon-192.png">
  <title>Groceries</title>
  <style>
    :root {
      --bg: #f6f7fb;
      --panel: #ffffff;
      --ink: #111827;
      --muted: #6b7280;
      --line: #e6e8ef;
      --accent: #e23744;
      --accent-dark: #bd1f31;
      --accent-soft: #fff0f1;
      --accent-2: #0f766e;
      --hot: #e23744;
      --good: #12805c;
      --good-soft: #e8f7f0;
      --gold: #ffd447;
      --gold-soft: #fff7c7;
      --soft: #ffffff;
      --warn: #926000;
      --shadow: 0 10px 30px rgba(31, 36, 49, .08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background:
        linear-gradient(180deg, #fff0f2 0, #f6f7fb 260px, #f6f7fb 100%);
      color: var(--ink);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
      -webkit-text-size-adjust: 100%;
      min-height: 100vh;
    }
    header {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      align-items: center;
      gap: 16px;
      padding: calc(12px + env(safe-area-inset-top)) clamp(16px, 4vw, 36px) 12px;
      background: var(--accent);
      color: #fff;
      border-bottom: 1px solid rgba(120, 20, 30, .18);
      box-shadow: 0 8px 28px rgba(171, 37, 49, .18);
      position: sticky;
      top: 0;
      z-index: 30;
    }
    header .muted { color: rgba(255, 255, 255, .82); }
    .brand-lockup {
      display: flex;
      align-items: center;
      gap: 10px;
      min-width: 0;
    }
    .brand-mark {
      width: 42px;
      height: 42px;
      border-radius: 8px;
      display: grid;
      place-items: center;
      background: #fff;
      color: var(--accent);
      font-weight: 900;
      box-shadow: 0 8px 18px rgba(120, 20, 30, .18);
      flex: 0 0 auto;
    }
    .brand-copy {
      min-width: 0;
    }
    .primary-actions {
      display: inline-flex;
      align-items: center;
      justify-content: flex-end;
      gap: 6px;
      flex-wrap: wrap;
      margin-top: 0;
    }
    .top-icon-actions {
      display: inline-flex;
      align-items: center;
      justify-content: flex-end;
      gap: 6px;
      margin-top: 0;
    }
    .top-tool-button {
      min-height: 40px;
      height: 40px;
      padding: 0 10px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      border-color: rgba(255, 255, 255, .46);
      background: rgba(255, 255, 255, .16);
      color: #fff;
      font-weight: 700;
      white-space: nowrap;
    }
    .top-tool-button.icon-only {
      width: 40px;
      padding: 0;
    }
    .top-tool-button svg {
      width: 20px;
      height: 20px;
      stroke: currentColor;
      stroke-width: 2.4;
      fill: none;
      stroke-linecap: round;
      stroke-linejoin: round;
    }
    .basket-button {
      position: relative;
    }
    .basket-badge {
      position: absolute;
      top: -5px;
      right: -5px;
      min-width: 19px;
      min-height: 19px;
      display: inline-grid;
      place-items: center;
      border-radius: 99px;
      padding: 1px 6px;
      background: #fff;
      color: var(--accent);
      font-size: 11px;
      line-height: 1;
    }
    .basket-badge[hidden] {
      display: none;
    }
    h1 { margin: 0; font-size: 23px; line-height: 1.1; font-weight: 850; }
    h2 { margin: 0 0 12px; font-size: 17px; font-weight: 800; }
    h3 { margin: 0 0 8px; font-size: 15px; font-weight: 800; }
    .section-note {
      margin: -6px 0 10px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
    }
    .panel-heading {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 8px;
    }
    .panel-heading h2 {
      margin-bottom: 0;
    }
    .panel-heading-tools {
      display: inline-flex;
      align-items: center;
      justify-content: flex-end;
      gap: 6px;
      flex-wrap: wrap;
      min-width: max-content;
    }
    .section-status {
      white-space: nowrap;
    }
    .section-status.is-ok,
    .status-chip.is-ok {
      background: var(--good-soft);
      color: var(--good);
    }
    .section-status.is-warn,
    .status-chip.is-warn {
      background: #fff2d7;
      color: var(--warn);
    }
    .section-status.is-hot,
    .status-chip.is-hot {
      background: var(--accent-soft);
      color: var(--hot);
    }
    .toggle-button {
      min-height: 30px;
      padding: 4px 8px;
      font-size: 12px;
      color: var(--accent-dark);
    }
    .collapsible[hidden] {
      display: none !important;
    }
    .find-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 8px;
      align-items: center;
    }
    .search-status {
      min-height: 16px;
      margin-top: 6px;
    }
    .inline-action {
      min-height: 28px;
      margin-top: 8px;
      padding: 4px 8px;
      font-size: 12px;
    }
    .floating-search-button {
      width: 40px;
      height: 40px;
      min-height: 40px;
      padding: 0;
      display: inline-grid;
      place-items: center;
      border-radius: 7px;
      border-color: rgba(255, 255, 255, .46);
      background: rgba(255, 255, 255, .16);
      color: #fff;
      box-shadow: none;
    }
    .floating-search-button svg {
      width: 22px;
      height: 22px;
      stroke: currentColor;
      stroke-width: 2.4;
      fill: none;
      stroke-linecap: round;
      stroke-linejoin: round;
    }
    .floating-search-button.is-open {
      background: #fff;
      color: var(--accent);
      border-color: #fff;
    }
    .floating-search-panel {
      position: fixed;
      right: max(18px, env(safe-area-inset-right));
      top: calc(72px + env(safe-area-inset-top));
      z-index: 25;
      width: min(360px, calc(100vw - 36px));
      padding: 10px;
      border: 1px solid rgba(226, 55, 68, .18);
      border-radius: 8px;
      background: rgba(255, 255, 255, .96);
      box-shadow: 0 18px 48px rgba(24, 31, 46, .18);
      backdrop-filter: blur(12px);
    }
    .floating-search-panel[hidden] {
      display: none !important;
    }
    .floating-search-panel .find-row {
      gap: 6px;
    }
    .floating-search-panel input {
      min-height: 40px;
    }
    .item-card.is-highlighted {
      border-color: rgba(226, 55, 68, .55);
      box-shadow: 0 0 0 3px rgba(226, 55, 68, .14), var(--shadow);
    }
    button, input, select {
      font: inherit;
    }
    button {
      border: 1px solid var(--line);
      background: #fff;
      border-radius: 7px;
      padding: 8px 10px;
      cursor: pointer;
      min-height: 38px;
      touch-action: manipulation;
      color: var(--ink);
      box-shadow: 0 1px 0 rgba(23, 17, 15, .04);
    }
    button.primary {
      background: var(--accent);
      color: #fff;
      border-color: var(--accent);
      font-weight: 700;
      box-shadow: 0 8px 16px rgba(226, 55, 68, .18);
    }
    button:disabled {
      opacity: .58;
      cursor: progress;
      box-shadow: none;
    }
    header button {
      border-color: rgba(255, 255, 255, .46);
      background: rgba(255, 255, 255, .16);
      color: #fff;
    }
    header button.primary {
      background: #fff;
      color: var(--accent);
      border-color: #fff;
      box-shadow: none;
    }
    .sidebar-toggle {
      display: inline-flex;
    }
    main {
      display: grid;
      grid-template-columns: 330px minmax(0, 1fr);
      gap: 18px;
      padding: 18px clamp(16px, 4vw, 36px) calc(96px + env(safe-area-inset-bottom));
    }
    aside, section.panel {
      min-width: 0;
    }
    .app-sidebar {
      min-width: 0;
    }
    .sidebar-head {
      display: none;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 10px;
      padding: 2px 0 8px;
      border-bottom: 1px solid var(--line);
    }
    .sidebar-head strong {
      font-size: 16px;
    }
    .sidebar-scrim {
      display: none;
    }
    .home-toolbar {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 12px;
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255, 255, 255, .88);
      box-shadow: var(--shadow);
    }
    .home-toolbar h2 {
      margin-bottom: 3px;
      font-size: 18px;
    }
    .shopping-brief {
      display: grid;
      grid-template-columns: minmax(0, 1.35fr) minmax(0, .85fr) minmax(0, .85fr);
      gap: 10px;
      margin-bottom: 12px;
    }
    .brief-card {
      display: grid;
      gap: 8px;
      min-width: 0;
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255, 255, 255, .92);
      box-shadow: var(--shadow);
    }
    .brief-card.primary {
      border-color: rgba(226, 55, 68, .22);
      background:
        linear-gradient(135deg, #fff 0, #fff 52%, #fff0f1 100%);
      box-shadow: 0 12px 34px rgba(226, 55, 68, .11);
    }
    .brief-kicker {
      color: var(--accent);
      font-size: 11px;
      font-weight: 800;
      text-transform: uppercase;
    }
    .brief-title {
      color: var(--ink);
      font-size: 18px;
      font-weight: 850;
      line-height: 1.15;
      overflow-wrap: anywhere;
    }
    .brief-card.primary .brief-title {
      font-size: 22px;
    }
    .brief-meta {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
    }
    .brief-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 2px;
    }
    .brief-actions button,
    .brief-actions a {
      min-height: 32px;
      padding: 5px 8px;
      font-size: 12px;
    }
    .brief-actions a.primary {
      border-color: var(--accent);
      background: var(--accent);
      color: #fff;
      font-weight: 700;
      box-shadow: 0 8px 16px rgba(226, 55, 68, .18);
    }
    .best-buys-panel {
      margin-bottom: 12px;
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255, 255, 255, .9);
      box-shadow: var(--shadow);
    }
    .best-buys-head {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: flex-start;
      margin-bottom: 10px;
    }
    .best-buys-head h2 {
      margin-bottom: 3px;
      font-size: 18px;
    }
    .best-buys-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(188px, 1fr));
      gap: 8px;
    }
    .buy-card {
      display: grid;
      gap: 7px;
      min-width: 0;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      box-shadow: 0 4px 14px rgba(24, 31, 46, .04);
    }
    .buy-card.is-alert {
      border-color: rgba(226, 55, 68, .24);
      background: var(--accent-soft);
    }
    .buy-card-title {
      font-weight: 800;
      line-height: 1.25;
      overflow-wrap: anywhere;
    }
    .buy-card-price {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      align-items: end;
    }
    .buy-card-price strong {
      font-size: 19px;
    }
    .buy-card-actions {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
    }
    .buy-card-actions button,
    .buy-card-actions a {
      min-height: 30px;
      padding: 4px 7px;
      font-size: 12px;
    }
    .home-actions {
      display: inline-flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: 6px;
    }
    .home-alert-button {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      font-weight: 700;
    }
    .home-alert-button.has-alerts {
      border-color: rgba(226, 55, 68, .28);
      background: var(--accent-soft);
      color: var(--accent-dark);
    }
    .home-alert-count {
      min-width: 20px;
      min-height: 20px;
      display: inline-grid;
      place-items: center;
      border-radius: 99px;
      padding: 2px 6px;
      background: var(--accent);
      color: #fff;
      font-size: 11px;
      line-height: 1;
    }
    .home-alert-count[hidden] {
      display: none;
    }
    .home-alert-panel {
      margin: -4px 0 12px;
      padding: 10px;
      border: 1px solid rgba(226, 55, 68, .2);
      border-radius: 8px;
      background: #fff;
      box-shadow: var(--shadow);
    }
    .home-alert-panel[hidden] {
      display: none !important;
    }
    .home-alert-panel .alert-card {
      box-shadow: none;
    }
    .panel-box, .item-card, .alert-card {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
    }
    .panel-box {
      padding: 14px;
      margin-bottom: 14px;
      background: rgba(255, 255, 255, .9);
      backdrop-filter: blur(12px);
    }
    aside .panel-box:first-child {
      border-color: rgba(226, 55, 68, .22);
      box-shadow: 0 12px 34px rgba(226, 55, 68, .11);
    }
    label { display: block; color: var(--muted); font-size: 12px; margin: 9px 0 4px; }
    .inline-check {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      margin-top: 10px;
    }
    .inline-check input {
      width: auto;
      margin: 0;
    }
    input, select {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 8px 9px;
      background: #fff;
      color: var(--ink);
    }
    input:focus, select:focus, button:focus-visible, a:focus-visible {
      outline: 3px solid rgba(226, 55, 68, .18);
      outline-offset: 2px;
    }
    .form-row {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
    }
    .muted { color: var(--muted); }
    .tiny { font-size: 12px; }
    .grid {
      display: grid;
      gap: 12px;
    }
    .review-panel {
      margin-bottom: 16px;
    }
    .review-heading {
      display: grid;
      gap: 3px;
    }
    .review-toolbar {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
      margin-bottom: 10px;
    }
    .review-summary {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }
    .review-metric {
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 6px 8px;
      background: #fff;
      font-size: 12px;
    }
    .review-metric strong {
      display: block;
      font-size: 15px;
      line-height: 1.15;
    }
    .segmented {
      display: inline-flex;
      flex-wrap: wrap;
      gap: 4px;
      justify-content: flex-end;
    }
    .segmented button {
      min-height: 32px;
      padding: 5px 8px;
      font-size: 12px;
    }
    .segmented button.is-active {
      background: var(--ink);
      color: #fff;
      border-color: var(--ink);
    }
    .review-list {
      display: grid;
      gap: 8px;
    }
    .review-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(118px, 160px);
      gap: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 10px;
    }
    .review-row.needs-review {
      border-color: rgba(226, 55, 68, .22);
      background: var(--accent-soft);
    }
    .review-row.looks-ok {
      background: #ffffff;
    }
    .review-row.exact {
      background: var(--good-soft);
      border-color: rgba(18, 128, 92, .28);
    }
    .review-title {
      font-weight: 700;
      line-height: 1.25;
      overflow-wrap: anywhere;
    }
    .review-product {
      margin-top: 5px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }
    .review-meta {
      display: flex;
      flex-wrap: wrap;
      gap: 5px;
      margin-top: 7px;
    }
    .review-price {
      text-align: right;
      display: grid;
      gap: 4px;
      align-content: start;
    }
    .review-price strong {
      font-size: 18px;
    }
    .status-chip.match-exact {
      background: var(--good-soft);
      color: var(--good);
    }
    .status-chip.match-watch {
      background: #fff2d7;
      color: var(--warn);
    }
    .status-chip.match-review {
      background: #fff0eb;
      color: var(--hot);
    }
    .item-card {
      padding: 14px;
    }
    .item-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
      margin-bottom: 12px;
    }
    .item-head > div:first-child {
      min-width: 0;
    }
    .item-head h2 {
      line-height: 1.2;
      overflow-wrap: anywhere;
    }
    .item-meta {
      display: grid;
      gap: 5px;
      margin-top: 5px;
    }
    .match-rule {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      align-items: center;
    }
    .match-rule select {
      border: 1px solid var(--line);
      border-radius: 7px;
      min-height: 32px;
      padding: 5px 26px 5px 8px;
      background: #fff;
      color: var(--text);
      font-size: 12px;
      max-width: 100%;
    }
    .match-rule-label {
      font-size: 11px;
      color: var(--muted);
      white-space: nowrap;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      padding: 4px 7px;
      border-radius: 6px;
      background: #f3f4f8;
      color: var(--muted);
      font-size: 12px;
      max-width: 100%;
    }
    .best {
      color: var(--good);
      background: var(--good-soft);
      font-weight: 700;
    }
    .prices {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(142px, 1fr));
      gap: 8px;
    }
    .price-box {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 9px;
      min-height: 118px;
      background: #fff;
      box-shadow: 0 4px 14px rgba(24, 31, 46, .04);
    }
    .price-box.is-best {
      border-color: rgba(18, 128, 92, .35);
      background: var(--good-soft);
    }
    .provider-name {
      font-size: 12px;
      color: var(--muted);
      margin-bottom: 6px;
      min-height: 30px;
    }
    .price {
      font-size: 19px;
      font-weight: 700;
      color: #141821;
    }
    .unit {
      color: var(--muted);
      font-size: 12px;
      margin-top: 2px;
    }
    .actions {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      margin-top: 9px;
    }
    .actions a, .link-button {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 5px 7px;
      color: var(--accent-dark);
      text-decoration: none;
      font-size: 12px;
      background: #fff;
      min-height: 32px;
      display: inline-flex;
      align-items: center;
    }
    .alert-card {
      display: grid;
      gap: 8px;
      padding: 10px;
      margin-bottom: 8px;
      color: inherit;
      text-decoration: none;
      border-color: rgba(226, 55, 68, .22);
      background: var(--accent-soft);
    }
    .alert-card:hover {
      border-color: rgba(226, 55, 68, .45);
      box-shadow: 0 8px 18px rgba(226, 55, 68, .12);
    }
    .alert-card strong { color: var(--hot); }
    .alert-top {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 8px;
      align-items: start;
    }
    .alert-actions {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
    }
    .alert-actions a,
    .alert-actions button {
      min-height: 32px;
      padding: 5px 8px;
      font-size: 12px;
    }
    .provider-status {
      display: grid;
      gap: 6px;
    }
    .basket-builder {
      display: grid;
      gap: 10px;
    }
    .basket-summary {
      display: grid;
      gap: 8px;
      padding: 12px;
      border: 1px solid rgba(226, 55, 68, .2);
      border-radius: 8px;
      background:
        linear-gradient(135deg, #fff 0, #fff 55%, #fff0f1 100%);
      box-shadow: 0 8px 22px rgba(226, 55, 68, .08);
    }
    .basket-kicker {
      color: var(--accent);
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0;
      text-transform: uppercase;
    }
    .basket-summary strong {
      overflow-wrap: anywhere;
    }
    .basket-summary-actions {
      margin-top: 0;
    }
    .basket-metrics {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 6px;
    }
    .basket-metric {
      border: 1px solid rgba(226, 55, 68, .12);
      border-radius: 7px;
      padding: 7px;
      background: #fff;
      min-width: 0;
    }
    .basket-metric strong {
      display: block;
      font-size: 15px;
      line-height: 1.2;
    }
    .basket-block {
      display: grid;
      gap: 7px;
      padding-top: 2px;
    }
    .basket-scan-status {
      display: grid;
      gap: 5px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px;
      background: #fff;
    }
    .basket-block-title,
    .provider-plan-head,
    .app-option-head {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      align-items: flex-start;
    }
    .basket-block-title strong,
    .provider-plan-head strong,
    .app-option-head strong {
      overflow-wrap: anywhere;
    }
    .provider-plan,
    .app-option,
    .basket-item {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 9px;
      background: #fff;
      box-shadow: 0 4px 14px rgba(24, 31, 46, .035);
    }
    .provider-plan,
    .basket-item {
      display: grid;
      gap: 7px;
    }
    .provider-plan-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }
    .provider-lines,
    .single-options {
      display: grid;
      gap: 5px;
    }
    .provider-line {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto auto;
      gap: 7px;
      align-items: center;
      padding: 4px 0;
    }
    .line-name {
      min-width: 0;
      overflow-wrap: anywhere;
    }
    .line-price {
      white-space: nowrap;
      font-weight: 700;
    }
    .app-option.is-close {
      border-color: rgba(18, 128, 92, .32);
      background: var(--good-soft);
    }
    .basket-items {
      display: grid;
      gap: 8px;
    }
    .basket-line {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 8px;
      align-items: center;
      padding: 8px 0;
      border-bottom: 1px solid var(--line);
    }
    .basket-line strong {
      overflow-wrap: anywhere;
    }
    .basket-line:last-child { border-bottom: 0; }
    .qty {
      display: inline-flex;
      align-items: center;
      gap: 4px;
    }
    .qty button {
      width: 28px;
      height: 28px;
      padding: 0;
    }
    .option-chips {
      display: flex;
      flex-wrap: wrap;
      gap: 5px;
    }
    .price-chip {
      display: inline-grid;
      gap: 1px;
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 5px 7px;
      background: #fff;
      font-size: 12px;
      max-width: 100%;
      min-width: 0;
    }
    .price-chip span,
    .price-chip strong {
      overflow-wrap: anywhere;
    }
    .price-chip.is-best {
      border-color: rgba(18, 128, 92, .35);
      background: var(--good-soft);
    }
    .price-chip.is-missing {
      background: #f5f6fa;
      color: var(--muted);
    }
    .recommendation {
      margin-top: 10px;
      padding: 10px;
      border-radius: 8px;
      background: var(--accent-soft);
      border: 1px solid rgba(226, 55, 68, .18);
    }
    .scan-card {
      display: grid;
      gap: 8px;
    }
    .scan-metrics {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 6px;
    }
    .scan-metric {
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 7px;
      background: #fff;
    }
    .scan-metric strong {
      display: block;
      font-size: 16px;
    }
    .auto-card {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      background: #fff;
    }
    .scanner-list {
      display: grid;
      gap: 8px;
    }
    .scanner-card {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 9px;
      background: #fff;
    }
    .scanner-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 8px;
    }
    .scanner-counts {
      display: flex;
      gap: 5px;
      flex-wrap: wrap;
      margin-top: 7px;
    }
    .status-chip {
      border-radius: 6px;
      padding: 3px 6px;
      background: #f3f4f8;
      color: var(--muted);
      font-size: 11px;
      line-height: 1.25;
      text-align: right;
      white-space: nowrap;
    }
    .status-chip.price-candidates {
      background: var(--good-soft);
      color: var(--good);
    }
    .status-chip.blocked,
    .status-chip.profile-in-use,
    .status-chip.rate-limited {
      background: #fff2d7;
      color: var(--warn);
    }
    .status-chip.error,
    .status-chip.navigation-error {
      background: var(--accent-soft);
      color: var(--hot);
    }
    .plan-line {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      font-size: 13px;
      padding: 4px 0;
    }
    .status-line {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      font-size: 13px;
    }
    .status-line span:last-child {
      color: var(--muted);
      text-align: right;
    }
    dialog {
      border: 1px solid var(--line);
      border-radius: 8px;
      width: min(460px, calc(100vw - 28px));
      padding: 0;
    }
    dialog form {
      padding: 16px;
    }
    .basket-dialog {
      width: min(760px, calc(100vw - 24px));
      max-height: min(820px, calc(100vh - 28px));
    }
    .basket-dialog .dialog-shell {
      padding: 16px;
      max-height: min(820px, calc(100vh - 28px));
      overflow: auto;
    }
    dialog::backdrop { background: rgba(0,0,0,.25); }
    @media (max-width: 1100px) {
      main { grid-template-columns: 1fr; }
      body.sidebar-open {
        overflow: hidden;
      }
      .sidebar-toggle {
        display: inline-flex;
      }
      .app-sidebar {
        position: fixed;
        inset: 0 auto 0 0;
        z-index: 35;
        width: min(380px, 92vw);
        overflow-y: auto;
        padding: calc(12px + env(safe-area-inset-top)) 12px calc(24px + env(safe-area-inset-bottom));
        background: var(--bg);
        box-shadow: 18px 0 50px rgba(24, 31, 46, .22);
        transform: translateX(-105%);
        transition: transform .18s ease;
      }
      body.sidebar-open .app-sidebar {
        transform: translateX(0);
      }
      .app-sidebar.is-open {
        transform: translateX(0);
      }
      .sidebar-head {
        display: flex;
      }
      .sidebar-scrim {
        display: block;
        position: fixed;
        inset: 0;
        z-index: 34;
        background: rgba(17, 24, 39, .38);
        opacity: 0;
        pointer-events: none;
        transition: opacity .18s ease;
      }
      body.sidebar-open .sidebar-scrim {
        opacity: 1;
        pointer-events: auto;
      }
      .prices { grid-template-columns: repeat(2, minmax(130px, 1fr)); }
    }
    @media (max-width: 760px) {
      header {
        grid-template-columns: minmax(0, 1fr) auto;
        gap: 10px;
        align-items: center;
      }
      .brand-mark {
        width: 38px;
        height: 38px;
      }
      .top-icon-actions {
        justify-self: end;
      }
      main {
        gap: 12px;
        padding: 12px max(10px, env(safe-area-inset-left)) calc(96px + env(safe-area-inset-bottom)) max(10px, env(safe-area-inset-right));
      }
      .app-sidebar {
        display: block;
      }
      .home-toolbar {
        display: grid;
        gap: 10px;
        padding: 12px;
      }
      .shopping-brief {
        grid-template-columns: 1fr;
      }
      .brief-card.primary .brief-title {
        font-size: 20px;
      }
      .home-actions {
        display: grid;
        grid-template-columns: 1fr auto;
        justify-content: stretch;
      }
      .basket-metrics {
        grid-template-columns: 1fr;
      }
      .provider-line {
        grid-template-columns: minmax(0, 1fr) auto;
      }
      .provider-line .line-price {
        grid-column: 2;
        grid-row: 1;
      }
      .provider-line a {
        grid-column: 1 / -1;
        width: max-content;
      }
      .panel-box {
        margin-bottom: 0;
        padding: 12px;
      }
      .item-card {
        padding: 12px;
      }
      .item-head {
        flex-direction: column;
      }
      .item-head .actions {
        width: 100%;
        display: grid;
        grid-template-columns: minmax(0, 1fr) auto auto;
        align-items: center;
      }
      .prices {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 6px;
        overflow: visible;
        padding-bottom: 0;
      }
      .price-box {
        min-width: 0;
        min-height: 104px;
        display: grid;
        grid-template-columns: 1fr;
        gap: 3px;
        align-content: start;
        padding: 7px;
      }
      .price-box .provider-name {
        min-height: 0;
        margin-bottom: 0;
        font-size: 11px;
        line-height: 1.2;
        overflow-wrap: anywhere;
      }
      .price-box .price {
        font-size: 16px;
        text-align: left;
        white-space: nowrap;
      }
      .price-box .unit,
      .price-box .tiny.muted {
        font-size: 11px;
        line-height: 1.2;
      }
      .price-box .unit,
      .price-box .tiny.muted,
      .price-box .actions {
        grid-column: 1 / -1;
      }
      .price-box .actions {
        margin-top: 3px;
        gap: 4px;
      }
      .price-box .manual-price {
        display: none;
      }
      .price-box .actions a,
      .price-box .actions .link-button {
        min-height: 28px;
        padding: 3px 6px;
        font-size: 11px;
      }
      .floating-search-panel {
        right: max(10px, env(safe-area-inset-right));
        top: calc(132px + env(safe-area-inset-top));
      }
      .option-chips {
        display: grid;
        grid-template-columns: 1fr;
      }
      .price-chip {
        grid-template-columns: minmax(0, 1fr) auto;
        align-items: center;
      }
      .price-chip span:last-child {
        text-align: right;
      }
      .review-toolbar {
        display: grid;
      }
      .segmented {
        justify-content: flex-start;
      }
      .review-row {
        grid-template-columns: 1fr;
      }
      .review-price {
        text-align: left;
        grid-template-columns: minmax(0, 1fr) auto;
        align-items: end;
      }
      .actions a,
      .link-button,
      button {
        min-height: 40px;
      }
    }
    @media (max-width: 560px) {
      .prices { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .form-row { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <div class="brand-lockup">
      <div class="brand-mark">GC</div>
      <div class="brand-copy">
      <h1>Groceries</h1>
      <div class="muted tiny" id="subtitle">Loading prices...</div>
      </div>
    </div>
    <div class="top-icon-actions" aria-label="Quick tools">
      <button class="top-tool-button icon-only sidebar-toggle" id="sidebarOpenBtn" type="button" aria-label="Open menu" aria-expanded="false" aria-controls="appSidebar">
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M4 6h16"></path>
          <path d="M4 12h16"></path>
          <path d="M4 18h16"></path>
        </svg>
      </button>
      <button class="floating-search-button" id="floatingSearchBtn" type="button" aria-label="Search saved items" aria-expanded="false" aria-controls="floatingSearchPanel">
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <circle cx="11" cy="11" r="7"></circle>
          <path d="M20 20l-4.2-4.2"></path>
        </svg>
      </button>
      <button class="top-tool-button icon-only basket-button" id="basketOpenBtn" type="button" aria-label="Open basket" aria-haspopup="dialog" aria-controls="basketDialog">
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M3 4h2l2.1 11h10.8l2-8H6.4"></path>
          <circle cx="9" cy="20" r="1.4"></circle>
          <circle cx="18" cy="20" r="1.4"></circle>
        </svg>
        <span class="basket-badge" id="basketBadge" hidden>0</span>
      </button>
    </div>
  </header>
  <main>
    <aside class="app-sidebar" id="appSidebar" aria-label="Grocery tools">
      <div class="sidebar-head">
        <strong>More</strong>
        <button class="toggle-button" id="sidebarCloseBtn" type="button">Close</button>
      </div>
      <div class="panel-box">
        <div class="panel-heading">
          <h2>Find or Add</h2>
          <span class="status-chip section-status is-ok" id="findStatusChip">Ready</span>
        </div>
        <div class="section-note">Find a saved item, or add a new thing you need right now.</div>
        <div class="find-row">
          <input id="itemSearchInput" type="search" placeholder="Search milk, chicken, Surf Excel">
          <button class="primary" id="itemSearchBtn" type="button">Go</button>
        </div>
        <div class="tiny muted search-status" id="itemSearchStatus"></div>
      </div>
      <div class="panel-box">
        <div class="panel-heading">
          <h2>Watchlist Backup</h2>
          <span class="status-chip section-status is-ok" id="watchlistBackupStatus">Ready</span>
        </div>
        <div class="section-note">Save or restore only your grocery names and matching rules. Prices and app sessions stay out.</div>
        <div class="actions">
          <button id="exportWatchlistBtn" type="button">Export</button>
          <button id="importWatchlistBtn" type="button">Import</button>
        </div>
        <label class="tiny muted inline-check">
          <input id="watchlistReplaceToggle" type="checkbox">
          Replace current saved items
        </label>
        <input id="watchlistImportFile" type="file" accept="application/json,.json" hidden>
      </div>
      <div class="panel-box">
        <div class="panel-heading">
          <h2>Price Drops</h2>
          <div class="panel-heading-tools">
            <span class="status-chip section-status" id="alertsStatus">...</span>
            <button class="toggle-button" id="alertsNotifyBtn" type="button">Notify</button>
            <button class="toggle-button" id="clearAlertsBtn" type="button">Clear all</button>
          </div>
        </div>
        <div class="section-note">Active drops with the time the price was checked.</div>
        <div id="alerts"></div>
      </div>
      <div class="panel-box">
        <h2>One-time Refresh</h2>
        <div class="section-note">Prepare a manual app check when you want fresh prices outside the normal background cycle.</div>
        <div id="scanStatus"></div>
        <button class="link-button" id="refreshBtn" type="button">Prepare check</button>
      </div>
      <div class="panel-box">
        <div class="panel-heading">
          <h2>Background Updates</h2>
          <div class="panel-heading-tools">
            <span class="status-chip section-status" id="autoCheckerStatus">...</span>
            <button class="toggle-button" id="scannerToggleBtn" type="button" aria-expanded="false" aria-controls="scannerDetails">Show details</button>
          </div>
        </div>
        <div class="section-note">Keeps your saved grocery prices fresh.</div>
        <div id="autoScan"></div>
        <div class="collapsible" id="scannerDetails" hidden>
          <div id="scanner"></div>
        </div>
      </div>
      <div class="panel-box">
        <div class="panel-heading">
          <h2>App Logins</h2>
          <div class="panel-heading-tools">
            <span class="status-chip section-status" id="providersStatus">...</span>
            <button class="toggle-button" id="providersToggleBtn" type="button" aria-expanded="false" aria-controls="providers">Show</button>
          </div>
        </div>
        <div class="section-note">Shows which grocery apps are ready and which need attention.</div>
        <div class="provider-status collapsible" id="providers" hidden></div>
      </div>
      <div class="panel-box">
        <h2>Deal Rules</h2>
        <div class="tiny muted" id="rules"></div>
      </div>
      <div class="panel-box review-panel" aria-label="Price confidence review">
        <div class="review-toolbar">
          <div class="review-heading">
            <div class="panel-heading">
              <h2>Price Sanity Check</h2>
              <div class="panel-heading-tools">
                <span class="status-chip section-status" id="reviewStatus">...</span>
                <button class="toggle-button" id="reviewToggleBtn" type="button" aria-expanded="false" aria-controls="reviewDetails">Show list</button>
              </div>
            </div>
            <div class="section-note">Items here may be the wrong pack, size, or promoted product. Open only when something looks suspicious.</div>
            <div class="review-summary" id="reviewSummary"></div>
          </div>
          <div class="segmented" id="reviewFilters">
            <button type="button" data-review-filter="review">Needs checking</button>
            <button type="button" data-review-filter="all">All prices</button>
            <button type="button" data-review-filter="scanner">Found by app</button>
            <button type="button" data-review-filter="exact">Trusted</button>
          </div>
        </div>
        <div class="collapsible" id="reviewDetails" hidden>
          <div id="matchReview"></div>
        </div>
      </div>
    </aside>
    <section class="panel">
      <div class="shopping-brief" id="shoppingBrief"></div>
      <div class="best-buys-panel" id="bestBuys"></div>
      <div class="home-toolbar">
        <div>
          <h2>Saved Items</h2>
          <div class="section-note">Your regular groceries. Each card highlights the cheapest app currently known.</div>
        </div>
        <div class="home-actions">
          <button class="home-alert-button" id="homeAlertsBtn" type="button" aria-expanded="false" aria-controls="homeAlertsPanel">
            Alerts <span class="home-alert-count" id="homeAlertsCount" hidden>0</span>
          </button>
          <button id="homeAddItemBtn" type="button">Need item</button>
        </div>
      </div>
      <div class="home-alert-panel" id="homeAlertsPanel" hidden>
        <div class="panel-heading">
          <h2>Active alerts</h2>
          <button class="toggle-button" id="homeAlertsCloseBtn" type="button">Close</button>
        </div>
        <div id="homeAlerts"></div>
      </div>
      <div class="grid" id="items"></div>
    </section>
  </main>
  <div class="sidebar-scrim" id="sidebarScrim" aria-hidden="true"></div>

  <dialog id="itemDialog">
    <form method="dialog" id="itemForm">
      <h2 id="itemDialogTitle">Add grocery item</h2>
      <div class="section-note" id="itemDialogNote">Save recurring items here. Use Need + Check when you are shopping right now.</div>
      <label>Name</label>
      <input name="name" required placeholder="Curry cut chicken">
      <label>Brand</label>
      <input name="brand" placeholder="Amul">
      <div class="form-row">
        <div>
          <label>Pack value</label>
          <input name="pack_value" type="number" step="0.01" placeholder="500">
        </div>
        <div>
          <label>Pack unit</label>
          <select name="pack_unit">
            <option value="g">g</option>
            <option value="kg">kg</option>
            <option value="ml">ml</option>
            <option value="l">l</option>
            <option value="pc">pc</option>
          </select>
        </div>
      </div>
      <label>Category</label>
      <input name="category" placeholder="Dairy">
      <label>When comparing prices</label>
      <select name="match_mode">
        <option value="exact">Exact item</option>
        <option value="category">Any close match</option>
        <option value="same_size">Same size only</option>
        <option value="unit">Best unit price</option>
      </select>
      <label>Target price</label>
      <input name="target_price" type="number" step="0.01" placeholder="250">
      <div class="actions">
        <button class="primary" value="save" data-submit-mode="save">Save</button>
        <button value="need_now" data-submit-mode="need_now">Need + Check</button>
        <button type="button" id="itemCancelBtn">Cancel</button>
      </div>
    </form>
  </dialog>

  <dialog id="priceDialog">
    <form method="dialog" id="priceForm">
      <h2>Add price</h2>
      <input type="hidden" name="item_id">
      <input type="hidden" name="provider_id">
      <div class="muted tiny" id="priceDialogTitle"></div>
      <label>Price</label>
      <input name="price" type="number" step="0.01" required>
      <label>MRP</label>
      <input name="mrp" type="number" step="0.01">
      <div class="form-row">
        <div>
          <label>Delivery fee</label>
          <input name="delivery_fee" type="number" step="0.01" value="0">
        </div>
        <div>
          <label>Handling fee</label>
          <input name="handling_fee" type="number" step="0.01" value="0">
        </div>
      </div>
      <label>Product URL</label>
      <input name="url" placeholder="Paste link if you have it">
      <div class="actions">
        <button class="primary" value="default">Save price</button>
        <button type="button" id="priceCancelBtn">Cancel</button>
      </div>
    </form>
  </dialog>

  <dialog id="basketDialog" class="basket-dialog">
    <div class="dialog-shell">
      <div class="panel-heading">
        <h2>Basket</h2>
        <button class="toggle-button" id="basketCloseBtn" type="button">Close</button>
      </div>
      <div class="section-note">Build the list you need, then compare the cheapest split with one-app options.</div>
      <div id="basket"></div>
    </div>
  </dialog>

  <div class="floating-search-panel" id="floatingSearchPanel" hidden>
    <div class="find-row">
      <input id="floatingSearchInput" type="search" placeholder="Find item">
      <button class="primary" id="floatingSearchGo" type="button">Go</button>
    </div>
    <div class="tiny muted search-status" id="floatingSearchStatus"></div>
  </div>

  <script>
    let state = null;
    let reviewFilter = 'review';
    let basketProviderGroups = {};
    let highlightedItemId = null;
    let floatingSearchOpen = false;
    let sidebarOpen = false;
    let homeAlertsOpen = false;
    const sectionsOpen = {
      review: false,
      scanner: false,
      providers: false,
    };
    const providerNames = {};
    let knownAlertIds = new Set(
      JSON.parse(localStorage.getItem('groceryKnownAlertIds') || '[]').map(Number)
    );
    let alertNotificationsEnabled = localStorage.getItem('groceryAlertNotifications') === 'enabled';
    let alertNotificationsPrimed = Boolean(localStorage.getItem('groceryAlertNotificationsPrimed'));

    function withAccessKey(path) {
      const key = new URLSearchParams(window.location.search).get('key');
      if (!key) return path;
      const url = new URL(path, window.location.origin);
      url.searchParams.set('key', key);
      return `${url.pathname}${url.search}${url.hash}`;
    }

    function appOpenUrl(url) {
      if (!url) return url;
      if (String(url).startsWith('/amazon-now')) return withAccessKey('/amazon-now');
      return url;
    }

    async function loadState() {
      try {
        const res = await fetch('/api/state');
        state = await res.json();
        state.providers.forEach(p => providerNames[p.id] = p.name);
        render();
      } catch (error) {
        if (!state) {
          document.getElementById('subtitle').textContent = 'Waiting for local server...';
        }
      }
    }

    function money(value) {
      if (value === null || value === undefined) return '-';
      return 'Rs ' + Number(value).toLocaleString('en-IN', { maximumFractionDigits: 0 });
    }

    function fmtTime(value) {
      if (!value) return '';
      return new Date(value).toLocaleString();
    }

    function fmtShortTime(value) {
      if (!value) return '';
      return new Date(value).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    }

    function fmtFriendlyTime(value) {
      if (!value) return '';
      const date = new Date(value);
      const today = new Date();
      const sameDay = date.toDateString() === today.toDateString();
      return sameDay
        ? date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
        : date.toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
    }

    function relativeTime(value) {
      if (!value) return '';
      const then = new Date(value).getTime();
      if (!Number.isFinite(then)) return '';
      const diffMs = Date.now() - then;
      const minutes = Math.max(0, Math.round(diffMs / 60000));
      if (minutes < 1) return 'just now';
      if (minutes < 60) return `${minutes} min ago`;
      const hours = Math.round(minutes / 60);
      if (hours < 24) return `${hours} hr ago`;
      const days = Math.round(hours / 24);
      return `${days} day${days === 1 ? '' : 's'} ago`;
    }

    function formatPack(value, unit) {
      if (value === null || value === undefined || !unit) return '';
      const number = Number(value);
      const label = Number.isInteger(number) ? String(number) : String(number);
      return `${label}${unit}`;
    }

    const flexibilityOrder = ['exact', 'category', 'same_size', 'unit'];
    const fallbackFlexibilityModes = {
      exact: { short_label: 'Exact', description: 'Only this product should count.' },
      category: { short_label: 'Any close match', description: 'Any acceptable brand or promoted option can count.' },
      same_size: { short_label: 'Same size', description: 'Any acceptable brand can count, but the pack size must match.' },
      unit: { short_label: 'Unit price', description: 'Any acceptable size can count; compare by unit price when available.' },
    };

    function flexibilityModes() {
      return state?.flexibility_modes || fallbackFlexibilityModes;
    }

    function normalizedMatchMode(mode) {
      return flexibilityOrder.includes(mode) ? mode : (mode === 'flexible' ? 'unit' : 'exact');
    }

    function flexibilityLabel(mode) {
      const clean = normalizedMatchMode(mode);
      return (flexibilityModes()[clean] || fallbackFlexibilityModes[clean]).short_label || clean;
    }

    function flexibilityDescription(mode) {
      const clean = normalizedMatchMode(mode);
      return (flexibilityModes()[clean] || fallbackFlexibilityModes[clean]).description || '';
    }

    function renderMatchModeSelect(item) {
      const selected = normalizedMatchMode(item.match_mode);
      const modes = flexibilityModes();
      return `
        <div class="match-rule">
          <span class="match-rule-label">Compare as</span>
          <select aria-label="Price matching rule for ${escapeAttr(item.name)}" onchange="updateItemMatchMode(${Number(item.id)}, this.value)">
            ${flexibilityOrder.map(mode => {
              const label = (modes[mode] || fallbackFlexibilityModes[mode]).short_label;
              return `<option value="${mode}" ${mode === selected ? 'selected' : ''}>${escapeHtml(label)}</option>`;
            }).join('')}
          </select>
        </div>
      `;
    }

    function searchUrl(providerId, item) {
      const provider = (state.providers || []).find(p => p.id === providerId);
      if (!provider || !provider.search_url) return '#';
      const query = [item.brand, item.name, formatPack(item.pack_value, item.pack_unit)]
        .filter(Boolean)
        .join(' ');
      if (providerId === 'jiomart') {
        return `https://www.jiomart.com/search/${encodeURIComponent(query)}`;
      }
      const encoded = encodeURIComponent(query).replace(/%20/g, '+');
      return provider.search_url.replace('{query}', encoded);
    }

    function providerLinkLabel(providerId, openKind = '') {
      if (providerId === 'amazon_fresh' && openKind === 'product') return 'Open product';
      if (providerId === 'amazon_fresh') return 'Search Now';
      if (openKind === 'now') return 'Open Now';
      return openKind === 'product' ? 'Open' : 'Search';
    }

    function setSectionStatus(id, text, mood = 'ok') {
      const node = document.getElementById(id);
      if (!node) return;
      node.textContent = text;
      node.classList.toggle('is-ok', mood === 'ok');
      node.classList.toggle('is-warn', mood === 'warn');
      node.classList.toggle('is-hot', mood === 'hot');
    }

    function setWatchlistBackupStatus(text, mood = 'ok') {
      setSectionStatus('watchlistBackupStatus', text, mood);
    }

    function exportWatchlist() {
      setWatchlistBackupStatus('Exporting', 'ok');
      window.location.href = '/api/watchlist/export';
      setTimeout(() => setWatchlistBackupStatus('Ready', 'ok'), 2500);
    }

    async function importSelectedWatchlist(event) {
      const input = event.target;
      const file = input.files?.[0];
      if (!file) return;
      const replace = Boolean(document.getElementById('watchlistReplaceToggle')?.checked);
      if (replace && !confirm('Replace current saved items with this watchlist? Price history stays in the database, but current saved items will be deactivated.')) {
        input.value = '';
        return;
      }
      setWatchlistBackupStatus('Importing', 'warn');
      try {
        const watchlist = JSON.parse(await file.text());
        const res = await fetch('/api/watchlist/import', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ watchlist, replace })
        });
        const payload = await res.json();
        if (!payload.ok) throw new Error(payload.error || 'Could not import watchlist');
        const result = payload.result || {};
        setWatchlistBackupStatus(`${Number(result.imported || 0)} added`, 'ok');
        await loadState();
        if (Number(result.skipped || 0)) {
          alert(`${Number(result.skipped)} item(s) could not be imported. Check the JSON and try again.`);
        }
      } catch (error) {
        setWatchlistBackupStatus('Import failed', 'hot');
        alert(error.message || 'Could not import watchlist');
      } finally {
        input.value = '';
      }
    }

    function syncSection(section, panelId, buttonId, showLabel, hideLabel) {
      const panel = document.getElementById(panelId);
      const button = document.getElementById(buttonId);
      if (!panel || !button) return;
      const open = Boolean(sectionsOpen[section]);
      panel.hidden = !open;
      button.textContent = open ? hideLabel : showLabel;
      button.setAttribute('aria-expanded', String(open));
    }

    function setSidebarOpen(open) {
      sidebarOpen = Boolean(open);
      document.body.classList.toggle('sidebar-open', sidebarOpen);
      document.getElementById('appSidebar')?.classList.toggle('is-open', sidebarOpen);
      document.getElementById('sidebarOpenBtn')?.setAttribute('aria-expanded', String(sidebarOpen));
    }

    function setHomeAlertsOpen(open) {
      homeAlertsOpen = Boolean(open);
      const panel = document.getElementById('homeAlertsPanel');
      const button = document.getElementById('homeAlertsBtn');
      if (panel) panel.hidden = !homeAlertsOpen;
      if (button) button.setAttribute('aria-expanded', String(homeAlertsOpen));
    }

    function renderCollapsibleStates() {
      syncSection('review', 'reviewDetails', 'reviewToggleBtn', 'Show list', 'Hide list');
      syncSection('scanner', 'scannerDetails', 'scannerToggleBtn', 'Show details', 'Hide details');
      syncSection('providers', 'providers', 'providersToggleBtn', 'Show', 'Hide');
    }

    function render() {
      const itemCount = Number(state.item_count ?? state.items.length);
      document.getElementById('subtitle').textContent = `${itemCount} saved items - updated ${fmtShortTime(state.now) || 'now'}`;
      setSectionStatus('findStatusChip', `${itemCount} items`, 'ok');
      renderRules();
      renderProviders();
      renderAlerts();
      renderBasket();
      renderShoppingBrief();
      renderBestBuys();
      renderScan();
      renderAutoScan();
      renderScanner();
      renderMatchReview();
      renderItems();
      renderCollapsibleStates();
    }

    function renderRules() {
      const s = state.config.settings;
      document.getElementById('rules').innerHTML = `
        A drop appears when today's price is at least ${s.min_10d_avg_drop_percent}% below the 10-day average
        or ${s.min_30d_avg_drop_percent}% below the 30-day average, after ${s.min_history_points}
        past prices. Fees are ${s.include_delivery_fees ? 'included' : 'not included'}.
      `;
    }

    function renderProviders() {
      const providers = state.providers || [];
      const ready = providers.filter(p => ['browser-ready', 'official-api-ready', 'order-history-ready', 'chrome-assisted'].includes(p.status)).length;
      const needs = providers.length - ready;
      setSectionStatus(
        'providersStatus',
        needs ? `${needs} need attention` : `${ready} ready`,
        needs ? 'warn' : 'ok'
      );
      document.getElementById('providers').innerHTML = state.providers.map(p => `
        <div class="status-line">
          <strong>${escapeHtml(p.name)}</strong>
          <span>${escapeHtml(p.status)}</span>
        </div>
      `).join('');
    }

    function saveKnownAlertIds(ids) {
      const compact = [...ids].filter(Number.isFinite).slice(-200);
      localStorage.setItem('groceryKnownAlertIds', JSON.stringify(compact));
      knownAlertIds = new Set(compact);
    }

    function renderAlertControls(alerts) {
      const count = alerts.length;
      setSectionStatus('alertsStatus', count ? `${count} active` : 'Clear', count ? 'hot' : 'ok');
      const clearButton = document.getElementById('clearAlertsBtn');
      if (clearButton) clearButton.disabled = !count;
      const homeButton = document.getElementById('homeAlertsBtn');
      const homeCount = document.getElementById('homeAlertsCount');
      if (homeButton) {
        homeButton.classList.toggle('has-alerts', count > 0);
      }
      if (homeCount) {
        homeCount.hidden = count === 0;
        homeCount.textContent = String(count);
      }
      const notifyButton = document.getElementById('alertsNotifyBtn');
      if (!('Notification' in window)) {
        if (notifyButton) {
          notifyButton.disabled = true;
          notifyButton.textContent = 'No notify';
          notifyButton.title = 'This browser does not expose notifications for this local app.';
        }
        return;
      }
      if (Notification.permission === 'granted' && alertNotificationsEnabled) {
        if (notifyButton) {
          notifyButton.disabled = false;
          notifyButton.textContent = 'Notify on';
          notifyButton.title = 'New alerts will notify while this app is open or supported by the browser.';
        }
        return;
      }
      if (Notification.permission === 'denied') {
        if (notifyButton) {
          notifyButton.disabled = true;
          notifyButton.textContent = 'Blocked';
          notifyButton.title = 'Notifications are blocked in browser settings.';
        }
        return;
      }
      if (notifyButton) {
        notifyButton.disabled = false;
        notifyButton.textContent = 'Notify';
        notifyButton.title = 'Enable notifications for new alerts.';
      }
    }

    function alertNotificationTitle(alert) {
      const name = [alert.brand, alert.name].filter(Boolean).join(' ');
      return `${Number(alert.drop_percent || 0).toFixed(0)}% drop: ${name}`;
    }

    async function showAlertNotification(alert) {
      if (!('Notification' in window) || Notification.permission !== 'granted') return;
      const url = appOpenUrl(alert.open_url || alert.search_url || '/');
      const body = `${alert.provider_name || providerNames[alert.provider_id] || alert.provider_id} - ${money(alert.current_price)} vs ${money(alert.reference_price)}`;
      const options = {
        body,
        tag: `grocery-alert-${alert.id}`,
        data: { url },
      };
      try {
        if (navigator.serviceWorker?.ready) {
          const registration = await navigator.serviceWorker.ready;
          if (registration.showNotification) {
            await registration.showNotification(alertNotificationTitle(alert), options);
            return;
          }
        }
        const notification = new Notification(alertNotificationTitle(alert), options);
        notification.onclick = () => window.open(url, '_blank', 'noopener,noreferrer');
      } catch (error) {
        try {
          new Notification(alertNotificationTitle(alert), options);
        } catch (_) {}
      }
    }

    function reconcileAlertNotifications(alerts) {
      const ids = new Set(alerts.map(alert => Number(alert.id)).filter(Number.isFinite));
      if (!alertNotificationsPrimed) {
        saveKnownAlertIds(new Set([...knownAlertIds, ...ids]));
        alertNotificationsPrimed = true;
        localStorage.setItem('groceryAlertNotificationsPrimed', '1');
        return;
      }
      const fresh = alerts.filter(alert => !knownAlertIds.has(Number(alert.id)));
      saveKnownAlertIds(new Set([...knownAlertIds, ...ids]));
      if (!alertNotificationsEnabled || !fresh.length) return;
      fresh.slice(0, 3).forEach(alert => showAlertNotification(alert));
    }

    function alertUpdatedLabel(alert) {
      const when = fmtFriendlyTime(alert.observed_at || alert.created_at || alert.detected_at);
      return when ? `Checked ${when}` : 'Checked just now';
    }

    function renderAlertList(alerts, limit = 12) {
      return alerts.length ? alerts.slice(0, limit).map(a => `
        <article class="alert-card">
          <div class="alert-top">
            <div>
              <strong>${Number(a.drop_percent).toFixed(0)}% drop</strong>
              <div>${escapeHtml([a.brand, a.name].filter(Boolean).join(' '))}</div>
              <div class="tiny muted">${escapeHtml(a.provider_name || providerNames[a.provider_id] || a.provider_id)} - ${money(a.current_price)} vs ${money(a.reference_price)}</div>
              <div class="tiny muted">${escapeHtml(alertUpdatedLabel(a))}</div>
            </div>
            <button class="toggle-button" type="button" onclick="dismissAlert(${Number(a.id)})">Dismiss</button>
          </div>
          <div class="alert-actions">
            <a class="link-button" href="${escapeAttr(appOpenUrl(a.open_url || a.search_url || '#'))}" target="_blank" rel="noreferrer">Open</a>
          </div>
        </article>
      `).join('') : '<div class="muted tiny">No deal alerts yet. They appear after a real drop versus your saved history.</div>';
    }

    function renderAlerts() {
      const alerts = state.alerts;
      renderAlertControls(alerts);
      reconcileAlertNotifications(alerts);
      const fullList = renderAlertList(alerts, 12);
      document.getElementById('alerts').innerHTML = fullList;
      document.getElementById('homeAlerts').innerHTML = renderAlertList(alerts, 5);
    }

    function updateBasketBadge(count) {
      const badge = document.getElementById('basketBadge');
      if (!badge) return;
      badge.hidden = count === 0;
      badge.textContent = String(count);
    }

    function renderBasket() {
      const basket = state.basket || {};
      const box = document.getElementById('basket');
      const items = Array.isArray(basket.items) ? basket.items : [];
      updateBasketBadge(items.length);
      if (!items.length) {
        box.innerHTML = '<div class="muted tiny">Use Need on any item to compare a basket.</div>';
        return;
      }
      const rec = basket.recommendation || {};
      const split = basket.split || {};
      const basketScan = state.basket_scan || {};
      const basketScanRunning = Boolean(basketScan.running);
      const groups = Array.isArray(split.provider_groups) ? split.provider_groups : [];
      basketProviderGroups = Object.fromEntries(groups.map(group => [group.provider_id, group]));
      const bestSingle = basket.best_single;
      const oneAppOptions = (
        Array.isArray(basket.close_single_options) && basket.close_single_options.length
          ? basket.close_single_options
          : Array.isArray(basket.single_app_options)
            ? basket.single_app_options.slice(0, 3)
            : []
      );
      const appCount = Number(split.app_count || groups.length || 0);
      const splitLabel = appCount ? `${appCount} app${appCount === 1 ? '' : 's'}` : 'apps';
      const splitTitle = split.label || 'Cheapest split';
      const recTitle = rec.mode === 'single_app' && rec.provider_name
        ? `Use ${rec.provider_name}`
        : rec.mode === 'split'
          ? `Split across ${splitLabel}`
          : 'Basket plan';
      const missing = Array.isArray(split.missing) ? split.missing : [];
      box.innerHTML = `
        <div class="basket-builder">
          <div class="basket-summary">
            <div>
              <div class="basket-kicker">Recommended</div>
              <strong>${escapeHtml(recTitle)}</strong>
              <div class="tiny muted">${escapeHtml(rec.reason || 'Basket plan ready.')}</div>
              ${Number(rec.extra_cost || 0) > 0 ? `<div class="tiny muted">Convenience cost: ${money(rec.extra_cost)}</div>` : ''}
              ${Number(rec.saving || 0) > 0 ? `<div class="tiny muted">Estimated saving: ${money(rec.saving)}</div>` : ''}
            </div>
            <div class="actions basket-summary-actions">
              <button class="primary" onclick="refreshBasketPrices()" ${basketScanRunning ? 'disabled' : ''}>${basketScanRunning ? 'Refreshing...' : 'Refresh prices'}</button>
            </div>
            <div class="basket-metrics">
              <div class="basket-metric"><span class="tiny muted">${escapeHtml(splitTitle)}</span><strong>${money(split.total)}</strong></div>
              <div class="basket-metric"><span class="tiny muted">Best one app</span><strong>${bestSingle ? money(bestSingle.total) : '-'}</strong></div>
              <div class="basket-metric"><span class="tiny muted">Items</span><strong>${items.length}</strong></div>
            </div>
          </div>
          ${renderBasketScanStatus(basketScan)}

          <div class="basket-block">
            <div class="basket-block-title">
              <strong>${escapeHtml(splitTitle)}</strong>
              <span class="status-chip">${escapeHtml(splitLabel)}</span>
            </div>
            ${groups.length ? groups.map(renderBasketProviderGroup).join('') : '<div class="tiny muted">No priced basket lines yet.</div>'}
            ${missing.length ? `<div class="tiny muted">Missing: ${escapeHtml(missing.slice(0, 3).join(', '))}${missing.length > 3 ? ` +${missing.length - 3} more` : ''}</div>` : ''}
          </div>

          <div class="basket-block">
            <div class="basket-block-title">
              <strong>One-app options</strong>
              <span class="status-chip">Gap ${money(basket.convenience_gap)}</span>
            </div>
            <div class="single-options">
              ${oneAppOptions.length ? oneAppOptions.map(renderBasketSingleOption).join('') : '<div class="tiny muted">No one-app basket is complete yet.</div>'}
            </div>
          </div>

          <div class="basket-block">
            <div class="basket-block-title">
              <strong>Items</strong>
              <button class="link-button" onclick="clearBasket()">Clear</button>
            </div>
            <div class="basket-items">
              ${items.map(renderBasketItem).join('')}
            </div>
          </div>
        </div>
      `;
    }

    function renderBasketScanStatus(scan) {
      if (!scan || (!scan.running && !scan.started_at && !scan.last_error)) return '';
      const running = Boolean(scan.running);
      const current = scan.current_provider_name || scan.current_provider || '';
      const lastRuns = Array.isArray(scan.last_runs) ? scan.last_runs : [];
      const lastText = lastRuns.length
        ? lastRuns.map(run => `${escapeHtml(run.provider_name || run.provider_id)} ${Number(run.imported || 0)}`).join(' - ')
        : 'Waiting for first app result';
      const statusText = running
        ? current
          ? `Checking ${current}`
          : 'Starting basket refresh'
        : scan.state === 'finished'
          ? 'Basket refresh finished'
          : scan.state || 'Basket refresh';
      return `
        <div class="basket-scan-status">
          <div class="scanner-head">
            <strong>${escapeHtml(statusText)}</strong>
            <span class="status-chip ${running ? 'price-candidates' : ''}">${running ? 'running' : escapeHtml(scan.state || 'idle')}</span>
          </div>
          <div class="tiny muted">${escapeHtml(scan.message || lastText)}</div>
          <div class="tiny muted">Last: ${lastText}</div>
          ${scan.last_error ? `<div class="tiny muted">${escapeHtml(scan.last_error)}</div>` : ''}
        </div>
      `;
    }

    function renderBasketProviderGroup(group) {
      const lines = Array.isArray(group.lines) ? group.lines : [];
      const openCount = Array.isArray(group.open_urls) ? group.open_urls.length : lines.filter(line => line.open_url).length;
      return `
        <div class="provider-plan">
          <div class="provider-plan-head">
            <strong>${escapeHtml(group.provider_name)}</strong>
            <span class="line-price">${money(group.total)}</span>
          </div>
          <div class="provider-plan-actions">
            <button class="link-button" onclick="copyProviderList('${escapeAttr(group.provider_id)}')">Copy list</button>
            ${openCount ? `<button class="link-button" onclick="openProviderItems('${escapeAttr(group.provider_id)}')">Open items</button>` : ''}
          </div>
          <div class="provider-lines">
            ${lines.map(line => `
              <div class="provider-line">
                <span class="line-name">
                  ${escapeHtml(line.display_name)} <span class="muted">x${basketQty(line.quantity)}</span>
                  ${line.rank_mode === 'unit_price' && line.matched_unit_price ? `<span class="tiny muted">best value: ${money(line.matched_unit_price)} ${escapeHtml(line.unit_label || '')}</span>` : ''}
                </span>
                <span class="line-price">${money(line.line_total)}</span>
            ${line.open_url ? `<a class="link-button" href="${escapeAttr(appOpenUrl(line.open_url))}" target="_blank" rel="noreferrer">${providerLinkLabel(line.provider_id, line.open_kind)}</a>` : ''}
              </div>
            `).join('')}
          </div>
        </div>
      `;
    }

    function renderBasketSingleOption(option) {
      const missingCount = Number(option.missing_count || 0);
      const extra = option.extra_vs_split;
      const valueMode = state.basket?.split?.rank_mode === 'unit_value';
      const note = missingCount > 0
        ? `${missingCount} missing`
        : valueMode
          ? 'Raw checkout total'
        : extra === null || extra === undefined
          ? 'Complete'
          : Number(extra) > 0
            ? `${money(extra)} more than split`
            : Number(extra) < 0
              ? `${money(Math.abs(Number(extra)))} below split`
              : 'Same as split';
      return `
        <div class="app-option ${option.within_convenience ? 'is-close' : ''}">
          <div class="app-option-head">
            <strong>${escapeHtml(option.provider_name)}</strong>
            <span class="status-chip ${option.complete ? 'price-candidates' : ''}">${option.complete ? 'complete' : `${Number(option.coverage_percent || 0)}%`}</span>
          </div>
          <div class="plan-line">
            <span>${escapeHtml(note)}</span>
            <span class="line-price">${money(option.total)}</span>
          </div>
        </div>
      `;
    }

    function renderBasketItem(item) {
      const quantity = Number(item.quantity || 0);
      const options = Array.isArray(item.options) ? item.options : [];
      const visibleOptions = options.slice(0, 5);
      const hiddenCount = Math.max(0, options.length - visibleOptions.length);
      return `
        <div class="basket-item">
          <div class="basket-line">
            <div>
              <strong class="tiny">${escapeHtml(item.display_name)}</strong>
              ${item.best ? `<div class="tiny muted">${item.best.rank_mode === 'unit_price' ? 'Best value' : 'Cheapest'}: ${escapeHtml(item.best.provider_name)} ${item.best.rank_mode === 'unit_price' && item.best.matched_unit_price ? `${money(item.best.matched_unit_price)} ${escapeHtml(item.best.unit_label || '')}` : money(item.best.unit_price)}</div>` : '<div class="tiny muted">No current price found.</div>'}
            </div>
            <div class="qty">
              <button onclick="setBasket(${item.item_id}, ${Math.max(0, quantity - 1)})">-</button>
              <span>${basketQty(quantity)}</span>
              <button onclick="setBasket(${item.item_id}, ${quantity + 1})">+</button>
            </div>
          </div>
          <div class="option-chips">
            ${visibleOptions.map(renderBasketPriceChip).join('')}
            ${hiddenCount ? `<span class="price-chip is-missing"><strong>More</strong><span>+${hiddenCount} apps</span></span>` : ''}
          </div>
        </div>
      `;
    }

    function renderBasketPriceChip(option) {
      if (!option.available) {
        return `
          <span class="price-chip is-missing">
            <strong>${escapeHtml(option.provider_name)}</strong>
            <span>No price</span>
          </span>
        `;
      }
      return `
        <span class="price-chip ${option.is_best ? 'is-best' : ''}">
          <strong>${escapeHtml(option.provider_name)}</strong>
          <span>${money(option.unit_price)} pack</span>
          ${option.matched_unit_price ? `<span>${money(option.matched_unit_price)} ${escapeHtml(option.unit_label || '')}</span>` : ''}
          <span class="muted">${money(option.line_total)} total</span>
        </span>
      `;
    }

    function basketItems() {
      return Array.isArray(state.basket?.items) ? state.basket.items : [];
    }

    function providerHealth() {
      const providers = state.providers || [];
      const readyStatuses = ['browser-ready', 'official-api-ready', 'order-history-ready', 'chrome-assisted'];
      const ready = providers.filter(p => readyStatuses.includes(p.status));
      return {
        ready: ready.length,
        total: providers.length,
        needs: Math.max(0, providers.length - ready.length),
      };
    }

    function bestBuyCandidates(limit = 6) {
      const seen = new Set();
      const candidates = [];
      [...(state.alerts || [])]
        .sort((a, b) => Number(b.drop_percent || 0) - Number(a.drop_percent || 0))
        .forEach(alert => {
        const itemId = Number(alert.item_id);
        const key = Number.isFinite(itemId)
          ? `item:${itemId}`
          : `title:${[alert.brand, alert.name, alert.provider_id].filter(Boolean).join('|').toLowerCase()}`;
        if (seen.has(key)) return;
        seen.add(key);
        if (Number.isFinite(itemId)) seen.add(`item:${itemId}`);
        candidates.push({
          kind: 'alert',
          itemId,
          title: [alert.brand, alert.name].filter(Boolean).join(' ') || 'Price drop',
          provider: alert.provider_name || providerNames[alert.provider_id] || alert.provider_id,
          price: alert.current_price,
          note: `${Number(alert.drop_percent || 0).toFixed(0)}% below usual`,
          meta: alertUpdatedLabel(alert),
          url: appOpenUrl(alert.open_url || alert.search_url || '#'),
          priority: 1000 + Number(alert.drop_percent || 0),
        });
      });
      (state.items || []).forEach(card => {
        const item = card.item || {};
        const itemId = Number(item.id);
        if (seen.has(`item:${itemId}`) || !card.best?.latest) return;
        const latest = card.best.latest;
        const target = Number(item.target_price || 0);
        const price = Number(latest.effective_price);
        const saving = target > 0 && Number.isFinite(price) ? target - price : 0;
        const age = relativeTime(latest.observed_at);
        candidates.push({
          kind: saving > 0 ? 'target' : 'best',
          itemId,
          title: card.display_name,
          provider: card.best.provider_name,
          price,
          note: latest.is_generic_alternative
            ? 'Cheapest acceptable alternative'
            : saving > 0 ? `${money(saving)} under target` : 'Current cheapest app',
          meta: age ? `Checked ${age}` : 'Latest known price',
          url: appOpenUrl(card.best.open_url || card.best.search_url || searchUrl(card.best.provider_id, item)),
          priority: saving > 0 ? 500 + saving : 100,
        });
      });
      return candidates
        .filter(card => card.title && Number.isFinite(Number(card.price)))
        .sort((a, b) => Number(b.priority || 0) - Number(a.priority || 0))
        .slice(0, limit);
    }

    function renderShoppingBrief() {
      const box = document.getElementById('shoppingBrief');
      if (!box) return;
      const basket = state.basket || {};
      const items = basketItems();
      const alerts = state.alerts || [];
      const auto = state.auto_scan || {};
      const health = providerHealth();
      const rec = basket.recommendation || {};
      const split = basket.split || {};
      let primary;
      if (items.length) {
        const recTitle = rec.mode === 'single_app' && rec.provider_name
          ? `Order from ${rec.provider_name}`
          : rec.mode === 'split'
            ? `Split across ${Number(split.app_count || 0)} apps`
            : 'Review your basket';
        primary = `
          <article class="brief-card primary">
            <div class="brief-kicker">Recommended basket</div>
            <div class="brief-title">${escapeHtml(recTitle)}</div>
            <div class="brief-meta">${escapeHtml(rec.reason || `${items.length} item${items.length === 1 ? '' : 's'} waiting in your basket.`)}</div>
            <div class="brief-actions">
              <button class="primary" type="button" onclick="openBasket()">Open basket</button>
              <button type="button" onclick="refreshBasketPrices()">Refresh prices</button>
            </div>
          </article>
        `;
      } else if (alerts.length) {
        const alert = alerts[0];
        primary = `
          <article class="brief-card primary">
            <div class="brief-kicker">Worth checking now</div>
            <div class="brief-title">${escapeHtml([alert.brand, alert.name].filter(Boolean).join(' ') || 'Price drop')}</div>
            <div class="brief-meta">${escapeHtml(alert.provider_name || providerNames[alert.provider_id] || alert.provider_id)} has it at ${money(alert.current_price)}. ${Number(alert.drop_percent || 0).toFixed(0)}% below usual.</div>
            <div class="brief-actions">
              <a class="link-button primary" href="${escapeAttr(appOpenUrl(alert.open_url || alert.search_url || '#'))}" target="_blank" rel="noreferrer">Open app</a>
              ${alert.item_id ? `<button type="button" onclick="setBasket(${Number(alert.item_id)}, 1)">Add to basket</button>` : ''}
            </div>
          </article>
        `;
      } else {
        primary = `
          <article class="brief-card primary">
            <div class="brief-kicker">Start shopping</div>
            <div class="brief-title">Tell me what you need</div>
            <div class="brief-meta">Search a saved item or add a new grocery. I will compare apps and keep the basket practical.</div>
            <div class="brief-actions">
              <button class="primary" type="button" onclick="openItemDialog('', 'need_now')">Need something</button>
              <button type="button" onclick="toggleFloatingSearch(true)">Search items</button>
            </div>
          </article>
        `;
      }
      const autoText = auto.running || auto.enabled
        ? `Background updates are on${auto.next_run_at ? `, next around ${fmtShortTime(auto.next_run_at)}` : ''}.`
        : 'Background updates are paused.';
      const freshness = relativeTime(state.now) || 'just now';
      box.innerHTML = `
        ${primary}
        <article class="brief-card">
          <div class="brief-kicker">Freshness</div>
          <div class="brief-title">Updated ${escapeHtml(freshness)}</div>
          <div class="brief-meta">${escapeHtml(autoText)}</div>
          <div class="brief-actions">
            <button type="button" onclick="setSidebarOpen(true); sectionsOpen.scanner = true; renderCollapsibleStates();">Manage</button>
          </div>
        </article>
        <article class="brief-card">
          <div class="brief-kicker">Apps</div>
          <div class="brief-title">${health.ready}/${health.total} ready</div>
          <div class="brief-meta">${health.needs ? `${health.needs} app${health.needs === 1 ? '' : 's'} need login or setup.` : 'All connected apps look ready.'}</div>
          <div class="brief-actions">
            <button type="button" onclick="setSidebarOpen(true); sectionsOpen.providers = true; renderCollapsibleStates();">Check apps</button>
          </div>
        </article>
      `;
    }

    function renderBestBuys() {
      const box = document.getElementById('bestBuys');
      if (!box) return;
      const buys = bestBuyCandidates(6);
      if (!buys.length) {
        box.innerHTML = `
          <div class="best-buys-head">
            <div>
              <h2>Best Buys Now</h2>
              <div class="section-note">No current prices yet. Add or refresh items to build this shortlist.</div>
            </div>
            <button class="toggle-button" type="button" onclick="openItemDialog('', 'need_now')">Need item</button>
          </div>
        `;
        return;
      }
      box.innerHTML = `
        <div class="best-buys-head">
          <div>
            <h2>Best Buys Now</h2>
            <div class="section-note">A short list worth acting on before you scroll through everything.</div>
          </div>
          <span class="status-chip">${buys.length} picks</span>
        </div>
        <div class="best-buys-grid">
          ${buys.map(card => `
            <article class="buy-card ${card.kind === 'alert' ? 'is-alert' : ''}">
              <div>
                <div class="buy-card-title">${escapeHtml(card.title)}</div>
                <div class="tiny muted">${escapeHtml(card.provider)} - ${escapeHtml(card.note)}</div>
              </div>
              <div class="buy-card-price">
                <strong>${money(card.price)}</strong>
                <span class="tiny muted">${escapeHtml(card.meta)}</span>
              </div>
              <div class="buy-card-actions">
                ${card.url ? `<a class="link-button" href="${escapeAttr(card.url)}" target="_blank" rel="noreferrer">Open</a>` : ''}
                ${Number.isFinite(card.itemId) ? `<button type="button" onclick="setBasket(${Number(card.itemId)}, 1)">Need</button><button type="button" onclick="refreshItemPrices(${Number(card.itemId)})">Check now</button>` : ''}
              </div>
            </article>
          `).join('')}
        </div>
      `;
    }

    function basketQty(value) {
      const number = Number(value || 0);
      if (Number.isInteger(number)) return String(number);
      return number.toFixed(2).replace(/[.]?0+$/, '');
    }

    function providerChecklist(group) {
      if (!group) return '';
      if (group.checklist) return group.checklist;
      const lines = Array.isArray(group.lines) ? group.lines : [];
      return [
        `${group.provider_name || 'App'} - ${lines.length} item${lines.length === 1 ? '' : 's'} - ${money(group.total)}`,
        ...lines.map(line => `- ${line.display_name} x${basketQty(line.quantity)} - ${money(line.line_total)}`)
      ].join('\\n');
    }

    async function copyProviderList(providerId) {
      const group = basketProviderGroups[providerId];
      const text = providerChecklist(group);
      if (!text) return;
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(text);
        } else {
          const textarea = document.createElement('textarea');
          textarea.value = text;
          textarea.style.position = 'fixed';
          textarea.style.left = '-9999px';
          document.body.appendChild(textarea);
          textarea.focus();
          textarea.select();
          document.execCommand('copy');
          textarea.remove();
        }
        alert('Copied this app list.');
      } catch (error) {
        alert(text);
      }
    }

    function openProviderItems(providerId) {
      const group = basketProviderGroups[providerId];
      const urls = [...new Set((group?.open_urls || []).map(appOpenUrl).filter(Boolean))];
      if (!urls.length) return;
      urls.slice(0, 6).forEach(url => window.open(url, '_blank', 'noopener,noreferrer'));
      if (urls.length > 6) {
        alert(`Opened first 6 links. ${urls.length - 6} more are still in the app list.`);
      }
    }

    function openBasket() {
      toggleFloatingSearch(false);
      document.getElementById('basketDialog').showModal();
    }

    function renderScan() {
      const scan = state.scan;
      const box = document.getElementById('scanStatus');
      if (!scan) {
        box.innerHTML = '<div class="muted tiny">Use Prepare check when you want a fresh round of app lookups.</div>';
        return;
      }
      box.innerHTML = `
        <div class="scan-card">
          <div>
            <strong>${escapeHtml(scan.status)}</strong>
            <div class="tiny muted">${escapeHtml(scan.message || 'Scan plan ready.')}</div>
            <div class="tiny muted">${fmtTime(scan.requested_at)}</div>
          </div>
          <div class="scan-metrics">
            <div class="scan-metric"><strong>${scan.item_count}</strong><span class="tiny muted">items</span></div>
            <div class="scan-metric"><strong>${scan.provider_count}</strong><span class="tiny muted">apps</span></div>
            <div class="scan-metric"><strong>${scan.target_count}</strong><span class="tiny muted">lookups</span></div>
          </div>
        </div>
      `;
    }

    function renderAutoScan() {
      const auto = state.auto_scan || {};
      const box = document.getElementById('autoScan');
      const enabled = Boolean(auto.enabled || auto.running);
      setSectionStatus(
        'autoCheckerStatus',
        enabled ? 'On' : 'Paused',
        enabled ? 'ok' : 'warn'
      );
      const lastRuns = Array.isArray(auto.last_runs) ? auto.last_runs : [];
      const lastText = lastRuns.length ? lastRuns.map(run => {
        return `${escapeHtml(run.provider_name || run.provider_id)}: ${Number(run.imported || 0)} prices saved`;
      }).join(' - ') : 'No completed check yet';
      const currentBatch = auto.current_batch || {};
      const nextBatch = auto.next_batch || {};
      const currentBatchText = currentBatch.start && currentBatch.end
        ? `Checking items ${Number(currentBatch.start)}-${Number(currentBatch.end)} of ${Number(currentBatch.total || 0)}`
        : nextBatch.start && nextBatch.end
          ? `Next: items ${Number(nextBatch.start)}-${Number(nextBatch.end)} of ${Number(nextBatch.total || 0)}`
        : `Checking ${Number(auto.limit || 20)} saved items at a time`;
      box.innerHTML = `
        <div class="auto-card">
          <div class="scanner-head">
            <strong>${enabled ? 'Checking in background' : 'Background checking paused'}</strong>
            <span class="status-chip ${enabled ? 'price-candidates' : ''}">${enabled ? 'On' : 'Off'}</span>
          </div>
          <div class="tiny muted">${enabled ? `Runs every ${Number(auto.interval_minutes || 60)} minutes` : 'Use Start when you want continuous price updates.'}${auto.next_run_at ? ` - next ${fmtTime(auto.next_run_at)}` : ''}</div>
          <div class="tiny muted">${currentBatchText}</div>
          <div class="tiny muted">Last: ${lastText}</div>
          ${auto.last_error ? `<div class="tiny muted">${escapeHtml(auto.last_error)}</div>` : ''}
          <div class="actions">
            <button class="link-button" onclick="startAutoScan()">Start</button>
            <button class="link-button" onclick="stopAutoScan()">Pause</button>
          </div>
        </div>
      `;
    }

    function renderScanner() {
      const scanner = state.scanner;
      const box = document.getElementById('scanner');
      if (!scanner) {
        box.innerHTML = '<div class="muted tiny">App check results will appear here after a price refresh runs.</div>';
        return;
      }
      const checked = scanner.providers.filter(p => p.status !== 'not_run');
      if (!checked.length) {
        box.innerHTML = '<div class="muted tiny">No app checks yet.</div>';
        return;
      }
      box.innerHTML = `
        <div class="tiny muted">Latest app checks${scanner.updated_at ? ` - ${fmtTime(scanner.updated_at)}` : ''}</div>
        <div class="scanner-list">
          ${checked.map(p => {
            const sample = p.sample || {};
            const prices = (sample.price_candidates || []).slice(0, 4).join(', ');
            const resultCount = Number(p.result_count || p.target_count || 0);
            const importedCount = Number(p.imported_count || 0);
            const savedText = importedCount
              ? `${importedCount} prices saved`
              : resultCount
                ? `${resultCount} possible prices found`
                : 'No prices saved yet';
            const allLimit = Math.max(Number(state.items.length || 0), Number(p.target_count || 0), 3);
            const chromeAssisted = p.provider_status === 'chrome-assisted';
            return `
              <div class="scanner-card">
                <div class="scanner-head">
                  <strong>${escapeHtml(p.provider_name)}</strong>
                  <span class="status-chip ${statusClass(p.status)}">${escapeHtml(p.label)}</span>
                </div>
                <div class="tiny muted">${escapeHtml(p.message)}</div>
                <div class="tiny muted">${savedText}</div>
                ${sample.display_name ? `<div class="tiny">Example: ${escapeHtml(sample.display_name)}</div>` : ''}
                ${prices ? `<div class="tiny muted">Prices spotted: ${escapeHtml(prices)}</div>` : ''}
                <div class="actions">
                  ${chromeAssisted ? '<span class="pill">Chrome-assisted</span>' : `
                    <button class="link-button" onclick="startProviderSetup('${escapeAttr(p.provider_id)}')">Login/setup</button>
                    <button class="link-button" onclick="startProviderProbe('${escapeAttr(p.provider_id)}', 3)">Quick test</button>
                    <button class="link-button" onclick="startProviderProbe('${escapeAttr(p.provider_id)}', 20)">Refresh 20</button>
                    <button class="link-button" onclick="startProviderProbe('${escapeAttr(p.provider_id)}', ${allLimit})">Refresh all</button>
                  `}
                  ${sample.screenshot_url ? `<a href="${escapeAttr(sample.screenshot_url)}" target="_blank" rel="noreferrer">Screenshot</a>` : ''}
                </div>
              </div>
            `;
          }).join('')}
        </div>
      `;
    }

    function renderMatchReview() {
      const review = state.match_review || { summary: {}, items: [] };
      const summary = review.summary || {};
      const needsCheck = Math.max(Number(summary.review || 0), Number(summary.ignored || 0));
      setSectionStatus(
        'reviewStatus',
        needsCheck ? `${needsCheck} check` : 'OK',
        needsCheck ? 'warn' : 'ok'
      );
      document.getElementById('reviewSummary').innerHTML = `
        <div class="review-metric"><strong>${Number(summary.total || 0)}</strong><span class="muted">prices</span></div>
        <div class="review-metric"><strong>${Number(summary.review || 0)}</strong><span class="muted">needs check</span></div>
        <div class="review-metric"><strong>${Number(summary.ignored || 0)}</strong><span class="muted">ignored</span></div>
        <div class="review-metric"><strong>${Number(summary.exact || 0)}</strong><span class="muted">looks good</span></div>
        <div class="review-metric"><strong>${Number(summary.scanner || 0)}</strong><span class="muted">from apps</span></div>
      `;
      document.querySelectorAll('[data-review-filter]').forEach(button => {
        button.classList.toggle('is-active', button.dataset.reviewFilter === reviewFilter);
      });
      const rows = (review.items || []).filter(entry => {
        if (reviewFilter === 'all') return true;
        if (reviewFilter === 'scanner') return entry.source === 'browser-probe';
        if (reviewFilter === 'exact') return entry.review?.status === 'exact';
        return entry.review?.status !== 'exact';
      }).slice(0, 36);
      const box = document.getElementById('matchReview');
      if (!rows.length) {
        box.innerHTML = '<div class="muted tiny">No prices in this view right now.</div>';
        return;
      }
      box.innerHTML = `<div class="review-list">${rows.map(renderReviewRow).join('')}</div>`;
    }

    function renderReviewRow(entry) {
      const review = entry.review || {};
      const notes = Array.isArray(review.notes) && review.notes.length
        ? review.notes
        : [review.pack_match ? 'pack visible' : 'pack not visible'];
      const pricingReasons = Array.isArray(entry.pricing_reasons) ? entry.pricing_reasons : [];
      const rowClass = review.status === 'exact' ? 'exact' : review.status === 'review' ? 'needs-review' : 'looks-ok';
      const discount = review.discount_percent ? `<span class="pill">${Number(review.discount_percent).toFixed(0)}% off</span>` : '';
      return `
        <article class="review-row ${rowClass}">
          <div>
            <div class="review-title">${escapeHtml(entry.display_name)}</div>
            <div class="tiny muted">
              ${escapeHtml(entry.provider_name)} - ${escapeHtml(review.source_label || entry.source)} - ${fmtTime(entry.observed_at)}
            </div>
            <div class="review-product">${escapeHtml(truncate(entry.title || 'No product title captured', 240))}</div>
            <div class="review-meta">
              <span class="status-chip match-${escapeAttr(review.status || 'watch')}">${escapeHtml(review.label || 'Check')}</span>
              ${entry.pricing_trusted ? '<span class="pill">used for prices</span>' : '<span class="pill">ignored for prices</span>'}
              <span class="pill">title match ${Math.round(Number(review.token_score ?? 0) * 100)}%</span>
              <span class="pill">${review.pack_match ? 'pack matches' : 'check pack'}</span>
              ${discount}
              ${pricingReasons.slice(0, 2).map(reason => `<span class="pill">${escapeHtml(reason)}</span>`).join('')}
              ${notes.slice(0, 3).map(note => `<span class="pill">${escapeHtml(note)}</span>`).join('')}
            </div>
          </div>
          <div class="review-price">
            <div>
              <strong>${money(entry.effective_price)}</strong>
              <div class="tiny muted">${entry.unit_price ? `${money(entry.unit_price)} ${escapeHtml(entry.unit_label)}` : escapeHtml(entry.pack_label || '')}</div>
              ${entry.mrp ? `<div class="tiny muted">MRP ${money(entry.mrp)}</div>` : ''}
            </div>
            ${entry.open_url ? `<a class="link-button" href="${escapeAttr(appOpenUrl(entry.open_url))}" target="_blank" rel="noreferrer">${providerLinkLabel(entry.provider_id, entry.open_kind)}</a>` : ''}
          </div>
        </article>
      `;
    }

    function renderItems() {
      if (!state.items.length && Number(state.item_count || 0) > 0) {
        document.getElementById('items').innerHTML = `
          <article class="item-card">
            <div class="item-head">
              <div>
                <h2>Tracked items</h2>
                <div class="muted tiny">Background checking is running; the full item list returns when it pauses.</div>
              </div>
              <span class="pill">${Number(state.item_count || 0)} items</span>
            </div>
          </article>
        `;
        return;
      }
      document.getElementById('items').innerHTML = state.items.map(card => {
        const item = card.item;
        const focusedScan = state.basket_scan || {};
        const focusedRunning = Boolean(focusedScan.running);
        const thisItemRefreshing = focusedRunning
          && focusedScan.scan_kind === 'item'
          && Number(focusedScan.focus_item_id) === Number(item.id);
        const bestPrefix = card.best?.latest?.is_generic_alternative ? 'Best alt' : 'Best';
        const searchText = [card.display_name, item.brand, item.name, item.category]
          .filter(Boolean)
          .join(' ')
          .toLowerCase();
        return `
          <article class="item-card ${highlightedItemId === item.id ? 'is-highlighted' : ''}" id="item-${item.id}" data-search-text="${escapeAttr(searchText)}">
            <div class="item-head">
              <div>
                <h2>${escapeHtml(card.display_name)}</h2>
                <div class="item-meta">
                  <div class="muted tiny">${escapeHtml(item.category || 'Uncategorized')} - ${escapeHtml(flexibilityDescription(item.match_mode))}</div>
                  ${renderMatchModeSelect(item)}
                </div>
              </div>
              <div class="actions">
                ${card.best ? `<span class="pill best">${bestPrefix}: ${escapeHtml(card.best.provider_name)} ${money(card.best.latest.effective_price)}</span>` : '<span class="pill">No prices yet</span>'}
                <button onclick="setBasket(${item.id}, 1)">Need</button>
                <button onclick="refreshItemPrices(${item.id})" ${focusedRunning ? 'disabled' : ''}>${thisItemRefreshing ? 'Refreshing...' : 'Refresh'}</button>
              </div>
            </div>
            ${thisItemRefreshing ? `<div class="tiny muted">Refreshing this item across all connected apps.</div>` : ''}
            <div class="prices">
              ${card.prices.map(p => renderPriceBox(item, p)).join('')}
            </div>
          </article>
        `;
      }).join('');
    }

    function renderPriceBox(item, p) {
      const latest = p.latest;
      const openSearchUrl = p.open_url || p.search_url || searchUrl(p.provider_id, item);
      const isAlternative = Boolean(latest?.is_generic_alternative);
      return `
        <div class="price-box ${p.is_best ? 'is-best' : ''}">
          <div class="provider-name">${escapeHtml(p.provider_name)}</div>
          ${latest ? `
            <div class="price">${money(latest.effective_price)}</div>
            ${isAlternative ? '<div class="tiny muted">Alternative</div>' : ''}
            <div class="unit">${latest.unit_price ? `${money(latest.unit_price)} ${escapeHtml(p.unit_label)}` : 'unit price unknown'}</div>
            <div class="tiny muted">${fmtTime(latest.observed_at)}</div>
          ` : `
            <div class="muted">No price</div>
            <div class="tiny muted">${escapeHtml(p.status)}</div>
          `}
          <div class="actions">
            <button class="link-button manual-price" onclick="openPriceDialog(${item.id}, '${p.provider_id}', '${escapeAttr(p.provider_name)}')">Add price</button>
            <a href="${escapeAttr(appOpenUrl(openSearchUrl))}" target="_blank" rel="noreferrer">${providerLinkLabel(p.provider_id, p.open_kind)}</a>
          </div>
        </div>
      `;
    }

    function normalizeLookup(value) {
      return String(value || '').trim().toLowerCase().replace(/\\s+/g, ' ');
    }

    function setSearchStatus(status, text) {
      if (status) status.textContent = text;
    }

    function renderMissingItemAction(status, rawValue) {
      if (!status) return;
      const value = String(rawValue || '').trim();
      status.innerHTML = `
        <div>No saved item found for "${escapeHtml(value)}".</div>
        <button class="inline-action" type="button">Add & check now</button>
      `;
      status.querySelector('button')?.addEventListener('click', () => {
        openItemDialog(value, 'need_now');
      });
    }

    function jumpToItem(inputId = 'itemSearchInput', statusId = 'itemSearchStatus') {
      const input = document.getElementById(inputId);
      const status = document.getElementById(statusId);
      if (!input) return false;
      const query = normalizeLookup(input.value);
      if (!query) {
        setSearchStatus(status, 'Type an item name first.');
        input.focus();
        return false;
      }
      const cards = [...document.querySelectorAll('.item-card[data-search-text]')];
      if (!cards.length) {
        setSearchStatus(status, 'Item list is still loading.');
        return false;
      }
      const match = cards.find(card => normalizeLookup(card.dataset.searchText).includes(query));
      if (!match) {
        renderMissingItemAction(status, input.value.trim());
        return false;
      }
      highlightedItemId = Number(String(match.id || '').replace('item-', '')) || null;
      document.querySelectorAll('.item-card.is-highlighted').forEach(card => card.classList.remove('is-highlighted'));
      match.classList.add('is-highlighted');
      setSidebarOpen(false);
      match.scrollIntoView({ behavior: 'smooth', block: 'start' });
      setSearchStatus(status, match.querySelector('h2')?.textContent || 'Item found.');
      window.setTimeout(() => {
        if (highlightedItemId && document.getElementById(`item-${highlightedItemId}`) === match) {
          match.classList.remove('is-highlighted');
          highlightedItemId = null;
        }
      }, 3500);
      return true;
    }

    function syncFloatingSearch() {
      const panel = document.getElementById('floatingSearchPanel');
      const button = document.getElementById('floatingSearchBtn');
      if (!panel || !button) return;
      panel.hidden = !floatingSearchOpen;
      button.classList.toggle('is-open', floatingSearchOpen);
      button.setAttribute('aria-expanded', String(floatingSearchOpen));
      if (floatingSearchOpen) {
        window.setTimeout(() => {
          const input = document.getElementById('floatingSearchInput');
          input?.focus();
          input?.select();
        }, 0);
      }
    }

    function toggleFloatingSearch(force) {
      floatingSearchOpen = typeof force === 'boolean' ? force : !floatingSearchOpen;
      syncFloatingSearch();
    }

    function runFloatingSearch() {
      const didFind = jumpToItem('floatingSearchInput', 'floatingSearchStatus');
      if (didFind) toggleFloatingSearch(false);
      return didFind;
    }

    function openItemDialog(prefillName = '', intent = 'save') {
      const dialog = document.getElementById('itemDialog');
      const form = document.getElementById('itemForm');
      form.reset();
      form.dataset.intent = intent;
      form.elements.name.value = prefillName || '';
      if (intent === 'need_now') {
        document.getElementById('itemDialogTitle').textContent = 'Need something new';
        document.getElementById('itemDialogNote').textContent = 'Add it to your basket and check prices across the connected apps now.';
        form.elements.match_mode.value = 'category';
        form.elements.category.value = 'Quick needs';
      } else {
        document.getElementById('itemDialogTitle').textContent = 'Add grocery item';
        document.getElementById('itemDialogNote').textContent = 'Save recurring items here. Use Need + Check when you are shopping right now.';
        form.elements.match_mode.value = 'exact';
      }
      dialog.showModal();
      window.setTimeout(() => form.elements.name.focus(), 0);
    }

    function openPriceDialog(itemId, providerId, providerName) {
      const form = document.getElementById('priceForm');
      form.reset();
      form.item_id.value = itemId;
      form.provider_id.value = providerId;
      document.getElementById('priceDialogTitle').textContent = providerName;
      document.getElementById('priceDialog').showModal();
    }

    async function updateItemMatchMode(itemId, matchMode) {
      const cleanMode = normalizedMatchMode(matchMode);
      const card = (state.items || []).find(entry => Number(entry.item?.id) === Number(itemId));
      const previousMode = card?.item?.match_mode || 'exact';
      if (cleanMode === normalizedMatchMode(previousMode)) return;
      const res = await fetch('/api/item/update', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ item_id: itemId, match_mode: cleanMode })
      });
      const payload = await res.json();
      if (!payload.ok) {
        alert(payload.error || 'Could not update this item');
        await loadState();
        return;
      }
      state = payload.state || state;
      render();
    }

    async function setBasket(itemId, quantity) {
      const res = await fetch('/api/basket', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ item_id: itemId, quantity })
      });
      const payload = await res.json();
      if (!payload.ok) alert(payload.error || 'Could not update basket');
      await loadState();
    }

    async function clearBasket() {
      await fetch('/api/basket/clear', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: '{}'
      });
      await loadState();
    }

    async function refreshBasketPrices() {
      const res = await fetch('/api/basket/scan', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: '{}'
      });
      const payload = await res.json();
      if (!payload.ok) {
        alert(payload.error || 'Could not refresh basket prices');
        return;
      }
      await loadState();
    }

    async function refreshItemPrices(itemId) {
      const res = await fetch('/api/item/scan', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ item_id: itemId })
      });
      const payload = await res.json();
      if (!payload.ok) {
        alert(payload.error || 'Could not refresh this item');
        return;
      }
      await loadState();
      scheduleProbeRefresh(7);
    }

    async function startProviderSetup(providerId) {
      const payload = { provider_id: providerId, minutes: 30 };
      const res = await fetch('/api/provider/setup', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if (!data.ok) {
        alert(data.error || 'Could not open setup browser');
        return;
      }
      await loadState();
      alert('Setup browser opened. Log in there and set your delivery location.');
    }

    let probePollTimer = null;

    function scheduleProbeRefresh(limit) {
      if (probePollTimer) clearInterval(probePollTimer);
      let ticks = Math.max(4, Math.min(20, Math.ceil(Number(limit || 3) / 4)));
      probePollTimer = setInterval(async () => {
        ticks -= 1;
        await loadState();
        if (ticks <= 0) {
          clearInterval(probePollTimer);
          probePollTimer = null;
        }
      }, 15000);
    }

    async function startProviderProbe(providerId, limit = 3) {
      const cleanLimit = Math.max(1, Number(limit || 3));
      const payload = { provider_id: providerId, limit: cleanLimit, headless: false };
      const res = await fetch('/api/provider/probe', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if (!data.ok) {
        alert(data.error || 'Could not start this price refresh');
        return;
      }
      await loadState();
      scheduleProbeRefresh(cleanLimit);
      alert(`Price refresh started for ${cleanLimit} item${cleanLimit === 1 ? '' : 's'}. This panel will update while it runs.`);
    }

    async function startAutoScan() {
      const res = await fetch('/api/auto-scan/start', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ limit: 20, interval_minutes: 60 })
      });
      const data = await res.json();
      if (!data.ok) {
        alert(data.error || 'Could not start auto scan');
        return;
      }
      await loadState();
      alert('Background updates are on. It checks a batch at a time, then keeps rotating through the rest.');
    }

    async function stopAutoScan() {
      const res = await fetch('/api/auto-scan/stop', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: '{}'
      });
      const data = await res.json();
      if (!data.ok) {
        alert(data.error || 'Could not stop auto scan');
        return;
      }
      await loadState();
    }

    async function dismissAlert(alertId) {
      const res = await fetch('/api/alerts/dismiss', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ alert_id: alertId })
      });
      const payload = await res.json();
      if (!payload.ok) {
        alert(payload.error || 'Could not dismiss this alert');
        return;
      }
      await loadState();
    }

    async function clearAlerts() {
      if (!state.alerts.length) return;
      const res = await fetch('/api/alerts/clear', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: '{}'
      });
      const payload = await res.json();
      if (!payload.ok) {
        alert(payload.error || 'Could not clear alerts');
        return;
      }
      saveKnownAlertIds(new Set());
      await loadState();
    }

    async function requestAlertNotifications() {
      if (!('Notification' in window)) {
        alert('This browser does not support notifications for this local app.');
        return;
      }
      const permission = Notification.permission === 'granted'
        ? 'granted'
        : await Notification.requestPermission();
      if (permission !== 'granted') {
        renderAlertControls(state.alerts || []);
        return;
      }
      alertNotificationsEnabled = true;
      alertNotificationsPrimed = true;
      localStorage.setItem('groceryAlertNotifications', 'enabled');
      localStorage.setItem('groceryAlertNotificationsPrimed', '1');
      saveKnownAlertIds(new Set((state.alerts || []).map(alert => Number(alert.id)).filter(Number.isFinite)));
      renderAlertControls(state.alerts || []);
      alert('Alert notifications are on while the app is open or supported by the browser.');
    }

    function escapeHtml(value) {
      return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    }

    function escapeAttr(value) {
      return escapeHtml(value).replace(/`/g, '&#96;');
    }

    function statusClass(value) {
      return String(value || '').replace(/_/g, '-').replace(/[^a-z0-9-]/gi, '').toLowerCase();
    }

    function truncate(value, limit) {
      const text = String(value ?? '');
      return text.length > limit ? `${text.slice(0, limit - 1)}...` : text;
    }

    function toggleSection(section) {
      sectionsOpen[section] = !sectionsOpen[section];
      renderCollapsibleStates();
    }

    document.getElementById('itemSearchBtn').addEventListener('click', jumpToItem);
    document.getElementById('itemSearchInput').addEventListener('keydown', event => {
      if (event.key === 'Enter') {
        event.preventDefault();
        jumpToItem();
      }
    });
    document.getElementById('floatingSearchBtn').addEventListener('click', event => {
      event.stopPropagation();
      toggleFloatingSearch();
    });
    document.getElementById('floatingSearchGo').addEventListener('click', runFloatingSearch);
    document.getElementById('floatingSearchInput').addEventListener('keydown', event => {
      if (event.key === 'Enter') {
        event.preventDefault();
        runFloatingSearch();
      }
      if (event.key === 'Escape') {
        toggleFloatingSearch(false);
      }
    });
    document.addEventListener('click', event => {
      if (!floatingSearchOpen) return;
      const panel = document.getElementById('floatingSearchPanel');
      const button = document.getElementById('floatingSearchBtn');
      if (panel?.contains(event.target) || button?.contains(event.target)) return;
      toggleFloatingSearch(false);
    });

    document.getElementById('sidebarOpenBtn')?.addEventListener('click', () => setSidebarOpen(true));
    document.getElementById('sidebarCloseBtn')?.addEventListener('click', () => setSidebarOpen(false));
    document.getElementById('sidebarScrim')?.addEventListener('click', () => setSidebarOpen(false));
    document.getElementById('homeAlertsBtn')?.addEventListener('click', () => setHomeAlertsOpen(!homeAlertsOpen));
    document.getElementById('homeAlertsCloseBtn')?.addEventListener('click', () => setHomeAlertsOpen(false));
    document.addEventListener('keydown', event => {
      if (event.key !== 'Escape') return;
      setSidebarOpen(false);
      setHomeAlertsOpen(false);
      toggleFloatingSearch(false);
    });

    document.getElementById('reviewToggleBtn').addEventListener('click', () => toggleSection('review'));
    document.getElementById('scannerToggleBtn').addEventListener('click', () => toggleSection('scanner'));
    document.getElementById('providersToggleBtn').addEventListener('click', () => toggleSection('providers'));
    document.getElementById('clearAlertsBtn').addEventListener('click', clearAlerts);
    document.getElementById('alertsNotifyBtn').addEventListener('click', requestAlertNotifications);
    document.getElementById('exportWatchlistBtn').addEventListener('click', exportWatchlist);
    document.getElementById('importWatchlistBtn').addEventListener('click', () => {
      document.getElementById('watchlistImportFile').click();
    });
    document.getElementById('watchlistImportFile').addEventListener('change', importSelectedWatchlist);

    document.getElementById('addItemBtn')?.addEventListener('click', () => {
      openItemDialog('', 'save');
    });
    document.getElementById('homeAddItemBtn').addEventListener('click', () => {
      openItemDialog('', 'need_now');
    });
    document.getElementById('basketOpenBtn').addEventListener('click', () => {
      openBasket();
    });
    document.getElementById('basketCloseBtn').addEventListener('click', () => {
      document.getElementById('basketDialog').close();
    });
    document.getElementById('itemCancelBtn').addEventListener('click', () => {
      document.getElementById('itemDialog').close();
    });
    document.getElementById('priceCancelBtn').addEventListener('click', () => {
      document.getElementById('priceDialog').close();
    });

    document.getElementById('refreshBtn')?.addEventListener('click', async () => {
      const button = document.getElementById('refreshBtn');
      button.disabled = true;
      button.textContent = 'Planning...';
      try {
        await fetch('/api/scan', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}' });
        await loadState();
      } finally {
        button.disabled = false;
        button.textContent = 'Prepare check';
      }
    });

    document.getElementById('reviewFilters').addEventListener('click', event => {
      const button = event.target.closest('[data-review-filter]');
      if (!button) return;
      reviewFilter = button.dataset.reviewFilter || 'review';
      sectionsOpen.review = true;
      renderMatchReview();
      renderCollapsibleStates();
    });

    document.getElementById('itemForm').addEventListener('submit', async event => {
      event.preventDefault();
      const submitMode = event.submitter?.dataset?.submitMode || event.submitter?.value || event.target.dataset.intent || 'save';
      if (submitMode === 'cancel') {
        document.getElementById('itemDialog').close();
        return;
      }
      const data = Object.fromEntries(new FormData(event.target).entries());
      if (submitMode === 'need_now') {
        data.add_to_basket = true;
        data.scan_now = true;
        data.quantity = 1;
        if (!data.category) data.category = 'Quick needs';
        if (!data.brand && !data.pack_value) data.match_mode = 'category';
      }
      const res = await fetch('/api/items', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data) });
      const payload = await res.json();
      if (!payload.ok) {
        alert(payload.error || 'Could not add item');
        return;
      }
      document.getElementById('itemDialog').close();
      await loadState();
      if (submitMode === 'need_now') {
        const label = data.name || 'This item';
        if (payload.scan_started) {
          alert(`${label} is in your basket. Checking prices now.`);
        } else if (payload.scan_error) {
          alert(`${label} is in your basket, but price checking is busy: ${payload.scan_error}`);
        }
      }
    });

    document.getElementById('priceForm').addEventListener('submit', async event => {
      event.preventDefault();
      const data = Object.fromEntries(new FormData(event.target).entries());
      const res = await fetch('/api/observations', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data) });
      const payload = await res.json();
      if (!payload.ok) alert(payload.error || 'Could not add price');
      document.getElementById('priceDialog').close();
      await loadState();
    });

    if ('serviceWorker' in navigator) {
      window.addEventListener('load', () => {
        navigator.serviceWorker.register('/service-worker.js')
          .then(registration => registration.update().catch(() => {}))
          .catch(() => {});
      });
    }

    loadState();
    setInterval(() => {
      loadState().catch(() => {});
    }, 30000);
  </script>
</body>
</html>"""


def serve(config_path: Path, db_path: Path, host: str, port: int) -> None:
    config = load_config(config_path)
    conn = open_db(db_path)
    try:
        seed_demo_data(conn, config)
        if not probe_imports_busy(config, db_path.parent):
            import_probe_results(conn, config, db_path.parent)
        GroceryHandler._state_cache = build_state(conn, config, db_path.parent)
        GroceryHandler._state_cache_at = time.monotonic()
    finally:
        conn.close()
    handler = GroceryHandler
    handler.config_path = config_path
    handler.db_path = db_path
    server = http.server.ThreadingHTTPServer((host, port), handler)
    print(f"{APP_NAME} running at http://{host}:{port}", flush=True)
    server.serve_forever()


def print_status(db_path: Path, config_path: Path) -> None:
    config = load_config(config_path)
    conn = open_db(db_path)
    try:
        state = build_state(conn, config)
        print(f"{APP_NAME}")
        print(f"Items: {len(state['items'])}")
        print(f"Alerts: {len(state['alerts'])}")
        for card in state["items"][:8]:
            best = card["best"]
            if best:
                print(f"- {card['display_name']}: {best['provider_name']} Rs {best['latest']['effective_price']:.0f}")
            else:
                print(f"- {card['display_name']}: no prices")
    finally:
        conn.close()


def export_watchlist_file(db_path: Path, output_path: Path) -> None:
    conn = open_db(db_path)
    try:
        payload = export_watchlist(conn)
    finally:
        conn.close()
    write_json(output_path, payload)
    print(f"Exported {payload['item_count']} watchlist item(s) to {output_path}")


def import_watchlist_file(db_path: Path, input_path: Path, replace: bool = False) -> None:
    payload = read_json(input_path)
    conn = open_db(db_path)
    try:
        result = import_watchlist(conn, payload, replace=replace)
    finally:
        conn.close()
    print(json.dumps(result, indent=2, ensure_ascii=False))


def print_scan_plan(db_path: Path, config_path: Path) -> None:
    config = load_config(config_path)
    conn = open_db(db_path)
    try:
        plan = create_scan_plan(conn, config, db_path.parent, source="cli")
        summary = plan["summary"]
        print(f"Created scan plan #{plan['run_id']}")
        print(f"Items: {summary['items']}")
        print(f"Providers: {summary['providers']}")
        print(f"Targets: {summary['targets']}")
        print(f"Ready targets: {summary['ready_targets']}")
        print(f"Setup targets: {summary['setup_targets']}")
        print(f"Manual targets: {summary['manual_targets']}")
        print(f"Plan: {db_path.parent / 'latest_scan_plan.json'}")
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Personal grocery price cockpit")
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--db", type=Path, default=Path("data/grocery.sqlite"))
    sub = parser.add_subparsers(dest="command", required=True)
    serve_parser = sub.add_parser("serve")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    sub.add_parser("seed")
    import_parser = sub.add_parser("import-zepto")
    import_parser.add_argument("--file", type=Path, default=Path("data/zepto_orders_extract.json"))
    import_parser.add_argument("--replace", action="store_true")
    export_watchlist_parser = sub.add_parser("export-watchlist")
    export_watchlist_parser.add_argument("--file", type=Path, default=Path("watchlist.json"))
    import_watchlist_parser = sub.add_parser("import-watchlist")
    import_watchlist_parser.add_argument("--file", type=Path, default=Path("watchlist.json"))
    import_watchlist_parser.add_argument("--replace", action="store_true")
    sub.add_parser("status")
    sub.add_parser("scan-plan")
    args = parser.parse_args(argv)

    if args.command == "serve":
        serve(args.config, args.db, args.host, args.port)
        return 0
    if args.command == "seed":
        config = load_config(args.config)
        conn = open_db(args.db)
        try:
            seed_demo_data(conn, config)
            print("Seeded starter grocery list.")
        finally:
            conn.close()
        return 0
    if args.command == "import-zepto":
        config = load_config(args.config)
        conn = open_db(args.db)
        try:
            result = import_zepto_orders(conn, config, args.file, replace=args.replace)
            print(json.dumps(result, indent=2, ensure_ascii=False))
        finally:
            conn.close()
        return 0
    if args.command == "export-watchlist":
        export_watchlist_file(args.db, args.file)
        return 0
    if args.command == "import-watchlist":
        import_watchlist_file(args.db, args.file, replace=args.replace)
        return 0
    if args.command == "status":
        print_status(args.db, args.config)
        return 0
    if args.command == "scan-plan":
        print_scan_plan(args.db, args.config)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
