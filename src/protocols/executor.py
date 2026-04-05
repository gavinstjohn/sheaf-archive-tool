from __future__ import annotations

import json
import logging
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from ..archive.integrity import sha256_file
from ..archive.layout import ensure_dirs, meta_path, media_path
from ..archive.sidecar import create_initial_sidecar
from ..db.queries import upsert_file_from_sidecar
from ..exceptions import SheafError
from ..oplog.writer import ActionType, LogTransaction, safe_copy

if TYPE_CHECKING:
    from .model import ImportProtocol, EnrichmentProtocol
    from ..adapter.base import BaseAdapter
    from ..config import Settings

log = logging.getLogger(__name__)

# Files to skip unconditionally regardless of protocol
_SKIP_PATTERNS = {
    # macOS resource forks and metadata
    lambda name: name.startswith("._"),
    lambda name: name == ".DS_Store",
    lambda name: name == "Thumbs.db",
}


@dataclass
class PlannedAction:
    source_file: Path
    dest_file: Path
    meta_file: Path
    capture_date: date
    capture_time: str | None   # "HHMM" or None
    category: str
    subcategory: str | None
    dest_filename: str


@dataclass
class PlanResult:
    """Output of ProtocolExecutor.plan() — actions to take plus what was filtered out."""
    actions: list[PlannedAction]
    # Files skipped because their extension wasn't in the protocol's triggers.
    # Key: lowercase extension (e.g. ".mts"), value: count.
    # Empty if no trigger extensions are defined (protocol accepts all files).
    skipped_extensions: dict[str, int] = field(default_factory=dict)


@dataclass
class ExecutionResult:
    files_copied: int = 0
    files_skipped: int = 0
    errors: list[str] = field(default_factory=list)
    transaction_id: str | None = None
    job_ids: list[str] = field(default_factory=list)


class ProtocolExecutor:
    """Executes import protocols against a source path."""

    def plan(
        self,
        source_path: Path,
        protocol: "ImportProtocol",
        settings: "Settings",
    ) -> PlanResult:
        """Walk source_path, apply the protocol, and return a PlanResult.

        No filesystem changes are made.
        """
        extensions = _trigger_extensions(protocol)
        actions: list[PlannedAction] = []
        skipped_extensions: dict[str, int] = {}
        index = 0

        # Pre-scan for source-level camera EXIF only when the protocol needs it.
        # Files like AVCHD .MTS often lack camera model in their own EXIF; inheriting
        # from siblings on the same card (e.g., JPGs) fills the gap automatically.
        templates = " ".join(filter(None, [
            protocol.category_template,
            protocol.subcategory_template,
            protocol.filename_template,
        ]))
        if "{camera_" in templates:
            source_make, source_model = _collect_source_camera_exif(source_path)
        else:
            source_make = source_model = ""

        for src_file in sorted(source_path.rglob("*")):
            if not src_file.is_file():
                continue
            if any(skip(src_file.name) for skip in _SKIP_PATTERNS):
                log.debug("Skipping %s (skip pattern)", src_file.name)
                continue
            if extensions and src_file.suffix.lower() not in extensions:
                log.debug("Skipping %s (extension not in triggers)", src_file.name)
                ext = src_file.suffix.lower() or "(no ext)"
                skipped_extensions[ext] = skipped_extensions.get(ext, 0) + 1
                continue

            exif = _extract_exif_data(src_file)

            ctx = _TemplateContext(
                date=exif.capture_date.strftime("%Y%m%d"),
                time=exif.capture_time or "",
                original_name=src_file.stem,
                original_filename=src_file.name,
                extension=src_file.suffix.lstrip(".").lower(),
                index=index,
                camera_make=exif.camera_make or source_make,
                camera_model=exif.camera_model or source_model,
            )

            category = ctx.render(protocol.category_template)
            subcategory = ctx.render(protocol.subcategory_template) if protocol.subcategory_template else None
            dest_filename = ctx.render(protocol.filename_template) + src_file.suffix

            dest = media_path(settings.archive_root, exif.capture_date, category, subcategory, dest_filename)
            sidecar = meta_path(settings.archive_root, exif.capture_date, category, subcategory, dest_filename)

            actions.append(PlannedAction(
                source_file=src_file,
                dest_file=dest,
                meta_file=sidecar,
                capture_date=exif.capture_date,
                capture_time=exif.capture_time,
                category=category,
                subcategory=subcategory,
                dest_filename=dest_filename,
            ))
            index += 1

        return PlanResult(actions=actions, skipped_extensions=skipped_extensions)

    def preview(self, result: PlanResult, archive_root: Path) -> str:
        """Return a human-readable table of planned actions."""
        actions = result.actions

        if not actions and not result.skipped_extensions:
            return "No files found in source."

        lines = []

        if actions:
            lines += [
                f"  {'Source':<40}  {'Destination':<60}  {'Date'}",
                "  " + "-" * 108,
            ]
            for a in actions:
                src = str(a.source_file)[-40:]
                dest = str(a.dest_file.relative_to(archive_root))[-60:]
                lines.append(f"  {src:<40}  {dest:<60}  {a.capture_date}")
            lines.append(f"\n  {len(actions)} file(s) would be imported.")
        else:
            lines.append("  No files match the protocol's extension triggers.")

        if result.skipped_extensions:
            total_skipped = sum(result.skipped_extensions.values())
            ext_summary = ", ".join(
                f"{ext} ×{n}"
                for ext, n in sorted(result.skipped_extensions.items(), key=lambda x: -x[1])
            )
            lines.append(f"  {total_skipped} file(s) not in protocol (not imported): {ext_summary}")

        return "\n".join(lines)

    def execute(
        self,
        actions: list[PlannedAction],
        protocol: "ImportProtocol",
        settings: "Settings",
        conn: sqlite3.Connection,
    ) -> ExecutionResult:
        """Execute a planned import, wrapped in a single LogTransaction.

        Returns an ExecutionResult with counts and the transaction ID.
        """
        from ..jobs.queue import enqueue_enrichment

        result = ExecutionResult()

        with LogTransaction(settings.logs_dir, protocol.name, dry_run=settings.dry_run) as tx:
            result.transaction_id = tx.transaction_id

            for action in actions:
                try:
                    # Skip if destination already exists
                    if action.dest_file.exists():
                        log.warning("Destination exists, skipping: %s", action.dest_file)
                        result.files_skipped += 1
                        continue

                    if not settings.dry_run:
                        # Copy the file
                        ensure_dirs(action.dest_file)
                        safe_copy(action.source_file, action.dest_file, tx)

                        # Hash and create sidecar
                        file_hash = sha256_file(action.dest_file)
                        protocol_metadata = {
                            "source_path": str(action.source_file),
                            "capture_time": action.capture_time or "",
                        }
                        sidecar_data = create_initial_sidecar(
                            sidecar_path=action.meta_file,
                            file_path=action.dest_file,
                            capture_date=action.capture_date.isoformat(),
                            file_type=action.category,
                            protocol_name=protocol.name,
                            file_hash=file_hash,
                            protocol_metadata=protocol_metadata,
                        )
                        tx.record(ActionType.SIDECAR_CREATED, dest_path=action.meta_file)

                        # Insert into database
                        rel_path = str(action.dest_file.relative_to(settings.archive_root))
                        file_id = upsert_file_from_sidecar(conn, sidecar_data, rel_path)

                        # Enqueue enrichment jobs
                        for chain_entry in protocol.enrichment_chain:
                            job_id = enqueue_enrichment(conn, file_id, chain_entry.protocol_name)
                            result.job_ids.append(job_id)

                    else:
                        # Dry-run: record intent without touching filesystem
                        tx.record(ActionType.FILE_CREATED,
                                  source_path=action.source_file,
                                  dest_path=action.dest_file)

                    result.files_copied += 1

                except Exception as e:
                    msg = f"{action.source_file.name}: {e}"
                    log.error("Import error: %s", msg)
                    result.errors.append(msg)

            if not settings.dry_run:
                conn.commit()

        return result


# ---------------------------------------------------------------------------
# Date/time extraction
# ---------------------------------------------------------------------------

# EXIF tags tried in preference order
_EXIF_TAGS = ["DateTimeOriginal", "CreateDate", "MediaCreateDate", "FileModifyDate"]

# Filename patterns: YYYYMMDD_HHMM or YYYYMMDD
_FILENAME_DATE_RE = re.compile(r"(\d{8})(?:_(\d{4}))?")


def _extract_exif_data(path: Path) -> _ExifData:
    """Return _ExifData for a file, using EXIF → filename pattern → mtime fallbacks.

    Camera make/model are empty strings when not available via EXIF.
    """
    # 1. exiftool (date + camera info)
    exif = _try_exiftool(path)
    if exif:
        return exif

    # 2. Filename pattern (date only; no camera info)
    m = _FILENAME_DATE_RE.search(path.stem)
    if m:
        try:
            d = datetime.strptime(m.group(1), "%Y%m%d").date()
            t = m.group(2)  # "HHMM" or None
            return _ExifData(capture_date=d, capture_time=t, camera_make="", camera_model="")
        except ValueError:
            pass

    # 3. File mtime (date only; no camera info)
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    log.debug("Using mtime for %s: %s", path.name, mtime.date())
    return _ExifData(
        capture_date=mtime.date(),
        capture_time=mtime.strftime("%H%M"),
        camera_make="",
        camera_model="",
    )


@dataclass
class _ExifData:
    capture_date: date
    capture_time: str | None   # "HHMM" or None
    camera_make: str           # normalized for directory use, "" if missing
    camera_model: str          # normalized for directory use, "" if missing


def _normalize_camera_tag(value: str) -> str:
    """Lowercase and replace spaces/special chars for safe directory names."""
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")


def _try_exiftool(path: Path) -> _ExifData | None:
    """Try to extract capture date/time and camera info via exiftool.

    Returns None if exiftool is unavailable or fails.
    """
    try:
        result = subprocess.run(
            ["exiftool", "-json",
             "-DateTimeOriginal", "-CreateDate", "-MediaCreateDate", "-FileModifyDate",
             "-Make", "-Model",
             str(path)],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        if not data:
            return None
        tags = data[0]

        cap_date: date | None = None
        cap_time: str | None = None
        for tag in _EXIF_TAGS:
            val = tags.get(tag)
            if val and val != "0000:00:00 00:00:00":
                try:
                    dt = datetime.strptime(val[:19], "%Y:%m:%d %H:%M:%S")
                    cap_date = dt.date()
                    cap_time = dt.strftime("%H%M")
                    break
                except ValueError:
                    continue

        if cap_date is None:
            return None

        make = _normalize_camera_tag(tags.get("Make", "") or "")
        model = _normalize_camera_tag(tags.get("Model", "") or "")
        return _ExifData(
            capture_date=cap_date,
            capture_time=cap_time,
            camera_make=make,
            camera_model=model,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass
    return None


def _collect_source_camera_exif(source_path: Path, limit: int = 20) -> tuple[str, str]:
    """Scan files in source_path to find a camera make and model.

    Returns (camera_make, camera_model), either of which may be empty.
    Stops as soon as a complete make+model pair is found, or after `limit` files.
    Used as source-level fallback for files that lack their own camera EXIF (e.g. .MTS).
    """
    best_make = ""
    best_model = ""
    checked = 0

    for path in sorted(source_path.rglob("*")):
        if not path.is_file():
            continue
        if any(skip(path.name) for skip in _SKIP_PATTERNS):
            continue
        exif = _try_exiftool(path)
        if exif:
            if not best_make and exif.camera_make:
                best_make = exif.camera_make
            if not best_model and exif.camera_model:
                best_model = exif.camera_model
            if best_make and best_model:
                break
        checked += 1
        if checked >= limit:
            break

    return best_make, best_model


# ---------------------------------------------------------------------------
# Trigger helpers
# ---------------------------------------------------------------------------

def _trigger_extensions(protocol: "ImportProtocol") -> set[str]:
    """Return the set of extensions this protocol should import.

    Checks include_extensions first (the explicit, new-style field), then falls
    back to reading extensions out of the legacy triggers list. Returns an empty
    set if no extension filter is declared, meaning the protocol accepts all files.
    """
    if protocol.include_extensions:
        return {
            ext.lower() if ext.startswith(".") else f".{ext.lower()}"
            for ext in protocol.include_extensions
        }
    exts: set[str] = set()
    for trigger in protocol.triggers:
        for ext in trigger.get("extensions", trigger.get("extension", [])):
            exts.add(ext.lower() if ext.startswith(".") else f".{ext.lower()}")
    return exts


# ---------------------------------------------------------------------------
# Template context
# ---------------------------------------------------------------------------

class _TemplateContext:
    """Simple template renderer using str.format_map()."""

    def __init__(
        self,
        date: str,
        time: str,
        original_name: str,
        original_filename: str,
        extension: str,
        index: int,
        camera_make: str = "",
        camera_model: str = "",
    ) -> None:
        make_model = "-".join(filter(None, [camera_make, camera_model]))
        self._vars = {
            "date": date,
            "time": time,
            "original_name": original_name,
            "original_filename": original_filename,
            "extension": extension,
            "index": index,
            "index2": f"{index:02d}",
            "index4": f"{index:04d}",
            "camera_make": camera_make,
            "camera_model": camera_model,
            "camera_make_model": make_model,
        }

    def render(self, template: str) -> str:
        try:
            return template.format_map(self._vars)
        except KeyError as e:
            raise SheafError(
                f"Template variable {e} not available. "
                f"Available: {list(self._vars)}"
            ) from e


# ---------------------------------------------------------------------------
# Enrichment execution
# ---------------------------------------------------------------------------

@dataclass
class EnrichmentContext:
    """All context needed to run one enrichment job."""
    job_id: str
    file_path: Path           # absolute path to the media file
    sidecar_path: Path        # absolute path to the .meta/ JSON sidecar
    sidecar_data: dict        # current sidecar contents
    protocol: "EnrichmentProtocol"
    settings: "Settings"
    conn: sqlite3.Connection


def run_enrichment(ctx: EnrichmentContext, adapter: "BaseAdapter | None" = None) -> dict:
    """Execute an enrichment protocol against a single file.

    Dispatches to the appropriate execution method (command or claude),
    updates the sidecar, and indexes the enrichment data in the database.

    Returns the enrichment data dict.
    """
    from ..archive.sidecar import update_sidecar
    from ..db.queries import bulk_upsert_metadata, get_file_by_path

    method = ctx.protocol.method or "command"

    if method == "command":
        enrichment_data = _run_command_enrichment(ctx)
    elif method == "claude":
        if adapter is None:
            raise ValueError("Claude adapter required for method='claude' but none provided")
        enrichment_data = _run_claude_enrichment(ctx, adapter)
    else:
        raise ValueError(f"Unknown enrichment method: {method!r} (expected 'command' or 'claude')")

    # Update sidecar
    rel_path = str(ctx.file_path.relative_to(ctx.settings.archive_root))
    with LogTransaction(ctx.settings.logs_dir, ctx.protocol.name,
                        dry_run=ctx.settings.dry_run) as tx:
        prior = update_sidecar(ctx.sidecar_path, {
            "enrichment_data": {ctx.protocol.name: enrichment_data},
            "enrichment_status": {ctx.protocol.name: "complete"},
        }, snapshot=True)
        if prior is not None:
            tx.record(ActionType.SIDECAR_UPDATED, dest_path=ctx.sidecar_path,
                      prior_snapshot=prior)

    # Index enrichment data in the database
    file_row = get_file_by_path(ctx.conn, rel_path)
    if file_row:
        flat = {
            f"enrichment.{ctx.protocol.name}.{k}": str(v)
            for k, v in enrichment_data.items()
            if isinstance(v, (str, int, float, bool))
        }
        if flat:
            bulk_upsert_metadata(ctx.conn, file_row["id"], flat)
            ctx.conn.commit()

    log.info("Enrichment '%s' complete for %s", ctx.protocol.name, ctx.file_path.name)
    return enrichment_data


def _run_command_enrichment(ctx: EnrichmentContext) -> dict:
    """Run a shell command and parse its stdout as JSON."""
    if not ctx.protocol.command_template:
        raise ValueError("command_template is required for method='command'")
    cmd = (ctx.protocol.command_template
           .replace("{file_path}", str(ctx.file_path))
           .replace("{archive_root}", str(ctx.settings.archive_root))
           .replace("{sidecar_path}", str(ctx.sidecar_path)))
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(
            f"Enrichment command failed (exit {result.returncode}): {result.stderr[:400]}"
        )
    return _parse_json_response(result.stdout)



def _run_claude_enrichment(ctx: EnrichmentContext, adapter: "BaseAdapter") -> dict:
    """Call the Claude adapter with file context + protocol instructions."""
    from ..adapter.base import Message

    file_context = _build_file_context(ctx)
    output_fields_hint = (
        f"Return a JSON object with these fields: {ctx.protocol.output_fields}"
        if ctx.protocol.output_fields
        else "Return a JSON object with any relevant enrichment fields."
    )
    prompt = (
        f"You are processing a media file for the Sheaf personal archive system.\n\n"
        f"Enrichment protocol: {ctx.protocol.name}\n"
        f"Instructions: {ctx.protocol.instructions or '(none — use your best judgment)'}\n\n"
        f"File context:\n{file_context}\n\n"
        f"{output_fields_hint}\n"
        f"Return ONLY valid JSON. No commentary, no markdown fences."
    )
    response = adapter.chat(
        messages=[Message(role="user", content=prompt)],
        system="You are a media enrichment processor. Always respond with valid JSON only.",
        max_tokens=4096,
    )
    return _parse_json_response(response.content)


def _build_file_context(ctx: EnrichmentContext) -> str:
    """Build a human-readable context string for the enrichment prompt."""
    lines = [
        f"Filename: {ctx.file_path.name}",
        f"File type: {ctx.sidecar_data.get('file_type', 'unknown')}",
        f"Capture date: {ctx.sidecar_data.get('capture_date', 'unknown')}",
        f"Imported by: {ctx.sidecar_data.get('imported_by_protocol', 'unknown')}",
    ]

    # Include protocol_metadata
    proto_meta = ctx.sidecar_data.get("protocol_metadata", {})
    if proto_meta:
        lines.append(f"Protocol metadata: {json.dumps(proto_meta, default=str)}")

    # Try EXIF
    exif_result = _try_exiftool(ctx.file_path)
    if exif_result is None:
        # Try full EXIF dump for enrichment context
        try:
            result = subprocess.run(
                ["exiftool", "-json", "-G", str(ctx.file_path)],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                if data:
                    useful = {
                        k: v for k, v in data[0].items()
                        if any(kw in k for kw in
                               ["Date", "Time", "Camera", "Make", "Model",
                                "Image", "GPS", "Exposure", "Focal", "Description",
                                "Comment", "Subject", "Keywords", "Title"])
                    }
                    if useful:
                        lines.append(f"EXIF/metadata: {json.dumps(useful, default=str)}")
        except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
            pass

    # For small text files, include content
    if ctx.file_path.suffix.lower() in (".txt", ".md", ".csv", ".json", ".xml"):
        try:
            content = ctx.file_path.read_text(errors="replace")
            if len(content) > 4000:
                content = content[:4000] + "\n... (truncated)"
            lines.append(f"File content:\n{content}")
        except OSError:
            pass

    return "\n".join(lines)


def _parse_json_response(content: str) -> dict:
    """Extract JSON from the adapter response, tolerating some noise."""
    content = content.strip()
    # Strip markdown fences if present
    if content.startswith("```"):
        content = re.sub(r"^```[a-z]*\n?", "", content)
        content = re.sub(r"\n?```$", "", content)
        content = content.strip()
    try:
        result = json.loads(content)
        if isinstance(result, dict):
            return result
        return {"value": result}
    except json.JSONDecodeError:
        # Return the raw text as a description field
        log.warning("Enrichment response was not valid JSON; storing as raw text.")
        return {"raw_output": content}
