from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Iterator


def sha256_file(path: Path) -> str:
    """Return 'sha256:<hex>' for a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"


def verify_sidecar_hash(sidecar: dict, file_path: Path) -> bool:
    """Return True if the file's current hash matches the sidecar record."""
    stored = sidecar.get("file_hash")
    if not stored:
        return False
    return sha256_file(file_path) == stored


def files_in_archive(archive_root: Path) -> Iterator[Path]:
    """Yield all non-.meta/ media files under archive_root."""
    for path in archive_root.rglob("*"):
        if path.is_file() and ".meta" not in path.parts:
            yield path


def walk_meta_sidecars(archive_root: Path) -> Iterator[tuple[Path, Path]]:
    """Yield (media_path, sidecar_path) pairs by mirroring the .meta/ tree.

    For each .json file found under any .meta/ directory, resolves the
    corresponding media file path.
    """
    for sidecar_path in archive_root.rglob(".meta/**/*.json"):
        if not sidecar_path.is_file():
            continue
        # Reconstruct media path: remove .meta segment and strip .json suffix
        # e.g. YYYY/YYYYMMDD/.meta/photo/cam/file.cr3.json
        #   -> YYYY/YYYYMMDD/photo/cam/file.cr3
        parts = list(sidecar_path.parts)
        try:
            meta_idx = parts.index(".meta")
        except ValueError:
            continue
        # Replace .meta with nothing (splice it out)
        media_parts = parts[:meta_idx] + parts[meta_idx + 1:]
        media_path = Path(*media_parts)
        # Strip .json suffix
        media_path = media_path.with_name(media_path.name[:-5])  # remove .json
        yield media_path, sidecar_path


def orphaned_db_records(conn: sqlite3.Connection, archive_root: Path) -> list[str]:
    """Return file_paths in DB whose files no longer exist on disk."""
    rows = conn.execute("SELECT file_path FROM files").fetchall()
    orphans = []
    for row in rows:
        full = archive_root / row["file_path"]
        if not full.exists():
            orphans.append(row["file_path"])
    return orphans


def missing_sidecars(archive_root: Path) -> list[Path]:
    """Return media files that have no corresponding .meta/ JSON sidecar."""
    missing = []
    for media_path in files_in_archive(archive_root):
        # Derive expected sidecar path
        rel = media_path.relative_to(archive_root)
        parts = list(rel.parts)
        # Insert .meta after YYYYMMDD segment (index 1)
        if len(parts) < 3:
            continue
        sidecar_parts = parts[:2] + [".meta"] + parts[2:]
        sidecar_path = archive_root / Path(*sidecar_parts)
        sidecar_path = sidecar_path.with_name(sidecar_path.name + ".json")
        if not sidecar_path.exists():
            missing.append(media_path)
    return missing
