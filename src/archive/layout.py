from __future__ import annotations

import datetime
from pathlib import Path


def date_to_dir(archive_root: Path, date: datetime.date) -> Path:
    """Return the YYYY/YYYYMMDD/ directory for a given date."""
    return archive_root / date.strftime("%Y") / date.strftime("%Y%m%d")


def media_path(
    archive_root: Path,
    date: datetime.date,
    category: str,
    subcategory: str | None,
    filename: str,
) -> Path:
    """Return the full destination path for a media file."""
    base = date_to_dir(archive_root, date) / category
    if subcategory:
        base = base / subcategory
    return base / filename


def meta_path(
    archive_root: Path,
    date: datetime.date,
    category: str,
    subcategory: str | None,
    filename: str,
) -> Path:
    """Return the .meta/ sidecar path corresponding to a media file."""
    day_dir = date_to_dir(archive_root, date)
    meta_base = day_dir / ".meta" / category
    if subcategory:
        meta_base = meta_base / subcategory
    return meta_base / (filename + ".json")


def validate_filename_prefix(filename: str, date: datetime.date) -> bool:
    """Return True if filename starts with the required YYYYMMDD_ prefix."""
    expected = date.strftime("%Y%m%d") + "_"
    return filename.startswith(expected)


def ensure_dirs(path: Path, dry_run: bool = False) -> Path:
    """Create parent directories for path. No-op in dry_run mode."""
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
    return path
