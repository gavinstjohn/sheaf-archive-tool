from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any


def insert_file(
    conn: sqlite3.Connection,
    file_path: str,
    capture_date: str | None = None,
    file_type: str | None = None,
    file_hash: str | None = None,
    import_timestamp: str | None = None,
    imported_by_protocol: str | None = None,
    enrichment_status: dict | None = None,
) -> int:
    """Insert a new file row. Returns the new row id."""
    cur = conn.execute(
        """
        INSERT INTO files
            (file_path, capture_date, file_type, file_hash,
             import_timestamp, imported_by_protocol, enrichment_status)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            file_path,
            capture_date,
            file_type,
            file_hash,
            import_timestamp or datetime.utcnow().isoformat(),
            imported_by_protocol,
            json.dumps(enrichment_status or {}),
        ),
    )
    return cur.lastrowid


def get_file_by_path(conn: sqlite3.Connection, file_path: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM files WHERE file_path = ?", (file_path,)
    ).fetchone()


def upsert_file(
    conn: sqlite3.Connection,
    file_path: str,
    **kwargs: Any,
) -> int:
    """Insert or update a file row. Returns the row id."""
    existing = get_file_by_path(conn, file_path)
    if existing:
        set_clauses = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values()) + [file_path]
        conn.execute(
            f"UPDATE files SET {set_clauses} WHERE file_path = ?", values
        )
        return existing["id"]
    return insert_file(conn, file_path, **kwargs)


def upsert_file_from_sidecar(
    conn: sqlite3.Connection,
    sidecar: dict,
    file_path: str,
) -> int:
    """Upsert a file row from sidecar data. Returns the row id."""
    enrichment_status = sidecar.get("enrichment_status", {})
    return upsert_file(
        conn,
        file_path=file_path,
        capture_date=sidecar.get("capture_date"),
        file_type=sidecar.get("file_type"),
        file_hash=sidecar.get("file_hash"),
        import_timestamp=sidecar.get("import_timestamp"),
        imported_by_protocol=sidecar.get("imported_by_protocol"),
        enrichment_status=json.dumps(enrichment_status),
    )


def insert_metadata(conn: sqlite3.Connection, file_id: int, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO metadata (file_id, key, value) VALUES (?, ?, ?)",
        (file_id, key, value),
    )


def bulk_upsert_metadata(conn: sqlite3.Connection, file_id: int, metadata_dict: dict) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO metadata (file_id, key, value) VALUES (?, ?, ?)",
        [(file_id, k, str(v)) for k, v in metadata_dict.items()],
    )


def get_metadata(conn: sqlite3.Connection, file_id: int) -> dict:
    rows = conn.execute(
        "SELECT key, value FROM metadata WHERE file_id = ?", (file_id,)
    ).fetchall()
    return {r["key"]: r["value"] for r in rows}


def count_files(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]


def count_files_by_type(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT file_type, COUNT(*) as count FROM files GROUP BY file_type ORDER BY count DESC"
    ).fetchall()


def last_import_date(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT MAX(import_timestamp) as ts FROM files"
    ).fetchone()
    return row["ts"] if row else None


def list_file_types(conn: sqlite3.Connection) -> list[str]:
    """Return distinct file_type values, sorted alphabetically."""
    rows = conn.execute(
        "SELECT DISTINCT file_type FROM files WHERE file_type IS NOT NULL ORDER BY file_type"
    ).fetchall()
    return [r["file_type"] for r in rows]


def search_files(
    conn: sqlite3.Connection,
    query: str | None = None,
    file_type: str | None = None,
    date_start: str | None = None,
    date_end: str | None = None,
    meta_filters: dict | None = None,
    limit: int = 500,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """Search and filter files.

    query: free-text match against file_path and metadata values (case-insensitive)
    file_type: exact match on file_type
    date_start / date_end: ISO-8601 date strings (inclusive)
    meta_filters: {key: value} pairs that must all match in the metadata table
    """
    conditions: list[str] = []
    params: list = []

    if file_type:
        conditions.append("f.file_type = ?")
        params.append(file_type)

    if date_start:
        conditions.append("f.capture_date >= ?")
        params.append(date_start)

    if date_end:
        conditions.append("f.capture_date <= ?")
        params.append(date_end)

    if query:
        like = f"%{query}%"
        conditions.append(
            "(f.file_path LIKE ? OR EXISTS ("
            "  SELECT 1 FROM metadata m"
            "  WHERE m.file_id = f.id AND (m.value LIKE ? OR m.key LIKE ?)"
            "))"
        )
        params.extend([like, like, like])

    if meta_filters:
        for k, v in meta_filters.items():
            conditions.append(
                "EXISTS (SELECT 1 FROM metadata m"
                "        WHERE m.file_id = f.id AND m.key = ? AND m.value = ?)"
            )
            params.extend([k, v])

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = (
        f"SELECT f.* FROM files f {where}"
        " ORDER BY f.capture_date DESC, f.import_timestamp DESC"
        " LIMIT ? OFFSET ?"
    )
    params.extend([limit, offset])
    return conn.execute(sql, params).fetchall()


def search_embeddings(
    conn: sqlite3.Connection,
    query_embedding: bytes,
    model: str,
    limit: int = 50,
    threshold: float = 0.5,
) -> list[tuple[sqlite3.Row, float]]:
    """Find files whose embeddings are similar to query_embedding.

    Returns [(file_row, similarity_score), ...] sorted by descending similarity.
    Requires the cosine_sim UDF to be registered on the connection via
    register_cosine_sim(conn).
    """
    rows = conn.execute(
        "SELECT f.*, e.embedding FROM files f"
        " JOIN embeddings e ON e.file_id = f.id"
        " WHERE e.embedding_model = ?",
        (model,),
    ).fetchall()

    if not rows:
        return []

    results = []
    for row in rows:
        sim = _cosine_sim_bytes(query_embedding, row["embedding"])
        if sim >= threshold:
            results.append((row, sim))

    results.sort(key=lambda x: x[1], reverse=True)
    return results[:limit]


def register_cosine_sim(conn: sqlite3.Connection) -> None:
    """Register a cosine_sim(a, b) scalar UDF on the connection.

    Both arguments are BLOBs containing little-endian float32 arrays.
    """
    conn.create_function("cosine_sim", 2, _cosine_sim_bytes, deterministic=True)


def _cosine_sim_bytes(a: bytes, b: bytes) -> float:
    """Cosine similarity between two float32 BLOBs."""
    import array as _array
    va = _array.array("f"); va.frombytes(a)
    vb = _array.array("f"); vb.frombytes(b)
    if len(va) != len(vb) or not va:
        return 0.0
    dot = sum(x * y for x, y in zip(va, vb))
    mag_a = sum(x * x for x in va) ** 0.5
    mag_b = sum(x * x for x in vb) ** 0.5
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)
