# Sheaf

Personal media archive management system — import, enrichment, and access layer on top of a date-based filesystem archive.

See `docs/DESIGN.md` for the full design requirements document.

---

## Setup

Requires Python 3.11+. Uses a virtual environment (Arch Linux externally-managed Python).

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

Point sheaf at your archive root and initialize:

```bash
.venv/bin/sheaf --archive /path/to/archive init
```

This creates `db/archive.db`, `logs/`, and default config files in `config/`.

---

## Commands

| Command | Status | Description |
|---|---|---|
| `sheaf init` | done | Initialize database and default config |
| `sheaf status` | done | Archive summary (file counts, last import) |
| `sheaf history` | done | List recent operations |
| `sheaf history show <tx-id>` | done | Show all actions in a transaction |
| `sheaf history rollback <tx-id>` | done | Reverse a transaction (dry-run by default; add `--confirm`) |
| `sheaf reindex` | done | Rebuild database from `.meta/` sidecars |
| `sheaf verify` | done | Check archive integrity |
| `sheaf protocols list` | done | List all learned protocols |
| `sheaf protocols show <name>` | done | Inspect a protocol |
| `sheaf import <path>` | done | Import media from a source path |
| `sheaf jobs` | done | View the enrichment job queue |
| `sheaf jobs --worker` | done | Run the enrichment worker (processes queue continuously) |
| `sheaf jobs review <job-id>` | done | Review and accept/reject a needs-review enrichment result |
| `sheaf search [query]` | done | Search by text, type, date range, or metadata |
| `sheaf browse` | done | Open the local web GUI (thumbnail grid) |

---

## Implementation Progress

### Phase 1 — Foundation ✓
Config, database schema, and archive path layout.

- `src/config.py` — `Settings` dataclass; reads `config/adapter.yaml`, `config/thresholds.yaml`, `SHEAF_ARCHIVE_ROOT`
- `src/db/schema.py` — `open_db()` with WAL + foreign keys; tables: `files`, `metadata`, `embeddings`, `jobs`; migration runner
- `src/db/queries.py` — typed query helpers over all tables
- `src/archive/layout.py` — `media_path`, `meta_path`, `date_to_dir`, `validate_filename_prefix`

### Phase 2 — Sidecar System and Operation Log ✓
Every archive write is wrapped in a logged transaction. Full rollback capability.

- `src/archive/sidecar.py` — atomic `write_sidecar` (`.tmp` + `os.replace`), `update_sidecar` with prior-state snapshot, `create_initial_sidecar`
- `src/archive/integrity.py` — `sha256_file`, `walk_meta_sidecars`, `orphaned_db_records`, `missing_sidecars`
- `src/oplog/writer.py` — `LogTransaction` context manager (NDJSON log, flushed per action); `safe_copy`, `safe_move` (raise on dest exists)
- `src/oplog/reader.py` — `list_transactions`, `get_transaction`
- `src/oplog/rollback.py` — `rollback_transaction` (reverses in order; restores from snapshot where available)

### Phase 3 — Protocol Model, Loader, and `reindex` ✓
Protocols load from YAML. `sheaf reindex` rebuilds the DB from `.meta/` sidecars.

- `src/protocols/model.py` — `Protocol`, `ImportProtocol`, `EnrichmentProtocol` dataclasses; `ProtocolMaturity` enum; `protocol_from_dict` / `protocol_to_dict`
- `src/protocols/loader.py` — `load_all_protocols`, `save_protocol`, `get_protocol`, `validate_protocol_yaml`
- `sheaf reindex [--full]` — incremental by default (skips files whose hash matches); `--full` drops and rebuilds
- `sheaf verify [--repair]` — checks orphaned DB records, missing sidecars, hash mismatches
- `sheaf protocols list / show <name>`

### Phase 4 — Import Pipeline ✓
Files import via a protocol YAML. No model required.

- `src/protocols/executor.py` — `ProtocolExecutor` with `plan()` / `execute()` / `preview()`; EXIF date extraction via `exiftool` (falls back to filename pattern, then mtime); macOS resource fork / `.DS_Store` skipping; template substitution (`{date}`, `{time}`, `{original_name}`, `{extension}`, `{index}`, …)
- `src/jobs/queue.py` — SQLite-backed job queue; `enqueue_enrichment`, `list_jobs`, `update_job_status`, atomic `get_next_queued_job`
- `sheaf import <path> --protocol <name> [--dry-run]` — shows preview table then executes; copies files, writes sidecars, inserts DB rows, enqueues enrichment jobs
- `sheaf jobs [--status] [--protocol]` — queue summary and job list
- Note: after a rollback, run `sheaf reindex` or `sheaf verify --repair` to resync the database

### Phase 5 — Adapter Layer and Conversational Learning ✓
Claude adapter. `sheaf import` enters a learning flow when no protocol matches.

- `src/adapter/base.py` — `BaseAdapter` ABC; `Message`, `ToolDefinition`, `AdapterResponse`, `ToolCall`, `AdapterCapabilities` types
- `src/adapter/claude.py` — `ClaudeAdapter`: raw `urllib.request` to Anthropic Messages API; exponential backoff on 429/529 (3 retries, cap 60s)
- `src/adapter/__init__.py` — `load_adapter(project_dir)` reads `config/adapter.yaml` and returns the right adapter
- `src/chat/session.py` — `ChatSession`: manages history, tool dispatch loop; `readline_chat()` REPL
- `src/protocols/matcher.py` — `match_protocols()`: one-shot prompt asking model to score all protocols against source; returns sorted `ProtocolMatch` list
- `src/protocols/author.py` — `draft_import_protocol()`: full learning flow with 5 tools (`list_source_files`, `read_exif`, `list_existing_protocols`, `preview_protocol`, `save_protocol`)
- `config/system_prompt.txt` — plain-text system prompt for the import learning conversation
- `sheaf import <path>` without `--protocol` now: matches → confirms (for non-trusted) → or enters learning flow

### Phase 6 — Job Worker and Enrichment Protocols ✓
Async enrichment queue. `sheaf jobs --worker` processes the queue.

- `src/protocols/executor.py` additions — `EnrichmentContext` dataclass; `run_enrichment(ctx, adapter)` calls Claude adapter with file metadata + protocol instructions, parses JSON response, updates sidecar and DB
- `src/jobs/worker.py` — `Worker` class: `run_once()` (claim → run → complete/needs-review/failed); `run_loop()` (daemon mode with Ctrl-C)
- `src/protocols/author.py` additions — `draft_enrichment_protocol()`: conversational flow for defining enrichment protocols; triggered automatically after a new import protocol is learned
- `sheaf jobs --worker` — run worker loop
- `sheaf jobs review <job-id>` — interactive review of needs-review jobs; accepts, rejects, or enters chat to refine
- Note: enrichment protocols are draft by default → jobs go to `needs-review`; promote to `trusted` via `sheaf protocols show` then edit the YAML

### Phase 7 — Search, GUI, and Full `status` ✓
Full-text search, thumbnail grid GUI, and complete dashboard.

- `src/db/queries.py` additions — `search_files()` (text + type + date + metadata filters); `search_embeddings()` with `_cosine_sim_bytes` (infrastructure for vector search when embeddings exist); `list_file_types()`; `register_cosine_sim()` UDF helper
- `src/web/server.py` — `SheafHandler` (stdlib `http.server`): routes `/`, `/api/search`, `/api/types`, `/media/<path>`, `/thumb/<path>`; inline HTML/CSS/JS single-page app; on-the-fly thumbnails via ImageMagick `convert` or `ffmpeg` with SVG placeholder fallback; thumbnail cache in `/tmp/sheaf_thumbs/`; `start_server(settings, port, open_browser)`
- `sheaf search [query] [--type] [--date] [--meta] [--browse]` — CLI search with optional browser output
- `sheaf browse [--port]` — launches GUI in browser
- `sheaf status` — full dashboard: file counts by type, last import date, job queue summary, protocol maturity breakdown

---

## Archive Structure

```
<archive_root>/
└── YYYY/
    └── YYYYMMDD/
        ├── <category>/
        │   └── [<subcategory>/]
        │       └── YYYYMMDD_<suffix>.<ext>
        └── .meta/
            └── <category>/
                └── [<subcategory>/]
                    └── YYYYMMDD_<suffix>.<ext>.json
```

The `YYYY/YYYYMMDD/` hierarchy and `YYYYMMDD_` filename prefix are enforced by the framework. Everything else (categories, subcategories, filename suffixes) is defined by import protocols.

## Project Structure

```
sheaf/
├── src/            # framework source
├── protocols/      # learned import and enrichment protocols
│   ├── import/
│   └── enrichment/
├── config/         # adapter config, model settings, thresholds
├── db/             # SQLite database index (not git-tracked)
├── logs/           # operation logs, partitioned by date
└── docs/           # design documents
```
