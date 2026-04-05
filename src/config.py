from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .exceptions import ArchiveConfigError

# Default project directory is the sheaf/ repo root (parent of src/)
_PROJECT_ROOT = Path(__file__).parent.parent


@dataclass
class Settings:
    archive_root: Path
    db_path: Path
    logs_dir: Path
    protocols_dir: Path
    confidence_threshold: float = 0.75
    dry_run: bool = False

    # Maturity promotion thresholds
    draft_runs_for_probationary: int = 3
    probationary_runs_for_trusted: int = 10


def load_settings(
    project_dir: Path = _PROJECT_ROOT,
    archive_root: Path | None = None,
    dry_run: bool = False,
) -> Settings:
    """Load settings from config/ files, with sensible defaults.

    archive_root can be passed explicitly (e.g. from a CLI flag) to override
    whatever is in config/adapter.yaml.
    """
    config_dir = project_dir / "config"
    adapter_cfg: dict = {}
    thresholds_cfg: dict = {}

    adapter_path = config_dir / "adapter.yaml"
    if adapter_path.exists():
        with open(adapter_path) as f:
            adapter_cfg = yaml.safe_load(f) or {}

    thresholds_path = config_dir / "thresholds.yaml"
    if thresholds_path.exists():
        with open(thresholds_path) as f:
            thresholds_cfg = yaml.safe_load(f) or {}

    # archive_root: explicit arg > config file > env var
    if archive_root is None:
        raw = adapter_cfg.get("archive_root") or os.environ.get("SHEAF_ARCHIVE_ROOT")
        if raw:
            archive_root = Path(raw).expanduser().resolve()

    if archive_root is None:
        raise ArchiveConfigError(
            "archive_root is not set. Pass --archive <path> or set it in "
            "config/adapter.yaml or the SHEAF_ARCHIVE_ROOT environment variable."
        )

    return Settings(
        archive_root=archive_root,
        db_path=project_dir / "db" / "archive.db",
        logs_dir=project_dir / "logs",
        protocols_dir=project_dir / "protocols",
        confidence_threshold=thresholds_cfg.get("confidence_threshold", 0.75),
        draft_runs_for_probationary=thresholds_cfg.get("draft_runs_for_probationary", 3),
        probationary_runs_for_trusted=thresholds_cfg.get("probationary_runs_for_trusted", 10),
        dry_run=dry_run,
    )
