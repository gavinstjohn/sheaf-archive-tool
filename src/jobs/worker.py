from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3
    from ..config import Settings

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.parent.parent


class Worker:
    """Processes enrichment jobs from the SQLite queue."""

    def __init__(self, conn: "sqlite3.Connection", settings: "Settings") -> None:
        self._conn = conn
        self._settings = settings
        self._adapter = None

    # ------------------------------------------------------------------
    # Adapter (loaded lazily so worker can start without API key if queue empty)
    # ------------------------------------------------------------------

    def _get_adapter(self):
        if self._adapter is None:
            from ..adapter import load_adapter
            self._adapter = load_adapter(_PROJECT_ROOT)
        return self._adapter

    # ------------------------------------------------------------------
    # Core processing
    # ------------------------------------------------------------------

    def run_once(self) -> bool:
        """Claim and process the next queued job.

        Returns True if a job was processed, False if the queue was empty.
        """
        from ..jobs.queue import get_next_queued_job, update_job_status
        from ..protocols.loader import get_protocol
        from ..protocols.executor import EnrichmentContext, run_enrichment
        from ..protocols.model import EnrichmentProtocol, ProtocolMaturity
        from ..archive.sidecar import read_sidecar

        job = get_next_queued_job(self._conn)
        if job is None:
            return False

        job_id = job["id"]
        protocol_name = job["protocol"]
        file_id = job["file_id"]
        log.info("Processing job %s: protocol=%s file_id=%s", job_id[:8], protocol_name, file_id)

        try:
            # Load and validate the protocol
            protocol = get_protocol(protocol_name, self._settings.protocols_dir)
            if not isinstance(protocol, EnrichmentProtocol):
                raise ValueError(f"{protocol_name!r} is not an enrichment protocol")

            # Resolve the file path from the database
            row = self._conn.execute(
                "SELECT file_path FROM files WHERE id = ?", (file_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"File id {file_id} not in database")

            rel_path = row["file_path"]
            abs_file = self._settings.archive_root / rel_path

            if not abs_file.exists():
                raise FileNotFoundError(f"Media file missing: {abs_file}")

            # Derive the sidecar path
            sidecar_path = _sidecar_path_for(self._settings.archive_root, rel_path)
            sidecar_data = read_sidecar(sidecar_path) if sidecar_path.exists() else {}

            ctx = EnrichmentContext(
                job_id=job_id,
                file_path=abs_file,
                sidecar_path=sidecar_path,
                sidecar_data=sidecar_data,
                protocol=protocol,
                settings=self._settings,
                conn=self._conn,
            )

            enrichment_data = run_enrichment(ctx, self._get_adapter())

            # Draft/probationary → needs-review; trusted → complete
            if protocol.maturity == ProtocolMaturity.TRUSTED:
                final_status = "complete"
            else:
                final_status = "needs-review"

            update_job_status(
                self._conn, job_id, final_status,
                result=json.dumps(enrichment_data),
            )
            log.info("Job %s → %s", job_id[:8], final_status)

        except Exception as e:
            log.error("Job %s failed: %s", job_id[:8], e)
            update_job_status(self._conn, job_id, "failed", error=str(e))

        return True

    def run_loop(self, poll_interval: int = 5) -> None:
        """Process jobs continuously until interrupted with Ctrl-C."""
        print(f"Worker started. Polling every {poll_interval}s. Press Ctrl-C to stop.")
        idle_reported = False
        try:
            while True:
                processed = self.run_once()
                if processed:
                    idle_reported = False
                else:
                    if not idle_reported:
                        print("Queue empty — waiting for jobs...")
                        idle_reported = True
                    time.sleep(poll_interval)
        except KeyboardInterrupt:
            print("\nWorker stopped.")


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _sidecar_path_for(archive_root: Path, rel_path: str) -> Path:
    """Derive the .meta/ sidecar path from a relative media file path.

    e.g. 2025/20250404/photo/ts5/20250404_1430.jpg
       → 2025/20250404/.meta/photo/ts5/20250404_1430.jpg.json
    """
    parts = list(Path(rel_path).parts)
    # Insert .meta after YYYY/YYYYMMDD/
    if len(parts) >= 3:
        sidecar_parts = parts[:2] + [".meta"] + parts[2:]
    else:
        sidecar_parts = [".meta"] + parts
    sidecar = archive_root / Path(*sidecar_parts)
    return sidecar.with_name(sidecar.name + ".json")
