from __future__ import annotations

import json
import logging
import shutil
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from ..exceptions import DestinationExistsError

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


class ActionType(str, Enum):
    FILE_CREATED = "file_created"
    FILE_MOVED = "file_moved"
    FILE_DELETED = "file_deleted"
    SIDECAR_CREATED = "sidecar_created"
    SIDECAR_UPDATED = "sidecar_updated"
    BINARY_META_WRITTEN = "binary_meta_written"
    TRANSACTION_END = "transaction_end"


class LogTransaction:
    """Context manager that groups archive actions into a single logged transaction.

    Each action is written to the NDJSON log immediately (before the transaction
    ends), so a crash mid-transaction still leaves a partial, recoverable record.

    Usage::

        with LogTransaction(logs_dir, "my-protocol") as tx:
            safe_copy(src, dest, tx)
            ...
    """

    def __init__(self, logs_dir: Path, protocol: str, dry_run: bool = False) -> None:
        self.transaction_id = str(uuid.uuid4())
        self.protocol = protocol
        self.dry_run = dry_run
        self._logs_dir = logs_dir
        self._log_path: Path | None = None
        self._fh = None
        self.actions: list[dict] = []

    def __enter__(self) -> "LogTransaction":
        if not self.dry_run:
            self._logs_dir.mkdir(parents=True, exist_ok=True)
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            self._log_path = self._logs_dir / f"{date_str}.log"
            self._fh = open(self._log_path, "a")
        log.debug("Transaction %s started (protocol=%s, dry_run=%s)",
                  self.transaction_id, self.protocol, self.dry_run)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._fh is not None:
            self._write_line({
                "transaction_id": self.transaction_id,
                "action_type": ActionType.TRANSACTION_END,
                "timestamp": _now(),
                "protocol": self.protocol,
                "dry_run": self.dry_run,
                "error": str(exc_val) if exc_val else None,
            })
            self._fh.close()
            self._fh = None
        log.debug("Transaction %s ended (actions=%d, error=%s)",
                  self.transaction_id, len(self.actions), exc_val)

    def record(
        self,
        action_type: ActionType,
        source_path: Path | None = None,
        dest_path: Path | None = None,
        prior_snapshot: str | None = None,
    ) -> None:
        """Append one action to the log, flushing immediately."""
        entry = {
            "transaction_id": self.transaction_id,
            "action_type": action_type.value,
            "timestamp": _now(),
            "protocol": self.protocol,
            "dry_run": self.dry_run,
            "source_path": str(source_path) if source_path else None,
            "dest_path": str(dest_path) if dest_path else None,
            "prior_snapshot": prior_snapshot,
        }
        self.actions.append(entry)
        if self._fh is not None:
            self._write_line(entry)

    def _write_line(self, obj: dict) -> None:
        self._fh.write(json.dumps(obj, default=str) + "\n")
        self._fh.flush()


# ---------------------------------------------------------------------------
# Safe filesystem operations
# ---------------------------------------------------------------------------

def safe_copy(
    src: Path,
    dest: Path,
    tx: LogTransaction,
    dry_run: bool = False,
) -> None:
    """Copy src to dest, recording the action. Raises DestinationExistsError if dest exists."""
    if dest.exists():
        raise DestinationExistsError(
            f"Destination already exists: {dest}\n"
            "Remove it manually or use --force to overwrite (not yet implemented)."
        )
    log.debug("safe_copy %s -> %s (dry_run=%s)", src, dest, dry_run)
    if not dry_run:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    tx.record(ActionType.FILE_CREATED, source_path=src, dest_path=dest)


def safe_move(
    src: Path,
    dest: Path,
    tx: LogTransaction,
    dry_run: bool = False,
) -> None:
    """Move src to dest, recording the action. Raises DestinationExistsError if dest exists."""
    if dest.exists():
        raise DestinationExistsError(f"Destination already exists: {dest}")
    log.debug("safe_move %s -> %s (dry_run=%s)", src, dest, dry_run)
    if not dry_run:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), dest)
    tx.record(ActionType.FILE_MOVED, source_path=src, dest_path=dest)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
