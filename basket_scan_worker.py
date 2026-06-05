#!/usr/bin/env python3
"""Run a focused scan for the current basket."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import grocery_cockpit as cockpit


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
CONFIG_PATH = ROOT / "config.json"
DB_PATH = DATA_DIR / "grocery.sqlite"
PLAN_PATH = DATA_DIR / "basket_scan_plan.json"
WORKER_PATH = ROOT / "browser_scan_worker.mjs"
STATUS_PATH = DATA_DIR / "basket_scan_status.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_status(status: dict[str, Any]) -> None:
    status["updated_at"] = now_iso()
    status["pid"] = os.getpid()
    cockpit.write_json(STATUS_PATH, status)


def write_provider_action(provider_id: str, provider_name: str, status: str, message: str, **extra: Any) -> None:
    payload = {
        "action": "basket_probe",
        "provider_id": provider_id,
        "provider_name": provider_name,
        "status": status,
        "started_at": now_iso(),
        "pid": os.getpid(),
        "message": message,
        **extra,
    }
    cockpit.write_json(DATA_DIR / f"{provider_id}_worker_action.json", payload)


def provider_target_count(plan: dict[str, Any], provider_id: str) -> int:
    return sum(1 for target in plan.get("targets", []) if target.get("provider_id") == provider_id)


def plan_focus(plan: dict[str, Any]) -> dict[str, Any]:
    targets = [target for target in plan.get("targets", []) if isinstance(target, dict)]
    item_ids = []
    labels = []
    for target in targets:
        item_id = target.get("item_id")
        if item_id not in item_ids:
            item_ids.append(item_id)
        label = str(target.get("display_name") or "").strip()
        if label and label not in labels:
            labels.append(label)
    return {
        "scan_kind": str(plan.get("source") or "basket"),
        "focus_item_id": item_ids[0] if len(item_ids) == 1 else None,
        "focus_label": labels[0] if len(labels) == 1 else "",
    }


def run_provider(
    provider_id: str,
    provider_name: str,
    limit: int,
    status: dict[str, Any],
    headless: bool,
) -> dict[str, Any]:
    provider_count = provider_target_count(cockpit.read_json(PLAN_PATH), provider_id)
    if provider_count <= 0:
        return {
            "provider_id": provider_id,
            "provider_name": provider_name,
            "finished_at": now_iso(),
            "returncode": 0,
            "imported": 0,
            "message": "No basket targets for this provider.",
        }

    clean_limit = max(1, min(int(limit), provider_count, 120))
    node_path = shutil.which("node") or "node"
    command = [
        node_path,
        str(WORKER_PATH),
        "probe",
        provider_id,
        "--plan",
        str(PLAN_PATH),
        "--limit",
        str(clean_limit),
        "--offset",
        "0",
    ]
    if headless:
        command.append("--headless")

    stdout_path = DATA_DIR / f"{provider_id}_basket_scan.log"
    stderr_path = DATA_DIR / f"{provider_id}_basket_scan.err.log"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

    status.update(
        {
            "state": "scanning",
            "current_provider": provider_id,
            "current_provider_name": provider_name,
            "last_started_at": now_iso(),
            "last_error": "",
        }
    )
    write_status(status)
    focus_label = str(status.get("focus_label") or "")
    write_provider_action(
        provider_id,
        provider_name,
        "running",
        f"Refreshing {focus_label}." if focus_label else "Basket price refresh is running.",
    )

    closed = cockpit.close_provider_profile_browsers(DATA_DIR, provider_id)
    timeout_seconds = max(150, clean_limit * 18)
    if provider_id == "amazon_fresh":
        timeout_seconds = max(timeout_seconds, 360)
    if provider_id == "dmart":
        timeout_seconds = max(timeout_seconds, 360)
    if provider_id == "bigbasket":
        timeout_seconds = max(timeout_seconds, 480)

    with stdout_path.open("a", encoding="utf-8") as stdout, stderr_path.open("a", encoding="utf-8") as stderr:
        stdout.write(f"\n--- {now_iso()} {' '.join(command)} ---\n")
        stdout.flush()
        result = subprocess.run(
            command,
            cwd=str(ROOT),
            stdout=stdout,
            stderr=stderr,
            text=True,
            timeout=timeout_seconds,
            creationflags=creationflags,
        )

    conn = cockpit.open_db(DB_PATH)
    try:
        imported = cockpit.import_probe_results_for_provider(conn, cockpit.load_config(CONFIG_PATH), DATA_DIR, provider_id)
    finally:
        conn.close()
    cockpit.mark_probe_results_imported(DATA_DIR, provider_id, imported=imported)

    message = f"Imported {imported} focused price(s)." if status.get("scan_kind") == "item" else f"Imported {imported} basket price(s)."
    if closed:
        message = f"Closed {closed} setup browser process(es). {message}"
    if result.returncode:
        message = f"Probe exited with code {result.returncode}. {message}"

    write_provider_action(provider_id, provider_name, "finished", message, imported=imported)
    return {
        "provider_id": provider_id,
        "provider_name": provider_name,
        "finished_at": now_iso(),
        "returncode": result.returncode,
        "imported": imported,
        "message": message,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh prices for the current Grocery Cockpit basket")
    parser.add_argument("--providers", default=",".join(cockpit.AUTO_SCAN_PROVIDER_IDS))
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--resume-auto", action="store_true")
    args = parser.parse_args(argv)

    providers = [provider.strip() for provider in args.providers.split(",") if provider.strip()]
    config = cockpit.load_config(CONFIG_PATH)
    provider_map = cockpit.provider_by_id(config)
    plan = cockpit.read_json(PLAN_PATH)
    focus = plan_focus(plan)
    total_items = int(plan.get("summary", {}).get("items") or args.limit or 1)
    scan_kind = focus.get("scan_kind") or "basket"
    focus_label = str(focus.get("focus_label") or "")
    start_message = f"{focus_label} price refresh started." if scan_kind == "item" and focus_label else "Basket price refresh started."
    finish_message = f"{focus_label} price refresh finished." if scan_kind == "item" and focus_label else "Basket price refresh finished."
    error_message = f"{focus_label} price refresh hit an error." if scan_kind == "item" and focus_label else "Basket price refresh hit an error."

    status: dict[str, Any] = {
        "running": True,
        "state": "starting",
        "providers": providers,
        "limit": total_items,
        "scan_kind": scan_kind,
        "focus_item_id": focus.get("focus_item_id"),
        "focus_label": focus_label,
        "resume_auto": bool(args.resume_auto),
        "started_at": now_iso(),
        "last_runs": [],
        "last_error": "",
        "source_plan": plan.get("run_id"),
        "message": start_message,
    }
    write_status(status)

    runs: list[dict[str, Any]] = []
    try:
        for provider_id in providers:
            provider = provider_map.get(provider_id)
            if not provider:
                continue
            run = run_provider(
                provider_id,
                provider.get("name", provider_id),
                limit=total_items,
                status=status,
                headless=bool(args.headless),
            )
            runs.append(run)
            status["last_runs"] = runs[-7:]
            write_status(status)
            time.sleep(1)
        status.update(
            {
                "running": False,
                "state": "finished",
                "current_provider": "",
                "current_provider_name": "",
                "last_runs": runs[-7:],
                "finished_at": now_iso(),
                "message": finish_message,
            }
        )
        write_status(status)
        if args.resume_auto:
            cockpit.launch_auto_scan(DATA_DIR, limit=20, interval_minutes=60)
        return 0
    except Exception as exc:
        status.update(
            {
                "running": False,
                "state": "error",
                "last_error": f"{type(exc).__name__}: {exc}",
                "message": error_message,
            }
        )
        write_status(status)
        if args.resume_auto:
            cockpit.launch_auto_scan(DATA_DIR, limit=20, interval_minutes=60)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
