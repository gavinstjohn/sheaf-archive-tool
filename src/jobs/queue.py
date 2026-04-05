from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone


def enqueue_enrichment(
    conn: sqlite3.Connection,
    file_id: int,
    protocol_name: str,
) -> str:
    """Enqueue an enrichment job. Returns the job ID."""
    job_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO jobs (id, type, status, protocol, file_id, created_at)
        VALUES (?, 'enrichment', 'queued', ?, ?, ?)
        """,
        (job_id, protocol_name, file_id, datetime.now(timezone.utc).isoformat()),
    )
    return job_id


def get_next_queued_job(conn: sqlite3.Connection) -> dict | None:
    """Atomically claim the next queued job, transitioning it to 'processing'.

    Returns the job row as a dict, or None if the queue is empty.
    """
    row = conn.execute(
        "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE jobs SET status = 'processing', updated_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), row["id"]),
    )
    conn.commit()
    return dict(row)


def update_job_status(
    conn: sqlite3.Connection,
    job_id: str,
    status: str,
    error: str | None = None,
    result: str | None = None,
) -> None:
    conn.execute(
        "UPDATE jobs SET status = ?, updated_at = ?, error = ?, result = ? WHERE id = ?",
        (status, datetime.now(timezone.utc).isoformat(), error, result, job_id),
    )
    conn.commit()


def list_jobs(
    conn: sqlite3.Connection,
    status: str | None = None,
    protocol: str | None = None,
) -> list[dict]:
    query = "SELECT j.*, f.file_path FROM jobs j LEFT JOIN files f ON j.file_id = f.id"
    conditions = []
    params: list = []
    if status:
        conditions.append("j.status = ?")
        params.append(status)
    if protocol:
        conditions.append("j.protocol = ?")
        params.append(protocol)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY j.created_at DESC"
    return [dict(r) for r in conn.execute(query, params).fetchall()]


def jobs_needing_review(conn: sqlite3.Connection) -> list[dict]:
    return list_jobs(conn, status="needs-review")


def count_jobs_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) as n FROM jobs GROUP BY status"
    ).fetchall()
    return {r["status"]: r["n"] for r in rows}
