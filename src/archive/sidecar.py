from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class SidecarFields:
    """Universal fields managed by the framework in every sidecar."""
    source_file: str
    capture_date: str               # YYYY-MM-DD
    file_type: str
    import_timestamp: str           # ISO-8601
    imported_by_protocol: str
    file_hash: str
    enrichment_status: dict = field(default_factory=dict)
    protocol_metadata: dict = field(default_factory=dict)
    enrichment_data: dict = field(default_factory=dict)
    binary_refs: dict = field(default_factory=dict)


def read_sidecar(sidecar_path: Path) -> dict:
    """Read and parse a sidecar JSON file."""
    with open(sidecar_path) as f:
        return json.load(f)


def write_sidecar(sidecar_path: Path, data: dict) -> None:
    """Atomically write sidecar data to disk (write .tmp, then os.replace)."""
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = sidecar_path.with_suffix(".tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, default=str)
            f.write("\n")
        os.replace(tmp, sidecar_path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def update_sidecar(
    sidecar_path: Path,
    updates: dict,
    snapshot: bool = True,
) -> dict | None:
    """Merge updates into an existing sidecar. Returns prior state if snapshot=True.

    If the sidecar does not exist, creates it with the given updates.
    """
    prior: dict | None = None
    if sidecar_path.exists():
        prior = read_sidecar(sidecar_path)
        data = dict(prior)
    else:
        data = {}

    # Deep-merge nested dicts for known nested keys
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key] = {**data[key], **value}
        else:
            data[key] = value

    write_sidecar(sidecar_path, data)
    return prior if snapshot else None


def create_initial_sidecar(
    sidecar_path: Path,
    file_path: Path,
    capture_date: str,
    file_type: str,
    protocol_name: str,
    file_hash: str,
    protocol_metadata: dict | None = None,
) -> dict:
    """Create the initial sidecar for a newly imported file. Returns the sidecar dict."""
    data: dict[str, Any] = {
        "source_file": file_path.name,
        "capture_date": capture_date,
        "file_type": file_type,
        "import_timestamp": datetime.now(timezone.utc).isoformat(),
        "imported_by_protocol": protocol_name,
        "file_hash": file_hash,
        "enrichment_status": {},
        "protocol_metadata": protocol_metadata or {},
        "enrichment_data": {},
        "binary_refs": {},
    }
    write_sidecar(sidecar_path, data)
    return data
