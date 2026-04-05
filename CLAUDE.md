# Sheaf

Personal media archive management system — import, enrichment, and access layer on top of a date-based filesystem archive.

Full design spec: @docs/DESIGN.md

## Project Structure

- `src/` — framework source code
- `protocols/import/` — learned import protocols (source-specific)
- `protocols/enrichment/` — learned enrichment protocols (media-type-general)
- `config/` — adapter config, model settings, thresholds
- `db/` — SQLite database index (not git-tracked)
- `logs/` — operation logs (partitioned by date)
- `docs/` — design documents and specs

## Key Principles

- The archive filesystem is ground truth. The database is a rebuildable index derived from `.meta/` sidecars. If the database is deleted, `sheaf reindex` reconstructs it.
- No hardcoded media types or categories. All media-specific behavior is defined by protocols. The framework only enforces the `YYYY/YYYYMMDD/` directory hierarchy and the `YYYYMMDD_` filename prefix.
- Append-mostly filesystem safety. Never overwrite existing archive files without explicit user confirmation. Default to copy, not move. All operations are logged for rollback.
- Local-first processing. Enrichment runs locally. The frontier model (via adapter layer) handles decision-making and protocol authoring only.
- Composable protocols. Import protocols are source-specific and declare which enrichment protocols to chain. Enrichment protocols are reusable across import sources.
- The archive contains only media and `.meta/` metadata. No tool artifacts, no git, no config files in the archive.

## CLI

The tool is invoked as `sheaf` with subcommands: `import`, `jobs`, `search`, `browse`, `reindex`, `status`, `protocols`, `verify`, `history`.

## Development Notes

- Python
- SQLite for the database
- Prefer stdlib and simplicity over heavy dependencies
- The adapter layer abstracts the frontier model API — build for Claude first, keep the interface model-agnostic
- After completing each implementation phase, update `README.md`: mark the phase as done, update the commands table status, and add any new files to the relevant phase section.
