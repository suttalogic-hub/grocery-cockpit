#!/usr/bin/env python3
"""Background auto-scan worker for Grocery Cockpit."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from math import ceil
from pathlib import Path
from typing import Any

import grocery_cockpit as cockpit
import provider_adapters


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
STATUS_PATH = DATA_DIR / "auto_scan_status.json"
CONFIG_PATH = ROOT / "config.json"
DB_PATH = DATA_DIR / "grocery.sqlite"
WORKER_PATH = ROOT / "browser_scan_worker.mjs"
DEFAULT_PROVIDERS = ",".join(cockpit.AUTO_SCAN_PROVIDER_IDS)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_status(payload: dict[str, Any]) -> None:
    payload["updated_at"] = now_iso()
    payload["pid"] = os.getpid()
    cockpit.write_json(STATUS_PATH, payload)


def write_provider_action(provider_id: str, provider_name: str, status: str, message: str, imported: int = 0) -> None:
    cockpit.write_json(
        DATA_DIR / f"{provider_id}_worker_action.json",
        {
            "action": "auto-probe",
            "provider_id": provider_id,
            "provider_name": provider_name,
            "status": status,
            "started_at": now_iso(),
            "pid": os.getpid(),
            "message": message,
            "imported": imported,
        },
    )


def read_previous_status() -> dict[str, Any]:
    try:
        return cockpit.read_json(STATUS_PATH)
    except Exception:
        return {}


def provider_target_count(provider_id: str) -> int:
    plan_path = DATA_DIR / "latest_scan_plan.json"
    try:
        plan = cockpit.read_json(plan_path)
    except Exception:
        return 0
    return sum(1 for target in plan.get("targets", []) if target.get("provider_id") == provider_id)


def batch_for_provider(provider_id: str, limit: int, cycle_index: int) -> dict[str, int]:
    total = provider_target_count(provider_id)
    if total <= 0:
        return {
            "offset": 0,
            "start": 0,
            "end": 0,
            "total": 0,
        }
    batch_count = max(1, ceil(total / limit))
    offset = (cycle_index % batch_count) * limit
    return {
        "offset": offset,
        "start": offset + 1,
        "end": min(offset + limit, total),
        "total": total,
    }


def batch_number_for_provider(provider_id: str, limit: int, cycle_index: int) -> int:
    total = provider_target_count(provider_id)
    if total <= 0:
        return cycle_index + 1
    batch_count = max(1, ceil(total / limit))
    return (cycle_index % batch_count) + 1


def visible_batch(provider_id: str, limit: int, cycle_index: int) -> dict[str, int]:
    batch = batch_for_provider(provider_id, limit, cycle_index)
    return {
        "start": batch["start"],
        "end": batch["end"],
        "total": batch["total"],
        "limit": limit,
    }


def set_next_batch_status(status: dict[str, Any], providers: list[str], limit: int) -> None:
    if not providers:
        status["next_batch"] = {}
        status["cycle_batch_number"] = int(status.get("cycles") or 0) + 1
        return
    cycle_index = int(status.get("cycles") or 0)
    provider_id = providers[0]
    status["cycle_batch_number"] = batch_number_for_provider(provider_id, limit, cycle_index)
    status["next_batch"] = visible_batch(provider_id, limit, cycle_index)


def run_provider(
    provider_id: str,
    limit: int,
    batch: dict[str, int],
    status: dict[str, Any],
) -> dict[str, Any]:
    config = cockpit.load_config(CONFIG_PATH)
    provider_map = cockpit.provider_by_id(config)
    provider_name = provider_map.get(provider_id, {}).get("name", provider_id)
    node_path = shutil.which("node") or "node"
    command = [
        node_path,
        str(WORKER_PATH),
        "probe",
        provider_id,
        "--limit",
        str(limit),
        "--offset",
        str(batch["offset"]),
    ]
    if cockpit.default_headless_scan():
        command.append("--headless")
    stdout_path = DATA_DIR / f"{provider_id}_auto_scan.log"
    stderr_path = DATA_DIR / f"{provider_id}_auto_scan.err.log"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

    status.update(
        {
            "state": "scanning",
            "current_provider": provider_id,
            "current_provider_name": provider_name,
            "current_batch": {
                "start": batch["start"],
                "end": batch["end"],
                "total": batch["total"],
                "limit": limit,
            },
            "last_started_at": now_iso(),
            "last_error": "",
        }
    )
    write_status(status)
    write_provider_action(provider_id, provider_name, "running", "Auto scan is running.")

    closed = cockpit.close_provider_profile_browsers(DATA_DIR, provider_id)
    timeout_seconds = provider_adapters.scan_timeout(provider_id, max(120, limit * 12))
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
        imported = cockpit.import_probe_results_for_provider(conn, config, DATA_DIR, provider_id)
    finally:
        conn.close()
    cockpit.mark_probe_results_imported(DATA_DIR, provider_id, imported=imported)

    message = f"Imported {imported} price(s)."
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
        "batch_start": batch["start"],
        "batch_end": batch["end"],
        "target_total": batch["total"],
        "message": message,
    }


def sleep_with_status(status: dict[str, Any], seconds: int) -> None:
    next_run_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    status.update(
        {
            "state": "sleeping",
            "current_provider": "",
            "current_provider_name": "",
            "current_batch": {},
            "next_run_at": next_run_at.isoformat(timespec="seconds"),
        }
    )
    write_status(status)
    while seconds > 0:
        time.sleep(min(5, seconds))
        seconds -= 5


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Grocery Cockpit auto scanning")
    parser.add_argument("--providers", default=DEFAULT_PROVIDERS)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--interval-minutes", type=float, default=60)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)

    providers = [provider.strip() for provider in args.providers.split(",") if provider.strip()]
    if not providers:
        raise SystemExit("Pick at least one provider.")
    limit = max(1, min(int(args.limit), 120))
    interval_seconds = max(60, int(float(args.interval_minutes) * 60))

    previous_status = read_previous_status()
    prior_cycles = int(previous_status.get("cycles") or 0)
    status: dict[str, Any] = {
        "enabled": not args.once,
        "state": "starting",
        "providers": providers,
        "limit": limit,
        "interval_minutes": round(interval_seconds / 60, 2),
        "started_at": now_iso(),
        "cycles": prior_cycles,
        "last_runs": previous_status.get("last_runs", []),
        "last_error": "",
        "rotation": True,
    }
    write_status(status)

    try:
        while True:
            cycle_runs = []
            cycle_index = int(status.get("cycles") or 0)
            set_next_batch_status(status, providers, limit)
            for provider_id in providers:
                try:
                    batch = batch_for_provider(provider_id, limit, cycle_index)
                    cycle_runs.append(run_provider(provider_id, limit, batch, status))
                except Exception as exc:  # noqa: BLE001 - keep the loop alive for unattended scans.
                    status["last_error"] = f"{type(exc).__name__}: {exc}"
                    status["state"] = "error"
                    write_status(status)
            status["cycles"] = int(status.get("cycles") or 0) + 1
            status["last_runs"] = cycle_runs
            status["last_finished_at"] = now_iso()
            set_next_batch_status(status, providers, limit)
            write_status(status)
            if args.once:
                status["enabled"] = False
                status["state"] = "finished"
                status["next_run_at"] = ""
                write_status(status)
                return 0
            sleep_with_status(status, interval_seconds)
    except KeyboardInterrupt:
        status["enabled"] = False
        status["state"] = "stopped"
        status["next_run_at"] = ""
        write_status(status)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
