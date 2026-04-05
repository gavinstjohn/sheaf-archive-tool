from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

from .reader import get_transaction
from .writer import ActionType

log = logging.getLogger(__name__)


def rollback_transaction(
    transaction_id: str,
    logs_dir: Path,
    dry_run: bool = False,
) -> list[str]:
    """Reverse all actions in a transaction, in reverse order.

    Returns a list of human-readable descriptions of what was (or would be) done.
    Raises ValueError if the transaction is not found.
    """
    entries = [
        e for e in get_transaction(logs_dir, transaction_id)
        if e.get("action_type") != ActionType.TRANSACTION_END
    ]
    if not entries:
        raise ValueError(f"Transaction not found: {transaction_id}")

    actions = list(reversed(entries))
    results = []

    for action in actions:
        action_type = action.get("action_type")
        src = Path(action["source_path"]) if action.get("source_path") else None
        dest = Path(action["dest_path"]) if action.get("dest_path") else None
        snapshot = action.get("prior_snapshot")

        if action_type == ActionType.FILE_CREATED:
            # Undo: delete the created file
            msg = f"delete {dest}"
            results.append(msg)
            if not dry_run and dest and dest.exists():
                dest.unlink()
                log.info("Rollback: deleted %s", dest)

        elif action_type == ActionType.FILE_MOVED:
            # Undo: move back from dest to src
            msg = f"move {dest} -> {src}"
            results.append(msg)
            if not dry_run and dest and src:
                src.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dest), src)
                log.info("Rollback: moved %s -> %s", dest, src)

        elif action_type == ActionType.FILE_DELETED:
            # Cannot undo a deletion without a snapshot
            msg = f"cannot restore deleted file {src} (no snapshot)"
            results.append(msg)
            log.warning("Rollback: %s", msg)

        elif action_type in (ActionType.SIDECAR_CREATED, ActionType.BINARY_META_WRITTEN):
            # Undo: delete the created sidecar/binary
            msg = f"delete {dest}"
            results.append(msg)
            if not dry_run and dest and dest.exists():
                dest.unlink()
                log.info("Rollback: deleted %s", dest)

        elif action_type == ActionType.SIDECAR_UPDATED:
            if snapshot:
                msg = f"restore {dest} from snapshot"
                results.append(msg)
                if not dry_run and dest:
                    prior = json.loads(snapshot)
                    import os
                    tmp = dest.with_suffix(".tmp")
                    with open(tmp, "w") as f:
                        json.dump(prior, f, indent=2)
                        f.write("\n")
                    os.replace(tmp, dest)
                    log.info("Rollback: restored %s from snapshot", dest)
            else:
                # Machine-generated update — delete the sidecar (can be regenerated)
                msg = f"delete {dest} (no snapshot; regenerable)"
                results.append(msg)
                if not dry_run and dest and dest.exists():
                    dest.unlink()
                    log.info("Rollback: deleted %s", dest)

        else:
            results.append(f"unknown action type {action_type!r} — skipped")

    return results
