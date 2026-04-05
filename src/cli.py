from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .exceptions import SheafError

_PROJECT_ROOT = Path(__file__).parent.parent
log = logging.getLogger(__name__)


def _setup_logging(project_dir: Path, verbose: bool) -> None:
    logs_dir = project_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    fh = logging.FileHandler(logs_dir / "sheaf.debug.log")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    root.addHandler(fh)

    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    root.addHandler(ch)


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------

def cmd_init(settings) -> None:
    from .db.schema import open_db

    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    settings.logs_dir.mkdir(parents=True, exist_ok=True)

    if not settings.archive_root.exists():
        print(f"Warning: archive root does not exist yet: {settings.archive_root}")

    conn = open_db(settings.db_path)
    conn.close()

    config_dir = _PROJECT_ROOT / "config"
    config_dir.mkdir(exist_ok=True)

    adapter_path = config_dir / "adapter.yaml"
    if not adapter_path.exists():
        adapter_path.write_text(
            f"# Sheaf adapter configuration\n"
            f"archive_root: {settings.archive_root}\n"
            f"provider: claude\n"
            f"model: claude-opus-4-6\n"
            f"api_key_env: ANTHROPIC_API_KEY\n"
        )
        print(f"Created {adapter_path}")

    thresholds_path = config_dir / "thresholds.yaml"
    if not thresholds_path.exists():
        thresholds_path.write_text(
            "confidence_threshold: 0.75\n"
            "draft_runs_for_probationary: 3\n"
            "probationary_runs_for_trusted: 10\n"
        )
        print(f"Created {thresholds_path}")

    print(f"Initialized sheaf.")
    print(f"  archive root : {settings.archive_root}")
    print(f"  database     : {settings.db_path}")
    print(f"  logs         : {settings.logs_dir}")


def cmd_history(settings, args) -> None:
    from .oplog.reader import list_transactions, get_transaction
    import datetime

    if hasattr(args, "transaction_id") and args.transaction_id:
        # show or rollback a specific transaction
        if args.history_cmd == "show":
            entries = get_transaction(settings.logs_dir, args.transaction_id)
            if not entries:
                print(f"Transaction not found: {args.transaction_id}")
                return
            for e in entries:
                atype = e.get("action_type", "?")
                src = e.get("source_path", "")
                dest = e.get("dest_path", "")
                ts = e.get("timestamp", "")[:19]
                snap = " [snapshot]" if e.get("prior_snapshot") else ""
                print(f"  {ts}  {atype:<22}  {src or dest}{snap}")

        elif args.history_cmd == "rollback":
            from .oplog.rollback import rollback_transaction
            dry = not getattr(args, "confirm", False)
            if dry:
                print(f"Dry-run rollback of {args.transaction_id}:")
            else:
                print(f"Rolling back {args.transaction_id}:")
            steps = rollback_transaction(args.transaction_id, settings.logs_dir, dry_run=dry)
            for s in steps:
                prefix = "  [would] " if dry else "  "
                print(f"{prefix}{s}")
            if dry:
                print("\nRe-run with --confirm to execute.")
        return

    # list transactions
    filter_date = None
    if getattr(args, "date", None):
        filter_date = datetime.date.fromisoformat(args.date)
    protocol = getattr(args, "protocol", None)

    txs = list_transactions(settings.logs_dir, filter_date=filter_date, protocol=protocol)
    if not txs:
        print("No transactions found.")
        return

    print(f"{'ID':<38}  {'Protocol':<20}  {'Started':<20}  {'Actions':>7}  {'Status'}")
    print("-" * 100)
    for tx in txs:
        tx_id = tx["transaction_id"][:8] + "…"
        proto = (tx["protocol"] or "")[:20]
        started = (tx["started_at"] or "")[:19]
        n = tx["action_count"]
        status = "error" if tx.get("error") else ("dry-run" if tx.get("dry_run") else "ok")
        print(f"  {tx['transaction_id'][:36]:<36}  {proto:<20}  {started:<20}  {n:>7}  {status}")


def cmd_reindex(settings, full: bool = False) -> None:
    from .db.schema import open_db
    from .db.queries import upsert_file_from_sidecar, bulk_upsert_metadata
    from .archive.integrity import walk_meta_sidecars, sha256_file
    from .archive.sidecar import read_sidecar

    if not settings.archive_root.exists():
        print(f"Archive root does not exist: {settings.archive_root}")
        return

    conn = open_db(settings.db_path)

    if full:
        print("Full reindex: dropping and rebuilding all tables...")
        conn.executescript("""
            DELETE FROM metadata;
            DELETE FROM embeddings;
            DELETE FROM jobs;
            DELETE FROM files;
        """)
        conn.commit()

    processed = skipped = errors = 0
    for media_path, sidecar_path in walk_meta_sidecars(settings.archive_root):
        try:
            sidecar = read_sidecar(sidecar_path)
            rel_path = str(media_path.relative_to(settings.archive_root))

            if not full:
                # Incremental: skip if hash matches
                from .db.queries import get_file_by_path
                row = get_file_by_path(conn, rel_path)
                if row and row["file_hash"] == sidecar.get("file_hash"):
                    skipped += 1
                    continue

            file_id = upsert_file_from_sidecar(conn, sidecar, rel_path)

            # Index protocol_metadata and enrichment_data as searchable key-value pairs
            meta = {}
            meta.update(sidecar.get("protocol_metadata", {}))
            meta.update({f"enrichment.{k}": v
                         for k, v in sidecar.get("enrichment_data", {}).items()
                         if isinstance(v, (str, int, float))})
            if meta:
                bulk_upsert_metadata(conn, file_id, meta)

            processed += 1
        except Exception as e:
            log.warning("Error processing %s: %s", sidecar_path, e)
            errors += 1

    conn.commit()
    conn.close()

    mode = "full" if full else "incremental"
    print(f"Reindex ({mode}): {processed} updated, {skipped} unchanged, {errors} errors.")


def cmd_verify(settings, repair: bool = False) -> None:
    from .db.schema import open_db
    from .archive.integrity import (
        orphaned_db_records,
        missing_sidecars,
        verify_sidecar_hash,
        files_in_archive,
    )
    from .archive.sidecar import read_sidecar

    if not settings.archive_root.exists():
        print(f"Archive root does not exist: {settings.archive_root}")
        return

    conn = open_db(settings.db_path)
    issues = 0

    # 1. Orphaned DB records
    orphans = orphaned_db_records(conn, settings.archive_root)
    if orphans:
        print(f"\nOrphaned DB records ({len(orphans)}) — in database but not on disk:")
        for p in orphans:
            print(f"  {p}")
        issues += len(orphans)
        if repair:
            conn.executemany("DELETE FROM files WHERE file_path = ?", [(p,) for p in orphans])
            conn.commit()
            print(f"  Repaired: removed {len(orphans)} orphaned records.")

    # 2. Missing sidecars
    no_sidecar = missing_sidecars(settings.archive_root)
    if no_sidecar:
        print(f"\nMissing sidecars ({len(no_sidecar)}) — media files with no .meta/ JSON:")
        for p in no_sidecar:
            print(f"  {p.relative_to(settings.archive_root)}")
        issues += len(no_sidecar)

    # 3. Hash mismatches
    mismatches = []
    for media_path in files_in_archive(settings.archive_root):
        rel = media_path.relative_to(settings.archive_root)
        parts = list(rel.parts)
        if len(parts) < 3:
            continue
        sidecar_parts = parts[:2] + [".meta"] + parts[2:]
        from pathlib import Path
        sidecar_path = settings.archive_root / Path(*sidecar_parts)
        sidecar_path = sidecar_path.with_name(sidecar_path.name + ".json")
        if sidecar_path.exists():
            try:
                sidecar = read_sidecar(sidecar_path)
                if not verify_sidecar_hash(sidecar, media_path):
                    mismatches.append(str(rel))
            except Exception as e:
                log.warning("Could not verify %s: %s", rel, e)

    if mismatches:
        print(f"\nHash mismatches ({len(mismatches)}) — file contents differ from sidecar record:")
        for p in mismatches:
            print(f"  {p}")
        issues += len(mismatches)

    conn.close()

    if issues == 0:
        print("Archive integrity OK — no issues found.")
    else:
        print(f"\n{issues} issue(s) found." + (" Repaired where possible." if repair else " Run with --repair to fix where possible."))


def cmd_protocols(settings, args) -> None:
    from .protocols.loader import load_all_protocols, get_protocol
    from .protocols.model import ImportProtocol

    proto_cmd = getattr(args, "proto_cmd", None) or "list"

    if proto_cmd == "list":
        imports, enrichments = load_all_protocols(settings.protocols_dir)
        all_protocols = list(imports.values()) + list(enrichments.values())
        if not all_protocols:
            print("No protocols found. They will be created during `sheaf import`.")
            return
        print(f"{'Name':<30}  {'Type':<12}  {'Maturity':<14}  Description")
        print("-" * 90)
        for p in sorted(all_protocols, key=lambda x: (x.type, x.name)):
            print(f"  {p.name:<28}  {p.type:<12}  {p.maturity.value:<14}  {p.description[:40]}")

    elif proto_cmd == "show":
        try:
            p = get_protocol(args.protocol_name, settings.protocols_dir)
        except Exception as e:
            print(f"Error: {e}")
            return

        print(f"Name        : {p.name}")
        print(f"Type        : {p.type}")
        print(f"Version     : {p.version}")
        print(f"Created     : {p.created}")
        print(f"Maturity    : {p.maturity.value}")
        print(f"Description : {p.description}")
        if p.confidence_threshold is not None:
            print(f"Confidence  : {p.confidence_threshold} (overrides global)")

        if isinstance(p, ImportProtocol):
            print(f"\nTriggers:")
            for t in p.triggers:
                print(f"  {t}")
            print(f"\nCategory    : {p.category_template}")
            if p.subcategory_template:
                print(f"Subcategory : {p.subcategory_template}")
            print(f"Filename    : {p.filename_template}")
            if p.enrichment_chain:
                print(f"\nEnrichment chain:")
                for e in p.enrichment_chain:
                    req = "" if e.required else " (optional)"
                    print(f"  → {e.protocol_name}{req}")
            else:
                print(f"\nEnrichment chain: (none)")
        else:
            print(f"\nMedia types : {', '.join(p.media_types) or '(any)'}")
            print(f"Outputs     : {', '.join(p.output_fields) or '(unspecified)'}")
            print(f"Method      : {p.method}")
            if p.method == "command" and p.command_template:
                print(f"Command     : {p.command_template}")
            elif p.method == "ollama":
                print(f"Ollama model: {p.ollama_model}")
                if p.ollama_url:
                    print(f"Ollama URL  : {p.ollama_url}")

        if p.instructions:
            print(f"\nInstructions:\n{p.instructions}")


def _resolve_protocol(source_path: Path, settings, conn):
    """Match a protocol to the source or run the learning flow to create one.

    Returns (ImportProtocol | None, is_new: bool).
    is_new is True when the protocol was just created via the learning flow.
    """
    from .protocols.loader import load_import_protocols
    from .protocols.matcher import match_protocols
    from .protocols.author import draft_import_protocol
    from .adapter import load_adapter
    from .exceptions import AdapterError

    try:
        adapter = load_adapter(_PROJECT_ROOT)
    except (AdapterError, Exception) as e:
        print(f"Adapter error: {e}")
        print("Set ANTHROPIC_API_KEY (or configure config/adapter.yaml) to enable automatic matching.")
        return None, False

    protocols = load_import_protocols(settings.protocols_dir)

    if protocols:
        print(f"Matching source against {len(protocols)} known protocol(s)...")
        try:
            matches = match_protocols(source_path, protocols, adapter, settings.confidence_threshold)
        except Exception as e:
            print(f"Matching failed: {e}")
            matches = []

        if matches:
            best = matches[0]
            threshold = best.protocol.confidence_threshold or settings.confidence_threshold
            print(f"Best match: {best.protocol.name} (confidence {best.confidence:.0%})")
            print(f"  {best.reasoning}")

            if best.confidence >= threshold:
                from .protocols.model import ProtocolMaturity
                if best.protocol.maturity == ProtocolMaturity.TRUSTED:
                    return best.protocol, False

                # Draft or probationary: ask for confirmation
                answer = input(f"\nUse protocol '{best.protocol.name}'? [Y/n] ").strip().lower()
                if answer in ("", "y", "yes"):
                    return best.protocol, False
                # User declined — fall through to learning flow
            else:
                print(f"Confidence {best.confidence:.0%} is below threshold {threshold:.0%}.")
        else:
            print("No existing protocols matched.")
    else:
        print("No protocols exist yet.")

    # Enter the learning flow
    print("\nStarting import learning flow...\n")
    try:
        protocol = draft_import_protocol(source_path, adapter, settings)
    except Exception as e:
        print(f"Learning flow error: {e}")
        return None, False
    if protocol is None:
        print("Import cancelled.")
        return None, False
    return protocol, True


def cmd_import(settings, args) -> None:
    from .db.schema import open_db
    from .protocols.loader import load_import_protocols, get_protocol
    from .protocols.executor import ProtocolExecutor
    from .protocols.model import ImportProtocol

    source_path = Path(args.source).expanduser().resolve()
    if not source_path.exists():
        print(f"Source path does not exist: {source_path}")
        return

    conn = open_db(settings.db_path)
    executor = ProtocolExecutor()

    is_new_protocol = False
    if args.protocol:
        # Use the named protocol directly, bypassing matching
        try:
            protocol = get_protocol(args.protocol, settings.protocols_dir)
        except Exception as e:
            print(f"Error: {e}")
            conn.close()
            return
        if not isinstance(protocol, ImportProtocol):
            print(f"Error: {args.protocol!r} is an enrichment protocol, not an import protocol.")
            conn.close()
            return
    else:
        protocol, is_new_protocol = _resolve_protocol(source_path, settings, conn)
        if protocol is None:
            conn.close()
            return

    print(f"Protocol  : {protocol.name} ({protocol.maturity.value})")
    print(f"Source    : {source_path}")
    print(f"Archive   : {settings.archive_root}")
    if settings.dry_run:
        print("[dry-run mode — no files will be copied]")
    print()

    actions = executor.plan(source_path, protocol, settings)

    if not actions:
        print("No matching files found in source.")
        conn.close()
        return

    print(executor.preview(actions, settings.archive_root))

    if settings.dry_run:
        conn.close()
        return

    print()
    result = executor.execute(actions, protocol, settings, conn)
    conn.close()

    print(f"Imported  : {result.files_copied} file(s)")
    if result.files_skipped:
        print(f"Skipped   : {result.files_skipped} (destination already exists)")
    if result.errors:
        print(f"Errors    : {len(result.errors)}")
        for e in result.errors:
            print(f"  {e}")
    if result.job_ids:
        print(f"Queued    : {len(result.job_ids)} enrichment job(s)")
    print(f"TX ID     : {result.transaction_id}")

    # After a newly learned import protocol, offer to set up enrichment
    if is_new_protocol and result.files_copied > 0 and not settings.dry_run:
        print()
        answer = input("Would you like to set up enrichment protocols for these files? [y/N] ").strip().lower()
        if answer in ("y", "yes"):
            from .protocols.author import draft_enrichment_protocol
            from .adapter import load_adapter
            try:
                adapter = load_adapter(_PROJECT_ROOT)
                # Pass a sample spread across the import, plus the total count
                step = max(1, len(actions) // 20)
                sample = [a.dest_file for a in actions[::step]][:30]
                draft_enrichment_protocol(sample, adapter, settings,
                                          total_imported=result.files_copied)
            except Exception as e:
                print(f"Enrichment setup error: {e}")


def cmd_jobs(settings, args) -> None:
    from .db.schema import open_db
    from .jobs.queue import list_jobs, count_jobs_by_status

    if not settings.db_path.exists():
        print("Database not found. Run `sheaf init` first.")
        return

    conn = open_db(settings.db_path)

    jobs_cmd = getattr(args, "jobs_cmd", None)

    # Worker mode
    if getattr(args, "worker", False):
        from .jobs.worker import Worker
        worker = Worker(conn, settings)
        worker.run_loop()
        conn.close()
        return

    # Review a specific job
    if jobs_cmd == "review":
        _cmd_jobs_review(conn, settings, args.job_id)
        conn.close()
        return

    # Default: list jobs
    status_filter = getattr(args, "status", None)
    protocol_filter = getattr(args, "protocol", None)

    if not status_filter and not protocol_filter:
        counts = count_jobs_by_status(conn)
        if not counts:
            print("No jobs in queue.")
            conn.close()
            return
        print("Job queue summary:")
        for status in ("needs-review", "processing", "queued", "complete", "failed"):
            n = counts.get(status, 0)
            if n:
                print(f"  {status:<14} {n}")
        print()

    jobs = list_jobs(conn, status=status_filter, protocol=protocol_filter)
    conn.close()

    if not jobs:
        print("No jobs found.")
        return

    print(f"  {'ID':<36}  {'Status':<14}  {'Protocol':<24}  File")
    print("  " + "-" * 100)
    for job in jobs:
        jid = job["id"][:36]
        st = job["status"][:14]
        proto = (job["protocol"] or "")[:24]
        fpath = (job.get("file_path") or "")[-50:]
        print(f"  {jid:<36}  {st:<14}  {proto:<24}  {fpath}")


def _cmd_jobs_review(conn, settings, job_id: str) -> None:
    """Enter a chat session to review a needs-review enrichment job."""
    from .jobs.queue import list_jobs, update_job_status
    from .adapter import load_adapter
    from .adapter.base import Message
    from .chat.session import ChatSession, readline_chat
    import json

    # Find job by prefix or full ID
    all_jobs = list_jobs(conn, status="needs-review")
    job = next(
        (j for j in all_jobs if j["id"] == job_id or j["id"].startswith(job_id)),
        None,
    )
    if job is None:
        # Also search all statuses
        all_jobs = list_jobs(conn)
        job = next(
            (j for j in all_jobs if j["id"] == job_id or j["id"].startswith(job_id)),
            None,
        )
    if job is None:
        print(f"Job not found: {job_id}")
        return

    print(f"Job      : {job['id']}")
    print(f"Status   : {job['status']}")
    print(f"Protocol : {job['protocol']}")
    print(f"File     : {job.get('file_path', '(unknown)')}")

    if job["status"] != "needs-review":
        print(f"\nThis job is '{job['status']}', not 'needs-review'. Nothing to review.")
        return

    result_data = {}
    if job.get("result"):
        try:
            result_data = json.loads(job["result"])
        except json.JSONDecodeError:
            result_data = {"raw": job["result"]}

    print("\nEnrichment result:")
    print(json.dumps(result_data, indent=2))

    answer = input("\nAccept this enrichment result? [Y/n/edit] ").strip().lower()
    if answer in ("", "y", "yes"):
        update_job_status(conn, job["id"], "complete")
        print("Accepted. Job marked complete.")
        return
    elif answer in ("n", "no"):
        update_job_status(conn, job["id"], "failed", error="Rejected by user.")
        print("Rejected. Job marked failed.")
        return

    # "edit" — enter a chat session to discuss/refine
    try:
        adapter = load_adapter(_PROJECT_ROOT)
    except Exception as e:
        print(f"Adapter error: {e}")
        return

    session = ChatSession(adapter, max_tokens=4096)

    from .adapter.base import ToolDefinition

    accepted_result: list[dict] = []

    session.register_tool(
        ToolDefinition(
            name="accept_result",
            description="Accept the final enrichment result and mark the job complete.",
            input_schema={
                "type": "object",
                "properties": {
                    "result": {
                        "type": "object",
                        "description": "The final enrichment data to store.",
                    },
                },
                "required": ["result"],
            },
        ),
        lambda args: _review_accept(conn, job["id"], args, accepted_result, session),
    )

    opening = session.tool_loop(
        f"I'm reviewing an enrichment result for file: {job.get('file_path', '?')}\n"
        f"Protocol: {job['protocol']}\n\n"
        f"Current result:\n{json.dumps(result_data, indent=2)}\n\n"
        "Please help me review and refine this result. Once we agree on the final "
        "version, call accept_result with the corrected data."
    )
    print(f"\nSheaf: {opening}\n")
    readline_chat(session)

    if not accepted_result:
        print("No result accepted. Job remains in needs-review.")


def _review_accept(conn, job_id, args, accepted_result, session):
    import json
    from .jobs.queue import update_job_status
    result = args.get("result", {})
    update_job_status(conn, job_id, "complete", result=json.dumps(result))
    accepted_result.append(result)
    session.done = True
    return "Result accepted and saved. Job marked complete."


def cmd_status(settings) -> None:
    from .db.schema import open_db
    from .db.queries import count_files, count_files_by_type, last_import_date
    from .jobs.queue import count_jobs_by_status
    from .protocols.loader import load_all_protocols

    archive_exists = settings.archive_root.exists()
    print(f"Archive  : {settings.archive_root}" + ("" if archive_exists else "  (not found)"))
    print(f"Database : {settings.db_path}")

    if not settings.db_path.exists():
        print("\nDatabase not found. Run `sheaf init` first.")
        return

    conn = open_db(settings.db_path)

    # Files
    total = count_files(conn)
    by_type = count_files_by_type(conn)
    last_import = last_import_date(conn)
    print(f"\nFiles    : {total:,}")
    for row in by_type:
        ft = row["file_type"] or "(unknown)"
        print(f"  {ft:<20} {row['count']:>6,}")
    print(f"Last import: {(last_import or 'never')[:19]}")

    # Jobs
    job_counts = count_jobs_by_status(conn)
    conn.close()
    if job_counts:
        print("\nJobs:")
        for status in ("needs-review", "queued", "processing", "complete", "failed"):
            n = job_counts.get(status, 0)
            if n:
                print(f"  {status:<14} {n:>6,}")

    # Protocols
    imports, enrichments = load_all_protocols(settings.protocols_dir)
    all_protos = list(imports.values()) + list(enrichments.values())
    if all_protos:
        from .protocols.model import ProtocolMaturity
        by_maturity: dict[str, int] = {}
        for p in all_protos:
            by_maturity[p.maturity.value] = by_maturity.get(p.maturity.value, 0) + 1
        maturity_str = ", ".join(
            f"{n} {m}" for m, n in by_maturity.items()
        )
        print(f"\nProtocols: {len(imports)} import, {len(enrichments)} enrichment  ({maturity_str})")
    else:
        print("\nProtocols: none yet")

    if settings.dry_run:
        print("\n[dry-run mode active]")


def cmd_search(settings, args) -> None:
    from .db.schema import open_db
    from .db.queries import search_files

    if not settings.db_path.exists():
        print("Database not found. Run `sheaf init` first.")
        return

    # Parse --meta key=value pairs
    meta_filters = {}
    for kv in getattr(args, "meta", []) or []:
        if "=" in kv:
            k, v = kv.split("=", 1)
            meta_filters[k.strip()] = v.strip()

    # Parse date range (single date or start..end)
    date_start = date_end = None
    date_arg = getattr(args, "date", None)
    if date_arg:
        if ".." in date_arg:
            parts = date_arg.split("..", 1)
            date_start, date_end = parts[0].strip(), parts[1].strip()
        else:
            date_start = date_end = date_arg.strip()

    conn = open_db(settings.db_path)
    rows = search_files(
        conn,
        query=args.query or None,
        file_type=getattr(args, "type", None),
        date_start=date_start,
        date_end=date_end,
        meta_filters=meta_filters or None,
        limit=200,
    )
    conn.close()

    if not rows:
        print("No results.")
        return

    if getattr(args, "browse", False):
        from .web.server import start_server
        # Launch the GUI — user can refine results in the browser
        start_server(settings, open_browser=True)
        return

    # CLI output
    print(f"{'Date':<12}  {'Type':<16}  Path")
    print("-" * 80)
    for row in rows:
        date = (row["capture_date"] or "")[:10]
        ftype = (row["file_type"] or "")[:16]
        print(f"  {date:<10}  {ftype:<16}  {row['file_path']}")
    print(f"\n{len(rows)} result(s).")


def cmd_browse(settings, args) -> None:
    from .web.server import start_server
    port = getattr(args, "port", 8765) or 8765
    start_server(settings, port=port, open_browser=True)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sheaf",
        description="Personal media archive management system.",
    )
    parser.add_argument(
        "--archive",
        metavar="PATH",
        help="Path to archive root (overrides config/adapter.yaml).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Preview actions without making any changes.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=False,
        help="Show DEBUG log output on stderr.",
    )

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    # init
    sub.add_parser("init", help="Initialize sheaf (create database and default config).")

    # status
    sub.add_parser("status", help="Show archive summary.")

    # history
    hist = sub.add_parser("history", help="Browse and manage the operation log.")
    hist_sub = hist.add_subparsers(dest="history_cmd", metavar="SUBCOMMAND")

    hist_list = hist_sub.add_parser("list", help="List recent transactions (default).")
    hist_list.add_argument("--date", metavar="YYYY-MM-DD", help="Filter by date.")
    hist_list.add_argument("--protocol", metavar="NAME", help="Filter by protocol.")

    hist_show = hist_sub.add_parser("show", help="Show all actions in a transaction.")
    hist_show.add_argument("transaction_id", metavar="TX-ID")

    hist_roll = hist_sub.add_parser("rollback", help="Reverse a transaction.")
    hist_roll.add_argument("transaction_id", metavar="TX-ID")
    hist_roll.add_argument(
        "--confirm",
        action="store_true",
        default=False,
        help="Actually execute the rollback (default is dry-run).",
    )

    # import
    import_p = sub.add_parser("import", help="Import media from a source path.")
    import_p.add_argument("source", metavar="PATH", help="Source file, directory, or device.")
    import_p.add_argument(
        "--protocol", "-p",
        metavar="NAME",
        help="Use this protocol (skips automatic matching).",
    )
    import_p.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Preview without copying any files.",
    )

    # jobs
    jobs_p = sub.add_parser("jobs", help="View the enrichment job queue.")
    jobs_p.add_argument("--status", metavar="STATUS", help="Filter by status.")
    jobs_p.add_argument("--protocol", metavar="NAME", help="Filter by protocol.")
    jobs_p.add_argument(
        "--worker",
        action="store_true",
        default=False,
        help="Run the enrichment worker (processes queue continuously).",
    )
    jobs_sub = jobs_p.add_subparsers(dest="jobs_cmd", metavar="SUBCOMMAND")
    jobs_review = jobs_sub.add_parser("review", help="Review a needs-review job.")
    jobs_review.add_argument("job_id", metavar="JOB-ID")

    # search
    search_p = sub.add_parser("search", help="Search the archive.")
    search_p.add_argument("query", nargs="?", metavar="QUERY",
                          help="Search text (omit to list all files).")
    search_p.add_argument("--type", metavar="TYPE", help="Filter by media type.")
    search_p.add_argument("--date", metavar="DATE_OR_RANGE",
                          help="Filter by date (YYYYMMDD) or range (start..end).")
    search_p.add_argument("--meta", metavar="KEY=VALUE", action="append",
                          help="Filter by metadata key=value (repeatable).")
    search_p.add_argument("--browse", action="store_true", default=False,
                          help="Open results in the web GUI.")

    # browse
    browse_p = sub.add_parser("browse", help="Open the local web GUI.")
    browse_p.add_argument("--port", type=int, default=8765, metavar="PORT",
                          help="Port to serve on (default 8765).")

    # reindex
    reindex_p = sub.add_parser("reindex", help="Rebuild database from .meta/ sidecars.")
    reindex_p.add_argument(
        "--full",
        action="store_true",
        default=False,
        help="Drop all records and rebuild from scratch.",
    )

    # verify
    verify_p = sub.add_parser("verify", help="Check archive integrity.")
    verify_p.add_argument(
        "--repair",
        action="store_true",
        default=False,
        help="Fix issues where possible (e.g. remove orphaned DB records).",
    )

    # protocols
    proto = sub.add_parser("protocols", help="Manage and inspect learned protocols.")
    proto_sub = proto.add_subparsers(dest="proto_cmd", metavar="SUBCOMMAND")
    proto_sub.add_parser("list", help="List all protocols.")
    proto_show = proto_sub.add_parser("show", help="Show protocol details.")
    proto_show.add_argument("protocol_name", metavar="NAME")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    _setup_logging(_PROJECT_ROOT, args.verbose)

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    # Load settings — archive_root from --archive flag if provided
    from .config import load_settings
    from .exceptions import ArchiveConfigError

    archive_root = Path(args.archive).expanduser().resolve() if args.archive else None

    try:
        settings = load_settings(
            project_dir=_PROJECT_ROOT,
            archive_root=archive_root,
            dry_run=args.dry_run,
        )
    except ArchiveConfigError as e:
        # init and status can still hint the user without a valid archive_root
        # when --archive is not provided; give a clear message.
        if args.command in ("init",) and archive_root is None:
            print(f"Error: {e}")
            sys.exit(1)
        elif archive_root is None:
            print(f"Error: {e}")
            sys.exit(1)
        else:
            raise

    try:
        if args.command == "init":
            cmd_init(settings)
        elif args.command == "status":
            cmd_status(settings)
        elif args.command == "history":
            if not hasattr(args, "history_cmd") or args.history_cmd is None:
                args.history_cmd = "list"
                args.date = None
                args.protocol = None
            cmd_history(settings, args)
        elif args.command == "import":
            # --dry-run on the subcommand overrides the global flag
            if getattr(args, "dry_run", False):
                settings.dry_run = True
            cmd_import(settings, args)
        elif args.command == "jobs":
            cmd_jobs(settings, args)
        elif args.command == "search":
            cmd_search(settings, args)
        elif args.command == "browse":
            cmd_browse(settings, args)
        elif args.command == "reindex":
            cmd_reindex(settings, full=args.full)
        elif args.command == "verify":
            cmd_verify(settings, repair=args.repair)
        elif args.command == "protocols":
            if not hasattr(args, "proto_cmd") or args.proto_cmd is None:
                args.proto_cmd = "list"
            cmd_protocols(settings, args)
        else:
            parser.print_help()
    except SheafError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
