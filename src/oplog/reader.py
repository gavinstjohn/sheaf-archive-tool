from __future__ import annotations

import json
from datetime import date
from pathlib import Path


def read_log_file(log_path: Path) -> list[dict]:
    """Parse an NDJSON log file into a list of action dicts."""
    entries = []
    with open(log_path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass  # skip malformed lines
    return entries


def list_transactions(
    logs_dir: Path,
    filter_date: date | None = None,
    protocol: str | None = None,
) -> list[dict]:
    """Return transaction summaries, most recent first.

    Each summary has: transaction_id, protocol, started_at, ended_at,
    action_count, dry_run, error.
    """
    if not logs_dir.exists():
        return []

    log_files = sorted(logs_dir.glob("*.log"), reverse=True)
    if filter_date:
        date_str = filter_date.strftime("%Y-%m-%d")
        log_files = [f for f in log_files if f.stem == date_str]

    # Collect all entries grouped by transaction_id
    tx_map: dict[str, dict] = {}
    tx_order: list[str] = []

    for log_file in log_files:
        for entry in read_log_file(log_file):
            tx_id = entry.get("transaction_id")
            if not tx_id:
                continue
            if protocol and entry.get("protocol") != protocol:
                continue
            if tx_id not in tx_map:
                tx_map[tx_id] = {
                    "transaction_id": tx_id,
                    "protocol": entry.get("protocol"),
                    "started_at": entry.get("timestamp"),
                    "ended_at": None,
                    "action_count": 0,
                    "dry_run": entry.get("dry_run", False),
                    "error": None,
                    "log_file": str(log_file),
                }
                tx_order.append(tx_id)
            tx = tx_map[tx_id]
            if entry.get("action_type") == "transaction_end":
                tx["ended_at"] = entry.get("timestamp")
                tx["error"] = entry.get("error")
            else:
                tx["action_count"] += 1

    return [tx_map[tid] for tid in tx_order]


def get_transaction(logs_dir: Path, transaction_id: str) -> list[dict]:
    """Return all action entries for a specific transaction_id."""
    if not logs_dir.exists():
        return []
    entries = []
    for log_file in sorted(logs_dir.glob("*.log")):
        for entry in read_log_file(log_file):
            if entry.get("transaction_id") == transaction_id:
                entries.append(entry)
    return entries
